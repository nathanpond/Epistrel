"""Changing a story's content rating (FR-RATE-6,7; #118).

``PUT /stories/{id}/content-rating {content_rating_id}`` compares the effective guardrail sets
of the story's rating and the target by policy id (#48's closure, live policies only):

- **Relaxing** (the target's set is a subset of the current one, an equal set included) applies
  at once, in one transaction under the story lock: the story's ``content_rating_id`` and
  ``config_revision`` move, and ``record_config_change`` records the setting
  ``content_rating_id`` (class ``interpretive``, the acting principal, scope ``story``) with
  its ``control`` operation and ``config_changed`` event (#64). It needs no content check
  (FR-RATE-6). Every operation that commits after it runs under the new rating: a Lite turn in
  flight depends on ``content_rating_id`` (``core.config_dependencies``), so it is re-checked
  under the relaxed rating and records its version before it commits. Naming the story's own
  rating changes nothing (200, no ``config_changes`` row).
- **Tightening** (the target's set adds any policy, a sideways move included) starts a history
  check (FR-RATE-7): one ``rating_check_jobs`` row and its #70 job, a committed ``control``
  operation that binds the request's ``Idempotency-Key`` and records ``story_paused``, and the
  story ``paused`` (``paused_by`` = the job), all in one transaction; 202 ``{job_id,
  operation_id, status: checking}``. While it is paused the story refuses truth-changing calls
  (``core.pause``). The job (``orchestrator.jobs.handlers.rating_check``) waits for a turn in
  flight to commit, reads the story's whole active history (``read_history``), evaluates the
  target's effective policies over it (``evaluate_history``), then ``finish`` applies the
  change or refuses it, and the story resumes either way (``story_resumed``).

``evaluate_history`` is the reusable evaluator, also #119's at creation: deterministic
policies first (``core.policies.evaluate``), then each model-assisted policy through the
injected ``ModelCheck`` within ``calls_per_record`` calls per record (outside any turn budget);
an uncertain or unaffordable verdict follows the policy's ``on_uncertain``. Every violation is
listed (a deterministic one does not end the check), in position order, as positions and
policy identifiers only, never the text (invariant 3). Verdicts are cached per record and
policy version (``guardrail_check_results``), so a check that is resumed or run again does not
ask twice. A story image counts against the change when no image checker is configured
(``image_unchecked``); M3 has no story images, so ``story_image_records`` answers none.

When everything passes, ``finish`` applies the change in one transaction: the rating and the
story's ``config_revision`` move, the config change is recorded with the principal who asked
(its own committed ``control`` operation, as #117's job records its changes), every active
turn variant's ``checked_guardrails`` is restamped with the new set, and the story resumes.
Otherwise nothing changes, the story resumes, the violations are kept on the row, and an
``audit_log`` row (``rating_change_refused``) records the refusal. A history that moved since
it was read (``timeline_revision``) or a target rating edited since (its version) runs the
check again; a target retired meanwhile, or a job that fails its last attempt, ends
``failed`` with the story resumed on its old rating. A job the framework ends without the
handler seeing it (the runtime cap, or a failed commit, on its last attempt) leaves the check
``checking``; ``reconcile`` ends it the same way when it is next read or the rating is changed
again, and in the worker's recovery pass and the retention sweep. A story deleted during its
check takes the check and its cached verdicts with it, and a run that then saves progress finds
the check gone and ends (``save_progress``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import ColumnElement, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock, control, pause
from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.config_changes import ConfigScope, record_config_change, write_operation
from epistrel.core.enums import (
    Authority,
    EventCategory,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
    StoryStatus,
)
from epistrel.core.errors import (
    RatingCheckNotFound,
    RatingRetired,
    StoryArchived,
    StoryNotFound,
)
from epistrel.core.events.types import (
    PinnedItem,
    StoryPausedEvent,
    StoryResumedEvent,
)
from epistrel.core.ids import new_ulid
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Policy, Scope, Target
from epistrel.core.ratings import effective_guardrails, load_graph, require_assignable
from epistrel.core.ratings.stamp import live_policies, stamp_of
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.tables import (
    content_ratings,
    events,
    guardrail_check_results,
    jobs,
    operations,
    policies,
    rating_check_jobs,
    stories,
    story_texts,
)
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.rating_checks import RatingCheckKind, RatingCheckStatus

RATING_SETTING = "content_rating_id"
"""The story-scope ``config_changes`` setting of a rating change; old and new are rating ids."""
RATING_CHECK_KIND = "rating_check"
"""The #70 job kind of a tightening's history check."""
IMAGE_UNCHECKED = "image_unchecked"
"""The ``code`` of a violation for a story image no image checker could check."""
PLAYER_INPUT = "player_input"
MESSAGE_RENDERED = "message_rendered"
NON_TURN_ORDER = {"seed": 0, "pin": 1, "instruction": 2, "image": 3}
"""Where non-turn records sort, after every turn."""

Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class RatingChangeRequest(BaseModel):
    """``PUT /stories/{id}/content-rating``: the rating the story should have."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"content_rating_id": "00000000000000000000RAT002"}]},
    )

    content_rating_id: Reference


class Direction(StrEnum):
    RELAX = "relax"
    TIGHTEN = "tighten"


def classify(current: Iterable[str], target: Iterable[str]) -> Direction:
    """Relaxing when the target's guardrails are a subset of the current ones (equal sets
    included); tightening when the target adds any policy, whatever it drops (FR-RATE-6,7)."""
    return Direction.RELAX if set(target) <= set(current) else Direction.TIGHTEN


@dataclass(frozen=True, slots=True)
class RatingApplied:
    """The rating the story has after the call, at its current version, with the story's
    revisions; ``changed`` is false when the story already had it (nothing written)."""

    content_rating_id: str
    rating_version: str
    config_revision: int
    timeline_revision: int
    changed: bool
    applied: Literal[True] = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class RatingCheckStarted:
    """A tightening handed to its history check (202): the check's job, the ``control``
    operation that paused the story, and the story's revisions."""

    job_id: str
    operation_id: str
    timeline_revision: int
    config_revision: int
    status: Literal["checking"] = "checking"
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


