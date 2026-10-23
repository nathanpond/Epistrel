"""U-15 (M5): the contradiction rules over a belief holder (docs/04 §3; FR-PROP-2; #159).

``test_u_15__belief_holder`` re-runs every pure fixture of U-15's M4 part (``tests/test_u_15.py``:
coverage, granularity, polarity and the functional key, the inverse's cardinality, key scoping,
closed sets and cardinality, exclusion and implication, ADR-160's forward implication, closed sets
over a key set and the rule-free cross-predicate conflict, weak predicates, approximate bounds and
holders) with each canon record recast as a belief of one character, and asserts the same verdict
every time. Then ``check_with`` over a real story: ``belief(A)``'s record set is A's partition plus
the canon A may read, the belief wins, and the closed sets (objective canon) enter it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import BELIEF_PREFIX, KeyedLookup, StructuredReadSet, touched_keys
from epistrel.core.tables import stories
from epistrel.core.verify import rules
from epistrel.core.verify.rules import CANON, Holder, Record, Verdict, VerdictKind
from epistrel.core.visibility import Visibility
from tests import canon_support as c
from tests import partition_support as ps
from tests import test_u_15 as m4
from tests import verify_support as vs
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration

SIBLING_OF = CORE_BY_NAME["sibling_of"].id

PURE_FIXTURES: tuple[Callable[[], None], ...] = (
    m4.test_u_15__coverage,
    m4.test_u_15__granularity,
    m4.test_u_15__polarity_and_functional,
    m4.test_u_15__inverse_cardinality,
    m4.test_u_15__key_scoping,
    m4.test_u_15__closed_sets_and_cardinality,
    m4.test_u_15__exclusion_and_implication,
    m4.test_u_15__forward_implication,
    m4.test_u_15__key_set_closed_sets,
    m4.test_u_15__cross_predicate,
    m4.test_u_15__weak_predicate,
    m4.test_u_15__approximate_bounds,
    m4.test_u_15__holders,
)
"""Every pure verdict fixture of U-15's M4 part (the registry, resolution-time and
normalization slices check what the rules stand on, not a verdict over records)."""


def _as_beliefs(records: Sequence[Record]) -> list[Record]:
    """Each record recast as a belief of its holder: the same proposition, no fact."""
    return [
        Record(r.proposition, r.proposition_id, belief_id=f"b{r.fact_id or r.proposition_id}")
        for r in records
    ]


def _shape(found: Verdict) -> tuple[Any, ...]:
    """A verdict as the rules decide it, whatever holder its records belong to."""
    return (
        found.kind,
        found.reason,
        found.rules,
        found.depth_capped,
        found.covered,
        found.uncovered,
        found.cited(),
        found.confidence,
        tuple(_shape(v) for v in found.remainder_verdicts),
    )


@pytest.mark.part("M5", "FR-PROP-2")
async def test_u_15__belief_holder(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every U-15 (M4) fixture gives the same verdict over a belief partition as over canon,
    citing the belief instead of the fact. Over a story (Bobby is in the kitchen in canon; Ann
    believes he is not; canon says Bobby knows only Ann; canon tells Bobby alone that Ann is at
    school): for ``belief(Ann)`` her disbelief is supported and the kitchen is contradicted by
    her belief, not by canon (FR-EPI-3); Bobby knowing Cal is contradicted by
    the closed set, until Ann believes he does; the public sibling fact is known to her, the
    fact scoped to Bobby is not. Complements: a proposition in Ann's partition is ``novel`` for
    ``belief(Bobby)``; read at a position before her belief, it is not her truth; the read set's
    belief keys are touched by a change of her beliefs, never by Bobby's."""
    calls: list[tuple[str, Any]] = []
    real = rules.check

    def spy(claim: Proposition, records: Sequence[Record] = (), **kw: Any) -> Verdict:
        found = real(claim, records, **kw)
        if kw.get("holder", CANON) == CANON:
            held = real(claim, _as_beliefs(records), **{**kw, "holder": Holder.belief(vs.ANN)})
            assert _shape(held) == _shape(found), (claim, found, held)
            assert held.holder == Holder.belief(vs.ANN)
            for e in held.evidence:
                assert e.fact_id is None
                assert (e.table == "beliefs") == (e.belief_id is not None)
            calls.append((found.kind.value, found.reason))
        return found

    monkeypatch.setattr(m4, "check", spy)
    for fixture in PURE_FIXTURES:
        fixture()
    kinds = {kind for kind, _ in calls}
    assert kinds == {"supported", "contradicted", "novel", "uncertain"}
    assert len(calls) > 80

    # -- over a story: the partition plus the canon the holder may read ------------------------
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    people, places = w.people, w.places
    await c.commit(engine, story, c.at(people["Bobby"], places["kitchen"], 0, None))
    sibling = Proposition(
        subject_id=people["Bobby"],
        predicate_id=SIBLING_OF,
        object_id=people["Ann"],
        valid_time=Interval.of(None, None),
    )
    await c.commit(engine, story, sibling)
    await c.commit(
        engine,
        story,
        c.at(people["Ann"], places["school"], 0, None),
        visibility=Visibility(scope="characters", characters=(bobby,)),
    )
    async with engine.begin() as conn:
        reg = await registry.load(conn, story)
        await store.declare_closed_set(
            conn,
            story,
            store.ClosedSet(c.KNOWS, people["Bobby"], (people["Ann"],)),
            reg,
            asserted_by="author",
        )
    # Ann's false belief: Bobby is not in the kitchen (a disbelief is the negated proposition).
    elsewhere = await ps.believe(
        engine, story, ann, c.at(people["Bobby"], places["kitchen"], 0, None, negated=True)
    )
    cal_school = await ps.believe(
        engine, story, ann, c.at(people["Cal"], places["school"], 0, None)
    )

    async def verdict(
        claim: Proposition, holder: Holder, **kw: Any
    ) -> tuple[Verdict, StructuredReadSet]:
        source = StructuredReadSet()
        async with engine.connect() as conn:
            found = await rules.check_with(
                KeyedLookup(conn, source), story, claim, holder=holder, **kw
            )
        return found, source

    as_ann, as_bobby = Holder.belief(ann), Holder.belief(bobby)
    not_in_kitchen = c.at(people["Bobby"], places["kitchen"], 10, 20, negated=True)
    in_kitchen = c.at(people["Bobby"], places["kitchen"], 10, 20)

    found, source = await verdict(not_in_kitchen, as_ann)
    assert found.kind is VerdictKind.SUPPORTED
    assert [(e.table, e.record_id) for e in found.evidence] == [("beliefs", elsewhere.id)]
    assert {k.holder for k in source.query_keys} == {f"{BELIEF_PREFIX}{ann}"}
    # Canon says otherwise, for canon (FR-EPI-3: the objective relation is canon's).
    assert (await verdict(not_in_kitchen, CANON))[0].kind is VerdictKind.CONTRADICTED
    found, _ = await verdict(in_kitchen, as_ann)
    assert found.kind is VerdictKind.CONTRADICTED
    assert rules.Rule.POLARITY in found.rules
    assert {e.table for e in found.evidence} == {"beliefs"}
    assert (await verdict(in_kitchen, CANON))[0].kind is VerdictKind.SUPPORTED

    knows_cal = Proposition(
        subject_id=people["Bobby"],
        predicate_id=c.KNOWS,
        object_id=people["Cal"],
        valid_time=Interval.of(10, 20),
    )
    found, _ = await verdict(knows_cal, as_ann)
    assert found.kind is VerdictKind.CONTRADICTED
    assert rules.Rule.CLOSED_SET in found.rules
    # The public sibling fact is known to Ann; the fact canon tells Bobby alone is not.
    found, source = await verdict(sibling, as_ann)
    assert found.kind is VerdictKind.SUPPORTED
    assert {e.table for e in found.evidence} == {"facts"}
    assert {k.holder for k in source.query_keys} == {f"{BELIEF_PREFIX}{ann}", "canon"}
    at_school = c.at(people["Ann"], places["school"], 10, 20)
    assert (await verdict(at_school, as_ann))[0].kind is VerdictKind.NOVEL
    assert (await verdict(at_school, as_bobby))[0].kind is VerdictKind.SUPPORTED

    # Complement: a proposition in Ann's partition is novel for Bobby's.
    cal_claim = c.at(people["Cal"], places["school"], 10, 20)
    assert (await verdict(cal_claim, as_ann))[0].kind is VerdictKind.SUPPORTED
    assert (await verdict(cal_claim, as_bobby))[0].kind is VerdictKind.NOVEL
    # Read before she came to disbelieve it, the kitchen is not hers to deny.
    before = (elsewhere.held_from.turn - 1, 0)
    found, _ = await verdict(not_in_kitchen, as_ann, through_position=before)
    assert found.kind is VerdictKind.CONTRADICTED
    assert {e.table for e in found.evidence} == {"facts"}

    # The read set: Ann's belief keys are touched by a change of her beliefs, not Bobby's.
    _, source = await verdict(knows_cal, as_ann)
    async with engine.connect() as conn:
        base = int(
            await conn.scalar(select(stories.c.timeline_revision).where(stories.c.id == story)) or 0
        )
    await ps.believe(
        engine,
        story,
        bobby,
        Proposition(
            subject_id=people["Bobby"],
            predicate_id=c.KNOWS,
            object_id=people["Cal"],
            valid_time=Interval.of(None, None),
        ),
    )
    async with engine.connect() as conn:
        assert await touched_keys(conn, story, base, source.query_keys) == []
    await ps.believe(
        engine,
        story,
        ann,
        Proposition(
            subject_id=people["Bobby"],
            predicate_id=c.KNOWS,
            object_id=people["Cal"],
            valid_time=Interval.of(None, None),
        ),
    )
    async with engine.connect() as conn:
        assert await touched_keys(conn, story, base, source.query_keys) != []
    # Her contrary belief wins over the closed set, as over any canon record.
    found, _ = await verdict(knows_cal, as_ann)
    assert found.kind is VerdictKind.SUPPORTED
    assert {e.table for e in found.evidence} == {"beliefs"}
    assert cal_school.holder_character_id == ann
