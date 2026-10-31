"""E-09: time estimation (docs/04 §8; FR-CLOCK-7,8, NFR-QUAL-9; #201).

**Estimates.** Each case of ``evals/sets/time_estimation/cases.jsonl`` is one narrated skip with
what #199's ``time_estimation`` purpose receives for it (``narrated_text``, ``activity_context``,
``established_times``) and a human-judged acceptable range (``acceptable_minutes``). The case
runs the live path's own call, ``orchestrator.structured.time_estimation.estimate``, with the
route the suite's fixture story resolves for the purpose (a scratch story on the seeded
``Structured`` profile with ``clock.level = on``, ``CLOCK_ON``; built once in ``setup``). No
render is generated, so only ``time_estimation`` reaches the run model.

An estimate is **in range** when the engine took it and its minutes lie in the range. An
estimate the live path would not take counts as out of range, as a turn would remove the skip
(NFR-PERF-7 (5)): a model error, an unparsable answer, 0 minutes, or a confidence under the
floor. The floor is the live path's: ``core.world_clock.skips.MIN_CONFIDENCE`` (0.3), and the
structured layer's ``uncertain_below`` deployment setting (default 0.5) before it, which turns
any answer below it into an Uncertain. At default settings the effective floor is therefore 0.5;
the suite measures exactly what a turn would keep.

- ``nfr_qual_9.within_range`` (rate, ≥ 85 %; the mean over seeds clears it by more than one
  standard deviation).
- ``e09.family.<family>`` and ``e09.kind.<kind>`` (report-only): the same per activity family
  and per kind of case (``explicit``, ``implicit``, ``inferred``; the first word of its notes).

**Pacing health** (the owner's decision of 2026-10-30; informational, no floor). The suite also
plays a seeded sample of ``PACING_CASES`` scenarios of the approved ``evals/sets/scenarios`` set
(``scenario_play``: E-12's stories) with the clock on, bounded further by ``--sample``
(``companions``), and reports ``e09.model_initiated_skips_removed_per_100_turns``: the turn
reports' ``repairs`` entries tagged ``unprompted_skip`` (#199: a model-initiated skip FR-CLOCK-8
refused) per turn played, shown as a percentage (removals per 100 turns). On the stub the
scenarios' scripted renders make it deterministic.

The stub's answer is scripted per case (``stub_answer``: the range's midpoint at confidence
0.9), so CI's ``--fail-on-gate`` stub run gates on the harness.

**The clock module** is #203's to deliver (``core.profiles.schema.DELIVERED``). Until then the
suite delivers it for its own run (``ClockDelivery``, opened in ``setup`` and closed in
``teardown``), as #179's zero-leak suites delivered the M5 modules until #181; once the clock is
delivered the patch does nothing, and #203 removes it.
"""

from __future__ import annotations

import json
import random
import types
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError, model_validator

from epistrel.core.profiles import schema
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.states.effects import UNPROMPTED_SKIP
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.evals import scenes
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import (
    Aggregate,
    CaseModel,
    MalformedCase,
    Outcome,
    Suite,
    Summary,
    Value,
    register,
)
from epistrel.evals.suites.scenario_play import ScenarioCase, play, script
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.config import Route, routes_of
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    TIME_ESTIMATION,
    TIME_ESTIMATION_MAX_TIMES,
    TIME_ESTIMATION_TEXT_MAX,
    TimeEstimate,
)
from epistrel.orchestrator.structured import time_estimation

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext, Scratch
    from epistrel.orchestrator.models.stub import StubAdapter

FAMILIES = ("meal", "movie", "drive", "flight", "sleep", "wait", "work-shift")
KINDS = ("explicit", "implicit", "inferred")
WITHIN = "nfr_qual_9.within_range"
PACING = "e09.model_initiated_skips_removed_per_100_turns"
CLOCK_ON: Mapping[str, Any] = {"clock": {"level": "on"}}
"""The settings merge patch the suite's stories run with (the clock on, FR-CLOCK-7)."""
PACING_SET = "scenarios/cases.jsonl"
PACING_CASES = 10
"""The scenarios the pacing-health sample plays (fewer when ``--sample`` is smaller)."""
PACING_SEED = 201
PACING_PREFIX = "pacing-"
CALL_DEADLINE_S = 300.0


CLOCK_LEVELS = frozenset({"prompt_only", schema.ON})
"""The ``clock`` levels #203 delivers."""


