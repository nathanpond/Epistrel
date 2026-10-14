"""Principal offboarding: one request applies per-type actions to everything a principal owns or
is related to (FR-AUTH-7; docs/02 §4.10; #117). This is not erasure (#124).

**The request.** ``{principal, actions, transfer_to?, dry_run?, ignore_warnings?}``. Owned
types (``stories``, ``characters``, ``scenarios``, ``personas``, ``media``, ``engine_profiles``)
take ``delete``, ``transfer`` or ``keep``, and ``relations`` takes ``remove`` or ``keep``. A
type not named is ``keep``. ``transfer`` needs ``transfer_to`` (422
``transfer_target_required``), which must be neither the principal nor ``system`` (422
``transfer_target_invalid``). ``system`` itself cannot be offboarded (422
``principal_reserved``). A principal with a queued or running offboarding is 409
``offboarding_in_progress``. An offboarding whose #70 job ended without closing it (the
runtime cap, or a failed commit, on the last attempt) is closed as ``failed`` with its partial
report when it is next requested or read (``reconcile``), so it never blocks the principal.

**The plan** (``plan``) is read in the request's transaction. It is a snapshot of ids, in the
fixed order ``stories, characters, scenarios, personas, media, engine_profiles, relations``, each
entry with its resolved action, so entities created for the principal later are not included.
The type rules:

- A character's companion scenario follows the character's action, never the scenarios action.
  It is not a snapshot entry of its own: it goes with its character through the character's
  own delete or transfer (#116).
- Media referenced by a transferred catalog entry (the character, its companion, or a
  transferred scenario: their drafts and every published version, ``REFERENCE_HOLDERS``) move
  to the same new owner, whatever the media action (``reason: referenced``). So does a
  transferred video's poster. Only the principal's own media move.
- ``relations: remove`` covers every entity with a relation naming the principal that the
  principal does not own, plus the ones it owns and transfers. A deleted entity's relations
  go with it. A kept one's stay.

The plan also gives per-type counts and the warnings, each
``{kind, entity_type, entity_id, principals}``:

- ``shared_story``: a story to be deleted that other principals are related to;
- ``profile_in_use``: an engine profile to be deleted that other owners' stories use;
- ``media_referenced``: a media asset to be retired that other owners' records reference;
- ``default_profile``: the deployment's default profile, which the job will not delete.

A dry run, or a request whose plan warns without ``ignore_warnings``, writes nothing and
answers the plan. Otherwise the plan is stored as an ``offboarding_jobs`` row whose id is its
#70 job's.

**The job** (the Orchestrator's ``offboarding`` handler) works through the snapshot one entity
at a time, through the type's normal path (``apply``): #50's story delete, the catalog deletes
(#103, #108, #116: a character takes its companion), #114's persona delete, #105's media retire,
#106's profile delete, and #104's transfer and relation removal. It acts as ``system``, and its
ownership rows name the job (``ownership.acting_job``). Before acting, it records the entry as
``progress.current`` (``save_progress``). Afterwards one transaction records the outcome and
moves ``progress.index`` past it. On resume, an entry that is ``current`` and already gone
(deleted) or already at ``transfer_to`` (transferred) is recorded as done, never acted on
twice. Each entity is re-checked when its turn comes:

- one now owned by someone else is ``skipped`` with reason ``not_owned``;
- one gone is ``skipped`` with reason ``gone``;
- a persona whose name ``transfer_to`` already uses is ``skipped`` with reason ``name_taken``;
- the default profile is ``skipped`` with reason ``default_profile``;
- a story that is busy (423) is deferred to the end and retried three times, then ``skipped``
  with reason ``busy``.

``finish`` freezes the report's ``remaining``: the live entities still owned by or related to
the principal (``core.principals.remaining``).

**Attribution stays.** Offboarding never touches events, so a story the principal did not own
still names it on its turns (FR-AUTH-4). Removing a person's content from other principals'
stories is erasure (FR-ADMIN-2).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, ownership, principals
from epistrel.core import stories as story_store
from epistrel.core.catalog import characters as catalog_characters
from epistrel.core.catalog import personas as catalog_personas
from epistrel.core.catalog import scenarios as catalog_scenarios
from epistrel.core.context import PRINCIPAL_PATTERN, SYSTEM_PRINCIPAL, principal_problem
from epistrel.core.errors import (
    CharacterNotFound,
    ConfigConflict,
    EntityNotFound,
    MediaNotFound,
    OffboardingInProgress,
    OffboardingNotFound,
    PersonaNameTaken,
    PersonaNotFound,
    PrincipalReserved,
    ProfileNotFound,
    ScenarioNotFound,
    StoryNotFound,
    TransferTargetInvalid,
    TransferTargetRequired,
)
from epistrel.core.media import assets as media
from epistrel.core.profiles import store as profiles
from epistrel.core.profiles.impact import TwoStageRequest
from epistrel.core.tables import (
    deployment_state,
    jobs,
    media_assets,
    offboarding_jobs,
    scenario_catalog,
    stories,
)
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.offboarding import ACTIVE_STATUSES, OffboardingStatus
from epistrel.core.tables.ownership import EntityType

OwnedAction = Literal["delete", "transfer", "keep"]
RelationAction = Literal["remove", "keep"]

OWNED_TYPES: tuple[str, ...] = tuple(principals.TYPES)
"""The owned types, in processing order."""
RELATIONS = "relations"
ORDER: tuple[str, ...] = (*OWNED_TYPES, RELATIONS)
"""The fixed processing order: every owned type, then relations."""

BUCKETS = ("deleted", "transferred", "removed", "kept", "skipped")
"""What a report lists, each by type."""

LOCK_NAMESPACE = 0x4F464642
"""``pg_advisory_xact_lock``'s first key for one principal's offboarding requests ("OFFB")."""

WARNING_KINDS = ("shared_story", "profile_in_use", "media_referenced", "default_profile")


def _principal_rule(label: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        found = value.strip(" \t")
        problem = principal_problem(found)
        if problem is not None:
            raise ValueError(f"{label} {problem}")
        return found

    return check


OffboardedPrincipal = Annotated[
    str, Field(pattern=PRINCIPAL_PATTERN), AfterValidator(_principal_rule("principal"))
]
TransferTarget = Annotated[
    str, Field(pattern=PRINCIPAL_PATTERN), AfterValidator(_principal_rule("transfer_to"))
]


class OffboardingActions(BaseModel):
    """The action for each type of entity the principal owns (``delete``, ``transfer`` or
    ``keep``), and for the relations naming it on entities it does not own (``remove`` or
    ``keep``). A type not named is ``keep``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stories: OwnedAction = Field(
        default="keep",
        description="`delete`: whole-story deletion, gone for every participant (FR-STORE-16).",
    )
    characters: OwnedAction = Field(
        default="keep",
        description="Catalog characters; a character's companion scenario goes with it.",
    )
    scenarios: OwnedAction = Field(
        default="keep", description="Scenarios other than companions (which follow characters)."
    )
    personas: OwnedAction = Field(default="keep", description="Saved player personas.")
    media: OwnedAction = Field(
        default="keep",
        description="Media assets; `delete` retires them (FR-MEDIA-2). Media a transferred "
        "catalog entry references move with it whatever this says.",
    )
    engine_profiles: OwnedAction = Field(
        default="keep",
        description="Engine profiles; `delete` retires them (FR-PROF-1). The deployment's "
        "default profile is never deleted.",
    )
    relations: RelationAction = Field(
        default="keep",
        description="The relations naming the principal on entities it does not own (or "
        "transfers).",
    )

    def of(self, kind: str) -> str:
        value: str = getattr(self, kind)
        return value


