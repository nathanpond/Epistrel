"""READSET-COMPLETENESS (FR-CONC-11,12), CONC-LINEAR's M4 part (FR-CONC-3) and
CONFIG-REVALIDATION's M4 part (FR-CONC-16), against PostgreSQL (docs/04 §6; #136).

Structured turns are driven by the test-only keyed reader (``tests.readset_support``): a turn
whose read phase makes its precondition lookups through ``KeyedLookup`` and whose scripted model
is held on a latch inside its call, so every interleaving is scripted (no sleeps, no wall-clock
bounds).

- ``test_readset_completeness``: random stories of facts (two predicates, five entities, exact
  windows, objective or latent visibility) and random turns of lookups (a subject, a pair or
  any; an object or any; a story-time or a window; omniscient or a character's viewpoint, some
  repeated). Every lookup is in the read set once, with the hash of exactly the facts an
  independent model of the story says it found (empty included). A random later fact committed
  while the turn is held regenerates the turn exactly when the model says it matches one of the
  keys (else the turn rebases), and the regenerated attempt's keys see it.
- ``test_conc_linear__structured``: a rule-based machine interleaves structured turns, their
  release, timeline controls, matching and unrelated fact commits, pins edits and derived,
  audit and settings work against one story; the M2 invariants hold for structured turns, whose
  rebase decisions use their keys: one chain, no two turns accepted on one base without rebase
  or regeneration, one read-set entry per key, and the revision moving by one per truth change.
- ``test_config_revalidation__verify``: random sequences of configuration changes land while a
  structured turn is held: interpretive verification settings (``verification.level``,
  ``engine_profile:<id>.<path>``), predicate registrations, and prospective ones
  (``verification.entailment_gate``). A turn with an interpretive change re-runs its
  ``REBASE_CHECKS`` (the stand-in for #140's verify) under the configuration current then, and
  commits recording it, or regenerates when the check fails there; a prospective change never
  re-checks, rebases or regenerates (the complement).
- ``test_config_revalidation__permission`` (#144): the same for novelty permission checks. A
  structured turn's read phase asks ``core.verify.permission`` about its claims (the Bobby/Jenni
  fixture, ``tests.novelty_support``). The author turning ``allow_new_arrivals`` off while it is
  held is an interpretive change: the turn re-checks at commit, the director's arrival is now
  denied (``policy``), and the turn regenerates under the new policy. A lock on the claim's own
  (subject, predicate) touches its keyed lock lookup and regenerates it, the claim then denied
  (``locked_canon``); a lock elsewhere moves only the ``author_locks`` member, so the turn
  re-checks, passes and commits. Complement: a prospective change (``verification.
  entailment_gate``) never re-checks, and each committed turn records the configuration
  current at its commit.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import config_dependencies, readset
from epistrel.core.canon.facts import Fact, Visibility
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind
from epistrel.core.errors import RevisionConflict, TimelineChanged
from epistrel.core.events import Operation
from epistrel.core.profiles import schema
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.readset import EMPTY_RESULT, result_hash
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.verify import permission
from tests import novelty_support as ns
from tests.auth_support import auth_settings
from tests.canon_support import World, at, auto_predicate, commit, world
from tests.concurrency_support import (
    assert_single_chain,
    change_setting,
    included_segment_ids,
    next_turn_index,
    regenerate,
    reject_render,
    revision,
    set_pins,
    summarise,
)
from tests.event_support import execute_in_tx
from tests.memory_support import use_structured
from tests.projection_support import swap, truncate, untruncate
from tests.readset_support import KeyedTurn, Lookup, start_keyed_turn, stored_read_set

pytestmark = pytest.mark.integration

ITEMS = ("door", "chest", "gate", "padlock", "bolt")
RELATIONS = ("has lock", "guards")
VIEWERS = (OMNISCIENT, "Bobby", "Ann")
"""Lookup viewpoints: omniscient, or a character's (``character:<id>``)."""


# -- the model of a story ---------------------------------------------------------------------


@dataclass(frozen=True)
class FactSpec:
    subject: int
    relation: int
    object: int
    lo: int
    hi: int | None
    latent: bool
    """Visible to Bobby (and omniscient) only."""


