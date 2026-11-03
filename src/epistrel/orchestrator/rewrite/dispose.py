"""Disposing of a plan's conflicts by the session's mode (FR-REWRITE-3, FR-DIRECT-4; #224).

Over the irreducible conflicts #223's plan found, ``resolution_mode`` decides only who settles
each: ``synchronous`` asks the author about every one (a required ``conflict`` question);
``best_effort`` settles each with the least change and reports it; ``threshold(severity)``
asks about those at or above the severity and settles the rest. Every question recommends the
least change (owner decision 4); an auto-resolved conflict gets a question row too,
``required = false`` and answered by principal ``engine``, so the author may override it until
the apply.

**The least change** (``least_change``): ``rewrite`` for narrative text (an event) or derived
text (a memory); ``cascade_undo`` for a dependent amendment part; ``drop`` otherwise. ``keep``
is never chosen automatically, except for a decision whose beat committed effects (#225:
``keep``, with a ``decision_effects_kept`` notification). An event the player or a consumer
wrote is still ``rewrite``, and it is always ``high``, so ``threshold`` always asks about it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from epistrel.orchestrator.rewrite.parse import Question, question_key
from epistrel.orchestrator.rewrite.templates import CONFLICT_PROMPT, CONFLICT_REASONS

ENGINE = "engine"
"""The principal of an auto-resolution."""
ACTION_LABELS = {
    "rewrite": "Rewrite it to agree",
    "drop": "Remove it",
    "keep": "Keep it as it is",
    "cascade_undo": "Undo the amendment it depends on",
}
RANK = {"low": 0, "high": 1}


def least_change(conflict: Mapping[str, Any]) -> str:
    """The action ``best_effort`` takes and every conflict question recommends."""
    table = conflict["record"]["table"]
    if conflict.get("reason") == "decision_effects":
        return "keep"  # #225: the effects stay committed; the prose is never regenerated
    if table == "amendment_parts":
        return "cascade_undo"
    if table in ("events", "memories"):
        return "rewrite"
    return "drop"


def asks(conflict: Mapping[str, Any], mode: str, threshold: str | None) -> bool:
    """Whether the author is asked about ``conflict`` under the mode."""
    if mode == "synchronous":
        return True
    if mode == "threshold":
        return RANK[str(conflict.get("severity") or "low")] >= RANK[str(threshold or "high")]
    return False


def conflict_question(conflict: Mapping[str, Any], *, required: bool) -> Question:
    actions = [str(a) for a in conflict.get("options") or ("rewrite", "drop", "keep")]
    options: tuple[dict[str, Any], ...] = tuple(
        {"id": f"o{n}", "label": ACTION_LABELS[a], "effect": {"action": a}}
        for n, a in enumerate(actions, start=1)
    )
    best = least_change(conflict)
    recommended = next(o["id"] for o in options if o["effect"]["action"] == best)
    return Question(
        kind="conflict",
        key=question_key("conflict", conflict["conflict_id"]),
        prompt=CONFLICT_PROMPT.format(
            table=conflict["record"]["table"],
            reason=str(conflict.get("reason", "")).replace("_", " "),
        ),
        options=options,
        recommended=str(recommended),
        reason=CONFLICT_REASONS[best],
        severity=str(conflict.get("severity") or "low"),
        required=required,
    )


@dataclass(frozen=True, slots=True)
class Disposition:
    """What a mode makes of a plan's conflicts: the questions to ask (required) and those it
    settles itself (not required, pre-answered with the recommendation)."""

    asked: tuple[Question, ...]
    auto: tuple[Question, ...]

    @property
    def questions(self) -> tuple[Question, ...]:
        return (*self.asked, *self.auto)


def dispose(
    conflicts: Sequence[Mapping[str, Any]], mode: str, threshold: str | None
) -> Disposition:
    asked: list[Question] = []
    auto: list[Question] = []
    for conflict in conflicts:
        if asks(conflict, mode, threshold):
            asked.append(conflict_question(conflict, required=True))
        else:
            auto.append(conflict_question(conflict, required=False))
    return Disposition(tuple(asked), tuple(auto))


__all__ = [
    "ACTION_LABELS",
    "ENGINE",
    "Disposition",
    "asks",
    "conflict_question",
    "dispose",
    "least_change",
]
