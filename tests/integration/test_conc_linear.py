"""docs/04 CONC-LINEAR, M2 part: no lost or duplicated turns under concurrency.

A Hypothesis rule-based machine interleaves turns held inside their model call (latches), their
release, timeline controls, truth changes inside and outside the Lite read set, and derived,
audit and settings work against one story. A small model of the story predicts the FR-CONC-4
outcome of every release from what committed since that attempt's base. After every step:

- the active turn variants are a single chain;
- the timeline revision equals 1 + the number of committed truth changes (derived, audit and
  settings work never move it);
- no two turns were accepted against the same base without rebase or regeneration.

The FR-CONC-6 slice (``test_conc_linear__idempotency``, #61) interleaves keyed turns — duplicates
sent together, retries, a different payload under a used key — with pins edits, and checks that
every idempotency key maps to at most one committed operation. The FR-CONC-5 slice
(``test_conc_linear__turn_concurrency``, #63) fires seeded bursts of overlapping turns at one
story while its ``turn_concurrency`` flips between ``reject`` and ``queue``, and checks that
the story's turns never overlap: each committed against the revision the one before it left.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from collections import Counter, defaultdict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.errors import (
    EpistrelError,
    IdempotencyConflict,
    RevisionConflict,
    StoryBusy,
    TimelineChanged,
)
from epistrel.core.idempotency import ReferenceReplay
from epistrel.core.ingest import IngestRequest, IngestResult
from epistrel.core.stories import StorySpec
from epistrel.core.turn_gate import TurnAccepted
from epistrel.orchestrator.lite.schemas import RespondRequest, RespondResult
from tests.auth_support import auth_settings
from tests.concurrency_support import (
    HeldTurn,
    assert_single_chain,
    change_setting,
    next_turn_index,
    regenerate,
    reject_render,
    revision,
    set_pins,
    start_turn,
    summarise,
    truth_outside_read_set,
)
from tests.event_support import execute_in_tx, new_story, rows
from tests.projection_support import swap, truncate, untruncate

MAX_IN_FLIGHT = 3
READ_SET_CHANGES = frozenset({"turn", "pins", "segment"})
"""Kinds of change a Lite turn's read set sees (window, pins, summary segments)."""


@dataclass
class InFlight:
    held: HeldTurn
    since: int
    """Index into the change log where the current attempt's snapshot was taken."""
    regenerations: int = 0


@dataclass
class Tally:
    outcomes: dict[str, int] = field(default_factory=dict)

    def add(self, name: str) -> None:
        self.outcomes[name] = self.outcomes.get(name, 0) + 1


