"""The pure policy evaluator (FR-GUARD-2,9): word and phrase matching, regex with a deadline,
entity lists, targets, and model-assisted policies left to the guardrail-check purpose."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from epistrel.core.policies import evaluate as evaluate_module
from epistrel.core.policies.evaluate import evaluate, words
from epistrel.core.policies.model import RULE_ADAPTER, PolicyKind, Scope, Target
from tests.policy_support import policy
from tests.policy_support import words as wordlist


def _ids(text: str, *policies: object, target: Target = Target.TEXT_OUTPUT) -> list[str]:
    return [v.policy_id for v in evaluate(text, policies, target)]  # type: ignore[arg-type]


def test_words_normalise_and_keep_apostrophes() -> None:
    assert words("Don't  STOP—the \uff26\uff35\uff2c\uff2c-width straße") == (
        "don't",
        "stop",
        "the",
        "full",
        "width",
        "strasse",
    )
    assert words("it\u2019s") == ("it\u2019s",)


def test_wordlist_matches_whole_words_and_phrases() -> None:
    gore = policy(wordlist("gore", "blood bath"), policy_id="gore")
    assert _ids("So much GORE here.", gore) == ["gore"]
    assert _ids("a Blood   Bath, then", gore) == ["gore"]
    assert _ids("full-width \uff27\uff2f\uff32\uff25", gore) == ["gore"]
    # complements: substrings and split phrases do not match
    assert _ids("Al Gore's gorey story", gore) == []
    assert _ids("blood, then a bath", gore) == []
    assert _ids("bath blood", gore) == []


def test_apostrophe_joins_a_word() -> None:
    don = policy(wordlist("don"), policy_id="don")
    assert _ids("don't", don) == []
    assert _ids("don, the", don) == ["don"]
    assert _ids("a policy word", policy(wordlist("!!!"), policy_id="none")) == []


def test_targets_and_order() -> None:
    out = policy(wordlist("x"), policy_id="out", scope=Scope.STORY)
    both = policy(wordlist("x"), policy_id="both")
    image = policy(wordlist("x"), policy_id="img", applies_to=(Target.IMAGE_PROMPT,))
    assert _ids("x", both, out, image) == ["both", "out"]
    assert _ids("x", both, out, image, target=Target.TEXT_INPUT) == ["both"]
    assert _ids("x", both, out, image, target=Target.IMAGE_PROMPT) == ["img"]
    assert _ids("x", both, both) == ["both"]
    violation = evaluate("x", [both], Target.TEXT_OUTPUT)[0]
    assert (violation.name, violation.message, violation.kind) == (
        "both",
        "both says no",
        PolicyKind.DETERMINISTIC,
    )


def test_entity_list_matches_any_whole_word_mention() -> None:
    names = policy({"type": "entity_list", "names": ["Mara Vell"]}, scope=Scope.STORY)
    assert len(evaluate("I saw mara vell.", [names], Target.TEXT_OUTPUT)) == 1
    assert evaluate("I saw Mara.", [names], Target.TEXT_OUTPUT) == []


def test_model_assisted_policies_are_skipped() -> None:
    check = policy({"type": "model_assisted", "prompt": "Is this cruel?"}, policy_id="m")
    assert check.kind is PolicyKind.MODEL_ASSISTED
    assert _ids("anything at all", check) == []


def test_regex_searches_ignoring_case() -> None:
    digits = policy({"type": "regex", "patterns": [r"\bcard \d{4}\b", "zz+"]}, policy_id="re")
    assert _ids("my CARD 1234 is", digits) == ["re"]
    assert _ids("buzzz", digits) == ["re"]
    assert _ids("card 12", digits) == []


def test_regex_past_its_deadline_is_a_violation() -> None:
    """Catastrophic backtracking cannot stall the caller: the search misses its deadline, the
    policy counts as broken (fail closed), and the next search runs on a fresh pool."""
    slow = policy({"type": "regex", "patterns": [r"(a+)+$"]}, policy_id="slow")
    started = time.perf_counter()
    assert _ids("a" * 25 + "!", slow) == ["slow"]
    assert time.perf_counter() - started < 1.0
    fine = policy({"type": "regex", "patterns": ["b"]}, policy_id="fine")
    assert _ids("abc", fine) == ["fine"]
    assert _ids("xyz", fine) == []


def test_regex_deadline_constant() -> None:
    assert pytest.approx(0.05) == evaluate_module.REGEX_DEADLINE_S


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "wordlist", "words": []},
        {"type": "wordlist", "words": ["x"] * 2001},
        {"type": "regex", "patterns": ["("]},
        {"type": "regex", "patterns": ["a" * 501]},
        {"type": "regex", "patterns": ["a"] * 21},
        {"type": "model_assisted", "prompt": "p" * 4001},
        {"type": "model_assisted", "prompt": "p", "on_uncertain": "maybe"},
        {"type": "allow", "words": ["x"]},
        {"type": "wordlist", "words": ["x"], "exempt": ["global"]},
    ],
)
def test_rule_limits(rule: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RULE_ADAPTER.validate_python(rule)
