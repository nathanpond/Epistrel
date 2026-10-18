"""Turn-time commitment lookups (FR-COMMIT-2,3,10; #146): which open thunk a detected claim
depends on, and whether a salient reference is already settled.

A ``core.readset.KEYED_READERS`` member: a turn reads canon here only through ``KeyedLookup``,
and each thunk lookup is recorded as a query key of the turn's read set (``reading = "thunk"``,
over the predicate with any subject: a subject-open thunk is resolved by a fact of whatever
subject the dependence names). The thunk table
itself is no canon, so the key's result is the hash of the open thunks found; a concurrent
turn that resolves one commits a fact of that predicate and subject, which touches the key, so
two turns never resolve one thunk twice (FR-COMMIT-10).

- ``dependence_target(lookup, story_id, claim)``: the open thunk ``claim`` fills (#148's
  detection annotates ``matched_thunk_id`` with it; #150 then calls ``lifecycle.depend``), or
  ``None``. At most ``thunks_max_open`` rows are scanned (the open set is capped).
- ``settled(lookup, story_id, sketch)``: whether canon already holds a value for a salient
  sketch (a committed or hidden fact filling it), in which case the reference creates no
  ledger entry.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import or_, select

from epistrel.core.commit.lifecycle import Thunk, max_open, open_clause, thunk_columns, thunk_of
from epistrel.core.commit.sketch import ThunkSketch, fills
from epistrel.core.enums import OpenSlot
from epistrel.core.propositions.model import Proposition
from epistrel.core.readset import EMPTY_RESULT, KeyedLookup, QueryKey
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables.commitments import thunks

THUNK_READING = "thunk"
"""The ``QueryKey.reading`` of a thunk lookup (apart from every canon key's)."""


def _hash(found: list[Thunk]) -> str:
    pairs = sorted((t.id, t.state.value) for t in found)
    if not pairs:
        return EMPTY_RESULT
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


async def open_thunks(
    lookup: KeyedLookup, story_id: str, predicate_id: str, subject_id: str | None
) -> list[Thunk]:
    """The open thunks of ``predicate_id`` whose subject is ``subject_id`` or open, oldest
    first (at most ``thunks_max_open``); the lookup is a query key of the turn."""
    conn = lookup.conn
    t = thunks.alias("t")
    subject = or_(t.c.open_slot == OpenSlot.SUBJECT.value, t.c.subject_id == subject_id)
    rows = await conn.execute(
        select(*thunk_columns(t))
        .where(
            t.c.story_id == story_id,
            t.c.predicate_id == predicate_id,
            subject,
            open_clause(t),
        )
        .order_by(t.c.id)
        .limit(await max_open(conn))
    )
    found = [thunk_of(r) for r in rows]
    lookup.read_set.record(
        QueryKey(
            # Any subject: a subject-open thunk is resolved by a fact of whatever subject.
            subjects=None,
            predicate_id=predicate_id,
            viewpoint=OMNISCIENT,
            reading=THUNK_READING,
            result_hash=_hash(found),
        )
    )
    return found


async def dependence_target(lookup: KeyedLookup, story_id: str, claim: Proposition) -> Thunk | None:
    """The open thunk ``claim`` fills (the oldest when several do), or ``None``."""
    for thunk in await open_thunks(lookup, story_id, claim.predicate_id, claim.subject_id):
        if fills(thunk.sketch, claim):
            return thunk
    return None


async def settled(
    lookup: KeyedLookup,
    story_id: str,
    sketch: ThunkSketch,
    *,
    through_position: tuple[int, int] | None = None,
) -> bool:
    """Whether a current canon fact (any visibility: a hidden one is decided too) fills
    ``sketch``: the reference is no undetermined value, so it needs no ledger entry."""
    subjects = None if sketch.subject_id is None else sketch.subject_id
    facts = await lookup.canon_at(
        story_id,
        predicate=sketch.predicate_id,
        subjects=subjects,
        object=sketch.object_id if sketch.open_slot is OpenSlot.SUBJECT else None,
        viewpoint=OMNISCIENT,
        through_position=through_position,
    )
    return any(fills(sketch, f.proposition) for f in facts)


__all__ = ["THUNK_READING", "dependence_target", "open_thunks", "settled"]