class ConcLinear(RuleBasedStateMachine):
    """One story; every rule runs to completion on the machine's own event loop, while held
    turns stay suspended inside their model call between rules."""

    url: ClassVar[str] = ""
    tally: ClassVar[Tally] = Tally()

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.engine: AsyncEngine = make_engine(auth_settings(self.url))
        self.story = self.run(new_story(self.engine))
        self.truth_changes = 0
        self.base_revision = self.run(revision(self.engine, self.story))
        self.log: list[tuple[str, bool]] = []
        """(kind, bumps the revision) of every committed change, in commit order."""
        self.in_flight: list[InFlight] = []
        self.accepted: list[dict[str, Any]] = []
        self.counter = 0
        self.budget = 2

    @initialize(budget=st.integers(0, 2))
    def story_settings(self, budget: int) -> None:
        """The regeneration budget is the story's prospective setting, read when a turn starts."""
        self.budget = budget
        self.run(
            execute_in_tx(
                self.engine,
                (
                    "UPDATE stories SET settings = jsonb_build_object("
                    "'regenerate_retry_budget', CAST(:b AS int)) WHERE id = :s",
                    {"b": budget, "s": self.story},
                ),
            )
        )

    def run[T](self, coro: Coroutine[Any, Any, T] | asyncio.Future[T]) -> T:
        return self.loop.run_until_complete(coro)

    def changed(self, kind: str, *, bumps: bool) -> None:
        self.log.append((kind, bumps))
        self.truth_changes += bumps

    def line(self) -> str:
        self.counter += 1
        return f"line {self.counter}"

    # -- turns -------------------------------------------------------------------------------

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule()
    def begin_turn(self) -> None:
        ConcLinear.tally.add("begin")
        held = self.run(start_turn(self.engine, self.story, self.line()))
        self.in_flight.append(InFlight(held=held, since=len(self.log)))

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule()
    def begin_another_turn(self) -> None:
        """Turns are the subject: a second rule doubles how often one begins."""
        self.begin_turn()

    @precondition(lambda self: bool(self.in_flight))
    @rule(pick=st.integers(0, MAX_IN_FLIGHT - 1))
    def release_turn(self, pick: int) -> None:
        self.release(self.in_flight[pick % len(self.in_flight)])

    @precondition(lambda self: bool(self.in_flight))
    @rule()
    def release_oldest_turn(self) -> None:
        self.release(self.in_flight[0])

    def release(self, flight: InFlight) -> None:
        ConcLinear.tally.add("release")
        expected = self.expected_outcome(flight)
        self.run(flight.held.release())
        last = flight.held.op.attempts[-1]
        assert last["outcome"] == expected, (last, self.log[flight.since :])
        ConcLinear.tally.add(expected)
        if expected == "regenerate":
            if flight.regenerations < self.budget:
                flight.regenerations += 1
                flight.since = len(self.log)
                assert not flight.held.done
                return
            with pytest.raises(RevisionConflict):
                self.run(flight.held.task)
            ConcLinear.tally.add("revision_conflict")
        elif expected == "abandon":
            with pytest.raises(TimelineChanged):
                self.run(flight.held.task)
        else:
            result = self.run(flight.held.task)
            self.accepted.append(result.attempts[-1])
            self.changed("turn", bumps=True)
        self.in_flight.remove(flight)

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

    # -- timeline controls -------------------------------------------------------------------

    @rule(to=st.integers(0, 20))
    def truncate_story(self, to: int) -> None:
        head = self.run(next_turn_index(self.engine, self.story))
        self.run(truncate(self.engine, self.story, to % head))
        self.changed("control", bumps=True)

    @rule()
    def untruncate_story(self) -> None:
        if self.run(untruncate(self.engine, self.story)) is not None:
            self.changed("control", bumps=True)

    @rule()
    def swap_story(self) -> None:
        if self.run(swap(self.engine, self.story)) is not None:
            self.changed("control", bumps=True)

    @rule()
    def regenerate_head(self) -> None:
        if self.run(next_turn_index(self.engine, self.story)) > 1:
            self.run(regenerate(self.engine, self.story))
            self.changed("control", bumps=True)

    # -- other truth changes -----------------------------------------------------------------

    @rule()
    def edit_pins(self) -> None:
        self.run(set_pins(self.engine, self.story, self.line()))
        self.changed("pins", bumps=True)

    @rule()
    def change_outside_read_set(self) -> None:
        self.run(truth_outside_read_set(self.engine, self.story))
        self.changed("outside", bumps=True)

    # -- work that never moves the revision --------------------------------------------------

    @rule()
    def derived_summary(self) -> None:
        head = self.run(next_turn_index(self.engine, self.story))
        if head == 1:
            return
        before = self.run(revision(self.engine, self.story))
        self.run(summarise(self.engine, self.story, head - 1))
        assert self.run(revision(self.engine, self.story)) == before
        self.changed("segment", bumps=False)

    @rule()
    def audit_and_settings(self) -> None:
        before = self.run(revision(self.engine, self.story))
        head = self.run(next_turn_index(self.engine, self.story))
        if head > 1:
            self.run(reject_render(self.engine, self.story, head - 1))
        self.run(change_setting(self.engine, self.story))
        assert self.run(revision(self.engine, self.story)) == before
        self.changed("setting", bumps=False)

    # -- invariants --------------------------------------------------------------------------

    @invariant()
    def single_chain(self) -> None:
        ConcLinear.tally.add("checked")
        self.run(assert_single_chain(self.engine, self.story))

    @invariant()
    def revision_counts_truth_changes(self) -> None:
        current = self.run(revision(self.engine, self.story))
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
            self.run(assert_single_chain(self.engine, self.story))
        finally:
            self.run(self.engine.dispose())
            self.loop.close()


@pytest.mark.part("M2", "FR-CONC-1,2,4")
def test_conc_linear(pg_url: str) -> None:
    """For any interleaving of held turns, their release, truncate/untruncate/swap/regenerate,
    pins edits, truth changes outside the read set, and derived, audit and settings work: one
    committed chain; no two turns accepted against one base without rebase or regeneration;
    the revision moves by exactly one per committed truth change. Complement: derived and
    audit work commit and the revision is identical. The run must have produced every
    FR-CONC-4 outcome, or the property says nothing about them."""
    ConcLinear.url = pg_url
    ConcLinear.tally = Tally()
    run_state_machine_as_test(  # type: ignore[no-untyped-call]
        ConcLinear,
        settings=settings(
            max_examples=50,
            stateful_step_count=50,
            deadline=None,
            derandomize=True,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
        ),
    )
    seen = ConcLinear.tally.outcomes
    assert all(seen.get(name, 0) > 0 for name in ("commit", "rebase", "regenerate", "abandon")), (
        seen
    )
    assert seen.get("revision_conflict", 0) > 0, seen


