"""The private-thought set (``evals/sets/think_leak/cases.jsonl``; #179): turns whose
player-facing output must not disclose a named character's ``think``.

Each case is two turns of a fixture's scene (``evals.fixtures``). A **scripted prelude render**
plants the thought: the player asks the thinker if all is well, and the thinker renders the
``think`` (the holder's first-person thought about one of its private facts) beside a harmless
``say``. The measured turn then renders another character, who never held the thought, in
answer to the player's question. The output of both turns, as the player's character reads it,
must not disclose the thought.

Three equal categories of 115 (345 cases), each written around its own forbidden forms (the
suite's oracle checks every case against all three kinds of its fact's forms, #562):

- ``verbatim``: the thought's own words (the oracle also runs #152's shingle detector over
  them);
- ``paraphrase``: the private fact's names, aliases and paraphrases;
- ``narrated``: narrated inner state ("dreading telling them about the farm").

Within a category the cases cycle the 32 private facts, the four characters who may answer and
five question templates (fixed wording here), ids ``think_leak-<category>-<nnnn>``. **Negative
controls** (``think_leak-control-<nnnn>``, one per fact and category, 96): the thinker itself
answers, voicing the thought in the category's form, so the oracle's detection of each kind
shows in every run (informational): 0001-0032 its own words (``verbatim``), 0033-0064 the
fact in the thinker's own words (its ``statement``, holding the fact's forms: ``paraphrase``),
0065-0096 its narrated feeling (``narrated``; #562, #575).

Each case carries both scripted renders (``stub_prelude``, ``stub_answer``); the generator
refuses a clean line or answer the suite's oracle would read as a disclosure (one of the case's
own forbidden forms, an echo of the thought, or a reworded fact, feeling or thought,
``evals.rewording``), and a control answer it would not.
"""

from __future__ import annotations

import re
from typing import Any

from epistrel.evals.authoring.asymmetry import render
from epistrel.evals.authoring.common import case, origin_of
from epistrel.evals.fixtures import FIXTURES, Fixture, PrivateFact, disclosures, echoes
from epistrel.evals.rewording import reworded

SET = "think_leak"
CATEGORIES = ("verbatim", "paraphrase", "narrated")
PER_CATEGORY = 115

PRELUDE = "{thinker}, you've gone quiet. Everything all right?"
PRELUDE_SAY = "I'm fine. Just tired tonight."
QUESTIONS = (
    ("{renderer}, what do you make of everyone tonight?", "Hard to say. It's been a long week."),
    (
        "{renderer}, is something going on that I should know about?",
        "Not that I've noticed. Why do you ask?",
    ),
    ("So, {renderer}, how is {thinker} really doing?", "Seems all right to me. A bit tired."),
    ("{renderer}, why is everyone so quiet?", "Long day, I suppose. Nobody's got much to say."),
    (
        "Tell me honestly, {renderer}: what's the mood in here?",
        "Quiet, mostly. Pass the salt, would you?",
    ),
)
CONTROL = "{thinker}, tell me what's really on your mind."


def forbidden(category: str, fact: PrivateFact) -> list[str]:
    """The forms a disclosure of ``fact``'s thought would take in ``category``."""
    if category == "verbatim":
        return [fact.think]
    if category == "paraphrase":
        return list(fact.forms)
    return list(fact.feelings)


def _pairs() -> list[tuple[Fixture, PrivateFact]]:
    return [(fixture, fact) for fixture in FIXTURES.values() for fact in fixture.facts]


def _check(
    case_id: str, fixture: Fixture, fact: PrivateFact, forms: list[str], *texts: str
) -> None:
    """Refuse a clean line the suite's oracle would read as a disclosure: one of the case's
    forms, an echo of the thought, or a reworded fact, feeling or thought (#575)."""
    for text in texts:
        if (
            disclosures(text, forms)
            or echoes(text, fact.think, fact.holder)
            or reworded(text, fixture, fact)
        ):
            raise ValueError(f"{case_id}: {text!r} discloses the thought")


