"""The erasure matcher's rules, one by one (ADR-154; #372, #373, #374): no database."""

from __future__ import annotations

from importlib import resources

import pytest

from epistrel.core.erasure import matcher as m
from epistrel.core.erasure.redact import Redactor, candidate_id
from epistrel.core.tables.media import MENTION_RULES

WILL = m.matcher(["Will Black"], ["Will", "Black"])


def found(matcher: m.Matcher, text: str) -> list[tuple[str, str]]:
    return [(text[x.start : x.end], x.rule.value) for x in matcher.mentions(text)]


def test_a_full_name_or_alias_is_certain_in_any_case() -> None:
    assert found(WILL, "Ty asked Will Black.") == [("Will Black", "name")]
    assert found(WILL, "ty asked WILL   BLACK") == [("WILL   BLACK", "name")]
    witch = m.matcher(["Morgana", "the Red Witch"])
    assert found(witch, "Beware the red witch, morgana.") == [
        ("the red witch", "name"),
        ("morgana", "name"),
    ]


def test_a_common_name_word_matches_only_capitalised() -> None:
    """Lowercase common words never match; capitalised mid-sentence they are certain."""
    assert found(WILL, "Ty will open the black door.") == []
    assert found(WILL, "Ty asked Will.") == [("Will", "part")]
    assert found(WILL, "Paint it Black, Ty.") == [("Black", "part")]
    rose = m.matcher(["Rose"])  # a one-word name that is a common word
    assert found(rose, "A rose by any other name.") == []
    assert found(rose, "Ask Rose.") == [("Rose", "name")]


def test_a_common_word_at_the_start_of_a_sentence_is_a_candidate() -> None:
    assert found(WILL, "Will you come?") == [("Will", "candidate")]
    assert found(WILL, "It rains. Will stays.") == [("Will", "candidate")]
    assert found(WILL, "Wait!\nWill stays.") == [("Will", "candidate")]
    assert found(WILL, 'He said, "Will you come?"') == [("Will", "candidate")]
    assert found(WILL, "Mr. Will came.") == [("Will", "part")]  # a title's dot
    assert found(WILL, "Ask (Will) now.") == [("Will", "part")]
    # The whole text, punctuation aside, is a name value, not prose.
    assert found(m.matcher(["Mum"]), "Mum") == [("Mum", "name")]
    assert found(m.matcher(["Mum"]), "Mum's tea is cold.") == [("Mum", "candidate")]


def test_a_rare_name_word_matches_in_any_case_as_written() -> None:
    """#373: no case-folding, so "Strauß" matches "strauß" but never "STRAUSS"."""
    strauss = m.matcher(["Jürgen Strauß"], ["Jürgen", "Strauß"])
    text = "Hi Strauß. hi strauß. Hi STRAUSS. Jürgen!"
    assert found(strauss, text) == [("Strauß", "part"), ("strauß", "part"), ("Jürgen", "part")]
    ipek = m.matcher(["İpek Kaya"], ["İpek", "Kaya"])
    assert found(ipek, "Hi İpek.") == [("İpek", "part")]


def test_hyphenated_halves_match_and_words_never_match_inside_words() -> None:
    """#374, and no over-match: "Ann" never matches inside "Annual" or "Annabel"."""
    mary = m.matcher(["Mary-Jane Watson"], ["Mary-Jane", "Watson", "Mary", "Jane"])
    assert found(mary, "Hi Mary. Hi Jane. Hi Mary-Jane.") == [
        ("Mary", "part"),
        ("Jane", "part"),
        ("Mary-Jane", "part"),
    ]
    ann = m.matcher(["Ann Lee"], ["Ann", "Lee"])
    assert found(ann, "The Annual report; Annabel and Leeds.") == []
    assert found(ann, "Ann's coat") == [("Ann", "candidate" if m.is_common("Ann") else "part")]


def test_the_guard_hashes_a_common_word_only_capitalised_mid_sentence() -> None:
    assert WILL.guard_terms() == ["^black", "^will", "will black"]
    assert m.guard_spans("Ty says it will rain tonight.") == []
    assert m.guard_spans("Will you come? Ask Will.") == ["^will"]
    assert m.matcher(["Marisol Quenby"], ["Marisol"]).guard_terms() == [
        "marisol",
        "marisol quenby",
    ]


def test_the_redactor_erases_certain_spans_and_confirmed_candidates() -> None:
    at = ("events", "e1", "payload.text")
    plain = Redactor(["Will Black"], ["Will", "Black"])
    assert plain.text("Will you come? Ask Will.", at) == "Will you come? Ask [erased]."
    (candidate,) = plain.candidates.values()
    assert candidate.candidate_id == candidate_id(*at, 0, "candidate")
    assert candidate.snippet == "Will you come? Ask Will."
    assert (plain.remaining, plain.unknown) == (1, frozenset())
    confirmed = Redactor(["Will Black"], ["Will", "Black"], confirmed=[candidate.candidate_id])
    assert confirmed.text("Will you come? Ask Will.", at) == "[erased] you come? Ask [erased]."
    assert confirmed.remaining == 0
    stale = Redactor(["Will Black"], ["Will", "Black"], confirmed=["f" * 32])
    assert stale.text("Ask Will.", at) == "Ask [erased]."
    assert stale.unknown == frozenset({"f" * 32})
    assert not plain.hits("Will you come?")
    assert plain.finds("Will you come?")


def test_a_snippet_is_short() -> None:
    text = "x" * 100 + " Will you come? " + "y" * 100
    start = text.index("Will")
    piece = m.snippet(text, start, start + 4)
    assert piece.startswith("…")
    assert piece.endswith("…")
    assert len(piece) <= 2 * m.SNIPPET_CHARS + 6


def test_the_common_word_list_is_bundled_with_its_licence() -> None:
    """Invariant 2: the data file states its source and its permissive licence."""
    text = resources.files("epistrel.core.erasure").joinpath(m.COMMON_WORDS_FILE).read_text()
    header = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    assert "SCOWL 2020.12.07" in header
    assert "Permission to use, copy, modify, distribute and sell" in header
    words = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert words == sorted(set(words))
    assert 9000 < len(words) < 13000
    assert {"will", "black", "rose", "grace"} <= m.common_words()
    assert not {"mary", "john", "marisol", "strauß"} & m.common_words()


@pytest.mark.parametrize("rule", list(m.Rule))
def test_the_rules_are_the_tables(rule: m.Rule) -> None:
    assert rule.value in MENTION_RULES
    assert len(MENTION_RULES) == len(m.Rule)
