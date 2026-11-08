"""Manual pruning (FR-STORE-19,32; #247): reclaim the storage a story's inactive records hold.

``POST /stories/{id}/prune`` is a two-stage control operation over the story's **targets**:
each discarded tail (the operations one truncation discarded, still ``discarded``) and each
dormant continuation (the operations one swap or regenerate left ``dormant``), grouped by the
transaction that flipped them. Without ``ignore_warnings`` it answers the impact report and
changes nothing: what would be deleted, the untruncates and swaps that would stop being
possible, what is retained, the targets excluded and why, and a ``promote_before_pruning``
suggestion per dormant continuation (fork it first to keep it). With ``ignore_warnings`` one
control operation, which bumps the timeline revision but is not a control action (an in-flight
turn rebases, it is not abandoned, FR-CONC-4):

- flips the targets' operations to the terminal membership ``pruned`` (their rows stay, with
  ``turn_report``/``read_set`` cleared, so outbox rows, outcomes, audit events and feedback keep
  a valid ``operation_id``);
- marks the ratings of the pruned messages (``core.feedback.mark_pruned``);
- deletes every row keyed to those operations in ``PRUNABLE_TABLES`` (and their subject-index
  rows) and the operations' events except audit and amendment events, redacted events and an
  event an outbox row names as its outcome (the events guard's ``epistrel.allow_prune``
  exemption, migration 0124);
- writes one ``pruning`` marker event (identifiers, ranges and counts only).

**Never deleted:** active records; amendments and a target any amendment part names a record
of (excluded whole, ``amendment_dependency``); outbox rows, outcomes and reconciliation marks;
erasure markers, redacted events and the erasure ledger; audit events; feedback rows. Afterwards
an untruncate or swap that needs a pruned operation is 409 ``pruned``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import Table, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership, OperationKind, OperationStatus, Provenance
from epistrel.core.errors import PruneTargetIneligible, PruneTargetUnknown
from epistrel.core.events import Operation
from epistrel.core.profiles.impact import TwoStageRequest
from epistrel.core.tables import (
    amendment_parts,
    belief_transitions,
    beliefs,
    client_events,
    dependency_edges,
    divergences,
    entities,
    escalations,
    events,
    exposures,
    external_effect_outbox,
    external_values,
    extraction_shadow,
    facts,
    lite_summary_segments,
    memories,
    operations,
    promissory_ledger,
    proposition_changes,
    scheduled_events,
    state_changes,
    state_definitions,
    stories,
    story_images,
    thunks,
    transcript_messages,
    utterances,
)
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

PRUNABLE_TABLES: tuple[Table, ...] = (
    belief_transitions,
    divergences,
    exposures,
    beliefs,
    utterances,
    memories,
    dependency_edges,
    thunks,
    promissory_ledger,
    escalations,
    extraction_shadow,
    facts,
    state_changes,
    scheduled_events,
    # #261 (FR-STORE-32): a pruned operation's story images; their assets fall to the media
    # sweep once no row references them.
    story_images,
    state_definitions,
    lite_summary_segments,
    proposition_changes,
    transcript_messages,
    client_events,
    external_values,
)
"""Tables whose rows of a pruned operation (by ``operation_id``) are deleted, children first."""
PRUNE_RETAINED_TABLES: frozenset[str] = frozenset(
    {
        "operations",
        "external_effect_outbox",
        "outbox_outcomes",
        "reconciliation_marks",
        "message_feedback",
        "feedback_history",
        "erasure_ledger",
        "idempotency_keys",
        "offboarding_jobs",
        "rating_check_jobs",
        "amendments",
        "rewrite_sessions",
        "directives",
        "genesis_deferred",
        "capability_coverage",
        "story_backfills",
        "turn_queue",
        "stories",
        "entities",
        "propositions",
        "state_proposals",
        "scene_state",
        "events",
    }
)
"""Tables with an operation reference whose rows pruning keeps (``events`` by its own rules)."""
GUARDED = frozenset(
    {
        "facts",
        "memories",
        "beliefs",
        "belief_transitions",
        "exposures",
        "divergences",
        "utterances",
        "state_changes",
    }
)
"""Append-only tables whose guard admits the delete under the story-delete setting."""

Kind = Literal["discarded_tail", "dormant_continuation"]


class PruneRequest(TwoStageRequest):
    """``POST /stories/{id}/prune``: the targets (ids from the report; omitted: every eligible
    one) and ``ignore_warnings`` (``ignoreWarnings``) to apply."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"ignore_warnings": True}]}
    )

    targets: list[StrictStr] | None = Field(default=None, max_length=200)


