"""docs/04 CONFIG-REVALIDATION, M2 part: configuration changes against turns in flight
(FR-CONC-14..16; #64).

A Hypothesis rule-based machine interleaves Lite turns held inside their model call with
configuration writes against one story: interpretive changes on the turn's dependencies (a
story policy's words, the story rating's guardrails switched on and off, a rating policy's
words, ``minor_age_threshold``), an interpretive change elsewhere (another rating's guidance),
and prospective changes (the story's ``turn_deadline``, the deployment's idempotency window).
The stand-in model writes each reply when its call starts, using a word the rules in force at
that moment allow — so a reply goes stale only when a rule tightens while it is held.

Checked on every release and commit:

- the first attempt after a release has the outcome the change log predicts: another turn
  committed → ``regenerate``; else a dependency changed → ``recheck``; else ``commit`` —
  prospective changes and interpretive changes elsewhere never re-check or regenerate;
- every committed turn's reply passes the policies in force at its commit, and the
  configuration it records equals the configuration current at its commit, so its revisions
  are at least those of every dependency change committed before it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections import Counter
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import config_dependencies
from epistrel.core.config_changes import read_settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.deployment import apply_settings
from epistrel.core.enums import Provenance
from epistrel.core.errors import RevisionConflict
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.core.story_settings import update_story_settings
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.policies.rebase import output_passes, output_recheck, staged_replies
from epistrel.orchestrator.policies.schemas import PolicyFields, PolicyReplacement
from epistrel.orchestrator.policies.store import guard_context
from tests.auth_support import auth_settings
from tests.concurrency_support import HeldModel, HeldTurn, start_turn
from tests.event_support import execute_in_tx, new_story
from tests.policy_support import story_item, words

MAX_IN_FLIGHT = 2
VOCABULARY = ("amber", "cobalt", "ochre")
SAFE = "plain"
WORD_SETS = st.sets(st.sampled_from(VOCABULARY), min_size=1).map(lambda found: sorted(found))
OPERATOR = RequestContext(principal="p-operator")
STORY_POLICY = "story words"
AUTHOR = RequestContext(principal="p-author")


@dataclass
class CompliantModel(HeldModel):
    """Writes its reply when the call starts, with a word the rules then in force allow —
    except that every other time it is asked again after a failed re-check it repeats its
    refused word, as a model that ignores the new rule would (the re-check must catch that
    too: no first-pass output gate exists before #68)."""

    machine: ConfigRevalidation | None = None
    word: str = SAFE

    async def __call__(self, op: Operation, inputs: Any) -> None:
        assert self.machine is not None
        refused = bool(op.attempts) and op.attempts[-1].get("recheck") == "failed"
        if not (refused and self.machine.stubborn()):
            self.word = self.machine.next_word()
        word = self.word
        self.calls += 1
        gate = asyncio.Event()
        self._gate = gate
        self._arrived.set()
        await gate.wait()
        op.append("player_input", {"text": self.said}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": f"reply {self.calls} says {word}"})


@dataclass
class InFlight:
    held: HeldTurn
    since: int
    """Index into the change log where the current attempt's configuration was captured."""
    key: tuple[Any, ...]
    """The dependency state the current attempt captured (``ConfigRevalidation.key``)."""
    verify: bool = False
    """The attempt follows a failed re-check (its output is checked before committing)."""


class ConfigRevalidation(RuleBasedStateMachine):
    """One story; every rule runs to completion on the machine's own event loop, while held
    turns stay suspended inside their model call between rules."""

    url: ClassVar[str] = ""
    tally: ClassVar[Counter[str]] = Counter()

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.engine: AsyncEngine = make_engine(auth_settings(self.url))
        self.lib = Engine(auth_settings(self.url, model_adapter="stub"), db=self.engine)
        self.words = itertools.cycle((*VOCABULARY, SAFE))
        self.log: list[str] = []
        self.in_flight: list[InFlight] = []
        self.story_words: list[str] = ["ochre"]
        self.rating_words: list[str] = ["amber"]
        self.rating_on = False
        self.story_version = 0
        self.rating_version = 0
        self.threshold_raised = False
        self.stubborn_count = 0
        self.start = self.run(read_settings(self.engine))
        self.story, self.rating, self.other, self.story_policy_id, self.rating_policy_id = self.run(
            self.setup()
        )

    async def setup(self) -> tuple[str, Any, Any, str, str]:
        story = await new_story(self.engine)
        rating = await create_rating(
            self.engine, RatingFields(name=f"CR {new_ulid()}"), principal="p-operator"
        )
        other = await create_rating(
            self.engine, RatingFields(name=f"CR other {new_ulid()}"), principal="p-operator"
        )
        await execute_in_tx(
            self.engine,
            (
                "UPDATE stories SET content_rating_id = :r WHERE id = :s",
                {"r": rating.rating.id, "s": story},
            ),
        )
        written = await self.lib.policies_set_story(
            AUTHOR, story, [story_item(words(*self.story_words), name=STORY_POLICY)]
        )
        made = await self.lib.policies_create(
            OPERATOR,
            PolicyFields.model_validate(
                {
                    "scope": "rating",
                    "scope_id": rating.rating.id,
                    "name": f"cr-{new_ulid().lower()}",
                    "rule": words(*self.rating_words),
                    "message": "Not at this rating.",
                }
            ),
        )
        return story, rating.rating, other.rating, written.policies[0].id, made.policy.id

    def run[T](self, coro: Coroutine[Any, Any, T] | asyncio.Future[T]) -> T:
        return self.loop.run_until_complete(coro)

    def changed(self, kind: str) -> None:
        self.log.append(kind)
        ConfigRevalidation.tally[f"change_{kind}"] += 1

    def key(self) -> tuple[Any, ...]:
        """What a Lite turn's dependencies currently are, as the model sees them: a policy's
        version moves on every edit, switching the rating's guardrails or the threshold back
        restores the earlier value."""
        return (
            self.story_version,
            self.rating_on,
            self.rating_version if self.rating_on else None,
            self.threshold_raised,
        )

    def stubborn(self) -> bool:
        self.stubborn_count += 1
        ConfigRevalidation.tally["stubborn"] += self.stubborn_count % 2
        return self.stubborn_count % 2 == 1

    def banned(self) -> set[str]:
        return {*self.story_words, *(self.rating_words if self.rating_on else ())}

    def next_word(self) -> str:
        """The model's next word that the rules in force allow."""
        while (word := next(self.words)) in self.banned():
            pass
        return word

    # -- turns -------------------------------------------------------------------------------

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule()
    def begin_turn(self) -> None:
        model = CompliantModel(f"line {len(self.log)}", machine=self)
        held = self.run(
            start_turn(
                self.engine,
                self.story,
                model.said,
                model=model,
                config_recheck=output_recheck(self.engine, self.lib.models, TurnBudget()),
            )
        )
        self.in_flight.append(InFlight(held=held, since=len(self.log), key=self.key()))

    @precondition(lambda self: len(self.in_flight) < MAX_IN_FLIGHT)
    @rule()
    def begin_another_turn(self) -> None:
        """Turns are the subject: a second rule doubles how often one begins."""
        self.begin_turn()

    @precondition(lambda self: bool(self.in_flight))
    @rule()
    def release_oldest_turn(self) -> None:
        self.release(self.in_flight[0])

    @precondition(lambda self: bool(self.in_flight))
    @rule(pick=st.integers(0, MAX_IN_FLIGHT - 1))
    def release_turn(self, pick: int) -> None:
        self.release(self.in_flight[pick % len(self.in_flight)])

    def release(self, flight: InFlight) -> None:
        since = self.log[flight.since :]
        if "turn" in since:
            expected = "regenerate"
        elif self.key() != flight.key:
            expected = "recheck"
        else:
            expected = "commit"
        op = flight.held.op
        first = len(op.attempts)
        self.run(flight.held.release())
        if not flight.verify:
            outcome = op.attempts[first]["outcome"]
            assert outcome == expected, (op.attempts[first:], since)
            ConfigRevalidation.tally[outcome] += 1
            if expected == "commit" and since:
                kinds = set(since) - {"dependency"} or {"restored"}
                quiet = "prospective" if kinds == {"prospective"} else "_".join(sorted(kinds))
                ConfigRevalidation.tally[f"quiet_{quiet}"] += 1
        for attempt in op.attempts[first:]:
            if "recheck" in attempt:
                ConfigRevalidation.tally[f"recheck_{attempt['recheck']}"] += 1
        if not flight.held.done:
            flight.since = len(self.log)
            flight.key = self.key()
            flight.verify = any(a.get("recheck") == "failed" for a in op.attempts[first:]) or any(
                a.get("reason") == "rebase_failed" for a in op.attempts[first:]
            )
            return
        self.in_flight.remove(flight)
        try:
            self.run(flight.held.task)
        except RevisionConflict:
            ConfigRevalidation.tally["revision_conflict"] += 1
            return
        ConfigRevalidation.tally["committed"] += 1
        self.run(self.check_committed(op))
        self.log.append("turn")

    async def check_committed(self, op: Operation) -> None:
        """The reply passes the rules in force now (= at its commit), and the configuration
        the turn records is the configuration now on every dependency."""
        replies = staged_replies(op)
        assert not any(word in reply.split() for reply in replies for word in self.banned())
        context = await guard_context(self.engine, self.story)
        assert await output_passes(self.lib.models, replies, context, budget=TurnBudget())
        async with self.engine.connect() as conn:
            now = await config_dependencies.capture(
                conn, self.story, config_dependencies.LITE_DEPENDENCIES
            )
        assert (
            config_dependencies.changed(
                op.config_revisions, now, config_dependencies.LITE_DEPENDENCIES
            )
            == ()
        )

    # -- interpretive changes on the turn's dependencies --------------------------------------

    @rule(chosen=WORD_SETS)
    def story_policy(self, chosen: list[str]) -> None:
        item = story_item(words(*chosen), id=self.story_policy_id, name=STORY_POLICY)
        self.run(self.lib.policies_set_story(AUTHOR, self.story, [item]))
        if chosen != self.story_words:
            self.story_words = chosen
            self.story_version += 1
            self.changed("dependency")

    @rule()
    def rating_guardrails(self) -> None:
        self.rating_on = not self.rating_on
        self.run(
            self.rating_policies(self.rating, [self.rating_policy_id] if self.rating_on else [])
        )
        self.changed("dependency")

    @rule(chosen=WORD_SETS)
    def rating_policy_words(self, chosen: list[str]) -> None:
        self.run(
            self.lib.policies_update(
                OPERATOR,
                self.rating_policy_id,
                PolicyReplacement.model_validate(
                    {
                        "name": f"cr-{new_ulid().lower()}",
                        "applies_to": ["text_output"],
                        "rule": words(*chosen),
                        "age_condition": None,
                        "message": "Not at this rating.",
                    }
                ),
            )
        )
        self.rating_words = chosen
        self.rating_version += 1
        # The policy is in the story's effective set only while its rating lists it.
        self.changed("dependency" if self.rating_on else "elsewhere")

    @rule()
    def threshold(self) -> None:
        current = self.run(read_settings(self.engine)).minor_age_threshold
        wanted = self.start.minor_age_threshold + (current == self.start.minor_age_threshold)
        self.run(apply_settings(self.engine, {"minor_age_threshold": wanted}, principal="p-op"))
        self.threshold_raised = not self.threshold_raised
        self.changed("dependency")

    # -- changes that must not touch a turn in flight -----------------------------------------

    @rule(guidance=st.sampled_from(["calm", "brisk"]))
    def other_rating(self, guidance: str) -> None:
        """Interpretive, but on a rating the story does not use."""
        self.run(self.rating_policies(self.other, [], guidance=f"{guidance} {new_ulid()}"))
        self.changed("elsewhere")

    @rule(deadline=st.sampled_from([30, 40]))
    def story_deadline(self, deadline: int) -> None:
        self.run(
            update_story_settings(
                self.engine, self.story, {"turn_deadline": deadline}, principal="p-author"
            )
        )
        self.changed("prospective")

    @rule(hours=st.sampled_from([12, 48]))
    def idempotency_window(self, hours: int) -> None:
        self.run(
            apply_settings(self.engine, {"idempotency_result_window": hours}, principal="p-op")
        )
        self.changed("prospective")

    async def rating_policies(self, rating: Any, policy_ids: list[str], guidance: str = "") -> None:
        await update_rating(
            self.engine,
            rating.id,
            RatingReplacement(
                name=rating.name,
                description="",
                display_order=rating.display_order,
                prompt_guidance=guidance,
                image_guidance="",
                policy_ids=policy_ids,
                inherits_from=[],
            ),
            principal="p-operator",
        )

    def teardown(self) -> None:
        try:
            for flight in list(self.in_flight):
                while not flight.held.done:
                    self.run(flight.held.release())
                with contextlib.suppress(RevisionConflict):
                    self.run(flight.held.task)
            restore = self.start.as_settings()
            self.run(
                apply_settings(
                    self.engine,
                    {
                        "minor_age_threshold": restore.minor_age_threshold,
                        "idempotency_result_window": restore.idempotency_result_window,
                    },
                    principal="p-op",
                )
            )
        finally:
            self.run(self.engine.dispose())
            self.loop.close()


@pytest.mark.part("M2", "FR-CONC-14..16")
def test_config_revalidation(pg_url: str) -> None:
    """For any interleaving of turns with configuration changes, every committed turn passed
    its policy checks under the configuration it records, which is current on every
    dependency at its commit; interpretive changes on its dependencies re-check it, and a
    re-check that fails regenerates.

    Complements asserted by count: prospective changes, and interpretive changes elsewhere,
    happened while turns were held and never caused a re-check, regeneration or conflict."""
    ConfigRevalidation.url = pg_url
    ConfigRevalidation.tally = Counter()
    run_state_machine_as_test(  # type: ignore[no-untyped-call]
        ConfigRevalidation,
        settings=settings(
            max_examples=100,
            stateful_step_count=40,
            deadline=None,
            derandomize=True,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
        ),
    )
    seen = ConfigRevalidation.tally
    for name in (
        "commit",
        "recheck",
        "regenerate",
        "committed",
        "recheck_passed",
        "recheck_failed",
        "quiet_prospective",
        "stubborn",
    ):
        assert seen[name] > 0, (name, seen)
    assert sum(n for k, n in seen.items() if k.startswith("quiet_") and "elsewhere" in k), seen
    print(f"CONFIG-REVALIDATION {dict(seen)}")
