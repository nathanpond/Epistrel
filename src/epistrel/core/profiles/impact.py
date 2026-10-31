"""Impact reports and the two-stage flow (FR-PROF-7,8,16, FR-SCN-2; #109).

**Two stages (FR-PROF-7).** Every operation that may degrade a story (assigning or changing a
story's profile, editing a profile stories follow, applying story overrides, creating a story
whose profile lacks a capability its scenario recommends) passes through
``two_stage(report_fn, apply_fn, ignore_warnings=...)``, inside the operation's own transaction
and after its prechecks:

1. ``report_fn()`` builds the ``ImpactReport``.
2. When the report warns and the request did not set ``ignore_warnings`` (``ignoreWarnings``
   for camelCase clients; ``TwoStageRequest``), nothing is applied: the result is the report,
   ``applied: false``. The caller writes nothing, and the Engine releases the call's
   ``Idempotency-Key``, so a confirmation sent under the same key is its first execution.
3. Otherwise ``apply_fn(report)`` runs and the result carries both, ``applied: true``.

A report warns when an affected story gains or loses a capability, needs a backfill or cannot
be upgraded, or when a scenario recommendation is unmet or a guarantee is lost
(``WARNING_KINDS``); a change of fingerprint or quality status alone applies at once. An invalid
configuration is refused before any report (422 ``invalid_profile``, FR-PROF-4), and a call
without the service credential never gets this far (#44): ``ignore_warnings`` overrides
neither. Each report is logged at INFO with counts and warning codes only.

**What a report holds.** For each affected story, ordered by story id and at most ``AFFECTED_MAX``
of them (``affected_count`` counts them all): the capabilities it gains and loses (a module switched
on or off, or raised or lowered a level; the pipeline by its level), whether a backfill is needed
and its estimate (model calls, tokens, duration; ``estimates``), whether the upgrade is unavailable,
and FR-PROF-16's stamp of the configuration it would run under: its fingerprint before and after,
and the capabilities enabled and the quality status after (``capabilities_after``,
``quality_status_after``). Then the scenario recommendations the result does not meet
(``recommended_by`` is the scenario's ``<catalog id>@<version>``), the guarantee-table rows it loses
(FR-VERIFY-15: ``guarantees.GUARANTEES`` ids, as ``docs/guarantees.md`` publishes them; #132), and
the warning kinds that made it two-stage. ``budget_warnings`` (#456) carries the profile
validator's ``below_safety_minimum`` for the configuration after the change (a profile edit's
new settings, a story's resolved configuration), informational only: it holds nothing. A report
on one story (a preview, an assignment,
overrides) also states the fingerprints, the capabilities and the quality status at its top. So does
a report that lists no story, since no entry can state them: a creation's states the new story's,
and an edit's, when no story follows the profile, states the profile's own before and after the edit
(what a story following it would run under). A report that lists a profile's following stories
leaves them null at its top.

**M3's interim rule (FR-PROF-8, until M7).** A change that adds data-producing capability
(``schema.data_increases``) to a story with history, meaning at least one committed, active
turn, cannot apply to it, because M3 has no backfill. The report lists that story with
``upgrade_unavailable: true`` and ``backfill.needed: true`` with the estimate; a confirmed change
applies to every other affected story, and that one keeps its previous configuration (a profile
edit pins it to the version it ran on). Its ``fingerprint_after`` and ``capabilities_after`` are
the ones it keeps. A story with no committed, active turn upgrades, as there is nothing to
backfill: one that never played, one whose only turn failed, one whose turns were truncated
away. So in M3 ``backfill.needed`` is true exactly when the upgrade is unavailable. No module
delivered in M3 is data-producing; ``epistrel.testing.profiles.data_producing`` marks one for
tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    StrictBool,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.ids import Ulid
from epistrel.core.profiles import effective, estimates, schema
from epistrel.core.profiles.capabilities import module_names
from epistrel.core.profiles.effective import Configuration
from epistrel.core.profiles.fingerprint import QualityStatus, RuntimeFacts, Stamp
from epistrel.core.profiles.guarantees import guarantees_gained, guarantees_lost, ordered
from epistrel.core.profiles.reservation import SafetyFacts
from epistrel.core.profiles.schema import ProfileSettings, Purposes, SettingsPatch
from epistrel.core.profiles.validate import ProfileValidationWarning, safety_warnings
from epistrel.core.tables import operations, stories, transcript_messages
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

AFFECTED_MAX = 1_000
"""The most affected stories a report lists; ``affected_count`` counts them all."""
CAMEL_CASE = "ignoreWarnings"
"""The camelCase spelling of ``ignore_warnings`` (FR-PROF-7)."""
WARNING_KINDS = (
    "capabilities_gained",
    "capabilities_lost",
    "backfill_needed",
    "upgrade_unavailable",
    "recommendations_unmet",
    "guarantees_lost",
)
"""Why a report holds its change for confirmation, in the order ``warnings`` lists them."""


# -- the report -----------------------------------------------------------------------------------


class BackfillEstimate(BaseModel):
    """What re-deriving a story's data would take (FR-PROF-7): model calls, tokens, and the
    duration in seconds with the calls made one after another."""

    model_config = ConfigDict(frozen=True)

    model_calls: int
    tokens: int
    duration_s: float


class Backfill(BaseModel):
    """Whether the change needs a backfill for this story, and its estimate when it does. In M3
    one is needed exactly when the upgrade is unavailable (no backfill exists until M7)."""

    model_config = ConfigDict(frozen=True)

    needed: bool
    estimate: BackfillEstimate | None = None


class AffectedStory(BaseModel):
    """One story a change affects: the capabilities it gains and loses, its backfill, whether
    the upgrade is unavailable to it (it has history, and the change adds data-producing
    capability), and its fingerprint before and after with the capabilities enabled and the
    quality status after: those of the configuration it would run under, which is its current
    one when the upgrade is unavailable (FR-PROF-16)."""

    model_config = ConfigDict(frozen=True)

    story_id: str
    capabilities_gained: list[str]
    capabilities_lost: list[str]
    backfill: Backfill
    upgrade_unavailable: bool
    fingerprint_before: str | None = None
    fingerprint_after: str | None = None
    capabilities_after: list[str] | None = None
    quality_status_after: QualityStatus | None = None


class UnmetRecommendation(BaseModel):
    """A capability a scenario recommends (FR-SCN-2) that the configuration does not enable;
    ``recommended_by`` is the scenario version, ``<catalog id>@<version>``."""

    model_config = ConfigDict(frozen=True)

    capability: str
    recommended_by: str


class ImpactReport(BaseModel):
    """What a change would do before it is applied (FR-PROF-7): the affected stories (ordered by
    story id, at most 1,000; ``affected_count`` counts them all), the scenario recommendations
    left unmet, the guarantees lost (the ids of the guarantee table's rows, FR-VERIFY-15, that
    held before and do not after) and gained (#181: the other way round), the fingerprints, the
    capabilities enabled and the quality status after (FR-PROF-16: at the top of a report on one
    story, or on none, where an edit of a profile no story follows states the profile's own; inside
    each entry of a report on a profile's following stories, with the top ones null), and the
    ``warnings`` that hold the change for confirmation (empty: it applies at once)."""

    model_config = ConfigDict(frozen=True)

    affected_stories: list[AffectedStory]
    affected_count: int
    recommendations_unmet: list[UnmetRecommendation]
    guarantees_lost: list[str]
    guarantees_gained: list[str] = Field(
        default_factory=list,
        description="The ids of the guarantee table's deterministic rows that do not hold before "
        "and do after (#181), as `guarantees_lost` names those lost: what the change makes the "
        "engine guarantee.",
    )
    fingerprint_before: str | None = None
    fingerprint_after: str | None = None
    capabilities_after: list[str] | None = None
    quality_status_after: QualityStatus | None = None
    warnings: list[str]
    budget_warnings: list[ProfileValidationWarning] = Field(
        default_factory=list,
        description="The profile validator's warnings for the configuration after the change "
        "(#456): `below_safety_minimum` when its `budgets.model_calls` is below a turn's "
        "single-primary safety minimum with the deployment's model-assisted policies, so every "
        "turn would be refused (NFR-PERF-6, FR-PROF-7), as `POST /engine-profiles/{id}/validate` "
        "answers it. Informational: it does not hold the change for confirmation.",
    )


# -- comparing two configurations ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Delta:
    """One change of configuration: the capabilities it adds and removes, the modules whose data
    it adds, the guarantees it loses, and the stamps before and after."""

    gained: tuple[str, ...]
    lost: tuple[str, ...]
    increases: tuple[str, ...]
    guarantees_lost: tuple[str, ...]
    before: Stamp
    after: Stamp
    guarantees_gained: tuple[str, ...] = ()


def _moved(before: ProfileSettings, after: ProfileSettings, *, up: bool) -> set[str]:
    """Modules on before and after whose level rose (``up``) or fell; the pipeline is named by
    its level, so its change is already a capability gained and lost."""
    old, new = schema.enabled_modules(before), schema.enabled_modules(after)
    moved: set[str] = set()
    for name in (old.keys() & new.keys()) - {"pipeline"}:
        rank_old, rank_new = schema.level_rank(name, old[name]), schema.level_rank(name, new[name])
        if (rank_new > rank_old) if up else (rank_new < rank_old):
            moved.add(name)
    return moved


def delta(before: Configuration, after: Configuration, facts: RuntimeFacts) -> Delta:
    """How ``after`` differs from ``before``."""
    had, has = set(module_names(before.settings)), set(module_names(after.settings))
    return Delta(
        gained=tuple(sorted((has - had) | _moved(before.settings, after.settings, up=True))),
        lost=tuple(sorted((had - has) | _moved(before.settings, after.settings, up=False))),
        increases=tuple(schema.data_increases(before.settings, after.settings)),
        guarantees_lost=guarantees_lost(before.settings, after.settings),
        before=before.stamp(facts),
        guarantees_gained=guarantees_gained(before.settings, after.settings),
        after=after.stamp(facts),
    )


@dataclass(frozen=True, slots=True)
class Recommendation:
    """The capabilities a scenario version recommends (FR-SCN-2), and that version."""

    capabilities: tuple[str, ...]
    recommended_by: str


def unmet(
    recommendation: Recommendation | None, settings: ProfileSettings
) -> list[UnmetRecommendation]:
    """The recommended capabilities ``settings`` does not enable, in the scenario's order."""
    if recommendation is None:
        return []
    enabled = set(module_names(settings))
    return [
        UnmetRecommendation(capability=name, recommended_by=recommendation.recommended_by)
        for name in recommendation.capabilities
        if name not in enabled
    ]


def affected(
    story_id: str, change: Delta, *, history: bool, transcript: tuple[int, int] = (0, 0)
) -> AffectedStory:
    """``story_id``'s entry: an upgrade is unavailable when the change adds data-producing
    capability and the story has history; then it keeps its configuration (the stamp after is
    the one before), and the estimate is over its transcript (``messages``, ``characters``)."""
    unavailable = bool(change.increases) and history
    estimate = None
    if unavailable:
        calls, tokens, duration = estimates.backfill_cost(*transcript, len(change.increases))
        estimate = BackfillEstimate(model_calls=calls, tokens=tokens, duration_s=duration)
    kept = change.before if unavailable else change.after
    return AffectedStory(
        story_id=story_id,
        capabilities_gained=list(change.gained),
        capabilities_lost=list(change.lost),
        backfill=Backfill(needed=unavailable, estimate=estimate),
        upgrade_unavailable=unavailable,
        fingerprint_before=change.before.fingerprint,
        fingerprint_after=kept.fingerprint,
        capabilities_after=list(kept.capabilities),
        quality_status_after=kept.quality_status,
    )


def _warnings(
    change: Delta | None,
    *,
    affects: bool,
    unavailable: bool,
    recommendations: Sequence[UnmetRecommendation],
) -> list[str]:
    found = {
        "capabilities_gained": affects and change is not None and bool(change.gained),
        "capabilities_lost": affects and change is not None and bool(change.lost),
        "backfill_needed": unavailable,
        "upgrade_unavailable": unavailable,
        "recommendations_unmet": bool(recommendations),
        "guarantees_lost": affects and change is not None and bool(change.guarantees_lost),
    }
    return [kind for kind in WARNING_KINDS if found[kind]]


def single_report(
    entry: AffectedStory,
    change: Delta,
    *,
    recommendations: Sequence[UnmetRecommendation] = (),
    budget_warnings: Sequence[ProfileValidationWarning] = (),
) -> ImpactReport:
    """The report of a change to one story (a preview, an assignment, overrides);
    ``budget_warnings`` are the validator's for its configuration after (#456)."""
    return ImpactReport(
        affected_stories=[entry],
        affected_count=1,
        recommendations_unmet=list(recommendations),
        guarantees_lost=list(change.guarantees_lost),
        guarantees_gained=list(change.guarantees_gained),
        fingerprint_before=entry.fingerprint_before,
        fingerprint_after=entry.fingerprint_after,
        capabilities_after=entry.capabilities_after,
        quality_status_after=entry.quality_status_after,
        warnings=_warnings(
            change,
            affects=True,
            unavailable=entry.upgrade_unavailable,
            recommendations=recommendations,
        ),
        budget_warnings=list(budget_warnings),
    )


def creation_report(
    after: Stamp, settings: ProfileSettings, recommendation: Recommendation | None
) -> ImpactReport:
    """The report of creating a story (#119): no story is affected yet; the new story's
    fingerprint, capabilities and quality status, and the scenario's recommendations its
    profile leaves unmet (the only warning a creation can carry)."""
    missing = unmet(recommendation, settings)
    return ImpactReport(
        affected_stories=[],
        affected_count=0,
        recommendations_unmet=missing,
        guarantees_lost=[],
        fingerprint_after=after.fingerprint,
        capabilities_after=list(after.capabilities),
        quality_status_after=after.quality_status,
        warnings=_warnings(None, affects=False, unavailable=False, recommendations=missing),
    )


# -- the stories a change reaches ----------------------------------------------------------------


async def with_history(conn: AsyncConnection, story_ids: Collection[str]) -> set[str]:
    """The stories among ``story_ids`` with history: at least one committed, active turn
    (generated or ingested; a failed turn is not committed, and a truncated one is not
    active)."""
    if not story_ids:
        return set()
    found = await conn.scalars(
        select(operations.c.story_id)
        .where(
            operations.c.story_id.in_(list(story_ids)),
            operations.c.kind == OperationKind.TURN.value,
            operations.c.membership == Membership.ACTIVE.value,
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        .distinct()
    )
    return {str(story_id) for story_id in found}


async def transcripts(
    conn: AsyncConnection, story_ids: Collection[str]
) -> dict[str, tuple[int, int]]:
    """Each story's active transcript: how many messages, and how many characters in all."""
    if not story_ids:
        return {}
    t = transcript_messages
    rows = await conn.execute(
        select(t.c.story_id, func.count(), func.coalesce(func.sum(func.length(t.c.text)), 0))
        .where(t.c.story_id.in_(list(story_ids)), t.c.membership == Membership.ACTIVE.value)
        .group_by(t.c.story_id)
    )
    return {str(r[0]): (int(r[1]), int(r[2])) for r in rows}


@dataclass(frozen=True, slots=True)
class Following:
    """A profile edit's report and the following stories that keep their configuration."""

    report: ImpactReport
    keep: frozenset[str]


async def following_report(
    conn: AsyncConnection,
    profile_id: str,
    change: Delta,
    overridden: Mapping[str, Delta] | None = None,
    *,
    budget_warnings: Sequence[ProfileValidationWarning] = (),
) -> Following:
    """The report of a change to every story that follows ``profile_id``'s latest version (a
    story pinned to an earlier version does not follow it, and is not affected). ``change`` is
    what a story without overrides sees; ``overridden`` holds each following story with
    overrides and the change it sees through them (#113: an override of a setting the edit
    changes keeps the story's value). When a change adds data-producing capability, the
    following stories are locked ``FOR NO KEY UPDATE``, in id order, before their history is
    read: a turn cannot commit on one until the edit has, so the stories kept are exactly the
    ones with history whose own change adds such capability. Each entry states the stamp its
    story would run under; when the report lists no story, its top states the profile's own,
    before and after the change (FR-PROF-16: every impact report states one).
    ``budget_warnings`` are the validator's for the profile's new version (#456)."""
    overridden = overridden or {}
    following = (
        stories.c.engine_profile_id == profile_id,
        stories.c.engine_profile_version_pin.is_(None),
    )
    count = int(await conn.scalar(select(func.count()).select_from(stories).where(*following)) or 0)
    keep: set[str] = set()
    increases = bool(change.increases) or any(d.increases for d in overridden.values())
    if increases and count:
        every = list(
            await conn.scalars(
                select(stories.c.id)
                .where(*following)
                .order_by(stories.c.id)
                .with_for_update(key_share=True)
            )
        )
        keep = {
            story
            for story in await with_history(conn, every)
            if overridden.get(story, change).increases
        }
    listed = [
        str(story_id)
        for story_id in await conn.scalars(
            select(stories.c.id).where(*following).order_by(stories.c.id).limit(AFFECTED_MAX)
        )
    ]
    sizes = await transcripts(conn, [s for s in listed if s in keep])
    entries = [
        affected(s, overridden.get(s, change), history=s in keep, transcript=sizes.get(s, (0, 0)))
        for s in listed
    ]
    unlisted = not entries  # no entry states a stamp, so the top states the profile's own
    seen = [change] if count > len(overridden) else []
    seen += [overridden[story] for story in sorted(overridden)]
    report = ImpactReport(
        affected_stories=entries,
        affected_count=count,
        recommendations_unmet=[],
        guarantees_lost=_lost(seen) if count else [],
        guarantees_gained=_gained(seen) if count else [],
        fingerprint_before=change.before.fingerprint if unlisted else None,
        fingerprint_after=change.after.fingerprint if unlisted else None,
        capabilities_after=list(change.after.capabilities) if unlisted else None,
        quality_status_after=change.after.quality_status if unlisted else None,
        warnings=_following_warnings(seen, unavailable=bool(keep)),
        budget_warnings=list(budget_warnings),
    )
    return Following(report, frozenset(keep))


def _lost(changes: Sequence[Delta]) -> list[str]:
    """The guarantees any of ``changes`` loses, by ``GUARANTEES`` id, in its order."""
    return ordered({name for found in changes for name in found.guarantees_lost})


def _gained(changes: Sequence[Delta]) -> list[str]:
    """The guarantees any of ``changes`` gains, by ``GUARANTEES`` id, in its order."""
    return ordered({name for found in changes for name in found.guarantees_gained})


def _following_warnings(changes: Sequence[Delta], *, unavailable: bool) -> list[str]:
    """The warning kinds of a report over stories that see ``changes``."""
    found: set[str] = {"backfill_needed", "upgrade_unavailable"} if unavailable else set()
    for one in changes:
        found.update(_warnings(one, affects=True, unavailable=False, recommendations=()))
    return [kind for kind in WARNING_KINDS if kind in found]


# -- the two stages --------------------------------------------------------------------------------


class TwoStageRequest(BaseModel):
    """Base of every request whose change may degrade a story (FR-PROF-7): without
    ``ignore_warnings`` a change the impact report warns of is not applied, and the answer is
    the report; with it, the change applies and the answer carries the report too.
    ``ignoreWarnings`` is the same flag for camelCase clients; sending both with different
    values is 422 ``validation_error``."""

    model_config = ConfigDict(extra="forbid")

    ignore_warnings: StrictBool = Field(
        default=False,
        validation_alias=AliasChoices("ignore_warnings", CAMEL_CASE),
        description="Apply the change even when its impact report warns (FR-PROF-7). An invalid "
        "configuration is refused all the same.",
    )

    @model_validator(mode="before")
    @classmethod
    def _one_spelling(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "ignore_warnings" in data and CAMEL_CASE in data:
            if data["ignore_warnings"] != data[CAMEL_CASE]:
                raise ValueError("ignore_warnings and ignoreWarnings disagree: send one of them")
            data = {key: value for key, value in data.items() if key != CAMEL_CASE}
        return data

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Document ``ignoreWarnings`` beside ``ignore_warnings`` (the published schema allows
        no other property)."""
        found = handler(core_schema)
        if handler.mode == "validation":
            target = handler.resolve_ref_schema(found)
            properties: dict[str, Any] = target.setdefault("properties", {})
            if "ignore_warnings" in properties:
                properties[CAMEL_CASE] = {
                    **properties["ignore_warnings"],
                    "title": "Ignorewarnings",
                    "description": "`ignore_warnings`, for camelCase clients (send one of the "
                    "two).",
                }
        return found


@dataclass(frozen=True, slots=True)
class TwoStageResult[T]:
    """A two-stage operation's outcome (FR-PROF-7): ``applied``, the ``impact`` report, and the
    operation's own ``result`` once applied (``None`` when the report held the change back).
    A deployment-scoped operation carries ``config_revision``, a story-scoped one
    ``timeline_revision``: the revision after the call either way."""

    applied: bool
    impact: ImpactReport
    result: T | None = None
    config_revision: int | None = None
    timeline_revision: int | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""

    def kept(self) -> bool:
        """Whether the call's result is stored under its ``Idempotency-Key``: only once it is
        applied (a report alone writes nothing, so its key is released)."""
        return self.applied


async def two_stage[T](
    report_fn: Callable[[], Awaitable[ImpactReport]],
    apply_fn: Callable[[ImpactReport], Awaitable[T]],
    *,
    ignore_warnings: bool,
) -> TwoStageResult[T]:
    """Build the report; apply the change unless the report warns and the request did not set
    ``ignore_warnings`` (see the module docstring)."""
    report = await report_fn()
    applied = not report.warnings or ignore_warnings
    log.info(
        "impact report",
        applied=applied,
        affected=report.affected_count,
        unavailable=sum(1 for entry in report.affected_stories if entry.upgrade_unavailable),
        warnings=list(report.warnings),
    )
    if not applied:
        return TwoStageResult(applied=False, impact=report)
    return TwoStageResult(applied=True, impact=report, result=await apply_fn(report))


# -- the preview ----------------------------------------------------------------------------------


class ImpactPreviewRequest(BaseModel):
    """``POST /stories/{id}/impact-preview``: the change to preview, a profile to assign
    (``profile_id``), a change of the story's overrides (``overrides``: a JSON merge patch over
    them, as ``PATCH /stories/{id}/profile-overrides`` takes its ``settings``, so ``null``
    removes an override), or both at once. Nothing is written."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "profile_id": "00000000000000000000PRF001",
                    "overrides": {"memory": {"rolling_summary": {"window_messages": 12}}},
                }
            ]
        },
    )

    profile_id: Ulid | None = None
    overrides: SettingsPatch | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.profile_id is None and self.overrides is None:
            raise ValueError("name a profile_id, overrides, or both")
        return self