class Position(BaseModel):
    """A turn and its variant."""

    turn: int
    variant: int


class PruneTarget(BaseModel):
    """One target: its id, kind, the range it spans and how many operations it holds."""

    id: str
    kind: Kind
    from_: Position = Field(serialization_alias="from")
    to: Position
    operations: int
    flipped_at: datetime | None = None


class Excluded(BaseModel):
    """A target left out and why (``amendment_ids``: the amendments that name its records;
    ``settling_change_ids``: #702, the active changes that settle one of its ``state_changes``
    rows, #218)."""

    target: str
    reason: Literal["amendment_dependency", "settlement_dependency", "in_flight"]
    amendment_ids: list[str] = Field(default_factory=list)
    settling_change_ids: list[str] = Field(default_factory=list)


class PruneImpact(BaseModel):
    """The report (see the module docstring). #272: ``story_images`` is how many story image
    rows the chosen targets hold, ``media_assets_purgeable`` how many of their assets nothing
    else would reference afterwards (the next media sweep purges them, #105's 24 h rule for a
    young asset unchanged)."""

    to_delete: dict[str, list[PruneTarget]]
    lost_operations: list[dict[str, Any]]
    retained: dict[str, int]
    excluded: list[Excluded]
    suggestions: list[dict[str, Any]]
    story_images: int = 0
    media_assets_purgeable: int = 0


class PruneResult(BaseModel):
    """An applied pruning: its operation (``prune_id``), the revision after it, and what it
    deleted by count (#272: ``deleted.story_images``, the story image rows)."""

    operation_id: str
    prune_id: str
    timeline_revision: int
    deleted: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PruneOutcome:
    """The answer: ``applied``, the report, and the result once applied."""

    applied: bool
    impact: PruneImpact
    result: PruneResult | None = None
    timeline_revision: int = 0
    replayed: bool = False


@dataclass
class _Target:
    id: str
    kind: Kind
    operation_ids: list[str]
    first: tuple[int, int]
    last: tuple[int, int]
    flipped_at: datetime | None
    excluded: Excluded | None = None

    def view(self) -> PruneTarget:
        return PruneTarget(
            id=self.id,
            kind=self.kind,
            from_=Position(turn=self.first[0], variant=self.first[1]),
            to=Position(turn=self.last[0], variant=self.last[1]),
            operations=len(self.operation_ids),
            flipped_at=self.flipped_at,
        )


@dataclass
class _Plan:
    targets: list[_Target] = field(default_factory=list)
    retained: dict[str, int] = field(default_factory=dict)


# -- planning -------------------------------------------------------------------------------------


