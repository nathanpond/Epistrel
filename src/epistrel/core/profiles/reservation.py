"""The safety reservation a profile's turns plan with (NFR-PERF-6, FR-PROF-7; #156).

A turn reserves, before its first model call, the calls that keep it safe: its primary render
(one in M4), and S = B + C + G + O. **B** is one ``extraction_backstop`` call per planned
render (one call covers the render's beats), **C** one for claim detection on the composed
message, and **G** the model-assisted guardrail calls (one per model-assisted input policy for
the turn's input, one per model-assisted output policy for its first output checkpoint). B and
C are reserved only when the story runs the ``structured`` pipeline at a verification level
with extraction; C only when the polish pass is on (a deterministic assembly adds no new text,
so its detection makes no call, #152). A Lite turn reserves its generation and G (#69).

**O** (#214, NFR-PERF-6) is a staged turn's outcome step (FR-EXT-15,16, #212): the outcome
render, its extraction (with extraction on) and its output check (one call per model-assisted
output policy): ``outcome_calls``, 3 with one such policy. It applies only to a ``structured``
story with ``states`` on that has a bound ``synchronous`` write-capable provider for an
external State (``synchronous``, the story-aware fact ``core.providers.synchronous_bound``);
else O = 0. It is reserved per turn, not per effect: one outcome step narrates every
synchronous effect.

``safety_minimum`` is ``1 + B + C + G + O``; ``budget_warnings`` is the profile validator's
warning when a profile's explicit ``budgets.model_calls`` is below it (``below_safety_minimum``),
for Lite and structured alike. ``outcome`` defaults to 0: a profile does not know a story's
providers, so the profile surfaces (validation, create, copy and update) pass 0 and the
story-aware ones (the turn's plan, a story's impact report, a provider binding) pass the story's
O (owner decision 13, 2026-10-30). Pure: the Orchestrator's planner and the profile validator
share it.

``SafetyFacts`` (#456) carries what the minimum needs beside the settings (the deployment's
model-assisted policies, G; #214: whether the story in view has a synchronous provider) to
every surface that warns: profile create, copy and update responses and the impact reports of a
profile edit, a story's assignment, its overrides and the impact preview, as ``POST
/engine-profiles/{id}/validate`` does. A later reservation term is a new field here, so those
call sites do not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from epistrel.core.profiles.schema import ProfileSettings, is_delivered, level_of

EXTRACTION_LEVELS = frozenset({"extraction", "entailment"})
"""The verification levels with claim extraction (FR-PROF-2)."""
BELOW_SAFETY_MINIMUM = "below_safety_minimum"
"""The profile validator's warning code (FR-PROF-7)."""
STATES_MODULE = "states"
"""The module a staged turn's State effects run under (``core.states.MODULE``)."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """The calls a turn reserves beside its primary render."""

    extraction: int = 0
    """B: one extraction per planned render."""
    composed: int = 0
    """C: the composed message's detection."""
    guardrail_input: int = 0
    guardrail_output: int = 0
    outcome: int = 0
    """O (#214): a staged turn's outcome step (its render, extraction and output check)."""

    @property
    def guardrail(self) -> int:
        """G."""
        return self.guardrail_input + self.guardrail_output

    @property
    def total(self) -> int:
        """S = B + C + G + O."""
        return self.extraction + self.composed + self.guardrail + self.outcome

    def payload(self) -> dict[str, int]:
        return {
            "extraction": self.extraction,
            "composed": self.composed,
            "guardrail": self.guardrail,
            "guardrail_input": self.guardrail_input,
            "guardrail_output": self.guardrail_output,
            "outcome": self.outcome,
        }


def structured(settings: ProfileSettings) -> bool:
    return settings.pipeline == "structured"


def extraction_on(settings: ProfileSettings) -> bool:
    """Whether a structured turn's detection runs the model layer (``verification.level``
    ``extraction`` or ``entailment``)."""
    return structured(settings) and settings.verification.level in EXTRACTION_LEVELS


def states_on(settings: ProfileSettings) -> bool:
    """Whether ``states`` runs at a delivered level under ``settings`` (a staged turn's effects
    need it)."""
    level = level_of(settings, STATES_MODULE)
    return level is not None and is_delivered(STATES_MODULE, level)