RatingChange = RatingApplied | RatingCheckStarted

StartJob = Callable[[AsyncConnection], Awaitable[str]]
"""Enqueue the check's job in the caller's transaction and return its id (the Engine's, which
owns the Orchestrator's job queue)."""


async def live_guardrail_ids(
    conn: AsyncConnection, rating_id: str, graph: Mapping[str, Any]
) -> frozenset[str]:
    """The live policies of ``rating_id``'s effective guardrails (a retired policy applies to
    nothing, so it neither relaxes nor tightens)."""
    ids = [g.policy_id for g in effective_guardrails(rating_id, graph)]
    if not ids:
        return frozenset()
    found = await conn.scalars(
        select(policies.c.id).where(policies.c.id.in_(ids), policies.c.retired_at.is_(None))
    )
    return frozenset(found)


async def control_event(
    conn: AsyncConnection,
    *,
    story_id: str,
    operation_id: str,
    type_: str,
    payload: BaseModel,
    actor: str,
) -> None:
    """Append one control event to the story under the lock the caller holds."""
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == story_id)
    )
    await conn.execute(
        insert(events).values(
            id=new_ulid(),
            story_id=story_id,
            seq=int(last or 0) + 1,
            operation_id=operation_id,
            category=EventCategory.CONTROL.value,
            principal=actor,
            type=type_,
            payload=payload.model_dump(mode="json"),
            provenance=Provenance.CONSUMER.value,
            authority=Authority.AUTHORITATIVE.value,
        )
    )


async def _current_version(conn: AsyncConnection, rating_id: str) -> str:
    return str(
        await conn.scalar(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == rating_id)
        )
    )


async def change_rating(
    db: AsyncEngine,
    story_id: str,
    request: RatingChangeRequest,
    *,
    principal: str,
    start_job: StartJob,
) -> RatingChange:
    """Relax at once or start a tightening's check; see the module docstring. 404 for an
    unknown story, 409 ``story_archived`` / ``story_paused``, 422 ``rating_retired`` for a
    target that cannot be assigned."""
    target_id = request.content_rating_id
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(
                    stories.c.status,
                    stories.c.content_rating_id,
                    stories.c.config_revision,
                    stories.c.timeline_revision,
                )
                .where(stories.c.id == story_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        if row.status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
            )
        if pause.is_paused(row.status) and await reconcile(
            conn, rating_check_jobs.c.story_id == story_id
        ):
            row = (
                await conn.execute(
                    select(
                        stories.c.status,
                        stories.c.content_rating_id,
                        stories.c.config_revision,
                        stories.c.timeline_revision,
                    ).where(stories.c.id == story_id)
                )
            ).one()
        await pause.refuse(conn, story_id, row.status)
        config_revision = int(row.config_revision)
        timeline_revision = int(row.timeline_revision)
        current_id = str(row.content_rating_id)
        if target_id == current_id:
            return RatingApplied(
                content_rating_id=current_id,
                rating_version=await _current_version(conn, current_id),
                config_revision=config_revision,
                timeline_revision=timeline_revision,
                changed=False,
            )
        await require_assignable(conn, target_id, field="body.content_rating_id")
        graph = await load_graph(conn)
        current = await live_guardrail_ids(conn, current_id, graph)
        target = await live_guardrail_ids(conn, target_id, graph)
        if classify(current, target) is Direction.RELAX:
            config_revision += 1
            await _set_rating(conn, story_id, target_id, config_revision)
            await record_config_change(
                conn,
                config_revision=config_revision,
                setting=RATING_SETTING,
                old=current_id,
                new=target_id,
                actor=principal,
                change_class=ConfigClass.interpretive,
                scope=ConfigScope.STORY,
                scope_id=story_id,
            )
            return RatingApplied(
                content_rating_id=target_id,
                rating_version=graph[target_id].version_id,
                config_revision=config_revision,
                timeline_revision=timeline_revision,
                changed=True,
            )
        job_id = await start_job(conn)
        operation_id = await control.record_control_operation(
            conn, actor=principal, story_id=story_id
        )
        now = clock.now()
        await conn.execute(
            insert(rating_check_jobs).values(
                id=job_id,
                story_id=story_id,
                from_rating_id=current_id,
                target_rating_id=target_id,
                target_rating_version=graph[target_id].version_id,
                status=RatingCheckStatus.CHECKING.value,
                progress={"done": 0, "total": 0},
                violations=[],
                started_by=principal,
                started_at=now,
                operation_id=operation_id,
            )
        )
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(status=StoryStatus.PAUSED.value, paused_by=job_id)
        )
        await control_event(
            conn,
            story_id=story_id,
            operation_id=operation_id,
            type_="story_paused",
            payload=StoryPausedEvent(
                job_id=job_id, cause=pause.RATING_CHECK, target_rating_id=target_id
            ),
            actor=principal,
        )
    return RatingCheckStarted(
        job_id=job_id,
        operation_id=operation_id,
        timeline_revision=timeline_revision,
        config_revision=config_revision,
    )


