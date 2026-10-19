"""The smoke suite's instruction scorer and labeled set (#437; the owner's decision of
2026-10-18, made before the re-run): an inflected form of the requested word follows the
instruction, and the set holds 60 cases per seed. Harness smoke, no NFR-QUAL floor."""

from __future__ import annotations

from pathlib import Path

import pytest

from epistrel.evals import SUITES, Direction, Rule
from epistrel.evals.report import Host, Report, SuiteResult, render_markdown
from epistrel.evals.suites.smoke import (
    INSTRUCTION_FOLLOWED,
    TURN_COMPLETED,
    SmokeCase,
    SmokeSuite,
    contains_word,
)

SETS = Path(__file__).resolve().parents[2] / "evals" / "sets"


@pytest.mark.parametrize(
    ("reply", "word"),
    [
        ("Mind the candle on the stair.", "candle"),
        ("Mind the candles on the stair.", "candle"),  # the miss #134 found by hand
        ("The old man candled the eggs.", "candle"),
        ("She was candling eggs all morning.", "candle"),
        ("Two ferries cross at dawn.", "ferry"),
        ("The cart was ferried over.", "ferry"),
        ("Wolves and halves.", "half"),
        ("The kitchen knives are sharp.", "knife"),
        ("Every compass needle points north; compasses rarely lie.", "compass"),
        ("We anchored in the bay.", "anchor"),
        ("They were quilling the letter.", "quill"),
        ("The pot was skimmed and the soup ladled.", "ladle"),
        ("Two honeys from the hills.", "honey"),
        ("HERONS on the river.", "heron"),
        ("The heron's nest is up the river.", "heron"),
        ("A lantern-lit road.", "lantern"),
    ],
)
def test_an_inflected_form_follows_the_instruction(reply: str, word: str) -> None:
    """The requested word, or a regular inflection of it (plural, -ed/-d, -ing), counts."""
    assert contains_word(reply, word)


@pytest.mark.parametrize(
    ("reply", "word"),
    [
        ("The road is long and the inn is warm.", "candle"),  # the word is absent
        ("A brass candlestick on the sill.", "candle"),  # a compound is another word
        ("The riverside path floods.", "river"),
        ("Peppery stew tonight.", "pepper"),  # a derivation, not an inflection
        ("A torch lights the way.", "lantern"),  # a synonym is not the word
        ("Hand me the scandal sheet.", "candle"),
        ("Owls hoot; the owlet sleeps.", "crow"),
        ("Honies is not how it is spelt.", "honey"),  # vowel + y keeps the y
        ("", "candle"),
    ],
)
def test_an_unrelated_word_or_no_use_of_the_word_still_fails(reply: str, word: str) -> None:
    """Complement: absence, a compound, a derivation or another word does not follow it."""
    assert not contains_word(reply, word)


def test_the_set_holds_sixty_cases_and_the_floors_are_unchanged() -> None:
    """60 cases per seed (the SD term reflects the model, not a small n), every keyword
    distinct and named in its case's request. The floors and the rate rule are unchanged:
    95 % completed, 80 % followed."""
    suite = SUITES["smoke"]
    assert isinstance(suite, SmokeSuite)
    cases = suite.load(SETS)
    assert len(cases) == 60
    assert all(isinstance(case, SmokeCase) for case in cases)
    keywords = [case.keyword for case in cases if isinstance(case, SmokeCase)]
    assert len(set(keywords)) == 60
    assert all(f'"{case.keyword}"' in case.say for case in cases if isinstance(case, SmokeCase))
    floors = {m.id: (m.rule, m.floor, m.direction) for m in suite.metrics}
    assert floors == {
        TURN_COMPLETED: (Rule.rate, 0.95, Direction.at_least),
        INSTRUCTION_FOLLOWED: (Rule.rate, 0.80, Direction.at_least),
    }


def test_the_metric_and_the_report_say_inflections_count() -> None:
    """The metric's description and the report's "How to read" section both say so."""
    suite = SUITES["smoke"]
    followed = next(m for m in suite.metrics if m.id == INSTRUCTION_FOLLOWED)
    assert "inflection" in followed.description
    report = Report(
        run_id="abc-2026-10-19",
        started_at="2026-10-19T00:00:00+00:00",
        model="m",
        fingerprint="sha256:" + "a" * 64,
        fingerprint_profile="Structured (M4) v1",
        adapter="openai",
        engine_sha="abc",
        host=Host(kind="local"),
        seed_base=1,
        seeds=[1, 2],
    )
    report.merge(SuiteResult.errored(suite, [], [1, 2]))
    how_to_read = render_markdown(report).split("## How to read this report", 1)[1]
    assert suite.reading_notes
    assert "inflected form" in how_to_read
    assert f"`{INSTRUCTION_FOLLOWED}`" in how_to_read