class OffboardingRequest(TwoStageRequest):
    """``POST /admin/offboarding`` (FR-AUTH-7): the principal to offboard, the action per type,
    the new owner for every ``transfer``, and the two-stage flags (FR-PROF-7)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "principal": "p-4d81e2",
                    "actions": {
                        "stories": "delete",
                        "personas": "delete",
                        "characters": "transfer",
                        "scenarios": "transfer",
                        "media": "transfer",
                    },
                    "transfer_to": "p-curator",
                    "dry_run": True,
                }
            ]
        },
    )

    principal: OffboardedPrincipal = Field(description="The principal to offboard (opaque).")
    actions: OffboardingActions = Field(
        default_factory=OffboardingActions, description="The action per type."
    )
    transfer_to: TransferTarget | None = Field(
        default=None,
        description="The new owner for every `transfer` (required with one; never the "
        "offboarded principal or `system`).",
    )
    dry_run: bool = Field(
        default=False,
        description="Answer the counts and warnings and change nothing (it wins over "
        "`ignore_warnings`).",
    )


@dataclass(frozen=True, slots=True)
class OffboardingWarning:
    """Something the offboarding would do that touches other principals (FR-AUTH-7). Warnings
    never block, but a request whose plan has any applies only with ``ignore_warnings``."""

    kind: str
    entity_type: str
    entity_id: str
    principals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OffboardingPlan:
    """A dry run, or a request held by its warnings: nothing was written (``applied`` false)."""

    counts: dict[str, dict[str, int]]
    warnings: tuple[OffboardingWarning, ...]
    config_revision: int
    applied: bool = False
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class OffboardingQueued:
    """An offboarding handed to a worker (202): its job, the counts and the warnings it was
    confirmed over."""

    job_id: str
    counts: dict[str, dict[str, int]]
    warnings: tuple[OffboardingWarning, ...]
    config_revision: int
    applied: bool = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


def stored(result: OffboardingPlan | OffboardingQueued) -> bool:
    """Whether a result is stored under the request's ``Idempotency-Key``: only an enqueued
    offboarding. A plan alone writes nothing, so its key is released (#109's two stages)."""
    return isinstance(result, OffboardingQueued)


# -- the request -------------------------------------------------------------------------------


def check_request(request: OffboardingRequest) -> None:
    """The request rules that need no database (see the module docstring)."""
    if request.principal == SYSTEM_PRINCIPAL:
        raise PrincipalReserved(
            f"`{SYSTEM_PRINCIPAL}` owns the presets and acts for background work; it cannot be "
            "offboarded",
            {"field": "body.principal", "principal": request.principal},
        )
    transferring = [kind for kind in OWNED_TYPES if request.actions.of(kind) == "transfer"]
    if transferring and request.transfer_to is None:
        raise TransferTargetRequired(
            "a `transfer` action needs `transfer_to`, the new owner",
            {"field": "body.transfer_to", "types": transferring},
        )
    if request.transfer_to is not None:
        if request.transfer_to == SYSTEM_PRINCIPAL:
            raise TransferTargetInvalid(
                f"entities cannot be transferred to `{SYSTEM_PRINCIPAL}`",
                {"field": "body.transfer_to", "reason": "reserved"},
            )
        if request.transfer_to == request.principal:
            raise TransferTargetInvalid(
                "`transfer_to` is the principal being offboarded",
                {"field": "body.transfer_to", "reason": "same_principal"},
            )


async def lock_principal(conn: AsyncConnection, principal: str) -> None:
    """Serialise one principal's offboarding requests (until the transaction ends)."""
    await conn.execute(select(func.pg_advisory_xact_lock(LOCK_NAMESPACE, func.hashtext(principal))))


async def reconcile(conn: AsyncConnection, where: ColumnElement[bool]) -> int:
    """Close as ``failed`` every queued or running offboarding matching ``where`` whose #70 job
    has ended (``done`` or ``failed``) or is gone, with its partial report, in ``conn``'s
    transaction; returns how many it closed.

    The handler marks its offboarding ``failed`` on its last attempt, but the job framework can
    also end a job without the handler seeing it: the runtime cap cancels the handler, or the
    commit itself fails, on the last attempt. Without this the row would stay active forever,
    and every later offboarding of the principal would be 409 ``offboarding_in_progress``."""
    ended = (
        await conn.execute(
            select(offboarding_jobs, jobs.c.status.label("job_status"), jobs.c.result)
            .select_from(offboarding_jobs.outerjoin(jobs, jobs.c.id == offboarding_jobs.c.id))
            .where(
                where,
                offboarding_jobs.c.status.in_(ACTIVE_STATUSES),
                or_(jobs.c.id.is_(None), jobs.c.status.in_(JOB_ENDED)),
            )
            .order_by(offboarding_jobs.c.id)
            .with_for_update(of=offboarding_jobs)
        )
    ).all()
    for row in ended:
        kind = _ended_class(row.job_status, row.result)
        await _close(
            conn,
            _job(row),
            OffboardingStatus.FAILED,
            {
                "class": kind,
                "message": f"the offboarding job ended before the offboarding finished ({kind})",
            },
        )
    return len(ended)


JOB_ENDED = ("done", "failed")
"""#70's terminal job statuses."""


def _ended_class(job_status: str | None, result: Any) -> str:
    """The error class a reconciled offboarding reports: the job's own failure class when it
    recorded one (an exception class name, never a message), else what happened to the job."""
    if job_status is None:
        return "JobMissing"
    error = result.get("error") if isinstance(result, Mapping) else None
    if job_status == "failed" and isinstance(error, str) and error.isidentifier():
        return error
    return "JobFailed" if job_status == "failed" else "JobEnded"


async def refuse_active(conn: AsyncConnection, principal: str) -> None:
    """409 ``offboarding_in_progress`` when the principal has a queued or running one (after
    closing any whose job already ended, ``reconcile``)."""
    await reconcile(conn, offboarding_jobs.c.principal == principal)
    active = await conn.scalar(
        select(offboarding_jobs.c.id).where(
            offboarding_jobs.c.principal == principal,
            offboarding_jobs.c.status.in_(ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise OffboardingInProgress(
            f"principal {principal!r} already has an offboarding in progress; read its progress "
            "and ask again once it finishes",
            {"principal": principal, "job_id": str(active)},
        )


async def config_revision(conn: AsyncConnection) -> int:
    found = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(found or 0)


# -- the plan ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plan:
    """The snapshot (entries in processing order), its counts and its warnings."""

    entries: list[dict[str, Any]]
    counts: dict[str, dict[str, int]]
    warnings: tuple[OffboardingWarning, ...]


async def _ids(conn: AsyncConnection, entity_type: EntityType, principal: str) -> list[str]:
    spec = principals.OWNED[entity_type]
    where = [spec.table.c.owner_principal == principal]
    if spec.listed is not None:
        where.append(spec.listed)
    found = await conn.scalars(select(spec.table.c.id).where(*where).order_by(spec.table.c.id))
    return [str(entity_id) for entity_id in found]


async def _companions(conn: AsyncConnection, character_ids: Sequence[str]) -> dict[str, str]:
    if not character_ids:
        return {}
    rows = await conn.execute(
        select(scenario_catalog.c.companion_of, scenario_catalog.c.id).where(
            scenario_catalog.c.companion_of.in_(list(character_ids))
        )
    )
    return {str(row.companion_of): str(row.id) for row in rows}


async def _posters(conn: AsyncConnection, media_ids: Sequence[str]) -> set[str]:
    if not media_ids:
        return set()
    found = await conn.scalars(
        select(media_assets.c.poster_media_id).where(
            media_assets.c.id.in_(list(media_ids)), media_assets.c.poster_media_id.is_not(None)
        )
    )
    return {str(poster) for poster in found}


async def plan(conn: AsyncConnection, request: OffboardingRequest) -> Plan:
    """The snapshot, counts and warnings of ``request`` (see the module docstring), read in
    ``conn``'s transaction."""
    principal = request.principal
    actions = request.actions
    ids = {
        key: await _ids(conn, entity_type, principal)
        for key, entity_type in principals.TYPES.items()
    }
    companions = await _companions(conn, ids["characters"])
    # Media a transferred entry references move with it (and a moving video's poster).
    moving_entries: set[tuple[str, str]] = set()
    if actions.characters == "transfer":
        moving_entries |= {(EntityType.CHARACTER.value, c) for c in ids["characters"]}
        moving_entries |= {(EntityType.SCENARIO.value, s) for s in companions.values()}
    if actions.scenarios == "transfer":
        moving_entries |= {(EntityType.SCENARIO.value, s) for s in ids["scenarios"]}
    own_media = set(ids["media"])
    references = await media.references(conn, ids["media"])
    referenced = {
        ref.media_id
        for ref in references
        if (ref.entity_type, ref.entity_id) in moving_entries and ref.media_id in own_media
    }
    if actions.media != "transfer":
        referenced |= await _posters(conn, sorted(referenced)) & own_media

    entries: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    for key in OWNED_TYPES:
        tally = {"delete": 0, "transfer": 0, "keep": 0}
        for entity_id in ids[key]:
            action = actions.of(key)
            entry: dict[str, Any] = {"type": key, "id": entity_id, "action": action}
            if key == "characters":
                entry["companion"] = companions.get(entity_id)
            if key == "media" and entity_id in referenced and action != "transfer":
                entry["action"] = "transfer"
                entry["reason"] = "referenced"
            tally[entry["action"]] += 1
            entries.append(entry)
        counts[key] = tally
    relation_tally = {"remove": 0, "keep": 0}
    for entity_type, entity_id, owner in await principals.related_entities(conn, principal):
        key = principals.TYPE_KEYS[entity_type]
        if owner == principal and actions.of(key) != "transfer":
            continue
        relation_tally[actions.relations] += 1
        entries.append(
            {
                "type": RELATIONS,
                "entity_type": entity_type.value,
                "id": entity_id,
                "action": actions.relations,
            }
        )
    counts[RELATIONS] = relation_tally
    warnings = await _warnings(conn, request, entries, references)
    return Plan(entries=entries, counts=counts, warnings=warnings)


async def _warnings(
    conn: AsyncConnection,
    request: OffboardingRequest,
    entries: Sequence[Mapping[str, Any]],
    references: Sequence[media.MediaReference],
) -> tuple[OffboardingWarning, ...]:
    principal = request.principal
    deleting: dict[str, list[str]] = {key: [] for key in OWNED_TYPES}
    for entry in entries:
        if entry["type"] in deleting and entry["action"] == "delete":
            deleting[entry["type"]].append(str(entry["id"]))
    found: list[OffboardingWarning] = []
    if deleting["stories"]:
        shared = await principals.related_principals(
            conn, EntityType.STORY, deleting["stories"], besides=principal
        )
        found += [
            OffboardingWarning("shared_story", EntityType.STORY.value, story_id, _sorted(others))
            for story_id, others in sorted(shared.items())
        ]
    if deleting["engine_profiles"]:
        rows = await conn.execute(
            select(stories.c.engine_profile_id, stories.c.owner_principal)
            .where(
                stories.c.engine_profile_id.in_(deleting["engine_profiles"]),
                stories.c.owner_principal != principal,
            )
            .distinct()
        )
        in_use: dict[str, set[str]] = {}
        for row in rows:
            in_use.setdefault(str(row.engine_profile_id), set()).add(str(row.owner_principal))
        found += [
            OffboardingWarning(
                "profile_in_use", EntityType.ENGINE_PROFILE.value, profile_id, _sorted(owners)
            )
            for profile_id, owners in sorted(in_use.items())
        ]
    if deleting["media"]:
        doomed = set(deleting["media"])
        held: dict[str, set[str]] = {}
        for ref in references:
            if ref.media_id in doomed and ref.owner is not None and ref.owner != principal:
                held.setdefault(ref.media_id, set()).add(ref.owner)
        found += [
            OffboardingWarning(
                "media_referenced", EntityType.MEDIA.value, media_id, _sorted(owners)
            )
            for media_id, owners in sorted(held.items())
        ]
    if deleting["engine_profiles"]:
        default = await profiles.default_profile(conn)
        if default in deleting["engine_profiles"]:
            found.append(
                OffboardingWarning("default_profile", EntityType.ENGINE_PROFILE.value, default)
            )
    return tuple(sorted(found, key=lambda w: (WARNING_KINDS.index(w.kind), w.entity_id)))


def _sorted(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(values, key=lambda value: value.encode()))


def _empty_report() -> dict[str, Any]:
    return {bucket: {kind: [] for kind in ORDER} for bucket in BUCKETS}


async def create(
    conn: AsyncConnection,
    job_id: str,
    request: OffboardingRequest,
    planned: Plan,
    *,
    created_by: str,
    operation_id: str | None,
) -> None:
    """Store the planned offboarding as ``job_id``'s row, ``queued``, in ``conn``'s
    transaction."""
    now = clock.now()
    await conn.execute(
        insert(offboarding_jobs).values(
            id=job_id,
            principal=request.principal,
            created_by=created_by,
            actions=request.actions.model_dump(mode="json"),
            transfer_to=request.transfer_to,
            request=request.model_dump(mode="json"),
            status=OffboardingStatus.QUEUED.value,
            snapshot=planned.entries,
            progress={"index": 0, "current": None, "deferred": []},
            counts=planned.counts,
            report=_empty_report(),
            operation_id=operation_id,
            created_at=now,
            updated_at=now,
        )
    )


# -- the job's steps ---------------------------------------------------------------------------

Guard = Callable[[AsyncConnection], Awaitable[None]]
"""Checked first in each of the job's own transactions: the worker's claim still holds (#70's
fencing). It raises when the claim was fenced, and the transaction rolls back."""


@dataclass(frozen=True, slots=True)
class Job:
    """An offboarding row as the job works on it."""

    id: str
    principal: str
    transfer_to: str | None
    status: str
    snapshot: list[dict[str, Any]]
    progress: dict[str, Any]


def _job(row: Any) -> Job:
    return Job(
        id=str(row.id),
        principal=str(row.principal),
        transfer_to=row.transfer_to,
        status=str(row.status),
        snapshot=list(row.snapshot or []),
        progress=dict(row.progress or {}),
    )


async def begin_run(db: AsyncEngine, job_id: str, guard: Guard) -> Job | None:
    """The row the job works on, ``running`` from its first run; ``None`` when it is gone."""
    async with db.begin() as conn:
        await guard(conn)
        row = (
            await conn.execute(
                select(offboarding_jobs).where(offboarding_jobs.c.id == job_id).with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None
        if row.status == OffboardingStatus.QUEUED.value:
            await conn.execute(
                update(offboarding_jobs)
                .where(offboarding_jobs.c.id == job_id)
                .values(status=OffboardingStatus.RUNNING.value, updated_at=clock.now())
            )
            return dataclasses.replace(_job(row), status=OffboardingStatus.RUNNING.value)
        return _job(row)


Outcome = tuple[str, str, Any]
"""``(bucket, type, record)``: one line of the report."""


async def save_progress(
    db: AsyncEngine,
    job_id: str,
    progress: Mapping[str, Any],
    outcomes: Sequence[Outcome],
    guard: Guard,
) -> None:
    """Record ``outcomes`` in the report and ``progress`` in one transaction."""
    async with db.begin() as conn:
        await guard(conn)
        report = await conn.scalar(
            select(offboarding_jobs.c.report)
            .where(offboarding_jobs.c.id == job_id)
            .with_for_update()
        )
        merged = dict(report or _empty_report())
        for bucket, kind, record in outcomes:
            by_type = dict(merged.get(bucket) or {})
            by_type[kind] = [*by_type.get(kind, []), record]
            merged[bucket] = by_type
        await conn.execute(
            update(offboarding_jobs)
            .where(offboarding_jobs.c.id == job_id)
            .values(progress=dict(progress), report=merged, updated_at=clock.now())
        )


async def finish(db: AsyncEngine, job: Job, guard: Guard) -> None:
    """``done``: the report's ``remaining`` frozen now."""
    async with db.begin() as conn:
        await guard(conn)
        await _close(conn, job, OffboardingStatus.DONE, None)


async def fail(db: AsyncEngine, job_id: str, error: BaseException) -> None:
    """``failed``, with the partial report and ``error {class, message}`` (the job's last
    attempt failed; the message names the class only, never what it was processing)."""
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(offboarding_jobs).where(offboarding_jobs.c.id == job_id).with_for_update()
            )
        ).one_or_none()
        if row is None or row.status not in ACTIVE_STATUSES:
            return
        kind = type(error).__name__
        await _close(
            conn,
            _job(row),
            OffboardingStatus.FAILED,
            {
                "class": kind,
                "message": f"the offboarding job stopped after its last attempt ({kind})",
            },
        )


async def _close(
    conn: AsyncConnection, job: Job, status: OffboardingStatus, error: dict[str, Any] | None
) -> None:
    report = await conn.scalar(
        select(offboarding_jobs.c.report).where(offboarding_jobs.c.id == job.id).with_for_update()
    )
    still = await principals.remaining(conn, job.principal)
    now = clock.now()
    await conn.execute(
        update(offboarding_jobs)
        .where(offboarding_jobs.c.id == job.id)
        .values(
            status=status.value,
            report={**dict(report or {}), "remaining": still.model_dump(mode="json")},
            progress={**job.progress, "current": None},
            error=error,
            updated_at=now,
            finished_at=now,
        )
    )


# -- one entity --------------------------------------------------------------------------------

_GONE = (
    StoryNotFound,
    CharacterNotFound,
    ScenarioNotFound,
    PersonaNotFound,
    MediaNotFound,
    ProfileNotFound,
    EntityNotFound,
)


async def _owner(db: AsyncEngine, entity_type: EntityType, entity_id: str) -> str | None:
    table = principals.OWNED[entity_type].table
    async with db.connect() as conn:
        found = await conn.scalar(select(table.c.owner_principal).where(table.c.id == entity_id))
    return None if found is None else str(found)


async def _delete(db: AsyncEngine, entity_type: EntityType, entity_id: str) -> None:
    actor = SYSTEM_PRINCIPAL
    if entity_type is EntityType.STORY:
        await story_store.delete_story(db, entity_id, principal=actor)
    elif entity_type is EntityType.CHARACTER:
        await catalog_characters.delete(db, entity_id, principal=actor)
    elif entity_type is EntityType.SCENARIO:
        await catalog_scenarios.delete(db, entity_id, principal=actor)
    elif entity_type is EntityType.PERSONA:
        await catalog_personas.delete(db, entity_id, principal=actor)
    elif entity_type is EntityType.MEDIA:
        await media.retire(db, entity_id, principal=actor)
    else:
        await profiles.delete_profile(db, entity_id, principal=actor)


def _with_companion(
    entry: Mapping[str, Any], bucket: str, record: Any, companion: Any
) -> list[Outcome]:
    found: list[Outcome] = [(bucket, str(entry["type"]), record)]
    if entry.get("companion"):
        found.append((bucket, "scenarios", companion))
    return found


async def apply(
    db: AsyncEngine, job: Job, entry: Mapping[str, Any], *, resumed: bool
) -> list[Outcome]:
    """Apply one snapshot entry through its type's normal path and answer its outcomes (see the
    module docstring). ``resumed``: the entry was ``progress.current`` when this run began, so
    its action may already have run. Raises ``StoryBusy`` for a story that is busy (the job
    defers it)."""
    kind = str(entry["type"])
    entity_id = str(entry["id"])
    action = str(entry["action"])
    if kind == RELATIONS:
        return await _apply_relations(db, job, entry, resumed=resumed)
    entity_type = principals.TYPES[kind]
    companion = entry.get("companion")
    owner = await _owner(db, entity_type, entity_id)
    to = job.transfer_to
    if owner is None:
        if resumed and action == "delete":
            return _with_companion(entry, "deleted", entity_id, companion)
        return [("skipped", kind, {"id": entity_id, "reason": "gone"})]
    if owner != job.principal:
        if resumed and action == "transfer" and owner == to:
            return _transferred(entry, entity_id, to)
        return [("skipped", kind, {"id": entity_id, "reason": "not_owned"})]
    if action == "keep":
        return _with_companion(entry, "kept", entity_id, companion)
    try:
        if action == "delete":
            await _delete(db, entity_type, entity_id)
            return _with_companion(entry, "deleted", entity_id, companion)
        assert to is not None  # noqa: S101 - a transfer is refused without transfer_to
        with catalog_personas.name_conflicts(to, field="body.transfer_to", persona_id=entity_id):
            await ownership.transfer_owner(
                db, entity_type, entity_id, to, principal=SYSTEM_PRINCIPAL
            )
        return _transferred(entry, entity_id, to)
    except _GONE:
        return [("skipped", kind, {"id": entity_id, "reason": "gone"})]
    except PersonaNameTaken:
        return [("skipped", kind, {"id": entity_id, "reason": "name_taken"})]
    except ConfigConflict:
        # Deleting the deployment's default profile is refused (#106): it stays, with a note.
        return [("skipped", kind, {"id": entity_id, "reason": "default_profile"})]


def _transferred(entry: Mapping[str, Any], entity_id: str, to: str | None) -> list[Outcome]:
    record: dict[str, Any] = {"id": entity_id, "to": to}
    if entry.get("reason"):
        record["reason"] = entry["reason"]
    companion = entry.get("companion")
    return _with_companion(
        entry,
        "transferred",
        record,
        {"id": companion, "to": to, "companion_of": entity_id},
    )


async def _apply_relations(
    db: AsyncEngine, job: Job, entry: Mapping[str, Any], *, resumed: bool
) -> list[Outcome]:
    entity_type = EntityType(str(entry["entity_type"]))
    entity_id = str(entry["id"])
    base = {"entity_type": entity_type.value, "id": entity_id}
    if entry["action"] == "keep":
        if await _owner(db, entity_type, entity_id) is None:
            return [("skipped", RELATIONS, {**base, "reason": "gone"})]
        return [("kept", RELATIONS, base)]
    names = await ownership.remove_relations_of(
        db, entity_type, entity_id, job.principal, principal=SYSTEM_PRINCIPAL
    )
    if names is None:
        return [("skipped", RELATIONS, {**base, "reason": "gone"})]
    if not names and not resumed:
        return [("skipped", RELATIONS, {**base, "reason": "none"})]
    return [("removed", RELATIONS, {**base, "relations": names})]


# -- reads -------------------------------------------------------------------------------------


class OffboardingProgress(BaseModel):
    """How far the job is: entries done (recorded) of the snapshot's total."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    done: int
    total: int


class CurrentEntity(BaseModel):
    """The entity the job is working on now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    id: str


class OffboardingError(BaseModel):
    """Why a failed job stopped: the exception class, and a message naming no content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    class_: str = Field(alias="class", serialization_alias="class")
    message: str


class OffboardingView(BaseModel):
    """An offboarding: its status (#70's enum), progress, the entity in hand, the request,
    and once it ends the report (``deleted``, ``transferred``, ``removed``, ``kept``,
    ``skipped``, each by type, and ``remaining``: what is still linked to the principal), with
    the error of a failed job."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    job_id: str
    principal: str
    status: OffboardingStatus
    progress: OffboardingProgress
    current: CurrentEntity | None
    counts: dict[str, dict[str, int]]
    report: dict[str, Any] | None
    error: OffboardingError | None
    request: dict[str, Any]
    created_by: str
    created_at: datetime
    finished_at: datetime | None


async def get(db: AsyncEngine, job_id: str) -> OffboardingView:
    """The offboarding ``job_id``; 404 when there is none (or its row was purged). One whose
    job ended without closing it reads ``failed`` (``reconcile``)."""
    async with db.begin() as conn:
        await reconcile(conn, offboarding_jobs.c.id == job_id)
        row = (
            await conn.execute(select(offboarding_jobs).where(offboarding_jobs.c.id == job_id))
        ).one_or_none()
    if row is None:
        raise OffboardingNotFound(f"offboarding {job_id} does not exist", {"job_id": job_id})
    progress = dict(row.progress or {})
    snapshot = list(row.snapshot or [])
    current = progress.get("current")
    finished = row.status in (OffboardingStatus.DONE.value, OffboardingStatus.FAILED.value)
    return OffboardingView(
        job_id=str(row.id),
        principal=str(row.principal),
        status=OffboardingStatus(row.status),
        progress=OffboardingProgress(
            done=int(progress.get("index", 0)) - len(progress.get("deferred") or []),
            total=len(snapshot),
        ),
        current=None
        if not current
        else CurrentEntity(type=str(current["type"]), id=str(current["id"])),
        counts=dict(row.counts or {}),
        report=dict(row.report) if finished else None,
        error=None if row.error is None else OffboardingError.model_validate(row.error),
        request=dict(row.request or {}),
        created_by=str(row.created_by),
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


async def purge_finished(conn: AsyncConnection, before: datetime, limit: int) -> int:
    """Delete up to ``limit`` finished offboardings that ended before ``before`` (#71's sweep,
    ``offboarding_report_retention_days``)."""
    doomed = (
        select(offboarding_jobs.c.id)
        .where(
            offboarding_jobs.c.status.not_in(ACTIVE_STATUSES),
            offboarding_jobs.c.finished_at < before,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    result = await conn.execute(offboarding_jobs.delete().where(offboarding_jobs.c.id.in_(doomed)))
    return int(result.rowcount or 0)


__all__ = [
    "BUCKETS",
    "ORDER",
    "OWNED_TYPES",
    "RELATIONS",
    "WARNING_KINDS",
    "CurrentEntity",
    "Guard",
    "Job",
    "OffboardingActions",
    "OffboardingError",
    "OffboardingPlan",
    "OffboardingProgress",
    "OffboardingQueued",
    "OffboardingRequest",
    "OffboardingView",
    "OffboardingWarning",
    "Outcome",
    "Plan",
    "apply",
    "begin_run",
    "check_request",
    "config_revision",
    "create",
    "fail",
    "finish",
    "get",
    "lock_principal",
    "plan",
    "purge_finished",
    "reconcile",
    "refuse_active",
    "save_progress",
    "stored",
]