async def _set_rating(
    conn: AsyncConnection, story_id: str, rating_id: str, config_revision: int
) -> None:
    await conn.execute(
        update(stories)
        .where(stories.c.id == story_id)
        .values(content_rating_id=rating_id, config_revision=config_revision)
    )


# -- the history and its evaluation -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One record of a story's active history to check: its cache ``key``, its ``position``
    (``{turn, variant, beat}`` or ``{kind, id}``), the ``target`` it is checked under, its
    text (for an image: its prompt), the characters it belongs to (character-scoped policies
    apply to those only), and whether it is a story image."""

    key: str
    position: dict[str, Any]
    target: Target
    text: str
    character_ids: tuple[str, ...] = ()
    image: bool = False


@dataclass(frozen=True, slots=True)
class HistoryViolation:
    """One record breaking one policy: where, and which policy (never the text). ``code`` is
    ``image_unchecked`` for a story image no image checker could check."""

    position: dict[str, Any]
    policy_id: str | None
    policy_name: str | None
    code: str | None = None

    def as_json(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "position": self.position,
            "policy_id": self.policy_id,
            "policy_name": self.policy_name,
        }
        if self.code is not None:
            found["code"] = self.code
        return found


CacheKey = tuple[str, str, int]
"""``(record key, policy id, policy version)``."""


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """Every violation in position order, how many records were checked, how many verdicts
    came from the cache, and the conclusive verdicts reached now (to store)."""

    violations: tuple[HistoryViolation, ...]
    checked: int
    cached: int
    verdicts: dict[CacheKey, str] = field(default_factory=dict)
    model_calls: int = 0

    @property
    def passed(self) -> bool:
        return not self.violations


class ModelCheck(Protocol):
    """One model-assisted policy against one text (the Orchestrator's ``guardrail check``
    purpose, outside any turn budget): ``True`` for a violation, ``False`` for a pass, ``None``
    when the model was uncertain or the call could not be afforded. A model that cannot answer
    raises (the check job retries)."""

    async def __call__(self, text: str, policy: Policy, target: Target) -> bool | None: ...


Progress = Callable[[int, int, Mapping[CacheKey, str]], Awaitable[None]]
"""``progress(done, total, verdicts)`` after each record, with the verdicts it reached."""


def _applies(policy: Policy, record: HistoryRecord) -> bool:
    if policy.scope is Scope.CHARACTER:
        return policy.scope_id in record.character_ids
    return True


def position_key(position: Mapping[str, Any]) -> tuple[Any, ...]:
    """Turns first (turn, variant, beat), then seeds, pins, instructions and images by id."""
    if "turn" in position:
        return (0, int(position["turn"]), int(position["variant"]), int(position.get("beat") or 0))
    return (1, NON_TURN_ORDER.get(str(position.get("kind")), 9), str(position.get("id")), 0)


def _uncertain(policy: Policy) -> str:
    rule = policy.rule
    on_uncertain = rule.on_uncertain if isinstance(rule, ModelAssistedRule) else "violation"
    return "violation" if on_uncertain == "violation" else "pass"


async def evaluate_history(
    target_guardrails: Sequence[Policy],
    records: Sequence[HistoryRecord],
    *,
    model_check: ModelCheck | None = None,
    calls_per_record: int = 0,
    cache: Mapping[CacheKey, str] | None = None,
    progress: Progress | None = None,
    image_check: Callable[[HistoryRecord], Awaitable[bool | None]] | None = None,
) -> HistoryResult:
    """Check every record against ``target_guardrails`` (the policies that apply, age
    conditions already decided); see the module docstring."""
    known = cache or {}
    violations: list[HistoryViolation] = []
    verdicts: dict[CacheKey, str] = {}
    cached = 0
    calls_made = 0
    total = len(records)
    for done, record in enumerate(records, start=1):
        found: dict[CacheKey, str] = {}
        target = Target.IMAGE_PROMPT if record.image else record.target
        applicable = [
            p for p in target_guardrails if target in p.applies_to and _applies(p, record)
        ]
        deterministic = [p for p in applicable if not isinstance(p.rule, ModelAssistedRule)]
        pending = [p for p in deterministic if (record.key, p.id, p.version) not in known]
        broken = {v.policy_id for v in evaluate(record.text, pending, target)} if pending else set()
        calls = 0
        for policy in applicable:
            key: CacheKey = (record.key, policy.id, policy.version)
            verdict = known.get(key)
            if verdict is not None:
                cached += 1
            elif not isinstance(policy.rule, ModelAssistedRule):
                verdict = "violation" if policy.id in broken else "pass"
                found[key] = verdict
            elif model_check is None or calls >= calls_per_record:
                verdict = _uncertain(policy)
            else:
                calls += 1
                answer = await model_check(record.text, policy, target)
                if answer is None:
                    verdict = _uncertain(policy)
                else:
                    verdict = "violation" if answer else "pass"
                    found[key] = verdict
            if verdict == "violation":
                violations.append(HistoryViolation(record.position, policy.id, policy.name))
        if record.image:
            checked = None if image_check is None else await image_check(record)
            if checked is None or checked:
                outputs = [
                    p
                    for p in target_guardrails
                    if Target.IMAGE_OUTPUT in p.applies_to and _applies(p, record)
                ]
                first = outputs[0] if outputs else None
                violations.append(
                    HistoryViolation(
                        record.position,
                        first.id if first else None,
                        first.name if first else None,
                        IMAGE_UNCHECKED if checked is None else None,
                    )
                )
        calls_made += calls
        verdicts.update(found)
        if progress is not None:
            await progress(done, total, found)
    ordered = sorted(
        violations, key=lambda v: (position_key(v.position), v.policy_id or "", v.code or "")
    )
    return HistoryResult(tuple(ordered), total, cached, verdicts, calls_made)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def story_image_records(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str] | None = None
) -> list[HistoryRecord]:
    """The story's active images (or, given ``operation_ids``, those turns' images, for a
    reactivation check, #120), each by its prompt (FR-IMG-5). M3 has no story images
    (``story_images`` arrives in M8, #272), so there are none."""
    return []


def _dotted(loc: Sequence[str | int]) -> str:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int) and parts:
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(str(part))
    return ".".join(parts)


