"""The escalation gate: which ``uncertain`` verdicts the entailment model is asked about
(FR-VERIFY-3, NFR-PERF-5; docs/05 ADR on the entailment-fallback gate; #142).

**Reasons.** The deterministic check (#140) says why a verdict is ``uncertain``; FR-VERIFY-3
names what may escalate:

- (a) an unresolved predicate: ``unresolved_predicate``;
- (b) a conflict that depends on world knowledge: ``world_knowledge``, and ``weak_semantics``
  (a different object on a weak predicate: whether the two exclude each other is world
  knowledge the registry does not hold);
- (c) a partial or ambiguous match the rules do not resolve. Every one is resolved by FR-PROP-10
  now, so no reason escalates under (c) (ADR-162).

``partial_coverage`` and ``granularity_refinement`` are resolved by #140's rules and never reach the
gate (``RESOLVED_IN_CORE``). ``unresolved_entity``, ``unresolved_time`` and ``argument_type`` never
escalate (``NEVER_ESCALATES``), nor does ``no_clock`` (#195: a clock check on a story without the
clock), nor ``advisory`` (#207: a value an advisory external provider answered, which settles
nothing by the owner's decision), nor ``provider_unknown`` (#206: a value pinned ``Unknown`` because
its provider was unavailable): the claim is not about anything the model could judge against the
records, so it takes the safe disposition at once. Neither does ``approximate_bounds`` (#467,
ADR-162). FR-PROP-10 already resolves it deterministically to ``uncertain``: the rules showed that
the claim holds under some admissible readings of the approximate bounds and not under others, and a
verdict needs every reading. The model sees the same bounds and knows no more about where they lie,
so no answer it gave could be taken (FR-VERIFY-8). The call would cost budget and latency and could
not change the outcome.

**The threshold.** A reason that may escalate does so when the deterministic confidence
(``Verdict.confidence``: 0.5 for ``world_knowledge``, 0.0 for (a) and ``weak_semantics``) is
below the effective threshold: the least of the profile's ``verification.entailment_gate``
and the claim's per-shape threshold. The gate launches conservatively: the default gate, 1.0,
escalates every one, and no shape is tightened by default. #155 tightens per shape (shadow
first, then promotion) and hands its live thresholds to the gate as a ``ShapeThresholds``.

**Predicate shape** (``PredicateShape``): ``(layer, cardinality, temporal kind, polarity)``,
the cluster key #155 reports yield by; stored as ``layer|cardinality|temporal|polarity``. An
unregistered predicate's shape is ``(auto, unknown, unknown, <polarity>)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from epistrel.core.propositions.model import Predicate, Proposition
from epistrel.core.verify.rules import VerdictReason

R = VerdictReason

ESCALATES: Mapping[VerdictReason, Literal["a", "b", "c"]] = MappingProxyType(
    {
        R.UNRESOLVED_PREDICATE: "a",
        R.WORLD_KNOWLEDGE: "b",
        R.WEAK_SEMANTICS: "b",
    }
)
"""The reasons that may escalate, with FR-VERIFY-3's clause (none is (c): ADR-162)."""

RESOLVED_IN_CORE = frozenset({R.PARTIAL_COVERAGE, R.GRANULARITY_REFINEMENT})
"""Resolved by #140's coverage logic (through their remainders); never escalated."""

NEVER_ESCALATES = frozenset(
    {
        R.UNRESOLVED_ENTITY,
        R.UNRESOLVED_TIME,
        R.ARGUMENT_TYPE,
        R.APPROXIMATE_BOUNDS,
        R.NO_CLOCK,
        R.ADVISORY,
        R.PROVIDER_UNKNOWN,
    }
)
"""Take the safe disposition directly, whatever the threshold (``approximate_bounds``: #467)."""

UNKNOWN = "unknown"
SHAPE_SEPARATOR = "|"


@dataclass(frozen=True, slots=True)
class PredicateShape:
    """The clustering key of a claim's predicate (see the module docstring)."""

    layer: str
    cardinality: str
    temporal: str
    polarity: str

    @classmethod
    def of(cls, claim: Proposition, predicate: Predicate | None) -> PredicateShape:
        if predicate is None:
            return cls("auto", UNKNOWN, UNKNOWN, claim.polarity.value)
        return cls(
            predicate.layer.value,
            predicate.cardinality.value,
            predicate.temporal_kind.value,
            claim.polarity.value,
        )

    def key(self) -> str:
        return SHAPE_SEPARATOR.join((self.layer, self.cardinality, self.temporal, self.polarity))


class ShapeThresholds(Protocol):
    """Per-shape thresholds (#155's live ``gate_thresholds`` rows, read from its per-process
    cache): the threshold for a shape, or ``None`` when the shape is not tightened."""

    def threshold(self, shape: PredicateShape) -> float | None: ...


@dataclass(frozen=True, slots=True)
class FixedThresholds:
    """Thresholds by shape key (``PredicateShape.key()``); empty by default."""

    by_shape: Mapping[str, float] = MappingProxyType({})

    def threshold(self, shape: PredicateShape) -> float | None:
        return self.by_shape.get(shape.key())


NO_TIGHTENING = FixedThresholds()

Outcome = Literal["escalate", "below_gate", "never_escalates", "resolved_in_core", "unnormalized"]


@dataclass(frozen=True, slots=True)
class GateDecision:
    """What the gate decided for one ``uncertain`` verdict, and on what: ``escalate`` (ask the
    model), ``below_gate`` (the confidence reached the threshold: the safe disposition),
    ``never_escalates``, ``resolved_in_core``, or ``unnormalized`` (a claim that did not
    become a proposition: the safe disposition)."""

    outcome: Outcome
    reason: VerdictReason
    shape: PredicateShape
    confidence: float
    threshold: float | None

    @property
    def escalate(self) -> bool:
        return self.outcome == "escalate"


class Gate:
    """The escalation gate over the profile's ``verification.entailment_gate`` and the
    per-shape thresholds (see the module docstring)."""

    def __init__(self, threshold: float, shapes: ShapeThresholds = NO_TIGHTENING) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("an entailment gate is between 0 and 1")
        self.default = threshold
        self.shapes = shapes

    def threshold_for(self, shape: PredicateShape) -> float:
        """The effective threshold: the least of the gate and the shape's own."""
        tightened = self.shapes.threshold(shape)
        return self.default if tightened is None else min(self.default, tightened)

    def decide(
        self, reason: VerdictReason, predicate_shape: PredicateShape, confidence: float
    ) -> GateDecision:
        if reason in RESOLVED_IN_CORE:
            return GateDecision("resolved_in_core", reason, predicate_shape, confidence, None)
        if reason not in ESCALATES:
            return GateDecision("never_escalates", reason, predicate_shape, confidence, None)
        threshold = self.threshold_for(predicate_shape)
        outcome: Outcome = "escalate" if confidence < threshold else "below_gate"
        return GateDecision(outcome, reason, predicate_shape, confidence, threshold)

    def should_escalate(
        self, reason: VerdictReason, predicate_shape: PredicateShape, confidence: float
    ) -> bool:
        """Whether an ``uncertain(reason)`` verdict of ``predicate_shape`` at ``confidence``
        goes to the entailment model."""
        return self.decide(reason, predicate_shape, confidence).escalate


__all__ = [
    "ESCALATES",
    "NEVER_ESCALATES",
    "NO_TIGHTENING",
    "RESOLVED_IN_CORE",
    "UNKNOWN",
    "FixedThresholds",
    "Gate",
    "GateDecision",
    "Outcome",
    "PredicateShape",
    "ShapeThresholds",
]