@dataclass(frozen=True)
class LookupSpec:
    subjects: tuple[int, ...] | None
    relation: int
    object: int | None
    time: int | None
    window: tuple[int, int] | None
    viewer: int


def _meets(a: tuple[int | None, int | None], b: tuple[int | None, int | None]) -> bool:
    lows = [x for x in (a[0], b[0]) if x is not None]
    highs = [x for x in (a[1], b[1]) if x is not None]
    return not lows or not highs or max(lows) < min(highs)


def _window(q: LookupSpec) -> tuple[int | None, int | None]:
    if q.time is not None:
        return (q.time, q.time + 1)
    assert q.window is not None
    return q.window


def _visible(f: FactSpec, q: LookupSpec) -> bool:
    return not f.latent or VIEWERS[q.viewer] in (OMNISCIENT, "Bobby")


def _found(f: FactSpec, q: LookupSpec) -> bool:
    """Whether lookup ``q`` answers fact ``f`` (the model of ``KeyedLookup.canon_at``)."""
    if f.relation != q.relation or not _visible(f, q):
        return False
    if q.subjects is not None and f.subject not in q.subjects:
        return False
    if q.object is not None and f.object != q.object:
        return False
    if q.time is not None:
        return f.lo <= q.time and (f.hi is None or q.time < f.hi)
    return _meets((f.lo, f.hi), _window(q))


def _touches(f: FactSpec, q: LookupSpec) -> bool:
    """Whether committing ``f`` touches ``q``'s key (FR-CONC-12): the same shape test, with the
    windows only needing to meet."""
    if f.relation != q.relation or not _visible(f, q):
        return False
    if q.subjects is not None and f.subject not in q.subjects:
        return False
    if q.object is not None and f.object != q.object:
        return False
    return _meets((f.lo, f.hi), _window(q))


item = st.integers(0, len(ITEMS) - 1)
relation = st.integers(0, len(RELATIONS) - 1)
fact_spec = st.builds(
    lambda s, r, o, lo, n, open_, latent: FactSpec(s, r, o, lo, None if open_ else lo + n, latent),
    item,
    relation,
    item,
    st.integers(0, 8),
    st.integers(1, 5),
    st.booleans(),
    st.booleans(),
)
lookup_spec = st.builds(
    lambda subjects, r, o, timed, t, a, n, v: LookupSpec(
        subjects, r, o, t if timed else None, None if timed else (a, a + n), v
    ),
    st.one_of(
        st.none(),
        st.tuples(item),
        st.sets(item, min_size=2, max_size=2).map(lambda s: tuple(sorted(s))),
    ),
    relation,
    st.one_of(st.none(), item),
    st.booleans(),
    st.integers(0, 12),
    st.integers(0, 10),
    st.integers(1, 6),
    st.integers(0, len(VIEWERS) - 1),
)


def _triple(f: FactSpec) -> tuple[int, int, int]:
    return (f.subject, f.relation, f.object)


@dataclass
class Story:
    world: World
    items: list[str]
    relations: list[str]

    def proposition(self, f: FactSpec) -> Proposition:
        return Proposition(
            subject_id=self.items[f.subject],
            predicate_id=self.relations[f.relation],
            object_id=self.items[f.object],
            valid_time=Interval.of(f.lo, f.hi),
        )

    def visibility(self, f: FactSpec) -> Visibility:
        if f.latent:
            return Visibility(scope="characters", characters=(self.world.characters["Bobby"],))
        return Visibility()

    def lookup(self, q: LookupSpec) -> Lookup:
        viewer = VIEWERS[q.viewer]
        return Lookup(
            predicate=self.relations[q.relation],
            subjects=None if q.subjects is None else tuple(self.items[i] for i in q.subjects),
            object=None if q.object is None else self.items[q.object],
            story_time=q.time,
            window=q.window,
            viewpoint=viewer if viewer == OMNISCIENT else self.world.viewpoint(viewer),
        )