def scenario_records(snapshot: Mapping[str, Any] | None) -> list[HistoryRecord]:
    """The genesis seeds of a story created from a scenario (FR-RATE-7, FR-SCN-9): the world
    description and every seed of its ``scenario_snapshot``, each at ``{kind: seed, id: <the
    field>}`` and checked as ``text_output``, as #119's creation check does
    (``genesis.create.check_rating``), so a tightening sees what a creation at the stricter
    rating would have refused. The opening narration is the story's turn 1, an event."""
    if not snapshot:
        return []
    draft = ScenarioDraft.model_validate(snapshot)
    found: list[tuple[str, str]] = []
    if draft.world.description:
        found.append(("world.description", draft.world.description))
    for loc, seed in draft.seeds():
        found.append((_dotted(loc) + ".text", seed.text))
    return [
        HistoryRecord(
            f"genesis:{locator}:{_digest(text)}",
            {"kind": "seed", "id": locator},
            Target.TEXT_OUTPUT,
            text,
        )
        for locator, text in found
    ]


async def event_records(
    conn: AsyncConnection, story_id: str, where: ColumnElement[bool]
) -> list[HistoryRecord]:
    """The player inputs (``text_input``) and rendered beats (``text_output``) of the story's
    operations matching ``where``, in ``seq`` order: a turn's at ``{turn, variant, beat}``, a
    position-less one's (genesis) at ``{kind: seed, id: <event id>}``."""
    found = await conn.execute(
        select(
            events.c.id,
            events.c.type,
            events.c.payload,
            events.c.character_id,
            events.c.beat,
            events.c.narrative_turn,
            events.c.turn_variant,
        )
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(
            events.c.story_id == story_id,
            events.c.type.in_([PLAYER_INPUT, MESSAGE_RENDERED]),
            where,
        )
        .order_by(events.c.seq)
    )
    records: list[HistoryRecord] = []
    for row in found:
        payload = dict(row.payload or {})
        text = str(payload.get("text") or "")
        people = [row.character_id] if row.character_id else []
        people += [str(c) for c in payload.get("render_character_ids") or () if c not in people]
        if row.narrative_turn is None:
            position: dict[str, Any] = {"kind": "seed", "id": row.id}
        else:
            position = {
                "turn": int(row.narrative_turn),
                "variant": int(row.turn_variant or 1),
                "beat": int(row.beat) if row.beat is not None else None,
            }
        target = Target.TEXT_INPUT if row.type == PLAYER_INPUT else Target.TEXT_OUTPUT
        records.append(HistoryRecord(row.id, position, target, text, tuple(people)))
    return records