def outcome_calls(settings: ProfileSettings, *, synchronous: bool, guardrail_output: int) -> int:
    """O (#214): the outcome step's calls, ``1 + extraction + G_out`` (with extraction on and one
    model-assisted output policy: 3), when ``synchronous`` (the story has a bound synchronous
    write-capable provider for an external State) and it runs ``structured`` with ``states``
    on; else 0. ``guardrail_output``: the model-assisted output policies one output checkpoint
    runs (the outcome step's own)."""
    if not synchronous or not structured(settings) or not states_on(settings):
        return 0
    return 1 + (1 if extraction_on(settings) else 0) + max(0, guardrail_output)


def reservation(
    settings: ProfileSettings,
    *,
    guardrail_input: int,
    guardrail_output: int,
    renders: int = 1,
    outcome: int = 0,
) -> Reservation:
    """The reservation of a turn with ``renders`` planned renders under ``settings``;
    ``outcome``: O (``outcome_calls``; 0 where no story is in view)."""
    on = extraction_on(settings)
    return Reservation(
        extraction=renders if on else 0,
        composed=1 if on and settings.repair.polish.enabled else 0,
        guardrail_input=guardrail_input,
        guardrail_output=guardrail_output,
        outcome=max(0, outcome),
    )


def safety_minimum(
    settings: ProfileSettings, *, guardrail_input: int, guardrail_output: int, outcome: int = 0
) -> int:
    """The single-primary minimum: ``1 + B + C + G + O`` (Lite: ``1 + G``); ``outcome`` is O on
    a story-aware surface, 0 on a profile's own (#214)."""
    return (
        1
        + reservation(
            settings,
            guardrail_input=guardrail_input,
            guardrail_output=guardrail_output,
            outcome=outcome,
        ).total
    )


def budget_warnings(
    settings: ProfileSettings, *, guardrail_input: int, guardrail_output: int, outcome: int = 0
) -> list[dict[str, Any]]:
    """``[{code: below_safety_minimum, …}]`` when the profile's explicit call cap is below its
    single-primary minimum (every turn would be refused), else ``[]``. ``outcome``: O on a
    story-aware surface, 0 on a profile's own (#214)."""
    cap = settings.budgets.model_calls
    found = reservation(
        settings,
        guardrail_input=guardrail_input,
        guardrail_output=guardrail_output,
        outcome=outcome,
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


@dataclass(frozen=True, slots=True)
class SafetyFacts:
    """What the safety minimum needs beside a profile's settings (#456): the model-assisted
    policies G counts, ``guardrail_input`` and ``guardrail_output`` (the deployment's global
    ones where no story is in view, as the validator counts them), and (#214) ``synchronous``:
    whether the story in view has a bound synchronous write-capable provider for an external
    State (``False`` on a profile surface, which knows no story's providers)."""

    guardrail_input: int = 0
    guardrail_output: int = 0
    synchronous: bool = False

    def outcome(self, settings: ProfileSettings) -> int:
        """O under ``settings`` with these facts (``outcome_calls``)."""
        return outcome_calls(
            settings, synchronous=self.synchronous, guardrail_output=self.guardrail_output
        )

    def minimum(self, settings: ProfileSettings) -> int:
        """``safety_minimum`` of ``settings`` with these facts."""
        return safety_minimum(
            settings,
            guardrail_input=self.guardrail_input,
            guardrail_output=self.guardrail_output,
            outcome=self.outcome(settings),
        )

    def warnings(self, settings: ProfileSettings) -> list[dict[str, Any]]:
        """``budget_warnings`` of ``settings`` with these facts."""
        return budget_warnings(
            settings,
            guardrail_input=self.guardrail_input,
            guardrail_output=self.guardrail_output,
            outcome=self.outcome(settings),
        )


__all__ = [
    "BELOW_SAFETY_MINIMUM",
    "EXTRACTION_LEVELS",
    "STATES_MODULE",
    "Reservation",
    "SafetyFacts",
    "budget_warnings",
    "extraction_on",
    "outcome_calls",
    "reservation",
    "safety_minimum",
    "states_on",
    "structured",
]