async def _story(engine: AsyncEngine) -> Story:
    w = await world(engine)
    async with engine.begin() as conn:
        items = [await store.add_entity(conn, w.story_id, "item", name) for name in ITEMS]
    relations = [await auto_predicate(engine, w.story_id, r) for r in RELATIONS]
    return Story(w, items, relations)


async def _write(engine: AsyncEngine, s: Story, f: FactSpec) -> Fact:
    return await commit(
        engine,
        s.world.story_id,
        s.proposition(f),
        kind=OperationKind.CONTROL,
        visibility=s.visibility(f),
    )


def _expected(lookups: list[LookupSpec], committed: dict[FactSpec, Fact]) -> list[str]:
    """The result hash of each distinct lookup, in first-asked order."""
    seen: dict[LookupSpec, str] = {}
    for q in lookups:
        if q not in seen:
            seen[q] = result_hash((x.id, x.tx_from) for f, x in committed.items() if _found(f, q))
    return list(seen.values())


@dataclass
class Tally:
    seen: Counter[str] = field(default_factory=Counter)


async def _completeness(
    url: str, initial: list[FactSpec], lookups: list[LookupSpec], later: FactSpec, tally: Tally
) -> None:
    engine = make_engine(auth_settings(url))
    try:
        s = await _story(engine)
        committed = {f: await _write(engine, s, f) for f in initial}
        held = await start_keyed_turn(engine, s.world.story_id, [s.lookup(q) for q in lookups])
        keys = held.read_set.query_keys
        # Every lookup is in the read set exactly once, with the hash of what it found.
        assert [k.result_hash for k in keys] == _expected(lookups, committed)
        tally.seen["empty"] += sum(k.result_hash == EMPTY_RESULT for k in keys)
        tally.seen["found"] += sum(k.result_hash != EMPTY_RESULT for k in keys)

        committed[later] = await _write(engine, s, later)
        touches = any(_touches(later, q) for q in lookups)
        await held.release()
        first = held.op.attempts[0]
        if touches:
            assert first["outcome"] == "regenerate", (first, later, lookups)
            assert first["reason"] == "read_set_touched"
            # The regenerated attempt's keys see the later fact.
            assert [k.result_hash for k in held.read_set.query_keys] == _expected(
                lookups, committed
            )
            tally.seen["regenerate"] += 1
            await held.release()
        else:
            assert first["outcome"] == "rebase", (first, later, lookups)
            tally.seen["rebase"] += 1
        result = await held.task
        assert result.attempts[-1]["outcome"] in ("commit", "rebase")
        stored = await stored_read_set(engine, held.op.operation_id)
        assert len(stored["keys"]) == len(set(lookups))
    finally:
        await engine.dispose()


@pytest.mark.part("M4", "FR-CONC-11,12")
def test_readset_completeness(pg_url: str) -> None:
    """See the module docstring. Complement: the runs had empty and non-empty keys, and later
    facts that did and did not touch the read set."""
    tally = Tally()

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(
        initial=st.lists(fact_spec, max_size=6, unique_by=_triple),
        lookups=st.lists(lookup_spec, min_size=1, max_size=4).flatmap(
            lambda qs: st.lists(st.sampled_from(qs), min_size=len(qs), max_size=len(qs) + 2)
        ),
        later=fact_spec,
    )
    def complete(initial: list[FactSpec], lookups: list[LookupSpec], later: FactSpec) -> None:
        if _triple(later) in {_triple(f) for f in initial}:
            later = FactSpec(
                later.subject,
                later.relation,
                (later.object + 1) % len(ITEMS),
                later.lo,
                later.hi,
                later.latent,
            )
            if _triple(later) in {_triple(f) for f in initial}:
                return
        asyncio.run(_completeness(pg_url, initial, lookups, later, tally))

    complete()
    assert all(tally.seen[k] > 0 for k in ("empty", "found", "regenerate", "rebase")), tally


# -- CONC-LINEAR, M4 part ---------------------------------------------------------------------

MAX_IN_FLIGHT = 3
NOW = 1_000
READ_SET_CHANGES = frozenset({"turn", "pins", "segment", "lock"})
"""Kinds of change a structured turn's read set sees: the Lite members, and a lock on the door
(its keyed precondition). A derived segment the prompt does not include (it does not continue
the included run from turn 1, FR-STORE-26) is ``derived``: it touches nothing."""


