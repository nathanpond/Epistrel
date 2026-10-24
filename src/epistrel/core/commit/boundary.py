"""The commit boundary is the regeneration boundary (FR-COMMIT-8,9, NFR-DETERM-2, FR-STORE-29;
docs/02 §4.4, §4.6; #154).

**Revert.** A fact commits in the message where a dependence first forces it
(``committed_at_position``, #146): it is open to that message's own regeneration and sticky to
every later one. ``revert`` runs as a ``core.membership`` follower, right after #130's flip and in
the same transaction, for every membership change of operations: a regenerate retiring the active
variant and a turn shelving a rewound tail (``core.events.operation``), and a truncate, an
untruncate or a swap (``core.timeline``'s first in-transaction hook, so before #137's projections
and #143's consolidation read). When the changed operations leave the timeline, every fact
``committed`` at one of their positions (``committed_at_position``'s ``(turn, variant)``,
whichever operation wrote the fact) moves to ``revised`` in place; when they come back, every
``revised`` fact at those positions moves back to ``committed`` (the symmetric swap-back). Facts
committed at any other position are not touched, so their rows stay byte-identical. The
facts guard (migration 0060) allows exactly those two edges.

Thunks need no write: a thunk follows the operation that introduced it, and its resolution is read
through the resolving operation's membership (``core.commit.lifecycle``), so a thunk the reverted
message resolved is open again, its ledger entries undischarged, and a thunk the message
introduced leaves with it. ``summary`` counts what one regenerated variant reverted and reopened
(``turn_report.commit``'s ``reverted_facts``/``reopened_thunks``).

**Resolution levels** (FR-COMMIT-9). ``facts.resolution_level`` is ``consequential`` or
``incidental`` (``NULL`` reads as ``consequential``). A consequential fact fixes what matters (the
letter is in the drawer, Marcus wrote it) and leaves its incidental detail undetermined (the
handwriting): ``depend_on_detail`` turns that detail into a thunk lazily, on the first ``think``
or ``do`` depending on an attribute of the fact's object, and resolves it once through
``lifecycle.depend`` (and so through ``core.commit.reconcile``), at ``incidental``. A ``say``
only mentions it: nothing is introduced or written. A rewind past the resolving turn discards the
detail's thunk and fact with that turn, so the detail is undetermined again while the
consequential fact, committed earlier, stays. ``detail_parent`` is the detail rule (the owner's
decision on #494): which consequential fact, if any, a ``think``/``do`` claim is a detail of. The
structured turn's commit (``orchestrator.structured.pipeline``) routes each novel claim it names
to ``depend_on_detail`` instead of canonizing it.

Logs carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, and_, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import FactCommitState, Membership, OpenSlot
from epistrel.core.membership import active_only, active_operation, register_follower
from epistrel.core.tables import operations
from epistrel.core.tables.commitments import thunks
from epistrel.core.tables.facts import facts

log = logging.getLogger(__name__)

CONSEQUENTIAL = "consequential"
INCIDENTAL = "incidental"
RESOLUTION_LEVELS: tuple[str, ...] = (CONSEQUENTIAL, INCIDENTAL)
"""``facts.resolution_level``'s values; ``NULL`` reads as ``consequential``."""

TurnVariant = tuple[int, int | None]
"""An operation's ``(narrative_turn, turn_variant)``: which turn variant a revert names (not a
narrative position bound, so not ``core.membership.Position``; #164 renamed the M4 alias)."""


@dataclass(frozen=True, slots=True)
class BoundaryChange:
    """What one revert did: facts moved to ``revised`` (``reverted``) or back to ``committed``
    (``restored``)."""

    reverted: int = 0
    restored: int = 0


async def _positions(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str]
) -> tuple[str | None, list[TurnVariant]]:
    rows = (
        await conn.execute(
            select(
                operations.c.story_id, operations.c.narrative_turn, operations.c.turn_variant
            ).where(
                operations.c.operation_id.in_(list(operation_ids)),
                operations.c.narrative_turn.is_not(None),
            )
        )
    ).all()
    story = story_id or next((str(r.story_id) for r in rows), None)
    found = sorted({(int(r.narrative_turn), r.turn_variant) for r in rows}, key=str)
    return story, found