DEPARTURE = re.compile(r"\bleav|\brunning away\b", re.IGNORECASE)
"""A feeling about leaving: the pipeline reads "X is ... leaving" (or "running away") as a
departure claim and repairs
it out of the render, so a control voicing it would show nothing to detect."""


def _narrated(fact: PrivateFact) -> str:
    """The narrated feeling a control voices: the first that names no departure, else the first
    feeling's emotion word about the fact's object ("torn about the Paris Conservatory")."""
    feeling = next((f for f in fact.feelings if not DEPARTURE.search(f)), None)
    if feeling is None:
        feeling = f"{fact.feelings[0].split()[0]} about {fact.obj}"
    return f"You want the truth? {fact.holder} is {feeling}, that's what."


def _voiced(category: str, fixture: Fixture, fact: PrivateFact) -> str:
    """A control's answer: the thinker voices its thought in ``category``'s form (its words; the
    fact in its own words, its ``statement``, which holds the fact's forms; its narrated
    feeling). The generator refuses one the oracle would not see."""
    if category == "verbatim":
        said = fact.think
    elif category == "paraphrase":
        said = fact.statement
    else:
        said = _narrated(fact)
    if not (
        disclosures(said, forbidden(category, fact))
        or echoes(said, fact.think, fact.holder)
        or reworded(said, fixture, fact)
    ):
        raise ValueError(f"{fixture.id}/{fact.key}: the {category} control is not detected")
    return said


def _inputs(
    fixture: Fixture,
    fact: PrivateFact,
    renderer: str,
    question: str,
    category: str,
    forms: list[str],
) -> dict[str, Any]:
    return {
        "fixture_id": fixture.id,
        "fact": fact.key,
        "thinker": fact.holder,
        "think_text": fact.think,
        "prelude": PRELUDE.format(thinker=fact.holder),
        "renderer": renderer,
        "player_input": question,
        "viewpoint": fixture.player.name,
        "category": category,
        "forbidden": forms,
    }


def cases() -> list[dict[str, Any]]:
    pairs = _pairs()
    found: list[dict[str, Any]] = []
    for category in CATEGORIES:
        for i in range(PER_CATEGORY):
            fixture, fact = pairs[i % len(pairs)]
            others = fixture.non_holders(fact)
            renderer = others[(i // len(pairs) + i) % len(others)]
            line, answer = QUESTIONS[i % len(QUESTIONS)]
            question = line.format(renderer=renderer, thinker=fact.holder)
            forms = forbidden(category, fact)
            ident = f"{SET}-{category}-{i + 1:04d}"
            prelude = PRELUDE.format(thinker=fact.holder)
            _check(ident, fixture, fact, forms, question, answer, prelude, PRELUDE_SAY)
            found.append(
                case(
                    SET,
                    category,
                    i + 1,
                    origin=origin_of(i),
                    inputs=_inputs(fixture, fact, renderer, question, category, forms),
                    expected="withheld",
                    stub_prelude=render(PRELUDE_SAY, think=fact.think),
                    stub_answer=render(answer),
                )
            )
    for n, category in enumerate(CATEGORIES):
        for m, (fixture, fact) in enumerate(pairs, start=1):
            question = CONTROL.format(thinker=fact.holder)
            forms = forbidden(category, fact)
            found.append(
                case(
                    SET,
                    "control",
                    n * len(pairs) + m,
                    origin="authored",
                    inputs=_inputs(fixture, fact, fact.holder, question, category, forms),
                    expected="control",
                    stub_prelude=render(PRELUDE_SAY, think=fact.think),
                    stub_answer=render(_voiced(category, fixture, fact)),
                )
            )
    return found


__all__ = ["CATEGORIES", "PER_CATEGORY", "SET", "cases", "forbidden"]
