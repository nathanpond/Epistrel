"""The repair ladder's deterministic rungs and classification (``core.repair.rungs``; #151).
No database: sentences, rung 1's narrowing, rung 2's substitution and FR-REPAIR-4's classes."""

from __future__ import annotations

from epistrel.core.repair.rungs import (
    FALLBACK,
    NARROWING,
    REGENERATION,
    SUBSTITUTION,
    RepairClass,
    classify,
    locate,
    narrow,
    sentences,
    substitute,
)

TEXT = "He hangs his coat on the hook. Then he plays with the hem! Fine."


def test_sentences_end_at_terminal_punctuation_and_closing_quotes() -> None:
    assert sentences(TEXT) == [(0, 30), (31, 58), (59, 64)]
    quoted = "He said “stop.” Then he left… Done"
    assert [quoted[s:e] for s, e in sentences(quoted)] == [
        "He said “stop.”",
        "Then he left…",
        "Done",
    ]
    assert sentences("") == []


def test_narrowing_drops_the_violating_sentences_and_never_all() -> None:
    span = locate(TEXT, ["hem"])
    assert span is not None
    assert narrow(TEXT, [span]) == "He hangs his coat on the hook. Fine."
    # Complement: nothing would be left, or no span: rung 1 does not apply.
    assert narrow("I'm 70.", [(4, 6)]) is None
    assert narrow(TEXT, []) is None


def test_locate_matches_whole_words_case_insensitively() -> None:
    assert locate("The Hook is here.", [None, "hook"]) == (4, 8)
    assert locate("A hooked fish.", ["hook"]) is None


def test_substitution_swaps_every_whole_word_occurrence() -> None:
    assert substitute("I'm 70. Yes, 70!", "70", "64") == "I'm 64. Yes, 64!"
    assert substitute("My degree is from Florida.", "florida", "OSU") == "My degree is from OSU."
    # Complement: an absent surface, a part of a word or an empty value changes nothing.
    assert substitute("I'm 700.", "70", "64") is None
    assert substitute("I'm 70.", "70", " ") is None


def test_classification_follows_the_rung_and_the_neighborhood() -> None:
    assert classify(NARROWING, 3) is RepairClass.COSMETIC
    assert classify(SUBSTITUTION, 1) is RepairClass.COSMETIC
    assert classify(SUBSTITUTION, 2) is RepairClass.SUBSTITUTION
    assert classify(REGENERATION, 5) is RepairClass.COSMETIC
    assert classify(REGENERATION, 6) is RepairClass.CASCADE
    assert classify(SUBSTITUTION, 4, cascade_threshold=3) is RepairClass.CASCADE
    assert classify(FALLBACK, 0) is RepairClass.CASCADE
