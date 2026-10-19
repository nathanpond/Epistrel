"""``core.verify`` over real canon (#140): the keyed fetch, active rows only, the profile gate,
the rebase re-check, auto-registration of a declared claim, and NFR-PERF-2's cost bounds.

- ``check_with`` reads through ``KeyedLookup``. Each fetch is a query key of the turn's read
  set, and an empty result is a key too.
- Only ``membership = active`` facts are evidence (#130). A fact written by a turn that a
  truncation discarded, or that a regeneration shelved (``dormant``), is never cited; the same
  fact is cited again after untruncation.
- ``verify`` is inert (nothing read, no verdict) when the story's profile has verification off
  (Lite). On ``Structured (M4)`` it answers and records the verdict for the rebase re-check,
  which fails once the verdict would change. With ``contradiction_checking`` off, no
  contradiction rule runs.
- NFR-PERF-2 is proved as a deterministic bound, never a wall-clock one (#430, #432): the
  statements and query keys of one ``check_with`` do not grow with the story's canon beyond
  the claim's own subject and predicate (P-02's timing is #153's).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import EMPTY_RESULT, KeyedLookup, StructuredReadSet, touched_keys
from epistrel.core.resolution.context import load_context, load_index, load_time_basis
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.verify import rules
from epistrel.core.verify.normalize import ClaimSketch, Unnormalizable, normalize_declared
from epistrel.core.verify.rules import Holder, Rule, VerdictKind
from tests.canon_support import KNOWS, LOCATED_AT, World, at, commit, world
from tests.memory_support import use_structured
from tests.projection_support import truncate, untruncate

pytestmark = pytest.mark.integration

SAME_AS = CORE_BY_NAME["same_as"].id
LOCATED_IN = CORE_BY_NAME["located_in"].id
HOLDS = CORE_BY_NAME["holds"].id


async def _check(engine: AsyncEngine, w: World, claim: Any, **kw: Any) -> tuple[Any, Any]:
    source = StructuredReadSet()
    async with engine.connect() as conn:
        found = await rules.check_with(KeyedLookup(conn, source), w.story_id, claim, **kw)
    return found, source


async def test_check_with_keys_every_fetch(engine: AsyncEngine) -> None:
    """Every read a verdict rests on is a query key, the empty ones included: the subject's and
    object's ``same_as``, the claim's predicate (windowed to the claim's span) and its
    containment predicate."""
    w = await world(engine)
    bobby, kitchen = w.people["Bobby"], w.places["kitchen"]
    fact = await commit(engine, w.story_id, at(bobby, kitchen, 0, 100), kind=OperationKind.CONTROL)
    verdict, source = await _check(engine, w, at(bobby, kitchen, 10, 20))
    assert verdict.kind is VerdictKind.SUPPORTED
    assert [(e.rule, e.fact_id) for e in verdict.evidence] == [(Rule.COVERAGE, fact.id)]
    keys = source.query_keys
    assert {k.predicate_id for k in keys} == {SAME_AS, LOCATED_AT, LOCATED_IN}
    located = [k for k in keys if k.predicate_id == LOCATED_AT]
    (main,) = located
    assert main.subjects == (bobby,)
    assert main.valid_window == (10, 20)
    assert main.result_hash != EMPTY_RESULT
    assert sum(k.result_hash == EMPTY_RESULT for k in keys) == len(keys) - 1
    assert len(keys) == 4  # same_as twice, located_at, located_in


async def test_an_inverse_cardinality_reads_by_object(engine: AsyncEngine) -> None:
    """#439: ``held_by`` (the inverse of ``holds``) is functional, so Ann holding the key while
    canon says Cal holds it is ``contradicted`` (``functional_key``), citing Cal's fact. The
    item's holders are read by object (any subject, the claim's window), so a later change of
    the key's holder touches the turn's read set. Complement: a change to another item touches
    nothing."""
    w = await world(engine)
    async with engine.begin() as conn:
        key = await store.add_entity(conn, w.story_id, "item", "key")
        ring = await store.add_entity(conn, w.story_id, "item", "ring")
    ann, bobby, cal = w.people["Ann"], w.people["Bobby"], w.people["Cal"]
    fact = await commit(
        engine, w.story_id, at(cal, key, 0, None, predicate=HOLDS), kind=OperationKind.CONTROL
    )
    verdict, source = await _check(engine, w, at(ann, key, 10, 20, predicate=HOLDS))
    assert verdict.kind is VerdictKind.CONTRADICTED
    assert [(e.rule, e.fact_id) for e in verdict.evidence] == [(Rule.FUNCTIONAL_KEY, fact.id)]
    (by_object,) = [k for k in source.query_keys if k.predicate_id == HOLDS and k.subjects is None]
    assert (by_object.object_id, by_object.valid_window) == (key, (10, 20))
    assert by_object.result_hash != EMPTY_RESULT
    async with engine.connect() as conn:
        base = int(
            await conn.scalar(
                text("SELECT timeline_revision FROM stories WHERE id = :s"), {"s": w.story_id}
            )
            or 0
        )
    keys = source.query_keys
    await commit(
        engine, w.story_id, at(bobby, ring, 10, 20, predicate=HOLDS), kind=OperationKind.CONTROL
    )
    async with engine.connect() as conn:
        assert await touched_keys(conn, w.story_id, base, keys) == []
    await commit(
        engine, w.story_id, at(bobby, key, 12, 18, predicate=HOLDS), kind=OperationKind.CONTROL
    )
    async with engine.connect() as conn:
        assert f"query_key:{keys.index(by_object)}" in await touched_keys(
            conn, w.story_id, base, keys
        )


async def test_dormant_facts_are_never_evidence(engine: AsyncEngine) -> None:
    """#130: verification reads only active derived rows. Bobby is in the kitchen by a fact the
    second turn wrote; truncating that turn discards the fact, and the same claim is then
    ``novel``, citing nothing; regenerating that turn later shelves it (``dormant``), with the
    same result. Complement: before the truncation, and again after untruncation, the active
    fact supports the claim; an inactive fact contradicts nothing."""
    w = await world(engine)
    bobby, kitchen, garden = w.people["Bobby"], w.places["kitchen"], w.places["garden"]
    await commit(engine, w.story_id, at(w.people["Ann"], garden, 0, None))
    fact = await commit(engine, w.story_id, at(bobby, kitchen, 0, None))
    claim = at(bobby, kitchen, 10, 20)
    before, _ = await _check(engine, w, claim)
    assert before.kind is VerdictKind.SUPPORTED
    assert fact.id in {e.fact_id for e in before.evidence}
    assert await truncate(engine, w.story_id, 0) is not None
    after, source = await _check(engine, w, claim)
    assert after.kind is VerdictKind.NOVEL
    assert after.evidence == ()
    denied, _ = await _check(engine, w, claim.negation())
    assert denied.kind is VerdictKind.NOVEL  # the dormant fact contradicts nothing
    assert await _membership(engine, fact.id) == "discarded"
    assert all(k.result_hash == EMPTY_RESULT for k in source.query_keys)
    await untruncate(engine, w.story_id)
    again, _ = await _check(engine, w, claim)
    assert again.kind is VerdictKind.SUPPORTED
    assert fact.id in {e.fact_id for e in again.evidence}
    # Regenerating the latest turn shelves its first variant: its fact is dormant, not evidence.
    cal = w.people["Cal"]
    shelf = await commit(engine, w.story_id, at(cal, kitchen, 0, None))
    head = await _head(engine, w.story_id)
    await commit(engine, w.story_id, at(w.people["Ann"], w.places["school"], 0, None), turn=head)
    assert await _membership(engine, shelf.id) == "dormant"
    shelved, _ = await _check(engine, w, at(cal, kitchen, 10, 20))
    assert shelved.kind is VerdictKind.NOVEL
    assert shelf.id not in {e.fact_id for e in shelved.evidence}


async def _head(engine: AsyncEngine, story_id: str) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT next_turn_index FROM stories WHERE id = :s"), {"s": story_id}
        )
    return int(found) - 1


async def _membership(engine: AsyncEngine, fact_id: str) -> str:
    async with engine.connect() as conn:
        return str(
            await conn.scalar(
                text("SELECT membership::text FROM facts WHERE id = :f"), {"f": fact_id}
            )
        )


async def test_verify_follows_the_profile(engine: AsyncEngine) -> None:
    """Under Lite (verification off) ``verify`` is inert: no verdict, no key. On ``Structured
    (M4)`` it answers, records the verdict, and the rebase re-check passes until a change would
    turn it (then it regenerates). With ``contradiction_checking`` off (a story override), the
    same conflict is not ``contradicted``."""
    w = await world(engine)
    bobby, kitchen = w.people["Bobby"], w.places["kitchen"]
    claim = at(bobby, kitchen, 10, 20)
    lite = StructuredReadSet()
    async with engine.connect() as conn:
        assert await rules.verify(conn, lite, w.story_id, claim) is None
    assert lite.query_keys == []
    assert rules.recorded(lite) == []

    await use_structured(engine, w.story_id)
    source = StructuredReadSet()
    source.start_attempt()
    async with engine.connect() as conn:
        found = await rules.verify(conn, source, w.story_id, claim)
    assert found is not None
    assert found.kind is VerdictKind.NOVEL
    assert source.query_keys
    (call,) = rules.recorded(source)
    assert call.kind is VerdictKind.NOVEL
    op = cast(Any, type("Op", (), {"read_set_source": source})())
    async with engine.connect() as conn:
        assert await rules.recheck(conn, op) is True
    await commit(engine, w.story_id, at(bobby, kitchen, 0, None), kind=OperationKind.CONTROL)
    async with engine.connect() as conn:
        assert await rules.recheck(conn, op) is False
    # A new attempt starts with no recorded verdict.
    source.start_attempt()
    assert rules.recorded(source) == []

    async with engine.connect() as conn:
        conflict = await rules.verify(conn, StructuredReadSet(), w.story_id, claim.negation())
    assert conflict is not None
    assert conflict.kind is VerdictKind.CONTRADICTED
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO story_profile_overrides (story_id, settings, revision) VALUES "
                "(:s, CAST(:d AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE SET "
                "settings = EXCLUDED.settings, revision = story_profile_overrides.revision + 1"
            ),
            {"s": w.story_id, "d": '{"verification": {"contradiction_checking": false}}'},
        )
    async with engine.connect() as conn:
        quiet = await rules.verify(conn, StructuredReadSet(), w.story_id, claim.negation())
    assert quiet is not None
    assert quiet.kind is not VerdictKind.CONTRADICTED


async def test_a_declared_claim_registers_its_relation(engine: AsyncEngine) -> None:
    """A declared claim whose relation the registry lacks auto-registers it with the weakest
    semantics in the caller's transaction (a configuration change, #131) and normalizes; a
    detected claim with the same relation stays ``uncertain(unresolved_predicate)``."""
    w = await world(engine)
    sketch = ClaimSketch(
        subject_text="Bobby", relation_text="apprenticed with", object_text="Ann", declared=True
    )
    async with engine.begin() as conn:
        reg = await registry.load(conn, w.story_id)
        context = await load_context(
            conn, w.story_id, position=0, speaker=NARRATOR, index=await load_index(conn, w.story_id)
        )
        basis = await load_time_basis(conn, w.story_id)
        detected, _ = await normalize_declared(
            conn,
            w.story_id,
            ClaimSketch(subject_text="Bobby", relation_text="apprenticed with", object_text="Ann"),
            NARRATOR,
            0,
            registry=reg,
            context=context,
            basis=basis,
        )
        found, after = await normalize_declared(
            conn, w.story_id, sketch, NARRATOR, 0, registry=reg, context=context, basis=basis
        )
    assert isinstance(detected, Unnormalizable)
    assert detected.reason is rules.VerdictReason.UNRESOLVED_PREDICATE
    predicate = after.named("apprenticed_with")
    assert predicate is not None
    assert predicate.weak
    assert after.config_revision == reg.config_revision + 1
    assert not isinstance(found, Unnormalizable)
    assert found.predicate_id == predicate.id


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def count(*args: Any) -> None:
        seen.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)


async def test_verify_cost_is_bounded(engine: AsyncEngine) -> None:
    """NFR-PERF-2's implementation bound: one ``check_with`` issues the same statements and the
    same query keys however much canon the story holds about other subjects and predicates
    (20 of each added here). Its reads are keyed by the claim's subject and predicate, so their
    cost follows those facts, not the story's size."""
    w = await world(engine)
    bobby, kitchen = w.people["Bobby"], w.places["kitchen"]
    await commit(engine, w.story_id, at(bobby, kitchen, 0, 100), kind=OperationKind.CONTROL)
    claim = at(bobby, kitchen, 10, 20)

    async def measure() -> tuple[int, int, VerdictKind]:
        with _statements(engine) as seen:
            verdict, source = await _check(engine, w, claim)
        return len(seen), len(source.query_keys), verdict.kind

    small = await measure()
    for n in range(20):
        await commit(
            engine,
            w.story_id,
            at(w.people["Ann"], w.places["garden"], n * 10, n * 10 + 10),
            kind=OperationKind.CONTROL,
        )
        await commit(
            engine,
            w.story_id,
            at(bobby, w.people["Cal"], n * 10, n * 10 + 5, predicate=KNOWS),
            kind=OperationKind.CONTROL,
        )
    large = await measure()
    assert small == large
    assert small[2] is VerdictKind.SUPPORTED


def test_other_holders_wait_for_their_stores() -> None:
    """A character's own records arrive in M5: a claim checked against such a holder is
    ``uncertain(unresolved_predicate)``, never a verdict. ``check_with`` reads canon only and
    refuses an utterance holder, whose records #150 passes to ``check``."""
    from tests import verify_support as vs

    said = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.years(2010, 2014))
    found = rules.check(said, vs.records(said), registry=vs.REGISTRY, holder=Holder.belief(vs.ANN))
    assert (found.kind, found.reason) == (
        VerdictKind.UNCERTAIN,
        rules.VerdictReason.UNRESOLVED_PREDICATE,
    )
