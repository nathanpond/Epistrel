"""The safety reservation a profile's turns plan with (NFR-PERF-6, FR-PROF-7; #156).

A turn reserves, before its first model call, the calls that keep it safe: its primary render
(one in M4), and S = B + C + G. **B** is one ``extraction_backstop`` call per planned render
(one call covers the render's beats), **C** one for claim detection on the composed message,
and **G** the model-assisted guardrail calls (one per model-assisted input policy for the
turn's input, one per model-assisted output policy for its first output checkpoint). B and C
are reserved only when the story runs the ``structured`` pipeline at a verification level with
extraction; C only when the polish pass is on (a deterministic assembly adds no new text, so
its detection makes no call, #152). A Lite turn reserves its generation and G (#69).

``safety_minimum`` is ``1 + B + C + G``; ``budget_warnings`` is the profile validator's warning
when a profile's explicit ``budgets.model_calls`` is below it (``below_safety_minimum``), for
Lite and structured alike. Pure: the Orchestrator's planner and the profile validator share
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from epistrel.core.profiles.schema import ProfileSettings

EXTRACTION_LEVELS = frozenset({"extraction", "entailment"})
"""The verification levels with claim extraction (FR-PROF-2)."""
BELOW_SAFETY_MINIMUM = "below_safety_minimum"
"""The profile validator's warning code (FR-PROF-7)."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """The calls a turn reserves beside its primary render."""

    extraction: int = 0
    """B: one extraction per planned render."""
    composed: int = 0
    """C: the composed message's detection."""
    guardrail_input: int = 0
    guardrail_output: int = 0

    @property
    def guardrail(self) -> int:
        """G."""
        return self.guardrail_input + self.guardrail_output

    @property
    def total(self) -> int:
        """S = B + C + G."""
        return self.extraction + self.composed + self.guardrail

    def payload(self) -> dict[str, int]:
        return {
            "extraction": self.extraction,
            "composed": self.composed,
            "guardrail": self.guardrail,
            "guardrail_input": self.guardrail_input,
            "guardrail_output": self.guardrail_output,
        }


def structured(settings: ProfileSettings) -> bool:
    return settings.pipeline == "structured"


def extraction_on(settings: ProfileSettings) -> bool:
    """Whether a structured turn's detection runs the model layer (``verification.level``
    ``extraction`` or ``entailment``)."""
    return structured(settings) and settings.verification.level in EXTRACTION_LEVELS


def reservation(
    settings: ProfileSettings, *, guardrail_input: int, guardrail_output: int, renders: int = 1
) -> Reservation:
    """The reservation of a turn with ``renders`` planned renders under ``settings``."""
    on = extraction_on(settings)
    return Reservation(
        extraction=renders if on else 0,
        composed=1 if on and settings.repair.polish.enabled else 0,
        guardrail_input=guardrail_input,
        guardrail_output=guardrail_output,
    )


def safety_minimum(
    settings: ProfileSettings, *, guardrail_input: int, guardrail_output: int
) -> int:
    """The single-primary minimum: ``1 + B + C + G`` (Lite: ``1 + G``)."""
    return (
        1
        + reservation(
            settings, guardrail_input=guardrail_input, guardrail_output=guardrail_output
        ).total
    )


def budget_warnings(
    settings: ProfileSettings, *, guardrail_input: int, guardrail_output: int
) -> list[dict[str, Any]]:
    """``[{code: below_safety_minimum, …}]`` when the profile's explicit call cap is below its
    single-primary minimum (every turn would be refused), else ``[]``."""
    cap = settings.budgets.model_calls
    found = reservation(
        settings, guardrail_input=guardrail_input, guardrail_output=guardrail_output
    )
    minimum = 1 + found.total
    if cap is None or cap >= minimum:
        return []
    return [
        {
            "code": BELOW_SAFETY_MINIMUM,
            "setting": "budgets.model_calls",
            "model_calls": cap,
            "minimum_required": minimum,
            "reservation": found.payload(),
        }
    ]


__all__ = [
    "BELOW_SAFETY_MINIMUM",
    "EXTRACTION_LEVELS",
    "Reservation",
    "budget_warnings",
    "extraction_on",
    "reservation",
    "safety_minimum",
    "structured",
]