@dataclass
class InFlight:
    held: KeyedTurn
    since: int
    regenerations: int = 0


class ConcLinearStructured(RuleBasedStateMachine):
    """One story; held structured turns stay suspended inside their model call between rules."""

    url: ClassVar[str] = ""
    tally: ClassVar[Counter[str]] = Counter()

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.engine: AsyncEngine = make_engine(auth_settings(self.url))
        self.story = self.run(_story(self.engine))
        self.base_revision = self.run(revision(self.engine, self.story.world.story_id))
        self.truth_changes = 0
        self.log: list[tuple[str, bool]] = []
        self.in_flight: list[InFlight] = []
        self.accepted: list[dict[str, Any]] = []
        self.counter = 0
        self.places = 0
        self.budget = 2

    @property
    def story_id(self) -> str:
        return self.story.world.story_id

    def run[T](self, coro: Coroutine[Any, Any, T] | asyncio.Future[T]) -> T:
        return self.loop.run_until_complete(coro)

    def changed(self, kind: str, *, bumps: bool) -> None:
        self.log.append((kind, bumps))
        self.truth_changes += bumps

    @initialize(budget=st.integers(0, 2))
    def story_settings(self, budget: int) -> None:
        self.budget = budget
        self.run(
            execute_in_tx(
                self.engine,
                (
                    "UPDATE stories SET settings = jsonb_build_object("
                    "'regenerate_retry_budget', CAST(:b AS int)) WHERE id = :s",
                    {"b": budget, "s": self.story_id},
                ),
            )
        )

    def door_lookup(self) -> Lookup:
        """Locks on the door now; asked twice per turn (one read-set entry)."""
        return Lookup(
            predicate=self.story.relations[0], subjects=self.story.items[0], story_time=NOW
        )

    # -- turns -------------------------------------------------------------------------------

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule()
    def begin_turn(self) -> None:
        held = self.run(
            start_keyed_turn(self.engine, self.story_id, [self.door_lookup(), self.door_lookup()])
        )
        self.in_flight.append(InFlight(held=held, since=len(self.log)))
        ConcLinearStructured.tally["begin"] += 1

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule()
    def begin_another_turn(self) -> None:
        self.begin_turn()

    @precondition(lambda self: bool(self.in_flight))
    @rule(pick=st.integers(0, MAX_IN_FLIGHT - 1))
    def release_turn(self, pick: int) -> None:
        self.release(self.in_flight[pick % len(self.in_flight)])

    @precondition(lambda self: bool(self.in_flight))
    @rule()
    def release_oldest_turn(self) -> None:
        self.release(self.in_flight[0])

    def expected_outcome(self, flight: InFlight) -> str:
        since = self.log[flight.since :]
        kinds = {kind for kind, _ in since}
        if "control" in kinds:
            return "abandon"
        if not any(bumps for _, bumps in since):
            return "commit"
        if kinds & READ_SET_CHANGES:
            return "regenerate"
        return "rebase"

    def release(self, flight: InFlight) -> None:
        expected = self.expected_outcome(flight)
        self.run(flight.held.release())
        last = flight.held.op.attempts[-1]
        assert last["outcome"] == expected, (last, self.log[flight.since :])
        ConcLinearStructured.tally[expected] += 1
        if expected == "regenerate":
            if flight.regenerations < self.budget:
                flight.regenerations += 1
                flight.since = len(self.log)
                assert not flight.held.done
                return
            with pytest.raises(RevisionConflict):
                self.run(flight.held.task)
            ConcLinearStructured.tally["revision_conflict"] += 1
        elif expected == "abandon":
            with pytest.raises(TimelineChanged):
                self.run(flight.held.task)
        else:
            result = self.run(flight.held.task)
            self.accepted.append(result.attempts[-1])
            stored = self.run(stored_read_set(self.engine, flight.held.op.operation_id))
            assert len(stored["keys"]) == 1, stored  # one entry per key
            self.changed("turn", bumps=True)
        self.in_flight.remove(flight)

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule(matching=st.booleans(), chance=st.integers(0, 3))
    def turn_over_a_fact(self, matching: bool, chance: int) -> None:
        """A turn with one fact committed under it: a lock regenerates it, anything else
        rebases (drawn as one step so both outcomes are reliably covered)."""
        if chance:
            return
        self.begin_turn()
        if matching:
            self.add_lock()
        else:
            self.unrelated_fact()
        self.release(self.in_flight[-1])

    # -- timeline controls -------------------------------------------------------------------

    @rule(to=st.integers(0, 20))
    def truncate_story(self, to: int) -> None:
        head = self.run(next_turn_index(self.engine, self.story_id))
        if self.run(truncate(self.engine, self.story_id, to % head)) is not None:
            self.changed("control", bumps=True)

    @rule()
    def untruncate_story(self) -> None:
        if self.run(untruncate(self.engine, self.story_id)) is not None:
            self.changed("control", bumps=True)

    @rule()
    def swap_story(self) -> None:
        if self.run(swap(self.engine, self.story_id)) is not None:
            self.changed("control", bumps=True)

    @rule()
    def regenerate_head(self) -> None:
        if self.run(next_turn_index(self.engine, self.story_id)) > 1:
            self.run(regenerate(self.engine, self.story_id))
            self.changed("control", bumps=True)

    # -- truth changes -----------------------------------------------------------------------

    @rule()
    def add_lock(self) -> None:
        """A new lock on the door (a new lock entity each time: never a no-op)."""
        self.counter += 1

        async def lock() -> None:
            async with self.engine.begin() as conn:
                bolt = await store.add_entity(conn, self.story_id, "item", f"lock {self.counter}")
            await commit(
                self.engine,
                self.story_id,
                Proposition(
                    subject_id=self.story.items[0],
                    predicate_id=self.story.relations[0],
                    object_id=bolt,
                    valid_time=Interval.of(0, None),
                ),
                kind=OperationKind.CONTROL,
            )

        self.run(lock())
        self.changed("lock", bumps=True)

    @rule()
    def unrelated_fact(self) -> None:
        """Ann moves (a functional predicate no key asks about): it supersedes, never touches."""
        self.places += 1
        place = ("kitchen", "garden", "school")[self.places % 3]
        w = self.story.world
        self.run(
            commit(
                self.engine,
                self.story_id,
                at(w.people["Ann"], w.places[place], 0, None),
                kind=OperationKind.CONTROL,
            )
        )
        self.changed("fact", bumps=True)

    @rule()
    def edit_pins(self) -> None:
        self.counter += 1
        self.run(set_pins(self.engine, self.story_id, f"pin {self.counter}"))
        self.changed("pins", bumps=True)

    # -- work that never moves the revision --------------------------------------------------

    @rule()
    def derived_summary(self) -> None:
        head = self.run(next_turn_index(self.engine, self.story_id))
        if head == 1:
            return
        before = self.run(included_segment_ids(self.engine, self.story_id))
        self.run(summarise(self.engine, self.story_id, head - 1))
        included = self.run(included_segment_ids(self.engine, self.story_id)) != before
        self.changed("segment" if included else "derived", bumps=False)

    @rule()
    def audit_and_settings(self) -> None:
        head = self.run(next_turn_index(self.engine, self.story_id))
        if head > 1:
            self.run(reject_render(self.engine, self.story_id, head - 1))
        self.run(change_setting(self.engine, self.story_id))
        self.changed("setting", bumps=False)

    # -- invariants --------------------------------------------------------------------------

    @invariant()
    def single_chain(self) -> None:
        self.run(assert_single_chain(self.engine, self.story_id))

    @invariant()
    def revision_counts_truth_changes(self) -> None:
        current = self.run(revision(self.engine, self.story_id))
        assert current == self.base_revision + self.truth_changes

    @invariant()
    def no_two_turns_on_one_base(self) -> None:
        plain = [a["base_revision"] for a in self.accepted if a["outcome"] == "commit"]
        assert len(plain) == len(set(plain)), self.accepted
        for attempt in self.accepted:
            moved = attempt["committed_revision"] != attempt["base_revision"] + 1
            assert moved == (attempt["outcome"] == "rebase"), attempt

    def teardown(self) -> None:
        try:
            while self.in_flight:
                flight = self.in_flight.pop()
                while not flight.held.done:
                    self.run(flight.held.release())
                exc = flight.held.task.exception()
                assert exc is None or isinstance(exc, RevisionConflict | TimelineChanged), exc
            self.run(assert_single_chain(self.engine, self.story_id))
        finally:
            self.run(self.engine.dispose())
            self.loop.close()


