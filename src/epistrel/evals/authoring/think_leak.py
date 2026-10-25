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
controls** (``think_leak-control-<nnnn>``, one per fact): the thinker itself answers, voicing
the thought, so the oracle's detection shows in every run (informational).

Each case carries both scripted renders (``stub_prelude``, ``stub_answer``); the generator
refuses a line or an answer that holds one of the case's own forbidden forms.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals.authoring.asymmetry import render
from epistrel.evals.authoring.common import case, origin_of
from epistrel.evals.fixtures import FIXTURES, Fixture, PrivateFact, disclosures, echoes

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


def _check(case_id: str, thinker: str, think: str, forms: list[str], *texts: str) -> None:
    for text in texts:
        if disclosures(text, forms) or echoes(text, think, thinker):
            raise ValueError(f"{case_id}: {text!r} discloses the thought")


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
            _check(ident, fact.holder, fact.think, forms, question, answer, prelude, PRELUDE_SAY)
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
    for m, (fixture, fact) in enumerate(pairs, start=1):
        question = CONTROL.format(thinker=fact.holder)
        forms = forbidden("verbatim", fact)
        found.append(
            case(
                SET,
                "control",
                m,
                origin="authored",
                inputs=_inputs(fixture, fact, fact.holder, question, "verbatim", forms),
                expected="control",
                stub_prelude=render(PRELUDE_SAY, think=fact.think),
                stub_answer=render(fact.think),
            )
        )
    return found


__all__ = ["CATEGORIES", "PER_CATEGORY", "SET", "cases", "forbidden"]