# -- the FR-CONC-6 slice (#61) ---------------------------------------------------------------

IDEMPOTENCY_ROUNDS = 12
IDEMPOTENCY_SEED = 61


async def _keyed_turn(
    lib: Engine, story: str, actor: str, said: str, key: str
) -> tuple[str, RespondResult | BaseException]:
    try:
        result = await lib.respond(
            RequestContext(principal="p-conc", idempotency_key=key),
            story,
            RespondRequest.model_validate(
                {"actor_character_id": actor, "player_input": {"say": said}}
            ),
        )
    except (EpistrelError, ReferenceReplay) as exc:
        return key, exc
    return key, result


@pytest.mark.part("M2", "FR-CONC-6")
async def test_conc_linear__idempotency(pg_url: str, engine: AsyncEngine) -> None:
    """Seeded interleavings of keyed turns — duplicates sent together, retries after the
    original, a different payload under a used key — with pins edits in between: every key maps
    to at most one committed operation and every answer under a key is that operation's
    message; the chain stays single and the revision moves by exactly one per committed truth
    change. Complement: the run committed turns, replayed duplicates and refused payloads."""
    rng = random.Random(IDEMPOTENCY_SEED)  # noqa: S311 - a fixed seed, not a secret
    lib = Engine(auth_settings(pg_url, model_adapter="stub"), db=engine)
    created = await lib.create_story(
        RequestContext(principal="p-conc"),
        StorySpec.model_validate(
            {
                "name": "conc idempotency",
                "characters": [
                    {
                        "name": "Wren",
                        "definition": "The player.",
                        "true_age": 30,
                        "is_player": True,
                    },
                    {"name": "Ada", "definition": "A guide.", "true_age": 40},
                ],
            }
        ),
    )
    story, actor = created.story_id, created.characters[0].id
    start = await revision(engine, story)
    answers: dict[str, set[str]] = defaultdict(set)
    payloads: dict[str, str] = {}
    tally: Counter[str] = Counter()
    pins = 0
    for round_ in range(IDEMPOTENCY_ROUNDS):
        work = []
        for k in range(rng.randint(1, 3)):
            key = f"conc-{IDEMPOTENCY_SEED}-{round_}-{k}"
            payloads[key] = f"round {round_} key {k}"
            work += [
                _keyed_turn(lib, story, actor, payloads[key], key) for _ in range(rng.randint(1, 4))
            ]
        if payloads and rng.random() < 0.5:  # a used key with a different payload
            used = rng.choice(sorted(payloads))
            work.append(_keyed_turn(lib, story, actor, payloads[used] + " (changed)", used))
        if rng.random() < 0.4:
            pins += 1
            work.append(_as_pair(set_pins(engine, story, f"pin {round_}")))
        rng.shuffle(work)
        for key, outcome in await asyncio.gather(*work):
            if isinstance(outcome, RespondResult):
                tally["replayed" if outcome.replayed else "committed"] += 1
                answers[key].add(outcome.message.message_id)
            elif isinstance(outcome, IdempotencyConflict):
                tally["refused"] += 1
            elif isinstance(outcome, BaseException):
                tally[type(outcome).__name__] += 1
        # Retry every key once more after the dust settles: replay, or (after a failure) run.
        for key, said in payloads.items():
            _, outcome = await _keyed_turn(lib, story, actor, said, key)
            if isinstance(outcome, RespondResult):
                answers[key].add(outcome.message.message_id)

    committed = await rows(
        engine,
        "SELECT o.idempotency_key, count(*) AS n, max(k.operation_id) AS bound, "
        "max(o.operation_id) AS op FROM operations o LEFT JOIN idempotency_keys k "
        "ON k.key = o.idempotency_key AND k.scope = 'story' AND k.scope_id = o.story_id "
        "WHERE o.story_id = :s AND o.kind = 'turn' AND o.status = 'committed' "
        "GROUP BY o.idempotency_key",
        s=story,
    )
    assert all(r.n == 1 and r.bound == r.op for r in committed), committed
    turn_messages = {
        r.idempotency_key: r.id
        for r in await rows(
            engine,
            "SELECT o.idempotency_key, e.id FROM events e JOIN operations o "
            "ON o.operation_id = e.operation_id WHERE o.story_id = :s "
            "AND e.type = 'message_rendered' AND o.status = 'committed'",
            s=story,
        )
    }
    for key, seen in answers.items():
        assert seen == {turn_messages[key]}, (key, seen)
    await assert_single_chain(engine, story)
    turns = len(committed)
    assert await revision(engine, story) == start + turns + pins
    print(f"CONC-LINEAR idempotency tally={dict(tally)} turns={turns} pins={pins}")
    assert all(tally[outcome] > 0 for outcome in ("committed", "replayed", "refused")), tally