@dataclass(frozen=True, slots=True)
class History:
    """A story's active history as read for a check, and the timeline revision it was read
    at."""

    records: tuple[HistoryRecord, ...]
    revision: int


async def read_history(conn: AsyncConnection, story_id: str) -> History:
    """Every record of the story's active history (FR-RATE-7): each active committed
    operation's player input (``text_input``) and rendered beats (``text_output``), generated
    or ingested, genesis included (a scenario's opening narration is its turn 1); the seeds and
    the world description of the scenario it was created from (``scenario_records``); the
    pinned memories and custom instructions (``text_input``); and the story's images. Read in
    the caller's transaction."""
    story = (
        await conn.execute(
            select(stories.c.timeline_revision, stories.c.scenario_snapshot).where(
                stories.c.id == story_id
            )
        )
    ).one_or_none()
    if story is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    revision = story.timeline_revision
    records = await event_records(
        conn,
        story_id,
        (operations.c.status == OperationStatus.COMMITTED.value)
        & (operations.c.membership == Membership.ACTIVE.value),
    )
    records.extend(scenario_records(story.scenario_snapshot))
    texts = (
        await conn.execute(
            select(story_texts.c.pinned, story_texts.c.custom_instructions).where(
                story_texts.c.story_id == story_id
            )
        )
    ).one_or_none()
    if texts is not None:
        for item in texts.pinned or ():
            pinned = PinnedItem.model_validate(item)
            records.append(
                HistoryRecord(
                    f"pin:{pinned.id}:{_digest(pinned.text)}",
                    {"kind": "pin", "id": pinned.id},
                    Target.TEXT_INPUT,
                    pinned.text,
                )
            )
        instructions = texts.custom_instructions or ""
        if instructions.strip():
            records.append(
                HistoryRecord(
                    f"instruction:{_digest(instructions)}",
                    {"kind": "instruction", "id": "custom_instructions"},
                    Target.TEXT_INPUT,
                    instructions,
                )
            )
    records.extend(await story_image_records(conn, story_id))
    return History(tuple(records), int(revision))


# -- the check's row ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    story_id: str
    from_rating_id: str
    target_rating_id: str
    target_rating_version: str
    status: str
    progress: dict[str, Any]
    violations: list[dict[str, Any]]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    started_by: str
    started_at: datetime
    finished_at: datetime | None
    operation_id: str | None
    checked_revision: int | None
    kind: str = RatingCheckKind.TIGHTENING.value


def _check(row: Any) -> Check:
    return Check(
        id=row.id,
        story_id=row.story_id,
        from_rating_id=row.from_rating_id,
        target_rating_id=row.target_rating_id,
        target_rating_version=row.target_rating_version,
        status=row.status,
        progress=dict(row.progress or {}),
        violations=list(row.violations or ()),
        result=dict(row.result) if row.result is not None else None,
        error=dict(row.error) if row.error is not None else None,
        started_by=row.started_by,
        started_at=row.started_at,
        finished_at=row.finished_at,
        operation_id=row.operation_id,
        checked_revision=int(row.checked_revision) if row.checked_revision is not None else None,
        kind=row.kind,
    )


async def load_check(db: AsyncEngine | AsyncConnection, job_id: str) -> Check | None:
    query = select(rating_check_jobs).where(rating_check_jobs.c.id == job_id)
    if isinstance(db, AsyncConnection):
        row = (await db.execute(query)).one_or_none()
    else:
        async with db.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
    return None if row is None else _check(row)


async def turn_in_flight(db: AsyncEngine, story_id: str) -> bool:
    """Whether a turn holds the story's turn slot (the check waits for it to commit)."""
    async with db.connect() as conn:
        holder = await conn.scalar(select(stories.c.in_flight_turn).where(stories.c.id == story_id))
    return holder is not None


async def load_cache(
    conn: AsyncConnection, story_id: str, keys: Iterable[str]
) -> dict[CacheKey, str]:
    """The stored verdicts of the story's records named by ``keys``."""
    wanted = sorted(set(keys))
    if not wanted:
        return {}
    found = await conn.execute(
        select(
            guardrail_check_results.c.record_key,
            guardrail_check_results.c.policy_id,
            guardrail_check_results.c.policy_version,
            guardrail_check_results.c.verdict,
        ).where(
            guardrail_check_results.c.story_id == story_id,
            guardrail_check_results.c.record_key.in_(wanted),
        )
    )
    return {(r.record_key, r.policy_id, int(r.policy_version)): r.verdict for r in found}


Guard = Callable[[AsyncConnection], Awaitable[None]]
"""Checked first in each of the job's own transactions: its claim still holds (#70)."""


