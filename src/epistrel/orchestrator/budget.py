"""The model-call budget and deadline a turn runs under (NFR-PERF-6,7, NFR-COST-1; #69).

``TurnBudget(max_calls, deadline_s, reserved)`` counts every model call the Model Access Layer
makes for a turn, and bounds the turn by a wall-clock deadline on the monotonic clock. ``None``
means unbounded. A call the budget cannot afford is not made. Each call's timeout is clamped to
the time left (``clamp``: ``min(call_timeout, remaining)``), so no call outlives the turn.

**Reservation.** A Lite turn reserves, before any call, its one generation call and the
model-assisted guardrail calls G (``reserved``: ``guardrail_input``, one per model-assisted input
policy for the turn's input, and ``guardrail_output``, one per model-assisted output policy for
the first output check). Those slots are *held*: only a call's first attempt may spend one
(``ModelAccessLayer.send`` claims it), and a retry (a #53 structured retry, the empty-reply
retry) needs a call from the unreserved remainder (``can_afford_unreserved``). Work the plan did
not reserve (a guardrail regeneration, a #47/#64 revalidation regeneration) first checks that
the remainder covers it and then ``hold``s it. Unused slots are released (``release_to``) after
the input gate, after the first generation, and after the first output check.

**Planning** (``plan_lite_budget``): the cap is the request's ``budget.max_calls``, else the
story's ``max_model_calls_per_turn``, else the deployment's, else the Lite formula
``lite_default_calls(G)`` = 4 + 5 (one primary) + G (NFR-PERF-6 with one primary and no staged
outcome, so a deployment's cap is not surprising when M4 arrives). A request override above
the story's cap, or a ``deadline_s`` above its ``turn_deadline``, is 422 ``validation_error``.
A cap below ``1 + G`` is 422 ``budget_below_safety_minimum`` with ``minimum_required``,
``max_calls`` and ``reserved``, raised before any model call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from epistrel.core.errors import BudgetBelowSafetyMinimum, InvalidRequest
from epistrel.core.settings_schema import (
    DEFAULT_TURN_DEADLINE_S,
    LITE_BASE_CALLS,
    MAX_CALLS_PER_TURN,
    MIN_CALLS_PER_TURN,
    MIN_DEADLINE_S,
)

GUARDRAIL_INPUT = "guardrail_input"
GUARDRAIL_OUTPUT = "guardrail_output"


def lite_default_calls(guardrail_calls: int) -> int:
    """The Lite default ``max_model_calls_per_turn``: ``4 + 5 + G`` (one primary, O = 0)."""
    return LITE_BASE_CALLS + guardrail_calls


@dataclass
class TurnBudget:
    """Calls remaining, the reserved guardrail calls, and the deadline for one turn (or one
    gated operation)."""

    max_calls: int | None = None
    deadline_s: float | None = None
    started: float = field(default_factory=time.monotonic)
    used: int = 0
    reserved: dict[str, int] = field(default_factory=dict)
    """The guardrail calls G reserved at planning: ``guardrail_input``, ``guardrail_output``."""
    held: int = 0
    """Reserved slots not yet spent: only a first attempt may spend one."""

    def __post_init__(self) -> None:
        if self.max_calls is not None and self.max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise ValueError("deadline_s must be > 0")

    @property
    def remaining_calls(self) -> int | None:
        return None if self.max_calls is None else max(0, self.max_calls - self.used)

    @property
    def guardrail_calls(self) -> int:
        """G: every reserved guardrail call."""
        return sum(self.reserved.values())

    @property
    def minimum_required(self) -> int:
        """One generation call plus G (the Lite safety minimum)."""
        return 1 + self.guardrail_calls

    def can_afford(self, calls: int = 1) -> bool:
        """Whether ``calls`` more calls fit, reserved slots included (a first attempt)."""
        remaining = self.remaining_calls
        return remaining is None or remaining >= calls

    def can_afford_unreserved(self, calls: int = 1) -> bool:
        """Whether ``calls`` more calls fit without touching a held reserved slot."""
        remaining = self.remaining_calls
        return remaining is None or remaining - self.held >= calls

    def hold(self, calls: int) -> None:
        """Reserve ``calls`` more slots (unplanned work whose affordability was checked)."""
        self.held += max(0, calls)

    def release_to(self, calls: int) -> None:
        """Release held slots down to ``calls`` (a phase finished without spending them)."""
        self.held = min(self.held, max(0, calls))

    def debit(self, *, claim: bool = True) -> None:
        """Count one call; ``claim``: a first attempt, which spends a held slot if any."""
        self.used += 1
        if claim and self.held > 0:
            self.held -= 1

    def refund(self) -> None:
        """Undo a debit for a call the adapter refused before sending anything."""
        self.used = max(0, self.used - 1)

    def remaining_s(self) -> float | None:
        """Seconds until the deadline (negative once passed); ``None`` when unbounded."""
        if self.deadline_s is None:
            return None
        return self.started + self.deadline_s - time.monotonic()

    def expired(self) -> bool:
        remaining = self.remaining_s()
        return remaining is not None and remaining <= 0

    def clamp(self, timeout: float) -> tuple[float, str]:
        """A call's timeout and what bounds it: ``(timeout, "timeout")``, or the time left and
        ``"deadline"`` when the turn ends sooner."""
        remaining = self.remaining_s()
        if remaining is None or remaining >= timeout:
            return timeout, "timeout"
        return max(0.0, remaining), "deadline"

    def elapsed_ms(self) -> int:
        return max(0, round((time.monotonic() - self.started) * 1000))

    def report(self) -> dict[str, Any]:
        """``{max_calls, reserved, used}`` as the turn report carries it."""
        return {
            "max_calls": self.max_calls,
            "reserved": {
                GUARDRAIL_INPUT: self.reserved.get(GUARDRAIL_INPUT, 0),
                GUARDRAIL_OUTPUT: self.reserved.get(GUARDRAIL_OUTPUT, 0),
            },
            "used": self.used,
        }


def effective_max_calls(
    story_max_calls: int | None, deployment_max_calls: int | None, guardrail_calls: int
) -> tuple[int, str]:
    """The story's cap and where it comes from: ``story``, ``deployment`` or ``formula``."""
    if story_max_calls is not None:
        return story_max_calls, "story"
    if deployment_max_calls is not None:
        return deployment_max_calls, "deployment"
    return lite_default_calls(guardrail_calls), "formula"