async def _targets(
    conn: AsyncConnection, story_id: str, older_than: datetime | None = None
) -> list[_Target]:
    o = operations.c
    aged = () if older_than is None else (o.membership_tx < older_than,)
    rows = list(
        await conn.execute(
            select(
                o.operation_id,
                o.membership,
                o.membership_tx,
                o.status,
                o.narrative_turn,
                o.turn_variant,
            )
            .where(
                o.story_id == story_id,
                o.membership.in_([Membership.DISCARDED.value, Membership.DORMANT.value]),
                *aged,
            )
            .order_by(o.membership_tx, o.narrative_turn, o.operation_id)
        )
    )
    grouped: dict[tuple[str, Any], list[Any]] = {}
    for row in rows:
        grouped.setdefault((str(row.membership), row.membership_tx), []).append(row)
    found: list[_Target] = []
    for (state, flipped_at), members in grouped.items():
        kind: Kind = (
            "discarded_tail" if state == Membership.DISCARDED.value else "dormant_continuation"
        )
        positioned = [
            (int(r.narrative_turn), int(r.turn_variant or 0))
            for r in members
            if r.narrative_turn is not None
        ] or [(0, 0)]
        ids = sorted(str(r.operation_id) for r in members)
        target = _Target(
            id=f"{kind}:{ids[0]}",
            kind=kind,
            operation_ids=ids,
            first=min(positioned),
            last=max(positioned),
            flipped_at=flipped_at,
        )
        if any(str(r.status) == OperationStatus.IN_FLIGHT.value for r in members):
            target.excluded = Excluded(target=target.id, reason="in_flight")
        found.append(target)
    working = await _image_work(conn, story_id, rows)
    for target in found:
        if target.excluded is None and working & set(target.operation_ids):
            target.excluded = Excluded(target=target.id, reason="in_flight")
    for target in found:
        if target.excluded is None:
            names = await _amendment_names(conn, story_id, target.operation_ids)
            if names:
                target.excluded = Excluded(
                    target=target.id, reason="amendment_dependency", amendment_ids=names
                )
        if target.excluded is None:
            settling = await _settling(conn, story_id, target.operation_ids)
            if settling:
                target.excluded = Excluded(
                    target=target.id,
                    reason="settlement_dependency",
                    settling_change_ids=settling,
                )
    return found


async def _image_work(conn: AsyncConnection, story_id: str, rows: Sequence[Any]) -> set[str]:
    """#272: the operations among ``rows`` (discarded or dormant) a story image job that has
    not ended works on: its position's turn variant (an automatic job's turn), so a target
    holding one is excluded ``in_flight``."""
    from epistrel.core.tables import image_jobs
    from epistrel.core.tables.images import TERMINAL_STATUSES, ImageJobKind

    by_position = {
        (int(r.narrative_turn), int(r.turn_variant or 0)): str(r.operation_id)
        for r in rows
        if r.narrative_turn is not None
    }
    ids = {str(r.operation_id) for r in rows}
    if not ids:
        return set()
    working: set[str] = set()
    for (origin,) in await conn.execute(
        select(image_jobs.c.origin).where(
            image_jobs.c.story_id == story_id,
            image_jobs.c.kind == ImageJobKind.STORY.value,
            image_jobs.c.status.not_in([s.value for s in TERMINAL_STATUSES]),
        )
    ):
        origin = dict(origin or {})
        auto = origin.get("auto")
        if isinstance(auto, dict) and str(auto.get("operation_id")) in ids:
            working.add(str(auto["operation_id"]))
        turn, variant = origin.get("turn"), origin.get("variant")
        if turn is not None and variant is not None:
            hit = by_position.get((int(turn), int(variant)))
            if hit is not None:
                working.add(hit)
    return working


async def _image_impact(conn: AsyncConnection, ids: Sequence[str]) -> tuple[int, int]:
    """#272: the story image rows of ``ids`` and how many of their assets nothing else would
    reference once they are gone."""
    from epistrel.core.media.assets import referenced

    if not ids:
        return 0, 0
    s = story_images.c
    doomed = list(await conn.execute(select(s.id, s.asset_id).where(s.operation_id.in_(list(ids)))))
    if not doomed:
        return 0, 0
    assets = sorted({str(r.asset_id) for r in doomed})
    kept = {
        str(a)
        for a in await conn.scalars(
            select(s.asset_id).where(s.asset_id.in_(assets), s.operation_id.not_in(list(ids)))
        )
    }
    kept |= await referenced(conn, assets, skip={"public.story_images"})
    return len(doomed), len(set(assets) - kept)