def _at(positions: Sequence[TurnVariant]) -> Any:
    turn = cast(facts.c.committed_at_position["turn"].astext, Integer)
    variant = cast(facts.c.committed_at_position["variant"].astext, Integer)
    return or_(
        *(
            and_(
                turn == t,
                variant.is_(None) if v is None else variant == v,
            )
            for t, v in positions
        )
    )


async def revert(
    conn: AsyncConnection,
    story_id: str | None,
    operation_ids: Sequence[str],
    to: Membership,
) -> BoundaryChange:
    """The commit boundary for operations ``operation_ids`` whose membership became ``to`` (see
    the module docstring): set-based, one ``UPDATE`` per direction."""
    if not operation_ids:
        return BoundaryChange()
    story, positions = await _positions(conn, story_id, operation_ids)
    if story is None or not positions:
        return BoundaryChange()
    leaving = Membership(to) is not Membership.ACTIVE
    was, now = (
        (FactCommitState.COMMITTED, FactCommitState.REVISED)
        if leaving
        else (FactCommitState.REVISED, FactCommitState.COMMITTED)
    )
    result = await conn.execute(
        update(facts)
        .where(
            facts.c.story_id == story,
            facts.c.commit_state == was.value,
            facts.c.committed_at_position.is_not(None),
            _at(positions),
        )
        .values(commit_state=now.value)
    )
    changed = int(result.rowcount)
    if changed:
        log.debug("commit boundary: %d facts %s in story %s", changed, now.value, story)
    return BoundaryChange(reverted=changed) if leaving else BoundaryChange(restored=changed)


async def _follow(
    conn: AsyncConnection, story_id: str | None, operation_ids: tuple[str, ...], to: Membership
) -> None:
    await revert(conn, story_id, operation_ids, to)


register_follower(_follow)


@dataclass(frozen=True, slots=True)
class Summary:
    """What a regenerated variant's retirement reverted (``turn_report.commit``)."""

    reverted_facts: int
    reopened_thunks: int


async def summary(conn: AsyncConnection, story_id: str, turn: int, variant: int) -> Summary:
    """The facts committed at ``(turn, variant)`` now ``revised``, and the thunks it resolved
    that are open again (still in the timeline, their resolver out of it)."""
    reverted = await conn.scalar(
        select(func.count()).where(
            facts.c.story_id == story_id,
            facts.c.commit_state == FactCommitState.REVISED.value,
            facts.c.committed_at_position.is_not(None),
            _at([(turn, variant)]),
        )
    )
    retired = (
        select(operations.c.operation_id)
        .where(
            operations.c.story_id == story_id,
            operations.c.narrative_turn == turn,
            operations.c.turn_variant == variant,
            ~active_operation(operations),
        )
        .scalar_subquery()
    )
    reopened = await conn.scalar(
        select(func.count()).where(
            thunks.c.story_id == story_id,
            active_only(thunks),
            thunks.c.resolution_operation_id.in_(retired),
        )
    )
    return Summary(int(reverted or 0), int(reopened or 0))


def level_of(value: str | None) -> str:
    """A fact's effective resolution level (``NULL`` is ``consequential``)."""
    return CONSEQUENTIAL if value is None else value


# -- resolution levels: incidental detail ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detail:
    """What ``depend_on_detail`` did: the detail's thunk (``None`` for a mention) and the
    dependence on it (``None`` for a mention)."""

    thunk_id: str | None
    dependence: Any = None