async def _as_pair(work: Coroutine[Any, Any, Any]) -> tuple[str, Any]:
    await work
    return "", None


TURN_CONCURRENCY_ROUNDS = 12
TURN_CONCURRENCY_SEED = 63


async def _turn_attempt(
    lib: Engine, story: str, actor: str, n: int, rng: random.Random
) -> RespondResult | IngestResult | BaseException:
    """One respond (sometimes ``respond_async``) or one single-event ingest."""
    ctx = RequestContext(principal="p-conc")
    try:
        if rng.random() < 0.3:
            event = {"client_event_id": f"cc-{n}", "kind": "narration", "text": f"Beat {n}."}
            return await lib.ingest(ctx, story, IngestRequest.model_validate({"events": [event]}))
        request = RespondRequest.model_validate(
            {"actor_character_id": actor, "player_input": {"say": f"Move {n}."}}
        )
        return await lib.respond(ctx, story, request, respond_async=rng.random() < 0.3)
    except (StoryBusy, TurnAccepted) as exc:
        return exc


@pytest.mark.part("M2", "FR-CONC-5")
async def test_conc_linear__turn_concurrency(pg_url: str, engine: AsyncEngine) -> None:
    """Seeded bursts of overlapping turns (respond, ``respond_async``, ingest) at one story whose
    ``turn_concurrency`` flips between ``reject`` and ``queue`` between bursts: every request
    commits exactly one turn, is refused busy with nothing written, or is accepted queued and
    commits later; the story's turns never overlap — each one committed against the revision
    the turn before it left, with no regeneration — and the active turns are one chain.
    Complement: the run refused turns under ``reject`` and queued turns under ``queue``."""
    rng = random.Random(TURN_CONCURRENCY_SEED)  # noqa: S311 - a fixed seed, not a secret
    lib = Engine(auth_settings(pg_url, model_adapter="stub"), db=engine)
    assert lib.models.stub is not None
    lib.models.stub.latency_s = 0.05
    ctx = RequestContext(principal="p-conc")
    created = await lib.create_story(
        ctx,
        StorySpec.model_validate(
            {
                "name": "conc turn concurrency",
                "characters": [
                    {
                        "name": "Wren",
                        "definition": "The player.",
                        "true_age": 30,
                        "is_player": True,
                    },
                    {"name": "Ada", "definition": "A guide.", "true_age": 40},
                ],
            }
        ),
    )
    story, actor = created.story_id, created.characters[0].id
    start = await revision(engine, story)
    tally: Counter[str] = Counter()
    expected = 0
    n = 0
    for _ in range(TURN_CONCURRENCY_ROUNDS):
        mode = rng.choice(["reject", "queue"])
        await lib.update_story_settings(ctx, story, {"turn_concurrency": mode})
        burst = []
        for _ in range(rng.randint(2, 5)):
            n += 1
            burst.append(_turn_attempt(lib, story, actor, n, rng))
        for outcome in await asyncio.gather(*burst):
            if isinstance(outcome, StoryBusy):
                tally[f"busy_{mode}"] += 1
                continue
            assert not isinstance(outcome, BaseException) or isinstance(outcome, TurnAccepted), (
                outcome
            )
            tally["accepted" if isinstance(outcome, TurnAccepted) else f"played_{mode}"] += 1
            expected += 1
        await asyncio.wait_for(lib.drainer.idle(), 30)

    turns = await rows(
        engine,
        "SELECT narrative_turn, base_revision, timeline_revision, attempts, started_at, "
        "committed_at FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY narrative_turn",
        s=story,
    )
    assert [t.narrative_turn for t in turns] == list(range(1, expected + 1)), tally
    for turn in turns:  # never two at once: each turn started from its predecessor's revision
        assert turn.timeline_revision == turn.base_revision + 1, turn
        assert all(a.get("outcome") == "commit" for a in turn.attempts), turn
    leftovers = await rows(
        engine,
        "SELECT status FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status IN ('queued', 'in_flight')",
        s=story,
    )
    assert leftovers == []
    await assert_single_chain(engine, story)
    assert await revision(engine, story) == start + expected
    queued = sum(1 for a, b in itertools.pairwise(turns) if b.started_at < a.committed_at)
    print(f"CONC-LINEAR turn concurrency tally={dict(tally)} turns={expected} queued={queued}")
    assert tally["busy_reject"] > 0, tally
    assert queued > 0, tally