async def save_progress(
    db: AsyncEngine,
    check: Check,
    done: int,
    total: int,
    verdicts: Mapping[CacheKey, str],
    guard: Guard,
    *,
    revision: int | None = None,
) -> bool:
    """Record ``done``/``total`` and store ``verdicts`` in one fenced transaction; ``False``,
    storing nothing, when the check is gone or no longer ``checking`` (the run then ends).

    The check's row is locked first. A story deleted meanwhile deletes it (``delete_story``
    empties ``rating_check_jobs`` before ``guardrail_check_results``), so either the deletion
    waits for this transaction and then removes the verdicts it stored, or this one waits for
    the deletion and finds the row gone: no verdict outlives its story."""
    async with db.begin() as conn:
        await guard(conn)
        status = await conn.scalar(
            select(rating_check_jobs.c.status)
            .where(rating_check_jobs.c.id == check.id)
            .with_for_update()
        )
        if status != RatingCheckStatus.CHECKING.value:
            return False
        values: dict[str, Any] = {"progress": {"done": done, "total": total}}
        if revision is not None:
            values["checked_revision"] = revision
        await conn.execute(
            update(rating_check_jobs).where(rating_check_jobs.c.id == check.id).values(**values)
        )
        if verdicts:
            statement = pg_insert(guardrail_check_results).values(
                [
                    {
                        "story_id": check.story_id,
                        "record_key": key,
                        "policy_id": policy_id,
                        "policy_version": version,
                        "verdict": verdict,
                    }
                    for (key, policy_id, version), verdict in verdicts.items()
                ]
            )
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        guardrail_check_results.c.story_id,
                        guardrail_check_results.c.record_key,
                        guardrail_check_results.c.policy_id,
                        guardrail_check_results.c.policy_version,
                    ],
                    set_={"verdict": statement.excluded.verdict, "at": func.now()},
                )
            )
    return True


class Outcome(StrEnum):
    APPLIED = "applied"
    REFUSED = "refused"
    RESTART = "restart"
    """The history or the target rating moved since the evaluation read them: run again."""
    GONE = "gone"
    """The story, or the check, is no longer there (deleted, or ended by another run)."""


async def _lock_story(conn: AsyncConnection, check: Check) -> Any:
    return (
        await conn.execute(
            select(
                stories.c.status,
                stories.c.paused_by,
                stories.c.timeline_revision,
                stories.c.config_revision,
                stories.c.content_rating_id,
            )
            .where(stories.c.id == check.story_id)
            .with_for_update()
        )
    ).one_or_none()


async def _resume(conn: AsyncConnection, check: Check, operation_id: str, outcome: str) -> None:
    await conn.execute(
        update(stories)
        .where(stories.c.id == check.story_id)
        .values(status=StoryStatus.ACTIVE.value, paused_by=None)
    )
    await control_event(
        conn,
        story_id=check.story_id,
        operation_id=operation_id,
        type_="story_resumed",
        payload=StoryResumedEvent(job_id=check.id, outcome=outcome),
        actor=check.started_by,
    )


async def finish(
    db: AsyncEngine,
    check: Check,
    result: HistoryResult,
    target_policies: Sequence[Policy],
    *,
    evaluated_version: str,
    evaluated_revision: int,
    guard: Guard,
) -> Outcome:
    """Apply the change (every record passed) or refuse it, and resume the story, in one
    fenced transaction under the story lock; see the module docstring."""
    async with db.begin() as conn:
        await guard(conn)
        current = await load_check(conn, check.id)
        if current is None or current.status != RatingCheckStatus.CHECKING.value:
            return Outcome.GONE
        story = await _lock_story(conn, check)
        if story is None:
            return Outcome.GONE
        version = await _current_version(conn, check.target_rating_id)
        if int(story.timeline_revision) != evaluated_revision or version != evaluated_version:
            await conn.execute(
                update(rating_check_jobs)
                .where(rating_check_jobs.c.id == check.id)
                .values(target_rating_version=version)
            )
            return Outcome.RESTART
        try:
            await require_assignable(conn, check.target_rating_id, field="body.content_rating_id")
        except RatingRetired as exc:
            await _fail_in(conn, check, story, exc)
            return Outcome.GONE
        now = clock.now()
        if result.passed:
            revision = int(story.config_revision) + 1
            await _set_rating(conn, check.story_id, check.target_rating_id, revision)
            operation_id = await write_operation(
                conn, story_id=check.story_id, actor=check.started_by
            )
            await record_config_change(
                conn,
                config_revision=revision,
                setting=RATING_SETTING,
                old=str(story.content_rating_id),
                new=check.target_rating_id,
                actor=check.started_by,
                change_class=ConfigClass.interpretive,
                scope=ConfigScope.STORY,
                scope_id=check.story_id,
            )
            await conn.execute(
                update(operations)
                .where(
                    operations.c.story_id == check.story_id,
                    operations.c.kind == OperationKind.TURN.value,
                    operations.c.status == OperationStatus.COMMITTED.value,
                    operations.c.membership == Membership.ACTIVE.value,
                )
                .values(
                    checked_guardrails=stamp_of(
                        check.target_rating_id, evaluated_version, target_policies
                    )
                )
            )
            await _resume(conn, check, operation_id, Outcome.APPLIED.value)
            await conn.execute(
                update(rating_check_jobs)
                .where(rating_check_jobs.c.id == check.id)
                .values(
                    status=RatingCheckStatus.APPLIED.value,
                    progress={"done": result.checked, "total": result.checked},
                    result={"rating_version": evaluated_version, "config_revision": revision},
                    finished_at=now,
                )
            )
            return Outcome.APPLIED
        operation_id = await control.record_control_operation(
            conn, actor=check.started_by, story_id=check.story_id
        )
        await _resume(conn, check, operation_id, Outcome.REFUSED.value)
        violations = [v.as_json() for v in result.violations]
        await conn.execute(
            update(rating_check_jobs)
            .where(rating_check_jobs.c.id == check.id)
            .values(
                status=RatingCheckStatus.REFUSED.value,
                progress={"done": result.checked, "total": result.checked},
                violations=violations,
                finished_at=now,
            )
        )
        await audit.record(
            conn,
            AuditKind.RATING_CHANGE_REFUSED,
            principal=check.started_by,
            story_id=check.story_id,
            policy_ids=sorted({v.policy_id for v in result.violations if v.policy_id}),
            details={
                "job_id": check.id,
                "target_rating_id": check.target_rating_id,
                "violations": len(violations),
            },
        )
        return Outcome.REFUSED