@pytest.mark.part("M4", "FR-CONC-3")
def test_conc_linear__structured(pg_url: str) -> None:
    """CONC-LINEAR for structured turns (see the module docstring). Complement: the run produced
    every FR-CONC-4 outcome, a conflict past the budget, and regenerations caused by a lock."""
    ConcLinearStructured.url = pg_url
    ConcLinearStructured.tally = Counter()
    run_state_machine_as_test(  # type: ignore[no-untyped-call]
        ConcLinearStructured,
        settings=settings(
            max_examples=100,
            stateful_step_count=30,
            deadline=None,
            derandomize=True,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
        ),
    )
    seen = ConcLinearStructured.tally
    assert all(seen[name] > 0 for name in ("commit", "rebase", "regenerate", "abandon")), seen
    assert seen["revision_conflict"] > 0, seen


@pytest.mark.part("M4", "FR-CONC-3")
def test_conc_linear__structured_unincluded_segment(pg_url: str) -> None:
    """The run Hypothesis found (b22, 5c3a749): a turn held over an unrelated fact and a
    derived segment over turn 2 alone, which the prompt does not include (no segment covers
    turn 1), rebases: the oracle must not count that segment as touching the read set."""
    ConcLinearStructured.url = pg_url
    ConcLinearStructured.tally = Counter()
    state = ConcLinearStructured()
    try:
        state.story_settings(budget=0)
        state.derived_summary()
        state.unrelated_fact()
        state.turn_over_a_fact(chance=0, matching=False)
        state.begin_another_turn()
        state.release_turn(pick=0)
        state.begin_turn()
        state.begin_turn()
        state.unrelated_fact()
        state.begin_another_turn()
        state.derived_summary()
        assert state.log[-2:] == [("fact", True), ("derived", False)], state.log
        state.release_oldest_turn()
        assert state.tally["rebase"] == 2, state.tally
    finally:
        state.teardown()


