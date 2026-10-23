"""Planning a structured turn under its budget (NFR-PERF-6,7, FR-VERIFY-13,14, FR-PROF-12; #156).

``plan_turn(request, effective, …)`` is the turn's plan before any model call: one primary
render in M4, ``beats_planned`` (the profile's ``render.max_beats``), the safety reservation
(``core.profiles.reservation``: B one extraction per planned render, C the composed message's
detection, G the model-assisted guardrail calls) and the optional work the profile turns on,
most important first (salience, entailment, the follow-up render, a model repair, the polish).
``minimum`` is ``1 + B + C + G``: one model-assisted policy on input and output makes it
exactly 5 (render, extraction, composed-message detection, input check, output check).

``plan_budget`` turns the plan into the turn's ``TurnBudget``: the cap is the request's
``budget.max_calls`` (never above the story's), else the story's ``budgets.model_calls`` from
the configuration captured at the turn's start, else NFR-PERF-6's formula ``4 + 5 per primary
+ G`` (as Lite's, #69). A cap below the minimum is 422 ``budget_below_safety_minimum`` with
``minimum_required``, ``max_calls``, ``reserved`` (#69's guardrail keys) and ``reservation``,
before any model call and with nothing recorded. The render and G are held in #69's pool (spent
in order, before any optional work can run); B and C are owner-only named slots (``TurnBudget``):
an ``extraction_backstop`` call spends B, the composed message's detection C, and nothing else
may. Unused B is released once the renders are detected, C when composition uses the
deterministic assembly. A narrator render (its triggers are read with the turn) reserves its
render and its extraction from the unreserved remainder when it is triggered; when it cannot,
the narrator is skipped (``narrator_skipped``).

**Optional work** asks ``TurnBudget.try_optional`` (or the adapter refuses its first attempt):
only what no reservation holds, never below the minimum, and only while it could start before
the deadline (``latency_estimate_s``, ``DEFAULT_LATENCY_ESTIMATE_S``). The degradation on
exhaustion follows NFR-PERF-7's order: ``skip_polish`` → ``repair_deterministic`` (or
``safe_fallback``) → ``entailment_safe_disposition`` → ``beat_replaced_safe_fallback`` (a render
whose reserved extraction cannot start before the deadline is replaced by the safe fallback
before anything it said is detected or committed). ``salience_skipped`` and
``narrator_skipped`` are reported beside them. A turn's ``degradation`` lists the ladder's steps
first, in that order (``ladder_rank``), then every other step in the order it was taken.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import BudgetBelowSafetyMinimum, InvalidRequest
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.effective import StoryProfile
from epistrel.core.profiles.reservation import Reservation, reservation
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.orchestrator.budget import (
    COMPOSED,
    ENTAILMENT,
    EXTRACTION,
    FOLLOW_UP,
    GUARDRAIL_INPUT,
    GUARDRAIL_OUTPUT,
    OPTIONAL_PRIORITY,
    POLISH,
    REPAIR,
    SALIENCE,
    TESTIMONY,
    TurnBudget,
    lite_default_calls,
)
from epistrel.orchestrator.lite.cast import ResolvedCast
from epistrel.orchestrator.policies.input_gate import input_policies, model_assisted
from epistrel.orchestrator.policies.output_gate import output_reservation
from epistrel.orchestrator.policies.store import guard_context

PRIMARIES = 1
"""Primary renders a structured turn plans in M4 (one character renders)."""
DEFAULT_LATENCY_ESTIMATE_S = 2.0
"""How long a reserved or optional call is assumed to take: one that could not start before
``deadline - this`` is unaffordable (#156's default, per purpose)."""

SKIP_POLISH = "skip_polish"
REPAIR_DETERMINISTIC = "repair_deterministic"
SAFE_FALLBACK = "safe_fallback"
ENTAILMENT_SAFE_DISPOSITION = "entailment_safe_disposition"
BEAT_REPLACED = "beat_replaced_safe_fallback"
SALIENCE_SKIPPED = "salience_skipped"
NARRATOR_SKIPPED = "narrator_skipped"
TESTIMONY_DEFAULTED = "testimony_defaulted"
"""NFR-PERF-7 step 3 (#162): a salient witness's testimony adjudication the budget (or the
per-turn cap) could not fit, decided by the rules' default instead."""
ADJUDICATION_UNAVAILABLE = "adjudication_unavailable"
"""#162: a testimony adjudication whose model failed; the rules' default decided it."""
LADDER = (
    SKIP_POLISH,
    REPAIR_DETERMINISTIC,
    TESTIMONY_DEFAULTED,
    ENTAILMENT_SAFE_DISPOSITION,
    BEAT_REPLACED,
)
"""NFR-PERF-7's order as M5 has it: what goes first when the budget or the deadline runs
short (``safe_fallback`` stands in for ``repair_deterministic`` when no deterministic rung
applied)."""


@dataclass(frozen=True, slots=True)
class TurnPlan:
    """A structured turn's plan, before any model call."""

    primaries: int
    beats_planned: int
    reservation: Reservation
    optional: tuple[str, ...]

    @property
    def minimum(self) -> int:
        """The single-primary minimum: ``1 + B + C + G``."""
        return self.primaries + self.reservation.total

    def payload(self) -> dict[str, Any]:
        return {
            "primaries": self.primaries,
            "beats_planned": self.beats_planned,
            "reservation": {
                "extraction": self.reservation.extraction,
                "composed": self.reservation.composed,
                "guardrail": self.reservation.guardrail,
            },
            "optional": list(self.optional),
            "minimum": self.minimum,
        }


def adjudicates(settings: ProfileSettings) -> bool:
    """Whether a turn under ``settings`` may adjudicate testimony with a model (#162): per-
    character partitions with ``character_knowledge.testimony = model`` delivered, and model
    salience (under ``commitment.salience = rules``, or with commitment off, salience is
    rule-based and adjudication is never called)."""
    from epistrel.core.profiles import schema

    return (
        module_on(settings, "partitions")
        and schema.level_of(settings, "testimony") == "model"
        and schema.is_delivered("testimony", "model")
        and settings.commitment.enabled
        and settings.commitment.salience != "rules"
    )


def optional_work(settings: ProfileSettings) -> tuple[str, ...]:
    """The optional work ``settings`` turns on, most important first."""
    found: list[str] = []
    commitment = settings.commitment.enabled
    if commitment and settings.commitment.salience != "rules":
        found.append(SALIENCE)
    if settings.verification.level == "entailment":
        found.append(ENTAILMENT)
    if commitment:
        found.append(FOLLOW_UP)
    if adjudicates(settings):
        found.append(TESTIMONY)
    if module_on(settings, "repair"):
        found.append(REPAIR)
    if settings.repair.polish.enabled:
        found.append(POLISH)
    return tuple(k for k in OPTIONAL_PRIORITY if k in found)


def plan_turn(
    request: Any,
    effective: StoryProfile,
    *,
    guardrail_input: int,
    guardrail_output: int,
) -> TurnPlan:
    """The turn's plan under ``effective`` (the configuration captured at its start); the
    request names no more than one primary in M4 (``resolve_structured_cast``)."""
    del request  # M4 plans one primary whatever the request names (#150's cast check)
    settings = effective.settings
    return TurnPlan(
        primaries=PRIMARIES,
        beats_planned=settings.render.max_beats,
        reservation=reservation(
            settings, guardrail_input=guardrail_input, guardrail_output=guardrail_output
        ),
        optional=optional_work(settings),
    )


def default_calls(plan: TurnPlan) -> int:
    """NFR-PERF-6's default cap for the plan: ``4 + 5 per primary + G`` (#69's formula)."""
    return lite_default_calls(plan.reservation.guardrail)


def plan_budget(
    plan: TurnPlan,
    *,
    story_max_calls: int | None,
    turn_deadline: float,
    override_max_calls: int | None = None,
    override_deadline_s: float | None = None,
    started: float | None = None,
    latency_estimate_s: float | None = None,
) -> TurnBudget:
    """The budget a structured turn runs under (see the module docstring); raises before any
    call."""
    found = plan.reservation
    cap = story_max_calls if story_max_calls is not None else default_calls(plan)
    max_calls = cap
    if override_max_calls is not None:
        if override_max_calls > cap:
            raise InvalidRequest(
                f"budget.max_calls may not exceed the story's cap of {cap} calls per turn",
                {"field": "body.budget.max_calls", "max_calls": cap},
            )
        max_calls = override_max_calls
    deadline_s = turn_deadline
    if override_deadline_s is not None:
        if override_deadline_s > turn_deadline:
            raise InvalidRequest(
                f"budget.deadline_s may not exceed the story's turn_deadline of {turn_deadline} s",
                {"field": "body.budget.deadline_s", "turn_deadline": turn_deadline},
            )
        deadline_s = override_deadline_s
    reserved = {GUARDRAIL_INPUT: found.guardrail_input, GUARDRAIL_OUTPUT: found.guardrail_output}
    if plan.minimum > max_calls:
        raise BudgetBelowSafetyMinimum(
            f"the turn's model budget of {max_calls} calls cannot cover the {plan.minimum} calls "
            "it must reserve (its render, the claim detection of its render and its composed "
            "message, and the model-assisted guardrail checks)",
            {
                "minimum_required": plan.minimum,
                "max_calls": max_calls,
                "reserved": reserved,
                "reservation": plan.payload()["reservation"],
            },
        )
    budget = TurnBudget(
        max_calls=max_calls,
        deadline_s=deadline_s,
        started=time.monotonic() if started is None else started,
        reserved=reserved,
        latency_estimate_s=(
            DEFAULT_LATENCY_ESTIMATE_S if latency_estimate_s is None else latency_estimate_s
        ),
    )
    budget.held = plan.primaries + found.guardrail
    budget.reserve(EXTRACTION, found.extraction)
    budget.reserve(COMPOSED, found.composed)
    if SALIENCE in plan.optional:
        budget.pending[SALIENCE] = plan.primaries
    return budget


async def plan_structured(
    db: AsyncEngine,
    story_id: str,
    cast: ResolvedCast,
    request: Any,
    configuration: StoryProfile,
    *,
    story_max_calls: int | None,
    turn_deadline: float,
    started: float,
    override: Any = None,
) -> tuple[TurnPlan, TurnBudget]:
    """The turn's plan and budget: G is one call per model-assisted input policy for the
    actor's input plus one per model-assisted output policy for the first output checkpoint
    (#69's count). Raises 422 ``budget_below_safety_minimum`` before any model call."""
    context = await guard_context(db, story_id)
    plan = plan_turn(
        request,
        configuration,
        guardrail_input=len(model_assisted(input_policies(context, actor=cast.actor.id))),
        guardrail_output=output_reservation(context, cast.render_ids),
    )
    budget = plan_budget(
        plan,
        story_max_calls=story_max_calls,
        turn_deadline=turn_deadline,
        override_max_calls=None if override is None else override.max_calls,
        override_deadline_s=None if override is None else override.deadline_s,
        started=started,
    )
    return plan, budget


def reserve_narrator(budget: TurnBudget, plan: TurnPlan) -> bool:
    """A triggered narrator render: its render and (with extraction on) its extraction,
    reserved from the unreserved remainder; ``False`` when they do not fit
    (``narrator_skipped``)."""
    extraction = 1 if plan.reservation.extraction else 0
    if budget.expired() or not budget.can_afford_unreserved(1 + extraction):
        return False
    budget.hold(1)
    budget.reserve(EXTRACTION, extraction)
    return True


def budget_report(plan: TurnPlan | None, budget: TurnBudget) -> dict[str, Any]:
    """``turn_report.budget`` of a structured turn: #69's ``{max_calls, reserved, used}`` with
    the plan's ``minimum``, the named reservation still unspent and released, the calls by
    kind, and the plan."""
    found = budget.report()
    if plan is None:
        return found
    return {
        **found,
        "minimum": plan.minimum,
        "reservation": plan.payload()["reservation"],
        "released": budget.released,
        "used_by_kind": dict(sorted(budget.used_by_kind.items())),
        "plan": plan.payload(),
    }


RANK = {
    SKIP_POLISH: 0,
    REPAIR_DETERMINISTIC: 1,
    SAFE_FALLBACK: 1,
    TESTIMONY_DEFAULTED: 2,
    ENTAILMENT_SAFE_DISPOSITION: 3,
    BEAT_REPLACED: 4,
}


def ladder_rank(step: str) -> int:
    """Where ``step`` sits in NFR-PERF-7's order (every other step after the ladder)."""
    return RANK.get(step, len(LADDER))


def ladder_steps(steps: Sequence[str]) -> list[str]:
    """The NFR-PERF-7 steps among ``steps``, in the ladder's order."""
    return sorted((s for s in steps if s in RANK), key=ladder_rank)


__all__ = [
    "ADJUDICATION_UNAVAILABLE",
    "BEAT_REPLACED",
    "DEFAULT_LATENCY_ESTIMATE_S",
    "ENTAILMENT_SAFE_DISPOSITION",
    "LADDER",
    "NARRATOR_SKIPPED",
    "PRIMARIES",
    "REPAIR",
    "REPAIR_DETERMINISTIC",
    "SAFE_FALLBACK",
    "SALIENCE_SKIPPED",
    "SKIP_POLISH",
    "TESTIMONY_DEFAULTED",
    "TurnPlan",
    "adjudicates",
    "budget_report",
    "default_calls",
    "ladder_rank",
    "ladder_steps",
    "optional_work",
    "plan_budget",
    "plan_structured",
    "plan_turn",
    "reserve_narrator",
]