def plan_lite_budget(
    *,
    story_max_calls: int | None,
    deployment_max_calls: int | None,
    turn_deadline: float = DEFAULT_TURN_DEADLINE_S,
    guardrail_input: int,
    guardrail_output: int,
    override_max_calls: int | None = None,
    override_deadline_s: float | None = None,
    started: float | None = None,
) -> TurnBudget:
    """The budget a Lite turn runs under; see the module docstring. Raises before any call."""
    reserved = {GUARDRAIL_INPUT: guardrail_input, GUARDRAIL_OUTPUT: guardrail_output}
    cap, _ = effective_max_calls(
        story_max_calls, deployment_max_calls, guardrail_input + guardrail_output
    )
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
    budget = TurnBudget(
        max_calls=max_calls,
        deadline_s=deadline_s,
        started=time.monotonic() if started is None else started,
        reserved=reserved,
    )
    minimum = budget.minimum_required
    if minimum > max_calls:
        raise BudgetBelowSafetyMinimum(
            f"the turn's model budget of {max_calls} calls cannot cover the {minimum} calls it "
            "must reserve (one generation plus the model-assisted guardrail checks)",
            {"minimum_required": minimum, "max_calls": max_calls, "reserved": reserved},
        )
    budget.held = minimum
    return budget


__all__ = [
    "DEFAULT_TURN_DEADLINE_S",
    "GUARDRAIL_INPUT",
    "GUARDRAIL_OUTPUT",
    "MAX_CALLS_PER_TURN",
    "MIN_CALLS_PER_TURN",
    "MIN_DEADLINE_S",
    "TurnBudget",
    "effective_max_calls",
    "lite_default_calls",
    "plan_lite_budget",
]