async def _fail_in(conn: AsyncConnection, check: Check, story: Any, error: BaseException) -> None:
    await _fail_as(conn, check, story, type(error).__name__)


async def _fail_as(conn: AsyncConnection, check: Check, story: Any, kind: str) -> None:
    if story is not None and story.paused_by == check.id:
        operation_id = await control.record_control_operation(
            conn, actor=check.started_by, story_id=check.story_id
        )
        await _resume(conn, check, operation_id, RatingCheckStatus.FAILED.value)
    await conn.execute(
        update(rating_check_jobs)
        .where(rating_check_jobs.c.id == check.id)
        .values(
            status=RatingCheckStatus.FAILED.value,
            error={"class": kind, "message": f"the rating check stopped ({kind})"},
            finished_at=clock.now(),
        )
    )


async def fail(db: AsyncEngine, job_id: str, error: BaseException) -> None:
    """``failed`` (the job's last attempt failed): the story resumes on its old rating. The
    message names the error's class only, never what was being checked."""
    async with db.begin() as conn:
        check = await load_check(conn, job_id)
        if check is None or check.status != RatingCheckStatus.CHECKING.value:
            return
        story = await _lock_story(conn, check)
        await _fail_in(conn, check, story, error)


JOB_ENDED = ("done", "failed")
"""#70's terminal job statuses."""


def _ended_class(job_status: str | None, result: Any) -> str:
    """The error class a reconciled check reports: the job's own failure class when it recorded
    one (an exception class name, never a message), else what happened to the job."""
    if job_status is None:
        return "JobMissing"
    error = result.get("error") if isinstance(result, Mapping) else None
    if job_status == "failed" and isinstance(error, str) and error.isidentifier():
        return error
    return "JobFailed" if job_status == "failed" else "JobEnded"


async def reconcile(conn: AsyncConnection, where: ColumnElement[bool]) -> int:
    """End as ``failed`` every ``checking`` check matching ``where`` whose #70 job has ended
    (``done`` or ``failed``) or is gone, resuming its story on its old rating, in ``conn``'s
    transaction; returns how many it ended.

    The handler ends its check on its last failed attempt, but the job framework can also end
    the job without the handler seeing it: the worker's runtime cap cancels the handler, or the
    commit itself fails, on the last attempt. Without this the story would stay paused for
    ever. It runs when the check is read (``get_check``), when the story's rating is changed
    again (``change_rating``), in the worker's start-up recovery pass and in the retention
    sweep. Each story is locked before its check (the order ``finish`` and ``delete_story``
    take)."""
    ended = (
        await conn.execute(
            select(
                rating_check_jobs.c.id,
                rating_check_jobs.c.story_id,
                jobs.c.status.label("job_status"),
                jobs.c.result,
            )
            .select_from(rating_check_jobs.outerjoin(jobs, jobs.c.id == rating_check_jobs.c.id))
            .where(
                where,
                rating_check_jobs.c.status == RatingCheckStatus.CHECKING.value,
                or_(jobs.c.id.is_(None), jobs.c.status.in_(JOB_ENDED)),
            )
            .order_by(rating_check_jobs.c.id)
        )
    ).all()
    closed = 0
    for row in ended:
        story = (
            await conn.execute(
                select(stories.c.paused_by).where(stories.c.id == row.story_id).with_for_update()
            )
        ).one_or_none()
        locked = (
            await conn.execute(
                select(rating_check_jobs)
                .where(
                    rating_check_jobs.c.id == row.id,
                    rating_check_jobs.c.status == RatingCheckStatus.CHECKING.value,
                )
                .with_for_update()
            )
        ).one_or_none()
        if locked is None:
            continue
        await _fail_as(conn, _check(locked), story, _ended_class(row.job_status, row.result))
        closed += 1
    return closed