class ClockDelivery:
    """The ``clock`` module delivered for a run until #203 delivers it (module docstring):
    ``open`` in ``Suite.setup``, ``close`` in ``Suite.teardown``; nothing when it is delivered."""

    def __init__(self) -> None:
        self._before: Mapping[str, frozenset[str]] | None = None

    def open(self) -> None:
        if self._before is not None or schema.is_delivered("clock", schema.ON):
            return
        self._before = schema.DELIVERED
        schema.DELIVERED = types.MappingProxyType({**schema.DELIVERED, "clock": CLOCK_LEVELS})

    def close(self) -> None:
        if self._before is not None:
            schema.DELIVERED = self._before
            self._before = None


def family_id(family: str) -> str:
    return f"e09.family.{family.replace('-', '_')}"


def kind_id(kind: str) -> str:
    return f"e09.kind.{kind}"


class TimeEstimationCase(CaseModel):
    """One labeled narrated skip (the authoring module's docstring)."""

    family: Literal["meal", "movie", "drive", "flight", "sleep", "wait", "work-shift"]
    narrated_text: str = Field(min_length=1, max_length=TIME_ESTIMATION_TEXT_MAX)
    activity_context: str = Field(default="", max_length=TIME_ESTIMATION_TEXT_MAX)
    established_times: list[str] = Field(default_factory=list, max_length=TIME_ESTIMATION_MAX_TIMES)
    acceptable_minutes: tuple[int, int]
    notes: str = Field(pattern=r"^(explicit|implicit|inferred): ")
    stub_answer: TimeEstimate

    @model_validator(mode="after")
    def _range(self) -> TimeEstimationCase:
        lo, hi = self.acceptable_minutes
        if not 0 < lo <= hi:
            raise ValueError("acceptable_minutes must be [lo, hi] with 0 < lo <= hi")
        return self

    @property
    def kind(self) -> str:
        return self.notes.split(":", 1)[0]

    def within(self, minutes: int) -> bool:
        lo, hi = self.acceptable_minutes
        return lo <= minutes <= hi


class PacingCase(CaseModel):
    """One scenario of the pacing-health sample (``companions``), under its own id."""

    scenario: ScenarioCase


def pacing_cases(sets_dir: Path, sample: int | None) -> list[CaseModel]:
    """A seeded sample of ``PACING_CASES`` scenarios (at most ``sample``), as ``PacingCase``s."""
    path = sets_dir / PACING_SET
    if not path.is_file():
        raise MalformedCase(f"e09: no scenario set at {path}")
    scenarios: list[ScenarioCase] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            scenarios.append(ScenarioCase.model_validate(json.loads(line)))
        except (ValueError, ValidationError) as exc:
            raise MalformedCase(f"e09: {path.name} line {number}: {exc}") from None
    count = PACING_CASES if sample is None else min(PACING_CASES, sample)
    ordered = sorted(scenarios, key=lambda s: s.id)
    chosen = random.Random(PACING_SEED).sample(ordered, min(count, len(ordered)))  # noqa: S311 - a reproducible sample
    return [
        PacingCase(id=f"{PACING_PREFIX}{s.id}", scenario=s)
        for s in sorted(chosen, key=lambda s: s.id)
    ]


