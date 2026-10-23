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
story's (its effective configuration's ``budgets.model_calls``, #113: its overrides, its
profile's version or the deployment's profile defaults), else the Lite formula
``lite_default_calls(G)`` = 4 + 5 (one primary) + G (NFR-PERF-6 with one primary and no staged
outcome, so a deployment's cap is not surprising when M4 arrives). A request override above
the story's cap, or a ``deadline_s`` above its turn deadline (``budgets.deadline_ms``), is 422
``validation_error``.
A cap below ``1 + G`` is 422 ``budget_below_safety_minimum`` with ``minimum_required``,
``max_calls`` and ``reserved``, raised before any model call.

**Embedding calls** (#141): a structured turn's retrieval embeds its query (and may re-embed
rehydrated memories in one batch); ``counted_embedder`` wraps the injected embedder so each
call is counted in ``TurnBudget.embedding_calls``, apart from the call cap.

**Named reservations** (#156, a structured turn; ``orchestrator.structured.planner`` plans
them). Beside the held pool, ``named`` holds owner-only slots: ``extraction`` (B, one per
planned render) and ``composed`` (C). Only their owner may spend one: a first attempt whose
purpose maps to that kind (``kind_of``: ``extraction_backstop`` is ``extraction``, or
``composed`` while ``charging(COMPOSED)``), and every other call sees them as spent
(``can_afford``, ``can_afford_unreserved``). A named slot the turn did not use is released
(``release_named``) and may then fund optional work. Optional work (``salience``,
``entailment``, ``follow_up``, ``testimony`` (#162), ``repair``, ``polish``, in that
priority) spends only what no reservation holds: the adapter refuses a first ``entailment``
or ``salience_detection`` attempt that would dip below the line, and the pipeline asks
``try_optional(kind, cost)`` before a follow-up render, a testimony adjudication, a model
repair or the polish. ``pending`` pre-allocates the
higher-priority salience calls so an earlier entailment call cannot starve them, and
``latency_estimate_s`` treats a call that could not start before ``deadline - estimate`` as
unaffordable (``can_start``). Once an optional kind is refused, no less important kind runs
after it in the turn (``denied``), so the degradation follows NFR-PERF-7's order. A Lite
budget has no named slots and behaves as #69 planned it.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
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

PER_PRIMARY_CALLS = 5
"""NFR-PERF-6's per-primary share of the default cap (``4 + 5 per primary + G``)."""
GUARDRAIL_INPUT = "guardrail_input"
GUARDRAIL_OUTPUT = "guardrail_output"

EXTRACTION = "extraction"
"""B: the planned renders' extraction calls (owner: ``extraction_backstop``)."""
COMPOSED = "composed"
"""C: the composed message's detection (owner: ``extraction_backstop`` while charging it)."""
SALIENCE = "salience"
DIRECTOR_ORDER = "director_order"
"""#174: the director's ordering of a sequential turn's primaries (one ``generation`` call)."""
DIRECTOR_TIEBREAK = "director_tiebreak"
"""#175: the director's judgment of a simultaneous turn's conflicting actions (one
``generation`` call)."""
ENTAILMENT = "entailment"
FOLLOW_UP = "follow_up"
TESTIMONY = "testimony"
"""#162: a salient witness's ``belief_adjudication`` call."""
REPAIR = "repair"
POLISH = "polish"
OPTIONAL_PRIORITY = (
    SALIENCE,
    DIRECTOR_ORDER,
    DIRECTOR_TIEBREAK,
    ENTAILMENT,
    FOLLOW_UP,
    TESTIMONY,
    REPAIR,
    POLISH,
)
"""Optional work, most important first: the pool is spent in this order (NFR-PERF-7 gives
polish up first, then repairs, then testimony adjudication, then entailment)."""
PURPOSE_KINDS = {
    "extraction_backstop": EXTRACTION,
    "entailment": ENTAILMENT,
    "belief_adjudication": TESTIMONY,
    "salience_detection": SALIENCE,
    "regeneration": REPAIR,
    "guardrail_check": "guardrail",
    "generation": "render",
}
"""What kind of call a purpose's call is, unless the caller is ``charging`` another kind."""


def lite_default_calls(guardrail_calls: int, primaries: int = 1) -> int:
    """The default ``max_model_calls_per_turn``: ``4 + 5 per primary + G`` (O = 0; one primary
    under Lite, #174's B under a sequential structured turn)."""
    return LITE_BASE_CALLS + PER_PRIMARY_CALLS * (max(1, primaries) - 1) + guardrail_calls


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
    embedding_calls: int = 0
    """The embedding calls the turn's retrievals made (a query, a rehydration batch; #141).
    Counted apart from ``used``: an embedding call is no model call of the cap (NFR-PERF-6
    counts generation-side calls), and it is reported under ``purpose=embedding`` and in
    ``model_calls`` like any other (#138)."""
    named: dict[str, int] = field(default_factory=dict)
    """Owner-only reservations not yet spent (#156): ``extraction`` (B), ``composed`` (C)."""
    pending: dict[str, int] = field(default_factory=dict)
    """Optional work pre-allocated by priority (#156): calls no lower-priority kind may take."""
    latency_estimate_s: float = 0.0
    """A call that could not start before ``deadline - this`` is unaffordable (#156)."""
    stage: str | None = None
    """The kind the current calls are charged to (``charging``), over ``PURPOSE_KINDS``."""
    used_by_kind: dict[str, int] = field(default_factory=dict)
    released: int = 0
    """Named slots released unspent (#156)."""
    denied: set[str] = field(default_factory=set)
    """Optional kinds the budget or the deadline refused this turn: no less important kind
    runs after one (NFR-PERF-7's order: polish goes before repairs, repairs before
    entailment)."""
    _claims: list[str | None] = field(default_factory=list, repr=False)

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

    @property
    def named_unspent(self) -> int:
        return sum(self.named.values())

    @property
    def reserved_unspent(self) -> int:
        """Every reserved slot not yet spent: the held pool and the named slots."""
        return self.held + self.named_unspent

    def can_afford(self, calls: int = 1) -> bool:
        """Whether ``calls`` more calls fit, held slots included (a first attempt); a named
        slot is only its owner's (``affordable``)."""
        remaining = self.remaining_calls
        return remaining is None or remaining - self.named_unspent >= calls

    def can_afford_unreserved(self, calls: int = 1) -> bool:
        """Whether ``calls`` more calls fit without touching any reserved slot."""
        remaining = self.remaining_calls
        return remaining is None or remaining - self.reserved_unspent >= calls

    def kind_of(self, purpose: str | None) -> str | None:
        """The kind a call of ``purpose`` is charged to."""
        if self.stage is not None:
            return self.stage
        return None if purpose is None else PURPOSE_KINDS.get(purpose, purpose)

    def _ahead_of(self, kind: str | None) -> int:
        """The optional calls pre-allocated to kinds more important than ``kind``."""
        if kind not in OPTIONAL_PRIORITY:
            return 0
        rank = OPTIONAL_PRIORITY.index(kind)
        return sum(self.pending.get(k, 0) for k in OPTIONAL_PRIORITY[:rank])

    def _denied_ahead(self, kind: str | None) -> bool:
        if kind not in OPTIONAL_PRIORITY:
            return False
        rank = OPTIONAL_PRIORITY.index(kind)
        return any(k in self.denied for k in OPTIONAL_PRIORITY[:rank])

    def _deny(self, kind: str | None) -> bool:
        if kind in OPTIONAL_PRIORITY:
            self.denied.add(kind)
        return False

    def can_start(self) -> bool:
        """Whether a call could still start and answer before the deadline (the latency
        estimate, #156); always while the turn has no deadline."""
        left = self.remaining_s()
        return left is None or left > self.latency_estimate_s

    def affordable(self, purpose: str | None, *, first: bool) -> bool:
        """Whether a call of ``purpose`` fits (the Model Access Layer asks before sending): a
        retry only from the unreserved remainder; a first attempt from its kind's named slot,
        else what no named slot holds (an optional kind also leaves the more important kinds'
        pre-allocation, and the latency estimate before the deadline)."""
        if not first:
            return self.can_afford_unreserved()
        kind = self.kind_of(purpose)
        remaining = self.remaining_calls
        if kind is not None and self.named.get(kind, 0) > 0:
            return remaining is None or remaining >= 1
        if kind in OPTIONAL_PRIORITY and (self._denied_ahead(kind) or not self.can_start()):
            return self._deny(kind)
        if remaining is None or remaining - self.named_unspent - self._ahead_of(kind) >= 1:
            return True
        return self._deny(kind)

    def hold(self, calls: int) -> None:
        """Reserve ``calls`` more slots (unplanned work whose affordability was checked)."""
        self.held += max(0, calls)

    def release_to(self, calls: int) -> None:
        """Release held slots down to ``calls`` (a phase finished without spending them)."""
        self.held = min(self.held, max(0, calls))

    def debit(self, *, claim: bool = True, purpose: str | None = None) -> None:
        """Count one call; ``claim``: a first attempt, which spends its kind's named slot, else
        its kind's pre-allocation, else a held slot if any."""
        self.used += 1
        kind = self.kind_of(purpose)
        if kind is not None:
            self.used_by_kind[kind] = self.used_by_kind.get(kind, 0) + 1
        spent: str | None = None
        if claim and kind is not None and self.named.get(kind, 0) > 0:
            self.named[kind] -= 1
            spent = f"named:{kind}"
        elif claim and kind is not None and self.pending.get(kind, 0) > 0:
            self.pending[kind] -= 1
            spent = f"pending:{kind}"
        elif claim and self.held > 0:
            self.held -= 1
            spent = "held"
        self._claims.append(spent)

    def refund(self, purpose: str | None = None) -> None:
        """Undo a debit for a call the adapter refused before sending anything."""
        self.used = max(0, self.used - 1)
        kind = self.kind_of(purpose)
        if kind is not None and self.used_by_kind.get(kind, 0) > 0:
            self.used_by_kind[kind] -= 1
        spent = self._claims.pop() if self._claims else None
        if spent == "held":
            self.held += 1
        elif spent is not None:
            pool, name = spent.split(":", 1)
            target = self.named if pool == "named" else self.pending
            target[name] = target.get(name, 0) + 1

    def reserve(self, kind: str, calls: int) -> None:
        """Add ``calls`` owner-only slots of ``kind`` (#156)."""
        if calls > 0:
            self.named[kind] = self.named.get(kind, 0) + calls

    def release_named(self, kind: str, calls: int | None = None) -> int:
        """Release up to ``calls`` (every one when ``None``) unspent ``kind`` slots; answers
        how many were released."""
        have = self.named.get(kind, 0)
        dropped = have if calls is None else min(have, max(0, calls))
        if dropped:
            self.named[kind] = have - dropped
            self.released += dropped
        return dropped

    def settle_optional(self, kind: str) -> None:
        """``kind``'s optional work is done (or skipped): its pre-allocation goes back."""
        self.pending.pop(kind, None)

    def try_optional(self, kind: str, cost: int) -> bool:
        """Optional work of ``kind`` costing ``cost`` calls: when the unreserved remainder, less
        the more important kinds' pre-allocation, covers it and it could start before the
        deadline, hold it and answer ``True`` (the caller releases what it leaves); else
        ``False`` and nothing is held."""
        if self.expired() or not self.can_start() or self._denied_ahead(kind):
            return self._deny(kind)
        if not self.can_afford_unreserved(cost + self._ahead_of(kind)):
            return self._deny(kind)
        self.pending.pop(kind, None)
        self.hold(cost)
        return True

    @contextmanager
    def charging(self, kind: str) -> Iterator[None]:
        """Charge the calls made inside to ``kind`` (a stage the purpose does not name)."""
        before = self.stage
        self.stage = kind
        try:
            yield
        finally:
            self.stage = before

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


def counted_embedder[**P, R](
    embed: Callable[P, Awaitable[R]], budget: TurnBudget
) -> Callable[P, Awaitable[R]]:
    """``embed`` (a ``core.retrieval`` ``Embedder`` or ``BatchEmbedder``) counting each call it
    makes in ``budget.embedding_calls``, failed calls included (#141)."""

    async def counted(*args: P.args, **kwargs: P.kwargs) -> R:
        budget.embedding_calls += 1
        return await embed(*args, **kwargs)

    return counted


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
    "COMPOSED",
    "DEFAULT_TURN_DEADLINE_S",
    "DIRECTOR_ORDER",
    "DIRECTOR_TIEBREAK",
    "ENTAILMENT",
    "EXTRACTION",
    "FOLLOW_UP",
    "GUARDRAIL_INPUT",
    "GUARDRAIL_OUTPUT",
    "MAX_CALLS_PER_TURN",
    "MIN_CALLS_PER_TURN",
    "MIN_DEADLINE_S",
    "OPTIONAL_PRIORITY",
    "PER_PRIMARY_CALLS",
    "POLISH",
    "PURPOSE_KINDS",
    "REPAIR",
    "SALIENCE",
    "TurnBudget",
    "counted_embedder",
    "effective_max_calls",
    "lite_default_calls",
    "plan_lite_budget",
]
