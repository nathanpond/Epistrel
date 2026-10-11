"""Age-conditioned policies (FR-GUARD-11, FR-CAT-12): who counts, how old they are, and
whether a policy applies — deterministically, before any model call.

Everything here is pure over a cast the gates pass in (``store.guard_context`` reads it with
the effective policies and the live ``minor_age_threshold``). A **minor** is a character whose
current age is below the threshold (default 18, so 17 and under).

- **Current age** is ``true_age`` plus the full years elapsed on the world clock since the age
  was recorded. M2 has no clock, so it is ``true_age``.
- **Present** under Lite is every character of the story's cast: Lite tracks no location, so
  nobody is ever elsewhere. ``involved`` is not narrower under Lite either (no entity
  tracking), so the one accepted condition, ``minor_involved_or_present``, holds exactly when
  the cast has a minor.
- An age-conditioned policy whose condition is false **does not apply**: it is left out of the
  checks (no ``guardrail_check`` call), so adult-only stories pay nothing for it.
- When it applies, the model receives **subjects**: every cast member as an opaque positional
  label (``character_1`` … in creation order) with an age. Names never leave the engine, and
  the registry's ``Subject`` schema refuses any other label shape.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from epistrel.core.policies.model import ModelAssistedRule, Policy
from epistrel.orchestrator.models.registry import GuardrailCheckInputs, Subject
from epistrel.orchestrator.policies.seeds.policies import THRESHOLD_PLACEHOLDER

LABEL_PREFIX = "character_"


@dataclass(frozen=True, slots=True)
class CastMember:
    """One character as the age condition sees it. ``character_id`` stays in the engine."""

    character_id: str
    ordinal: int
    true_age: int
    age_recorded_at: datetime | None = None


def current_age(member: CastMember) -> int:
    """``true_age`` + full years elapsed on the world clock; M2 has no clock, so ``true_age``.
    (``age_recorded_at`` is #50's wall-clock stamp and is not story time, so it adds nothing.)"""
    return member.true_age


def in_order(cast: Iterable[CastMember]) -> tuple[CastMember, ...]:
    """The cast in creation order (``ordinal``)."""
    return tuple(sorted(cast, key=lambda member: member.ordinal))


def present(cast: Iterable[CastMember]) -> tuple[CastMember, ...]:
    """Who is present for the content: under Lite, the whole cast."""
    return in_order(cast)


def has_minor(cast: Iterable[CastMember], threshold: int) -> bool:
    return any(current_age(member) < threshold for member in present(cast))


def applies(policy: Policy, cast: Sequence[CastMember], threshold: int) -> bool:
    """Whether ``policy`` applies to content in a story with ``cast``: always for a policy
    without an age condition; for ``minor_involved_or_present``, when a minor is present."""
    if policy.age_condition is None:
        return True
    return has_minor(cast, threshold)


def partition(
    policies: Iterable[Policy], cast: Sequence[CastMember], threshold: int
) -> tuple[tuple[Policy, ...], tuple[Policy, ...]]:
    """``(active, inactive)``: the policies that apply, and the age-conditioned ones whose
    condition is false (#56 lists the latter as ``inactive_policies``). Order is kept."""
    active: list[Policy] = []
    inactive: list[Policy] = []
    for policy in policies:
        (active if applies(policy, cast, threshold) else inactive).append(policy)
    return tuple(active), tuple(inactive)


def label(position: int) -> str:
    """The opaque label of the ``position``-th cast member (1-based)."""
    return f"{LABEL_PREFIX}{position}"


def subjects(cast: Sequence[CastMember]) -> list[Subject]:
    """Every present cast member as ``{label, age}`` in creation order; never a name."""
    return [
        Subject(label=label(position), age=current_age(member))
        for position, member in enumerate(present(cast), start=1)
    ]


def policy_prompt(prompt: str, threshold: int) -> str:
    """The policy's prompt with ``{minor_age_threshold}`` replaced by the live threshold."""
    return prompt.replace(THRESHOLD_PLACEHOLDER, str(threshold))


def check_inputs(
    text: str, policy: Policy, cast: Sequence[CastMember], threshold: int
) -> GuardrailCheckInputs:
    """The ``guardrail_check`` request for one model-assisted ``policy`` over ``text``:
    subjects only for an age-conditioned policy (``[]`` otherwise, #56)."""
    rule = policy.rule
    if not isinstance(rule, ModelAssistedRule):
        raise TypeError(f"policy {policy.id} is not model-assisted")
    if policy.age_condition is None:
        return GuardrailCheckInputs(text=text, policy_prompt=rule.prompt)
    return GuardrailCheckInputs(
        text=text,
        policy_prompt=policy_prompt(rule.prompt, threshold),
        subjects=subjects(cast),
    )


__all__ = [
    "LABEL_PREFIX",
    "CastMember",
    "applies",
    "check_inputs",
    "current_age",
    "has_minor",
    "in_order",
    "label",
    "partition",
    "policy_prompt",
    "present",
    "subjects",
]
