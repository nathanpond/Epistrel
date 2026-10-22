"""The pure policy evaluator (FR-GUARD-2,9): word and phrase matching, regex with a deadline,
entity lists, targets, and model-assisted policies left to the guardrail-check purpose."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

import pytest
from pydantic import ValidationError

from epistrel.core.policies import evaluate as evaluate_module
from epistrel.core.policies.evaluate import evaluate, matched_span, words
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


def test_a_script_s_own_marks_stay_inside_its_words() -> None:
    """#417: a Devanagari vowel sign (Mc "ा", Mn "े") and the nukta of "ढ़" are word characters,
    the erasure matcher's (``core.erasure.matcher.WORD_CHAR``), so a policy word is the whole
    word: "राम" is not a word of "रामायण", "रामेश" or "काराम". Complement: "राम" alone still
    breaks the policy, and a script-neutral diacritic (U+0334) still ends a word (#410)."""
    assert words("रामायण पढ़ो") == ("रामायण", "पढ़ो")
    assert words("राम") == ("राम",)
    assert words("quenby\u0334s") == ("quenby", "s")
    ram = policy(wordlist("राम"), policy_id="ram")
    names = policy({"type": "entity_list", "names": ["राम दास"]}, policy_id="names")
    for word in ("रामायण", "रामेश", "काराम", "रामा"):
        assert _ids(f"Ty reads {word} now.", ram, names) == [], word
    assert _ids("राम आया, रामायण पढ़ो।", ram, names) == ["ram"]
    assert _ids("कल राम दास आया।", ram, names) == ["ram", "names"]
    assert _ids("रामायण दास", names) == []
    assert matched_span("रामायण और राम दास", ram) == "राम"
    assert matched_span("कल राम दास आया।", names) == "राम दास"
    assert matched_span("रामायण पढ़ो", policy(wordlist("पढ़ो"))) == "पढ़ो"


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


def test_timed_out_regex_workers_are_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A search past its deadline kills its worker rather than leaving it to backtrack: every
    worker the pool gives up on after a timeout has exited, and no more than ``REGEX_WORKERS``
    are alive (#290). The given-up workers are recorded as they are discarded, since a discarded
    worker is untracked by the pool whether or not it was killed (#300)."""
    slow = policy({"type": "regex", "patterns": [r"(a+)+$"]}, policy_id="slow")
    pool = evaluate_module._REGEX
    discard = evaluate_module._RegexPool._discard
    discarded: list[subprocess.Popen[bytes]] = []

    def recording(self: evaluate_module._RegexPool, worker: evaluate_module._RegexWorker) -> None:
        discarded.append(worker.process)
        discard(self, worker)

    monkeypatch.setattr(evaluate_module._RegexPool, "_discard", recording)
    timeouts = evaluate_module.REGEX_WORKERS + 2
    for _ in range(timeouts):
        assert _ids("a" * 40 + "!", slow) == ["slow"]
    assert len(discarded) == timeouts
    assert [p.poll() is not None for p in discarded] == [True] * timeouts
    assert len(pool.processes()) <= evaluate_module.REGEX_WORKERS
    fine = policy({"type": "regex", "patterns": ["b"]}, policy_id="fine")
    assert _ids("abc", fine) == ["fine"]
    assert len(pool.processes()) <= evaluate_module.REGEX_WORKERS


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_process_exits_after_a_regex_timeout() -> None:
    """The issue's repro: a process whose search timed out exits promptly instead of spinning
    on the abandoned search, and leaves no worker behind (#290). The child leads its own
    process group, which its workers inherit, so a worker that outlives it, re-parented and
    still spinning, keeps that group alive (#300)."""
    script = (
        "from epistrel.core.policies.evaluate import evaluate\n"
        "from epistrel.core.policies.model import Target\n"
        "from tests.policy_support import policy\n"
        "p = policy({'type': 'regex', 'patterns': ['(a+)+$']}, applies_to=(Target.TEXT_INPUT,))\n"
        "print(len(evaluate('a' * 40 + '!', [p], Target.TEXT_INPUT)))\n"
    )
    started = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, _ = child.communicate(timeout=30)
        assert child.returncode == 0
        assert stdout.strip() == "1"
        assert time.monotonic() - started < 15
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()


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
