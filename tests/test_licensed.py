"""#1060: ``orchestrator.verify.licensed``'s text rules (the end-to-end cases are
``tests/integration/test_m12_1060_licensed.py``)."""

from __future__ import annotations

import pytest

from epistrel.orchestrator.verify.licensed import content_words, directed, echoes, words


def test_words_drop_case_and_punctuation() -> None:
    assert words("It's time to vote.") == ["it's", "time", "to", "vote"]


@pytest.mark.parametrize(
    ("span", "inputs", "expected"),
    [
        ("It's time to vote", ["It's time to vote."], True),
        ("time to vote", ["Okay. It's TIME to vote!"], True),
        ("stands by the fire", ["", "Maren stands by the fire."], True),
        # Complements: words out of order, a word the input lacks, a part-word, nothing said.
        ("vote to time", ["It's time to vote."], False),
        ("It's time to vote now", ["It's time to vote."], False),
        ("ote", ["It's time to vote."], False),
        ("", ["It's time to vote."], False),
        ("...", ["..."], False),
        ("It's time to vote", [], False),
    ],
)
def test_an_echo_is_a_run_of_the_inputs_words(span: str, inputs: list[str], expected: bool) -> None:
    assert echoes(span, inputs) is expected


def test_content_words_skip_short_and_common_words() -> None:
    assert content_words("Maren settles onto the log beside the fire.") == {
        "maren",
        "settl",
        "beside",
        "fire",
    }


@pytest.mark.parametrize(
    ("mode", "about_actor", "steers", "expected"),
    [
        ("support", True, ["Take your place by the fire."], True),
        ("support", False, ["Take your place by the fire."], True),
        ("selfonly", True, ["Take your place by the fire."], True),
        ("selfonly", False, ["Take your place by the fire."], False),
        ("support", True, ["Keep the tone tense."], False),
        ("support", True, [], False),
    ],
)
def test_a_directive_licenses_what_its_words_bear_on(
    mode: str, about_actor: bool, steers: list[str], expected: bool
) -> None:
    span = "settles onto the log beside the fire"
    assert directed(span, steers, mode=mode, about_actor=about_actor) is expected  # type: ignore[arg-type]