async def _settling(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> list[str]:
    """#702 (#218): the active ``state_changes`` rows that settle a change of
    ``operation_ids`` (``settles_change_id``: a settle-now of a discarded origin); the target
    is kept whole while one does."""
    from epistrel.core.membership import active_only

    origin = state_changes.alias("origin")
    found = await conn.scalars(
        select(state_changes.c.id)
        .join(origin, origin.c.id == state_changes.c.settles_change_id)
        .where(
            state_changes.c.story_id == story_id,
            active_only(state_changes),
            origin.c.operation_id.in_(list(operation_ids)),
        )
    )
    return sorted(str(f) for f in found)


async def _amendment_names(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> list[str]:
    """The amendments whose parts name a record of ``operation_ids`` (an event, a fact or a
    memory of theirs); an undo or redo would need it."""
    p = amendment_parts.c
    from sqlalchemy import union_all

    records = union_all(
        select(events.c.id).where(events.c.operation_id.in_(list(operation_ids))),
        select(facts.c.id).where(facts.c.operation_id.in_(list(operation_ids))),
        select(memories.c.id).where(memories.c.operation_id.in_(list(operation_ids))),
    ).subquery()
    found = await conn.scalars(
        select(p.amendment_id)
        .where(
            p.story_id == story_id,
            or_(
                p.record_id.in_(select(records.c.id)),
                p.written_fact_id.in_(select(records.c.id)),
            ),
        )
        .distinct()
    )
    return sorted(str(f) for f in found)


async def _retained(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> dict[str, int]:
    ids = list(operation_ids)
    if not ids:
        return {"audit_events": 0, "outbox_rows": 0, "feedback_marked": 0}
    audit = await conn.scalar(
        select(func.count())
        .select_from(events)
        .where(
            events.c.operation_id.in_(ids),
            or_(events.c.category.in_(["audit", "amendment"]), events.c.redacted_at.is_not(None)),
        )
    )
    outbox = await conn.scalar(
        select(func.count())
        .select_from(external_effect_outbox)
        .where(external_effect_outbox.c.operation_id.in_(ids))
    )
    from epistrel.core import feedback as core_feedback

    feedback = await core_feedback.count_rated(conn, story_id, ids)
    return {
        "audit_events": int(audit or 0),
        "outbox_rows": int(outbox or 0),
        "feedback_marked": int(feedback or 0),
    }


def _chosen(found: Sequence[_Target], wanted: Sequence[str] | None) -> list[_Target]:
    if wanted is None:
        return [t for t in found if t.excluded is None]
    known = {t.id: t for t in found}
    chosen: list[_Target] = []
    for name in wanted:
        target = known.get(name)
        if target is None:
            raise PruneTargetUnknown("the story has no such pruning target", {"target": name})
        if target.excluded is not None:
            raise PruneTargetIneligible(
                "the target cannot be pruned",
                {"target": name, "reason": target.excluded.reason},
            )
        chosen.append(target)
    return chosen


def _impact(
    found: Sequence[_Target],
    chosen: Sequence[_Target],
    retained: dict[str, int],
    story_id: str,
    images: tuple[int, int] = (0, 0),
) -> PruneImpact:
    tails = [t.view() for t in chosen if t.kind == "discarded_tail"]
    dormant = [t.view() for t in chosen if t.kind == "dormant_continuation"]
    lost = [
        {"operation": "untruncate" if t.kind == "discarded_tail" else "swap", "target": t.id}
        for t in chosen
    ] + [{"operation": "promote", "target": t.id} for t in chosen if t.kind != "discarded_tail"]
    suggestions = [
        {
            "code": "promote_before_pruning",
            "target": t.id,
            "request": {
                "method": "POST",
                "path": f"/stories/{story_id}/fork",
                "body": {"variant_chain": {"turn": t.first[0], "variant": t.first[1]}},
            },
        }
        for t in chosen
        if t.kind == "dormant_continuation"
    ]
    return PruneImpact(
        to_delete={"discarded_tails": tails, "dormant_continuations": dormant},
        lost_operations=lost,
        retained=retained,
        excluded=[t.excluded for t in found if t.excluded is not None],
        suggestions=suggestions,
        story_images=images[0],
        media_assets_purgeable=images[1],
    )


# -- the request ----------------------------------------------------------------------------------


async def prune(
    db: AsyncEngine,
    story_id: str,
    request: PruneRequest,
    *,
    principal: str,
    trigger: Literal["manual", "policy"] = "manual",
    policy: dict[str, Any] | None = None,
    older_than: datetime | None = None,
) -> PruneOutcome:
    """``POST /stories/{id}/prune`` (see the module docstring); #248's policy run calls it with
    ``trigger = policy`` and its cut-off (``older_than``)."""
    from epistrel.core.stories import require_writable

    await require_writable(db, story_id)
    async with db.connect() as conn:
        found = await _targets(conn, story_id, older_than)
        chosen = _chosen(found, request.targets)
        ids = [i for t in chosen for i in t.operation_ids]
        retained = await _retained(conn, story_id, ids)
        images = await _image_impact(conn, ids)
        revision = int(
            await conn.scalar(select(stories.c.timeline_revision).where(stories.c.id == story_id))
            or 0
        )
    impact = _impact(found, chosen, retained, story_id, images)
    if not request.ignore_warnings or not chosen:
        return PruneOutcome(False, impact, timeline_revision=revision)
    deleted: dict[str, Any] = {}

    async def plan(conn: AsyncConnection) -> list[str]:
        again = _chosen(await _targets(conn, story_id, older_than), request.targets)
        return sorted(i for t in again for i in t.operation_ids)

    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        control_action=False,
    )
    planned = await op.begin(read=plan)
    if planned != sorted(ids):
        await op.abandon()
        raise PruneTargetIneligible(
            "the targets changed while the pruning was planned; send it again",
            {"reason": "moved"},
        )
    op.append(
        "pruning",
        {
            "prune_id": op.operation_id,
            "trigger": trigger,
            "policy": policy,
            "impact": impact.model_dump(mode="json", by_alias=True),
            "targets": [
                {
                    "kind": t.kind,
                    "id": t.id,
                    "from": {"turn": t.first[0], "variant": t.first[1]},
                    "to": {"turn": t.last[0], "variant": t.last[1]},
                    "operation_ids": t.operation_ids,
                }
                for t in chosen
            ],
            "retained": retained,
        },
    )

    async def step(conn: AsyncConnection) -> None:
        deleted.update(await _apply(conn, story_id, ids))

    op.add_commit_step(step)
    await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - a committed story operation
    result = PruneResult(
        operation_id=op.operation_id,
        prune_id=op.operation_id,
        timeline_revision=int(op.timeline_revision),
        deleted=deleted,
    )
    log.info("pruned", story_id=story_id, operations=len(ids))
    return PruneOutcome(True, impact, result, timeline_revision=int(op.timeline_revision))


async def _apply(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> dict[str, Any]:
    """The deletions of one pruning, in its operation's commit transaction."""
    from epistrel.core import feedback
    from epistrel.core.erasure import index

    o = operations.c
    await conn.execute(
        update(operations)
        .where(o.operation_id.in_(list(ids)))
        .values(
            membership=Membership.PRUNED.value,
            membership_tx=func.transaction_timestamp(),
            turn_report=None,
            read_set=None,
        )
    )
    messages = list(
        await conn.scalars(
            select(events.c.id).where(
                events.c.operation_id.in_(list(ids)), events.c.type == "message_rendered"
            )
        )
    )
    await feedback.mark_pruned(conn, story_id, [str(m) for m in messages])
    rows: dict[str, int] = {}
    keys = await _state_keys(conn, ids)
    propositions_named = await _propositions_named(conn, ids)
    minted = [
        str(e)
        for e in await conn.scalars(
            select(entities.c.id).where(
                entities.c.story_id == story_id, entities.c.created_by_operation_id.in_(list(ids))
            )
        )
    ]
    # #702: the guards' own prune exemption (migration 0126), not the story-delete setting.
    await conn.execute(text("SELECT set_config('epistrel.allow_prune', :s, true)"), {"s": story_id})
    try:
        for table in PRUNABLE_TABLES:
            key = table.c.operation_id
            doomed = list(await conn.scalars(select(table.c[_pk(table)]).where(key.in_(list(ids)))))
            if not doomed:
                continue
            if table is story_images:
                await _drop_image_verdicts(conn, story_id, [str(d) for d in doomed])
            pk = table.c[_pk(table)]
            for start in range(0, len(doomed), CHUNK):
                # #702: deleted in chunks of ``CHUNK`` rows, all in the one transaction.
                chunk = doomed[start : start + CHUNK]
                await conn.execute(delete(table).where(pk.in_(chunk)))
                await index.drop_many(conn, table.name, [str(d) for d in chunk])
            rows[table.name] = len(doomed)
    finally:
        await conn.execute(text("SELECT set_config('epistrel.allow_prune', '', true)"))
    if keys:
        from epistrel.core import states

        # #702: the projection of the touched State keys refolded from what remains.
        await states.refresh(conn, story_id, keys)
    rows.update(await _orphans(conn, story_id, propositions_named, minted))
    kept_outcomes = select(external_effect_outbox.c.outcome_event_id).where(
        external_effect_outbox.c.outcome_event_id.is_not(None)
    )
    doomed_events = list(
        await conn.scalars(
            select(events.c.id).where(
                events.c.operation_id.in_(list(ids)),
                events.c.category.not_in(["audit", "amendment"]),
                events.c.redacted_at.is_(None),
                events.c.id.not_in(kept_outcomes),
            )
        )
    )
    await conn.execute(text("SELECT set_config('epistrel.allow_prune', :s, true)"), {"s": story_id})
    try:
        if doomed_events:
            await conn.execute(delete(events).where(events.c.id.in_(doomed_events)))
    finally:
        await conn.execute(text("SELECT set_config('epistrel.allow_prune', '', true)"))
    await index.drop_many(conn, "events", [str(e) for e in doomed_events])
    await index.drop_many(conn, "operations", list(ids))
    trimmed = await _trim_proposals(conn, story_id, {str(e) for e in doomed_events})
    if trimmed:
        rows["state_proposals"] = trimmed
    return {
        "operations": len(ids),
        "events": len(doomed_events),
        "story_images": rows.get(story_images.name, 0),
        "rows_by_table": rows,
    }


async def _drop_image_verdicts(
    conn: AsyncConnection, story_id: str, image_ids: Sequence[str]
) -> None:
    """#272: the stored rating-check verdicts of pruned story images (their prompt's and their
    output's, ``core.ratings.change``'s record keys)."""
    from epistrel.core.ratings.change import IMAGE_KEY, OUTPUT_SUFFIX
    from epistrel.core.tables import guardrail_check_results

    keys = [f"{IMAGE_KEY}{i}" for i in image_ids]
    keys += [k + OUTPUT_SUFFIX for k in keys]
    g = guardrail_check_results.c
    for start in range(0, len(keys), CHUNK):
        await conn.execute(
            delete(guardrail_check_results).where(
                g.story_id == story_id, g.record_key.in_(keys[start : start + CHUNK])
            )
        )


CHUNK = 5_000
"""#702: the most rows one delete statement of a pruning removes."""


async def _state_keys(conn: AsyncConnection, ids: Sequence[str]) -> list[tuple[str, str | None]]:
    """The ``(state_id, scope_key)`` keys the pruned operations' State changes touch."""
    found = await conn.execute(
        select(state_changes.c.state_id, state_changes.c.scope_key)
        .where(state_changes.c.operation_id.in_(list(ids)))
        .distinct()
    )
    return [(str(r.state_id), None if r.scope_key is None else str(r.scope_key)) for r in found]


PROPOSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("facts", "proposition_id"),
    ("beliefs", "proposition_id"),
    ("exposures", "proposition_id"),
    ("promissory_ledger", "proposition_id"),
    ("proposition_changes", "proposition_id"),
    ("scheduled_events", "proposition_id"),
    ("divergences", "asserted_proposition_id"),
    ("divergences", "believed_proposition_id"),
    ("amendment_parts", "proposition_id"),
    ("belief_state", "proposition_id"),
    ("nested_belief_state", "proposition_id"),
)
"""#702: every column that names a proposition (``memories.proposition_ids`` and
``utterances.proposition_ids`` are arrays, read apart)."""


async def _propositions_named(conn: AsyncConnection, ids: Sequence[str]) -> set[str]:
    """The propositions the pruned operations' rows name (candidates for the orphan sweep)."""
    from epistrel.core.tables import metadata

    found: set[str] = set()
    for table_name, column in PROPOSITION_COLUMNS:
        table = metadata.tables[table_name]
        if "operation_id" not in table.c:
            continue
        found.update(
            str(p)
            for p in await conn.scalars(
                select(table.c[column]).where(
                    table.c.operation_id.in_(list(ids)), table.c[column].is_not(None)
                )
            )
        )
    return found


async def _orphans(
    conn: AsyncConnection, story_id: str, candidates: set[str], minted: Sequence[str]
) -> dict[str, int]:
    """#702: the propositions only the pruned rows named, and the entities (with their
    aliases) only the pruned turns minted, that nothing retained references, deleted."""

    from epistrel.core.tables import entity_aliases, metadata, propositions

    deleted: dict[str, int] = {}
    referenced: set[str] = set()
    if candidates:
        wanted = sorted(candidates)
        for table_name, column in PROPOSITION_COLUMNS:
            table = metadata.tables[table_name]
            referenced.update(
                str(p)
                for p in await conn.scalars(
                    select(table.c[column]).where(table.c[column].in_(wanted))
                )
            )
        for table_name in ("memories", "utterances"):
            table = metadata.tables[table_name]
            referenced.update(
                str(p)
                for p in await conn.scalars(
                    select(func.unnest(table.c.proposition_ids)).where(
                        table.c.story_id == story_id,
                        table.c.proposition_ids.overlap(wanted),
                    )
                )
            )
    orphan_props = sorted(candidates - referenced)
    kept_entities: set[str] = set()
    if minted:
        p = propositions.c
        kept_entities.update(
            str(x)
            for x in await conn.scalars(
                select(p.subject_id).where(
                    p.subject_id.in_(list(minted)), p.id.not_in(orphan_props)
                )
            )
        )
        kept_entities.update(
            str(x)
            for x in await conn.scalars(
                select(p.object_id).where(p.object_id.in_(list(minted)), p.id.not_in(orphan_props))
            )
        )
        for table_name, column in (
            ("state_definitions", "entity_id"),
            ("scheduled_events", "place_id"),
            ("placement", "entity_id"),
            ("placement", "location_id"),
            ("placement", "container_id"),
            ("relationships", "subject_id"),
            ("relationships", "object_id"),
            ("scene_state", "location_id"),
            ("thunks", "subject_id"),
            ("thunks", "object_id"),
            ("dependency_edges", "target_id"),
            ("entities", "merged_into"),
        ):
            table = metadata.tables[table_name]
            kept_entities.update(
                str(x)
                for x in await conn.scalars(
                    select(table.c[column]).where(table.c[column].in_(list(minted)))
                )
            )
        kept_entities.update(
            str(x)
            for x in await conn.scalars(
                select(entities.c.id).where(
                    entities.c.id.in_(list(minted)), entities.c.character_id.is_not(None)
                )
            )
        )
    orphan_entities = sorted(set(minted) - kept_entities)
    await conn.execute(
        text("SELECT set_config('epistrel.allow_story_delete', :s, true)"), {"s": story_id}
    )
    try:
        if orphan_props:
            await conn.execute(delete(propositions).where(propositions.c.id.in_(orphan_props)))
            deleted["propositions"] = len(orphan_props)
        if orphan_entities:
            await conn.execute(
                delete(entity_aliases).where(entity_aliases.c.entity_id.in_(orphan_entities))
            )
            await conn.execute(delete(entities).where(entities.c.id.in_(orphan_entities)))
            deleted["entities"] = len(orphan_entities)
    finally:
        await conn.execute(text("SELECT set_config('epistrel.allow_story_delete', '', true)"))
    return deleted


async def _trim_proposals(conn: AsyncConnection, story_id: str, gone: set[str]) -> int:
    """#702: a State proposal's evidence naming a deleted event is dropped; a pending proposal
    left with none is deleted. How many proposals changed."""
    from epistrel.core.tables import state_proposals

    if not gone:
        return 0
    sp = state_proposals.c
    changed = 0
    for row in await conn.execute(
        select(sp.id, sp.evidence, sp.status).where(sp.story_id == story_id)
    ):
        evidence = list(row.evidence or [])
        kept = [e for e in evidence if str(dict(e).get("event_id")) not in gone]
        if len(kept) == len(evidence):
            continue
        changed += 1
        if not kept and str(getattr(row.status, "value", row.status)) == "proposed":
            await conn.execute(delete(state_proposals).where(sp.id == row.id))
        else:
            await conn.execute(update(state_proposals).where(sp.id == row.id).values(evidence=kept))
    return changed


# -- the policy and the history (#248) ------------------------------------------------------------


class PruneRun(BaseModel):
    """One pruning of the story, manual or by its policy (``GET /stories/{id}/prune-runs``)."""

    prune_id: str
    trigger: str
    policy: dict[str, Any] | None
    principal: str
    at: datetime
    timeline_revision: int | None
    impact: dict[str, Any] | None


class PruneRuns(BaseModel):
    """Every pruning of the story, newest first."""

    prune_runs: list[PruneRun]


async def list_prune_runs(db: AsyncEngine, story_id: str) -> PruneRuns:
    """Every ``pruning`` marker of the story, newest first; 404 for an unknown story."""
    from epistrel.core.errors import StoryNotFound

    e, o = events.c, operations.c
    async with db.connect() as conn:
        if await conn.scalar(select(stories.c.id).where(stories.c.id == story_id)) is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        rows = await conn.execute(
            select(e.payload, e.tx_time, o.principal, o.timeline_revision)
            .join(operations, o.operation_id == e.operation_id)
            .where(e.story_id == story_id, e.type == "pruning")
            .order_by(e.seq.desc())
        )
        runs = [
            PruneRun(
                prune_id=str(dict(r.payload).get("prune_id")),
                trigger=str(dict(r.payload).get("trigger", "manual")),
                policy=dict(r.payload).get("policy"),
                principal=str(r.principal),
                at=r.tx_time,
                timeline_revision=None if r.timeline_revision is None else int(r.timeline_revision),
                impact=dict(r.payload).get("impact"),
            )
            for r in rows
        ]
    return PruneRuns(prune_runs=runs)


async def run_policy(db: AsyncEngine, now: datetime) -> dict[str, int]:
    """#248: one pruning run over every story whose ``prune_policy`` is set (and that is
    active: a paused, archived or blocked story waits for the next run), each pruning its
    targets older than its policy, as ``system``. Idempotent: a pruned target is no longer
    eligible."""
    from datetime import timedelta

    from epistrel.core.context import SYSTEM_PRINCIPAL
    from epistrel.core.enums import StoryStatus

    async with db.connect() as conn:
        found = list(
            await conn.execute(
                select(stories.c.id, stories.c.settings).where(
                    # #703: the partial index ``ix_stories_prune_policy``'s predicate.
                    stories.c.settings["prune_policy"].isnot(None),
                    func.jsonb_typeof(stories.c.settings["prune_policy"]) == "object",
                    stories.c.status == StoryStatus.ACTIVE.value,
                )
            )
        )
    pruned = stories_seen = 0
    for row in found:
        policy = dict(row.settings or {}).get("prune_policy")
        if not isinstance(policy, dict) or not policy.get("older_than_days"):
            continue
        stories_seen += 1
        days = int(policy["older_than_days"])
        try:
            outcome = await prune(
                db,
                str(row.id),
                PruneRequest(ignore_warnings=True),
                principal=SYSTEM_PRINCIPAL,
                trigger="policy",
                policy={"older_than_days": days},
                older_than=now - timedelta(days=days),
            )
        except Exception as exc:  # one story's failure never stops the run
            log.warning("policy pruning failed", story_id=str(row.id), error=type(exc).__name__)
            continue
        pruned += int(outcome.applied)
    return {"stories": stories_seen, "pruned": pruned}


def _pk(table: Table) -> str:
    return next(iter(table.primary_key.columns)).name


__all__ = [
    "PRUNABLE_TABLES",
    "PRUNE_RETAINED_TABLES",
    "PruneImpact",
    "PruneOutcome",
    "PruneRequest",
    "PruneResult",
    "PruneRun",
    "PruneRuns",
    "PruneTarget",
    "list_prune_runs",
    "prune",
    "run_policy",
]
