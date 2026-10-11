"""The age condition (#55; FR-GUARD-11, FR-CAT-12), pure: current age, who is present under
Lite, when an age-conditioned policy applies, the subjects a check receives, and the
redacted violation."""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from epistrel.core.policies.model import Violation
from epistrel.orchestrator.models.registry import GuardrailCheckInputs, Subject
from epistrel.orchestrator.policies.age import (
    CastMember,
    applies,
    check_inputs,
    current_age,
    has_minor,
    partition,
    policy_prompt,
    subjects,
)
from epistrel.orchestrator.policies.check import guardrail_violation
from epistrel.orchestrator.policies.seeds.policies import (
    MINORS_POLICY_ID,
    PROMPT,
    THRESHOLD_PLACEHOLDER,
)
from epistrel.testing.ops import make_policy

SEEDED = dataclasses.replace(
    make_policy({"type": "model_assisted", "prompt": PROMPT}, name="minors"),
    id=MINORS_POLICY_ID,
    age_condition="minor_involved_or_present",
)
PLAIN = make_policy({"type": "model_assisted", "prompt": "No dragons."}, name="dragons")
WORDS = make_policy({"type": "wordlist", "words": ["wyvern"]}, name="wyverns")


def member(ordinal: int, age: int) -> CastMember:
    return CastMember(character_id=f"c{ordinal}", ordinal=ordinal, true_age=age)


ADULTS = (member(0, 34), member(1, 18))
WITH_MINOR = (member(1, 17), member(0, 34))


def test_current_age_is_the_true_age_without_a_clock() -> None:
    assert current_age(member(0, 16)) == 16


def test_minor_is_below_the_threshold() -> None:
    """17 is a minor at the default 18; 18 is not. Complement: a raised threshold makes the
    18-year-old a minor too."""
    assert has_minor(WITH_MINOR, 18)
    assert not has_minor(ADULTS, 18)
    assert has_minor(ADULTS, 19)
    assert not has_minor((), 18)


def test_applies_only_with_a_minor_present() -> None:
    """An age-conditioned policy applies only when the whole cast (Lite's "present") has a
    minor; a policy without a condition always applies."""
    assert applies(SEEDED, WITH_MINOR, 18)
    assert not applies(SEEDED, ADULTS, 18)
    assert applies(PLAIN, ADULTS, 18)
    active, inactive = partition([PLAIN, SEEDED, WORDS], ADULTS, 18)
    assert (active, inactive) == ((PLAIN, WORDS), (SEEDED,))
    assert partition([PLAIN, SEEDED], WITH_MINOR, 18) == ((PLAIN, SEEDED), ())


def test_subjects_are_positional_in_creation_order() -> None:
    assert subjects(WITH_MINOR) == [
        Subject(label="character_1", age=34),
        Subject(label="character_2", age=17),
    ]


def test_subject_labels_refuse_names() -> None:
    """The registry schema admits only ``character_<n>`` labels (complement: one passes)."""
    for name in ("Maribelle", "character_", "character_0", "character_1 Ada", ""):
        with pytest.raises(ValidationError):
            Subject(label=name, age=16)
    assert Subject(label="character_12", age=16).label == "character_12"


def test_check_inputs() -> None:
    """The seeded policy's check carries the subjects and the live threshold; any other
    policy's check carries no subjects."""
    inputs = check_inputs("some text", SEEDED, WITH_MINOR, 21)
    assert inputs.subjects == subjects(WITH_MINOR)
    assert "anyone under 21." in inputs.policy_prompt
    assert THRESHOLD_PLACEHOLDER not in inputs.policy_prompt
    assert check_inputs("some text", PLAIN, WITH_MINOR, 21) == GuardrailCheckInputs(
        text="some text", policy_prompt="No dragons."
    )
    with pytest.raises(TypeError):
        check_inputs("some text", WORDS, WITH_MINOR, 21)
    assert policy_prompt("under {minor_age_threshold}", 18) == "under 18"


def test_violation_message_redacts_the_minor() -> None:
    """An age-conditioned violation says a minor is involved or present; any other says only
    how many policies it broke."""
    minors = Violation(SEEDED.id, SEEDED.name, SEEDED.message, SEEDED.kind)
    dragons = Violation(PLAIN.id, PLAIN.name, PLAIN.message, PLAIN.kind)
    error = guardrail_violation([minors], [SEEDED, PLAIN])
    assert (error.code, error.http_status) == ("guardrail_violation", 422)
    assert error.message == "Input violates 1 policy: a minor is involved or present."
    plain = guardrail_violation([dragons, minors], [PLAIN], what="Output")
    assert plain.message == "Output violates 2 policies."
    assert plain.details["violations"][0] == {
        "policy_id": PLAIN.id,
        "name": PLAIN.name,
        "message": PLAIN.message,
    }
