"""How each dependency edge type is classified in a blast radius (FR-DIRECT-3,8; #223, #225).

``CASCADE_RULES`` maps each #220 edge type to a handler ``(ctx, dependent) -> Classification``,
registered with ``@rule`` and used identically by the preview and the apply:

- ``mentions`` → incidental, ``fix`` (a passing reference is rewritten in place by substituting
  the new value's name for the old one's surface forms);
- ``presupposes`` → cascaded, ``fix`` (narration that assumed the old value), or #223's conflict
  when the substitution cannot repair it;
- ``causes`` → cascaded, ``re_evaluate``: a decision the old fact was a reason for is kept when
  #140's check under the hypothetical canon still supports one of its other premises (#707,
  ``hypothetical.decision_supported``: ``plan`` settles it to ``keep``), and otherwise
  regenerated (#224's ``rewrite_record``) and flagged ``regenerated``; one whose beat committed
  State effects or an outbox row is never regenerated (``plan`` makes it a conflict recommending
  ``keep``, ``decision_effects_kept``);
- ``derives`` → by ``acquisition_kind``: a firsthand belief (``observation``, ``discovery``)
  follows the fact (``cascade``: a correction transition to the new proposition, the old
  confidence kept); an ``inference`` is re-run (#707: ``re_run``, which ``plan`` settles to
  ``cascade`` when its premises under the hypothetical canon support the new value, else
  ``keep``); a secondhand or attributed one (``testimony``, ``correction``,
  ``revision``, ``author``, ``resolution``, ``author_fiat``) stays
  (``keep``: the character believed what they were told; a kept belief canon now contradicts is
  a ``belief_now_false`` notification); derived memories are cascaded through
  ``rewrite_record``;
- ``asserts`` → ``relabel``: what was said stays said; only the utterance's objective relation
  is re-evaluated against the new canon (FR-DIRECT-8), never its text or intent.

A handler is pure. ``plan`` escalates a cascaded text record to an irreducible conflict when the
substitution cannot repair it (see ``plan``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from epistrel.core.enums import DependencyEdgeType

Bucket = Literal["incidental", "cascaded", "relabel"]
FIRSTHAND = frozenset({"observation", "discovery"})
"""Acquisition kinds whose belief follows the changed fact (#225)."""
INFERENCE = "inference"
RE_RUN = "re_run"
"""#707: an inferred belief's provisional action; ``plan`` settles it to ``cascade`` or
``keep`` by re-running the inference's premises."""


@dataclass(frozen=True, slots=True)
class Classification:
    bucket: Bucket
    action: str


@dataclass(frozen=True, slots=True)
class PlanContext:
    """What a handler may know: the change being planned (its preview JSON)."""

    change: dict[str, Any]


Handler = Callable[[PlanContext, Any], Classification]

CASCADE_RULES: dict[DependencyEdgeType, Handler] = {}


def rule(edge: DependencyEdgeType) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        CASCADE_RULES[edge] = handler
        return handler

    return register


@rule(DependencyEdgeType.MENTIONS)
def _mentions(ctx: PlanContext, dependent: Any) -> Classification:
    return Classification("incidental", "fix")


@rule(DependencyEdgeType.PRESUPPOSES)
def _presupposes(ctx: PlanContext, dependent: Any) -> Classification:
    return Classification("cascaded", "fix")


@rule(DependencyEdgeType.DERIVES)
def _derives(ctx: PlanContext, dependent: Any) -> Classification:
    table = dependent.dependent[0]
    if table == "memories":
        return Classification("cascaded", "regenerate")
    kind = getattr(dependent, "acquisition_kind", None)
    if kind in FIRSTHAND:
        return Classification("cascaded", "cascade")
    if kind == INFERENCE:
        # #707: the plan re-runs the inference's premises under the hypothetical canon
        # (``hypothetical.inference_follows``): ``cascade`` when they support the new value,
        # else ``keep``.
        return Classification("cascaded", RE_RUN)
    return Classification("cascaded", "keep")


@rule(DependencyEdgeType.CAUSES)
def _causes(ctx: PlanContext, dependent: Any) -> Classification:
    return Classification("cascaded", "re_evaluate")


@rule(DependencyEdgeType.ASSERTS)
def _asserts(ctx: PlanContext, dependent: Any) -> Classification:
    return Classification("relabel", "relabel")


def classify(ctx: PlanContext, dependent: Any) -> Classification:
    """``dependent``'s classification by its edge type."""
    return CASCADE_RULES[dependent.edge_type](ctx, dependent)


__all__ = [
    "CASCADE_RULES",
    "FIRSTHAND",
    "INFERENCE",
    "RE_RUN",
    "Classification",
    "PlanContext",
    "classify",
    "rule",
]