# -- reads --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RatingCheckView:
    """A rating check's state (``GET /stories/{id}/content-rating/checks/{job_id}``): its
    ``kind`` (``tightening``, or ``reactivation``, #120), status (``checking``, ``applied``,
    ``reactivated``, ``refused``, ``failed``), progress, who started it and when, the target;
    once applied the rating version and the story's ``config_revision``; once reactivated the
    ``outcome`` (``{operation, result}``: the edit the check held back and what it answered);
    once refused every violation by position and policy; once failed the error's class."""

    job_id: str
    story_id: str
    status: str
    progress: dict[str, int]
    started_at: datetime
    started_by: str
    finished_at: datetime | None
    target: dict[str, str]
    from_rating_id: str
    operation_id: str | None
    rating_version: str | None = None
    config_revision: int | None = None
    violations: tuple[dict[str, Any], ...] = ()
    error: dict[str, Any] | None = None
    kind: str = RatingCheckKind.TIGHTENING.value
    outcome: dict[str, Any] | None = None


async def get_check(db: AsyncEngine, story_id: str, job_id: str) -> RatingCheckView:
    """The check ``job_id`` of ``story_id``: 404 for an unknown story, or a job that is not
    one of the story's checks (another story's, or purged)."""
    async with db.begin() as conn:
        exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
        if exists is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        await reconcile(
            conn, (rating_check_jobs.c.id == job_id) & (rating_check_jobs.c.story_id == story_id)
        )
        check = await load_check(conn, job_id)
    if check is None or check.story_id != story_id:
        raise RatingCheckNotFound(
            f"story {story_id} has no rating check {job_id}",
            {"story_id": story_id, "job_id": job_id},
        )
    result = check.result or {}
    reactivation = check.kind == RatingCheckKind.REACTIVATION.value
    return RatingCheckView(
        job_id=check.id,
        story_id=check.story_id,
        status=check.status,
        progress={
            "done": int(check.progress.get("done", 0)),
            "total": int(check.progress.get("total", 0)),
        },
        started_at=check.started_at,
        started_by=check.started_by,
        finished_at=check.finished_at,
        target={"rating_id": check.target_rating_id, "version": check.target_rating_version},
        from_rating_id=check.from_rating_id,
        operation_id=check.operation_id,
        rating_version=None if reactivation else result.get("rating_version"),
        config_revision=None if reactivation else result.get("config_revision"),
        violations=tuple(check.violations),
        error=check.error,
        kind=check.kind,
        outcome=dict(result) if reactivation and check.result is not None else None,
    )


async def pause_of(conn: AsyncConnection, story_id: str) -> dict[str, Any] | None:
    """What ``GET /stories/{id}`` shows of a paused story: ``{job_id, cause, progress}``."""
    status = await conn.scalar(select(stories.c.status).where(stories.c.id == story_id))
    if not pause.is_paused(status):
        return None
    found = await pause.details(conn, story_id)
    return {k: found[k] for k in ("job_id", "cause", "progress")}


async def purge_finished(conn: AsyncConnection, before: datetime, limit: int) -> int:
    """Delete up to ``limit`` finished checks that ended before ``before`` (retention)."""
    ids = (
        select(rating_check_jobs.c.id)
        .where(
            rating_check_jobs.c.status != RatingCheckStatus.CHECKING.value,
            rating_check_jobs.c.finished_at < before,
        )
        .limit(limit)
        .scalar_subquery()
    )
    result = await conn.execute(delete(rating_check_jobs).where(rating_check_jobs.c.id.in_(ids)))
    return int(result.rowcount or 0)


__all__ = [
    "IMAGE_UNCHECKED",
    "RATING_CHECK_KIND",
    "RATING_SETTING",
    "Check",
    "Direction",
    "History",
    "HistoryRecord",
    "HistoryResult",
    "HistoryViolation",
    "ModelCheck",
    "Outcome",
    "RatingApplied",
    "RatingChange",
    "RatingChangeRequest",
    "RatingCheckStarted",
    "RatingCheckView",
    "change_rating",
    "classify",
    "evaluate_history",
    "event_records",
    "fail",
    "finish",
    "get_check",
    "live_guardrail_ids",
    "live_policies",
    "load_cache",
    "load_check",
    "pause_of",
    "position_key",
    "purge_finished",
    "read_history",
    "reconcile",
    "save_progress",
    "scenario_records",
    "story_image_records",
    "turn_in_flight",
]