# -- CONFIG-REVALIDATION, M4 part --------------------------------------------------------------

NO_FACTS = RuntimeFacts(models={}, templates={})
LEVELS = ("declared", "extraction", "entailment")
FAILING_LEVEL = "declared"
"""The stand-in verifier refuses the staged output under this level."""

change = st.sampled_from(("level", "registry", "prospective"))


@dataclass
class Verifier:
    """A ``REBASE_CHECKS`` stand-in for #140's verify: it reads the story's verification level
    on the commit's connection and refuses the staged output under ``FAILING_LEVEL``."""

    levels: list[str] = field(default_factory=list)

    async def __call__(self, conn: AsyncConnection, op: Operation) -> bool:
        assert op.story_id is not None
        level = (await effective_configuration(conn, op.story_id)).settings.verification.level
        self.levels.append(level)
        return level != FAILING_LEVEL


async def _level(engine: AsyncEngine, story_id: str, level: str) -> None:
    await set_overrides(
        engine,
        story_id,
        ProfileOverridesPatch(settings={"verification": {"level": level}}, ignore_warnings=True),
        principal="p-author",
        facts=NO_FACTS,
    )


async def _revalidation(
    url: str, changes: list[tuple[str, int]], tally: Counter[str], verifier: Verifier
) -> None:
    engine = make_engine(auth_settings(url))
    try:
        s = await _story(engine)
        story = s.world.story_id
        await use_structured(engine, story)
        await _level(engine, story, "entailment")
        verifier.levels.clear()
        held = await start_keyed_turn(
            engine,
            story,
            [Lookup(predicate=s.relations[0], subjects=s.items[0], story_time=NOW)],
            budget=1,
        )
        registered = False
        level = "entailment"
        for n, (kind, pick) in enumerate(changes):
            if kind == "level":
                level = LEVELS[pick % len(LEVELS)]
                await _level(engine, story, level)
            elif kind == "registry":
                await auto_predicate(engine, story, f"relation {n}")
                registered = True
            else:
                await set_overrides(
                    engine,
                    story,
                    ProfileOverridesPatch(
                        settings={"verification": {"entailment_gate": (pick % 10) / 10}},
                        ignore_warnings=True,
                    ),
                    principal="p-author",
                    facts=NO_FACTS,
                )
        # A dependency moved when the registry grew or the level ended elsewhere than it began.
        interpretive = registered or level != "entailment"
        await held.release()
        first = held.op.attempts[0]
        if not interpretive:
            # The complement: a prospective change never re-checks, rebases or regenerates.
            assert first["outcome"] == "commit", first
            assert verifier.levels == []
            tally["prospective"] += 1
        else:
            assert first["outcome"] == "recheck", first
            assert verifier.levels == [level], (verifier.levels, level)
            if level == FAILING_LEVEL:
                assert first["recheck"] == "failed"
                assert not held.done  # regenerating under the new configuration
                tally["failed"] += 1
                await held.release()
            else:
                assert first["recheck"] == "passed"
                tally["passed"] += 1
        result = await held.task
        async with engine.connect() as conn:
            current = await config_dependencies.capture(
                conn, story, config_dependencies.STRUCTURED_DEPENDENCIES
            )
        recorded = result.operation.config_revisions or {}
        # The turn records the configuration current at its commit (every dependency).
        for name in config_dependencies.STRUCTURED_DEPENDENCIES:
            assert recorded.get(name) == (current or {}).get(name), name
        assert (
            await _committed_config(engine, result.operation.operation_id)
        ) == result.operation.config_revisions
    finally:
        await engine.dispose()