@register
class E09Suite(Suite):
    name = "e09"
    title = "E-09: time estimation (narrated skips within the acceptable range)"
    set_path = "time_estimation/cases.jsonl"
    min_cases = 200
    guarantees = ("time_estimation",)
    ci_gate = True
    case_model = TimeEstimationCase
    dials = CLOCK_ON
    profile_label = "Structured, clock on"
    metrics = (
        Metric(
            WITHIN,
            Rule.rate,
            0.85,
            Direction.at_least,
            "the estimate the engine took lies in the acceptable range",
        ),
        *(
            Metric(family_id(f), Rule.informational, description=f"{f} skips in range")
            for f in FAMILIES
        ),
        *(Metric(kind_id(k), Rule.informational, description=f"{k} skips in range") for k in KINDS),
        Metric(
            PACING,
            Rule.informational,
            description="model-initiated skips removed (FR-CLOCK-8) per 100 turns played",
        ),
    )
    reading_notes = (
        "E-09 runs the live path's `time_estimation` call per case, with the fixture story's "
        "route and no render. An estimate the engine would not take counts as out of range: a "
        "model error, an unparsable answer, 0 minutes, or a confidence under the floor (the "
        "structured layer's `uncertain_below`, 0.5 by default, then `MIN_CONFIDENCE`, 0.3).",
        "`e09.model_initiated_skips_removed_per_100_turns` is pacing health, informational: "
        "the `unprompted_skip` repairs per turn over a seeded sample of the `scenarios` set "
        "played with the clock on, shown as a percentage (removals per 100 turns).",
    )

    def __init__(self) -> None:
        self.scratch: Scratch | None = None
        self.route: Route | None = None
        self.delivery = ClockDelivery()

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        from epistrel.evals.runner import Scratch

        self.delivery.open()
        self.scratch = Scratch(engine, "e09-clock")
        created = await self.scratch.create(
            StorySpec(
                characters=[
                    CharacterSpec(
                        name="Cal", definition="A traveller.", true_age=30, is_player=True
                    )
                ]
            )
        )
        await scenes.structured(engine, ctx, created.story_id, CLOCK_ON)
        async with engine.db.connect() as conn:
            profile = await effective_configuration(conn, created.story_id)
        self.route = routes_of(profile.settings).get(time_estimation.TIME_ESTIMATION_PURPOSE)

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        try:
            if self.scratch is not None:
                await self.scratch.delete_all()
        finally:
            self.scratch, self.route = None, None
            self.delivery.close()

    def companions(self, sets_dir: Path, sample: int | None) -> list[CaseModel]:
        return pacing_cases(sets_dir, sample)

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        if isinstance(case, PacingCase):
            return await self._pacing(ctx, case)
        assert isinstance(case, TimeEstimationCase)  # noqa: S101 - load() validated it
        found = await time_estimation.estimate(
            ctx.models,
            narrated_text=case.narrated_text,
            activity_context=case.activity_context,
            established_times=case.established_times,
            budget=TurnBudget(deadline_s=CALL_DEADLINE_S),
            route=self.route,
        )
        within = found is not None and case.within(found.minutes)
        observations: dict[str, bool | None] = {
            WITHIN: within,
            family_id(case.family): within,
            kind_id(case.kind): within,
        }
        detail: dict[str, Any] = {
            "acceptable_minutes": list(case.acceptable_minutes),
            "estimate": None
            if found is None
            else {"minutes": found.minutes, "confidence": found.confidence},
        }
        output = "no estimate" if found is None else f"{found.minutes} min"
        return Outcome(observations, output=output, detail=detail)

    async def _pacing(self, ctx: CaseContext, case: PacingCase) -> Outcome:
        try:
            played = await play(ctx, case.scenario, CLOCK_ON)
        except ModelError as exc:
            # A turn that failed counts no turn and no removal: pacing health has no floor, and
            # an error is not a refused skip (and never a miss of the estimate floor).
            return Outcome({PACING: None}, detail={"pacing_error": exc.code})
        removed = sum(t.repairs.count(UNPROMPTED_SKIP) for t in played.turns)
        return Outcome(
            {PACING: None},
            output=f"{removed} removed in {len(played.turns)} turns",
            detail={"pacing_turns": len(played.turns), "pacing_removed": removed},
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        if isinstance(case, TimeEstimationCase):
            return {TIME_ESTIMATION.name: case.stub_answer.model_dump_json()}
        return {}

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        if isinstance(case, PacingCase):
            # The scenario's renders, scripted under the pacing case's own id.
            script(case.scenario.model_copy(update={"id": case.id}), stub)
            return
        super().script_stub(case, stub)

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        turns, removed = _pacing_totals(outcomes)
        return {PACING: Value(removed / turns if turns else None, turns)}

    def summarize(self, outcomes: Sequence[Outcome], model: str) -> Summary:
        turns, removed = _pacing_totals(outcomes)
        if not turns:
            return Summary(notes=("Pacing health: no scenario turn was played.",))
        rate = 100 * removed / turns
        return Summary(
            notes=(
                f"Pacing health (informational): {removed} model-initiated skips removed in "
                f"{turns} turns over every seed, {rate:.2f} per 100 turns.",
            )
        )


def _pacing_totals(outcomes: Sequence[Outcome]) -> tuple[int, int]:
    turns = sum(int(o.detail.get("pacing_turns", 0)) for o in outcomes)
    removed = sum(int(o.detail.get("pacing_removed", 0)) for o in outcomes)
    return turns, removed


__all__ = [
    "CLOCK_LEVELS",
    "CLOCK_ON",
    "FAMILIES",
    "KINDS",
    "PACING",
    "PACING_CASES",
    "WITHIN",
    "ClockDelivery",
    "E09Suite",
    "PacingCase",
    "TimeEstimationCase",
    "family_id",
    "kind_id",
    "pacing_cases",
]