async def preview(
    db: AsyncEngine,
    story_id: str,
    request: ImpactPreviewRequest,
    *,
    facts: RuntimeFacts,
    purposes: Purposes | None = None,
    safety: SafetyFacts | None = None,
) -> ImpactReport:
    """What assigning ``request.profile_id`` and/or applying ``request.overrides`` would do to
    ``story_id``. ``overrides`` is a JSON merge patch over the story's own overrides, applied as
    the overrides write applies one (#113, ``effective.resolve``): a setting it names replaces
    the story's override of it, ``null`` removes one, and the rest are kept, so the report is the
    one the write would answer. A profile assigned keeps the story's overrides too. 404 for an
    unknown story; 422 ``profile_deleted`` for a profile that cannot be assigned; 422
    ``validation_error`` or ``invalid_profile`` for a patch or a result that cannot run. The
    story's own profile is no reassignment: it keeps the version it runs on. ``safety``: what
    the report's ``budget_warnings`` count (#456)."""
    async with db.connect() as conn:
        current = await effective.effective_configuration(conn, story_id, validate=False)
        if request.profile_id is not None and request.profile_id != current.profile_id:
            target = await effective.assignable(conn, request.profile_id)
            profile_id, version, document = target.profile_id, target.version, target.document
        else:
            profile_id, version, document = current.profile_id, current.version, current.document
        settings = effective.resolve(current, document, request.overrides, purposes=purposes)
        change = delta(current.configuration(), Configuration(profile_id, version, settings), facts)
        history = await with_history(conn, [story_id]) if change.increases else set()
        sizes = await transcripts(conn, history)
    entry = affected(
        story_id, change, history=story_id in history, transcript=sizes.get(story_id, (0, 0))
    )
    return single_report(entry, change, budget_warnings=safety_warnings(settings, safety))


__all__ = [
    "AFFECTED_MAX",
    "CAMEL_CASE",
    "WARNING_KINDS",
    "AffectedStory",
    "Backfill",
    "BackfillEstimate",
    "Delta",
    "Following",
    "ImpactPreviewRequest",
    "ImpactReport",
    "Recommendation",
    "TwoStageRequest",
    "TwoStageResult",
    "UnmetRecommendation",
    "affected",
    "creation_report",
    "delta",
    "following_report",
    "preview",
    "single_report",
    "transcripts",
    "two_stage",
    "unmet",
    "with_history",
]