async def depend_on_detail(
    conn: AsyncConnection,
    operation: Any,
    parent_fact_id: str,
    claim: Any,
    *,
    beat: int | None,
    channel: str,
    registry: Any = None,
) -> Detail:
    """A beat's ``claim`` about an attribute of consequential fact ``parent_fact_id``'s object
    (FR-COMMIT-9): a ``say`` mentions it (nothing happens); a ``think``/``do`` introduces the
    detail's thunk (subject the object, the attribute open; once: the open one is found
    again) at the parent fact's valid-time, linked to the parent (``parent_fact_id`` in the
    sketch), and depends on it, so it resolves once, through reconciliation, at
    ``incidental``."""
    from epistrel.core.canon import facts as canon
    from epistrel.core.commit import lifecycle
    from epistrel.core.commit.sketch import ThunkSketch

    if channel not in lifecycle.DEPENDENCE_CHANNELS:
        return Detail(None)
    story_id = str(operation.story_id)
    parent = await canon.read_version(conn, story_id, parent_fact_id)
    owner = parent.proposition.object_id or parent.proposition.subject_id
    if claim.subject_id != owner:
        raise ValueError("a detail's claim is about the consequential fact's object")
    sketch = ThunkSketch(
        predicate_id=claim.predicate_id,
        subject_id=owner,
        object_id=None,
        open_slot=OpenSlot.LITERAL if claim.literal is not None else OpenSlot.OBJECT,
        parent_fact_id=parent_fact_id,
    )
    valid_time = parent.valid_from if parent.valid_from is not None else None
    found = await lifecycle.find_open(conn, story_id, sketch)
    if found is None:
        resolved = await _resolved_detail(conn, story_id, sketch)
        if resolved is not None:
            found = resolved
        else:
            found = await lifecycle.introduce(
                conn, operation, sketch, valid_time=valid_time, beat=beat, on=True
            )
    if found is None:  # pragma: no cover - commitment on above
        return Detail(None)
    dependence = await lifecycle.depend(
        conn,
        operation,
        found.id,
        claim,
        beat=beat,
        channel=channel,  # type: ignore[arg-type]
        registry=registry,
        on=True,
        resolution_level=INCIDENTAL,
    )
    return Detail(found.id, dependence)


DETAIL_CHANNELS: frozenset[str] = frozenset({"think", "do"})
"""The beats whose claim about an open attribute is a detail's dependence (FR-COMMIT-9: a
character's ``think`` or ``do``); a ``say`` only mentions it."""


async def detail_parent(
    conn: AsyncConnection, story_id: str, claim: Any, *, channel: str
) -> str | None:
    """The detail rule (FR-COMMIT-9; the owner's decision on #494, 2026-10-22): the consequential
    fact whose incidental detail ``claim`` (of a ``channel`` beat) is, or ``None``. A ``think``
    or ``do`` claim about an attribute a committed fact stored at ``consequential`` leaves open
    is a detail of that fact: the claim's subject is the fact's object (its subject when it has
    none, ``depend_on_detail``'s owner), its predicate is not the fact's, and canon holds no
    committed value of that predicate for that subject (a claim against what canon fixed is the
    verifier's contradiction, never a resolution). The oldest such fact when several are; a
    ``say`` is a mention and names none. ``NULL`` levels are ordinary canon, not a parent: only
    a fact written at ``consequential`` defers its detail. The caller hands only claims the
    verifier found novel."""
    from epistrel.core.canon import facts as canon

    subject = getattr(claim, "subject_id", None)
    if channel not in DETAIL_CHANNELS or subject is None:
        return None
    committed = FactCommitState.COMMITTED
    fixed = await canon.canon(conn, story_id, subject=subject, predicate=claim.predicate_id)
    if any(f.commit_state is committed for f in fixed):
        return None
    found = {
        f.id: f
        for f in (
            *await canon.canon(conn, story_id, object=subject),
            *await canon.canon(conn, story_id, subject=subject),
        )
        if f.commit_state is committed
        and f.resolution_level == CONSEQUENTIAL
        and (f.proposition.object_id or f.proposition.subject_id) == subject
        and f.proposition.predicate_id != claim.predicate_id
    }
    return min(found) if found else None


async def _resolved_detail(conn: AsyncConnection, story_id: str, sketch: Any) -> Any:
    """The detail's thunk when it is already resolved in the timeline (so a later claim is
    checked against its value, never a second thunk)."""
    from epistrel.core.commit import lifecycle

    t = thunks.alias("t")
    rows = await conn.execute(
        select(*lifecycle.thunk_columns(t))
        .where(
            t.c.story_id == story_id,
            t.c.predicate_id == sketch.predicate_id,
            t.c.subject_id.is_not_distinct_from(sketch.subject_id),
            t.c.open_slot == sketch.open_slot.value,
            active_only(t),
        )
        .order_by(t.c.id)
    )
    for row in rows:
        found = lifecycle.thunk_of(row)
        if found.state.value == "resolved" and found.sketch.key == sketch.key:
            return found
    return None


__all__ = [
    "CONSEQUENTIAL",
    "DETAIL_CHANNELS",
    "INCIDENTAL",
    "RESOLUTION_LEVELS",
    "BoundaryChange",
    "Detail",
    "Summary",
    "depend_on_detail",
    "detail_parent",
    "level_of",
    "revert",
    "summary",
]