async def _committed_config(engine: AsyncEngine, operation_id: str) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(
            text("SELECT config_revisions FROM operations WHERE operation_id = :o"),
            {"o": operation_id},
        )


@pytest.mark.part("M4", "FR-CONC-16")
def test_config_revalidation__verify(pg_url: str) -> None:
    """See the module docstring. Complement: the runs re-checked and passed, re-checked and
    failed, and committed straight through prospective changes."""
    assert schema.setting_tags()["verification.level"].change_class.value == "interpretive"
    assert schema.setting_tags()["verification.entailment_gate"].change_class.value == (
        "prospective"
    )
    tally: Counter[str] = Counter()
    verifier = Verifier()
    readset.register_rebase_check(verifier)
    try:

        @settings(
            max_examples=60,
            deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
        )
        @given(changes=st.lists(st.tuples(change, st.integers(0, 9)), min_size=1, max_size=3))
        def revalidate(changes: list[tuple[str, int]]) -> None:
            asyncio.run(_revalidation(pg_url, changes, tally, verifier))

        revalidate()
    finally:
        readset.remove_rebase_check(verifier)
    assert all(tally[k] > 0 for k in ("prospective", "passed", "failed")), tally


# -- CONFIG-REVALIDATION, M4 part: novelty permission (#144) ------------------------------------


@dataclass
class Permissions:
    """A held turn's read phase: ``permission.check`` on its claims, each attempt's codes."""

    cast: ns.Cast
    claims: list[tuple[Proposition, permission.Asserter]]
    codes: list[list[str]] = field(default_factory=list)

    async def __call__(self, conn: AsyncConnection, read_set: readset.StructuredReadSet) -> Any:
        found = [
            await permission.check(conn, read_set, self.cast.story_id, claim, asserter)
            for claim, asserter in self.claims
        ]
        self.codes.append([f.code() if f is not None else "off" for f in found])
        return [[code] for code in self.codes[-1]]


async def _permission_turn(
    engine: AsyncEngine, cast: ns.Cast, claims: list[tuple[Proposition, permission.Asserter]]
) -> tuple[KeyedTurn, Permissions]:
    read = Permissions(cast, claims)
    held = await start_keyed_turn(engine, cast.story_id, [], budget=1, read=read)
    return held, read


async def _finish(engine: AsyncEngine, held: KeyedTurn, story_id: str) -> None:
    while not held.done:
        await held.release()
    result = await held.task
    async with engine.connect() as conn:
        current = await config_dependencies.capture(
            conn, story_id, config_dependencies.STRUCTURED_DEPENDENCIES
        )
    recorded = result.operation.config_revisions or {}
    for name in config_dependencies.STRUCTURED_DEPENDENCIES:
        assert recorded.get(name) == (current or {}).get(name), name


async def _permission_revalidation(url: str) -> None:
    engine = make_engine(auth_settings(url))
    try:
        cast = await ns.bobby_and_jenni(engine)
        story = cast.story_id
        arrival = (ns.claim(cast.stranger, ns.LOCATED_AT, cast.porch), cast.director)
        backstory = (ns.claim(cast.bobby, ns.ATTENDED, cast.camp), cast.render(cast.bobby))

        # The arrival policy turned off while the turn is held: re-checked, now denied.
        held, read = await _permission_turn(engine, cast, [arrival])
        assert read.codes == [["permitted:arrival"]]
        await ns.set_arrivals(engine, story, allowed=False)
        await held.release()
        first = held.op.attempts[0]
        assert (first["outcome"], first["recheck"]) == ("recheck", "failed"), first
        assert first["config_changed"] == ["allow_new_arrivals"], first
        assert not held.done  # regenerating under the new policy
        assert read.codes[-1] == ["denied:policy"]
        await _finish(engine, held, story)
        assert held.op.config_revisions is not None
        assert held.op.config_revisions["allow_new_arrivals"] is False
        await ns.set_arrivals(engine, story, allowed=True)

        # A lock on the claim's own key: its keyed lock lookup is touched, the turn regenerates
        # and the claim is then denied.
        held, read = await _permission_turn(engine, cast, [backstory])
        assert read.codes == [["permitted:own"]]
        await commit(
            engine,
            story,
            ns.claim(cast.bobby, ns.ATTENDED, cast.osu, ns.SEED_SPAN),
            kind=OperationKind.CONTROL,
            provenance="author",
            author_locked=True,
        )
        await held.release()
        first = held.op.attempts[0]
        assert (first["outcome"], first["reason"]) == ("regenerate", "read_set_touched"), first
        assert read.codes[-1] == ["denied:locked_canon"]
        await _finish(engine, held, story)

        # A lock elsewhere moves only the ``author_locks`` member: re-checked, passed.
        held, read = await _permission_turn(engine, cast, [arrival])
        await commit(
            engine,
            story,
            ns.claim(cast.jenni, ns.ATTENDED, cast.camp, ns.SEED_SPAN),
            kind=OperationKind.CONTROL,
            provenance="author",
            author_locked=True,
        )
        await held.release()
        first = held.op.attempts[0]
        assert (first["outcome"], first["recheck"]) == ("recheck", "passed"), first
        assert first["config_changed"] == ["author_locks"], first
        await _finish(engine, held, story)
        assert read.codes == [["permitted:arrival"]]

        # The complement: a prospective change never re-checks.
        held, read = await _permission_turn(engine, cast, [arrival])
        await set_overrides(
            engine,
            story,
            ProfileOverridesPatch(
                settings={"verification": {"entailment_gate": 0.4}}, ignore_warnings=True
            ),
            principal="p-author",
            facts=NO_FACTS,
        )
        await held.release()
        assert held.op.attempts[0]["outcome"] == "commit", held.op.attempts[0]
        await _finish(engine, held, story)
        assert read.codes == [["permitted:arrival"]]
    finally:
        await engine.dispose()


@pytest.mark.part("M4", "FR-CONC-16")
def test_config_revalidation__permission(pg_url: str) -> None:
    """See the module docstring."""
    assert schema.setting_tags()["verification.entailment_gate"].change_class.value == (
        "prospective"
    )
    assert "allow_new_arrivals" in config_dependencies.STRUCTURED_DEPENDENCIES
    assert "author_locks" in config_dependencies.STRUCTURED_DEPENDENCIES
    asyncio.run(_permission_revalidation(pg_url))
