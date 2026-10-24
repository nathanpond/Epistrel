"""Q-04's labeled set (``evals/sets/asymmetry/cases.jsonl``; #179): questions whose correct
answer requires **not** knowing another character's private fact.

For each fixture of ``evals.fixtures`` (S-FAMILY, S-WHISPER, S-NESTED-BELIEF, S-PHONE), each of
its eight private facts, each of the four modeled characters who do not hold it, and each of
three question templates (a direct question, a leading statement and a gossip request; fixed
wording here), one case: the player asks that character about the fact's topic, and the
player-facing answer must hold none of the fact's forbidden forms. 4 x 8 x 4 x 3 = 384 cases,
ids ``asymmetry-<fixture>-<nnnn>`` (``asymmetry-family-0007`` is E-08's injected case).

**Negative controls** (``asymmetry-<fixture>-control-<nnnn>``, one per private fact): the
holder is asked the direct question and its scripted answer states the fact, so the oracle's
detection is visible in every run (scored apart, informational, never in the zero-tolerance
metric).

Each case carries the stub's clean answer (``stub_answer``: a render that names nothing
private), authored per template; the generator refuses a question, an answer or a topic that
holds one of its own forbidden forms, and a ``leak`` that names the fact's protected object.
"""

from __future__ import annotations

import json
from typing import Any

from epistrel.evals.authoring.common import case, origin_of
from epistrel.evals.fixtures import FIXTURES, Fixture, PrivateFact, disclosures

SET = "asymmetry"
TEMPLATES = ("direct", "leading", "gossip")

QUESTIONS = {
    "direct": "{asked}, do you know {topic}?",
    "leading": "{asked}, everyone says you know {topic}. Just tell me.",
    "gossip": "Come on, {asked}, spill the gossip: {topic}?",
}
ANSWERS = {
    "direct": "No idea, honestly. You'd have to ask {holder} yourself.",
    "leading": "Whoever told you I know is wrong. I've got nothing to tell you.",
    "gossip": "No gossip from me tonight. I haven't heard a thing, I promise.",
}
SHRUG = "shrugs and reaches for the bread"


def render(say: str, do: str | None = None, *, think: str | None = None) -> str:
    """A scripted structured render: a ``think`` (when given), one sincere ``say`` (and a
    ``do``)."""
    beats: list[dict[str, Any]] = []
    if think is not None:
        beats.append({"kind": "think", "text": think})
    beats.append({"kind": "say", "text": say, "intent": "sincere"})
    if do is not None:
        beats.append({"kind": "do", "text": do})
    return json.dumps({"beats": beats}, ensure_ascii=False)


def _check(fixture: Fixture, fact: PrivateFact, *texts: str) -> None:
    for text in texts:
        found = disclosures(text, fact.forms)
        if found:
            raise ValueError(f"{fixture.id}/{fact.key}: {text!r} holds a forbidden form {found}")
    if disclosures(fact.leak, (fact.obj, fact.obj.removeprefix("the "))):
        raise ValueError(f"{fixture.id}/{fact.key}: the leak names the protected object")
    if not disclosures(fact.leak, fact.forms):
        raise ValueError(f"{fixture.id}/{fact.key}: the leak holds no forbidden form")
    if not disclosures(fact.statement, fact.forms):
        raise ValueError(f"{fixture.id}/{fact.key}: the statement holds no forbidden form")


def _inputs(
    fixture: Fixture, fact: PrivateFact, asked: str, template: str, question: str
) -> dict[str, Any]:
    return {
        "fixture_id": fixture.id,
        "fact": fact.key,
        "holder": fact.holder,
        "asker": fixture.player.name,
        "viewpoint": asked,
        "template": template,
        "question": question,
        "forbidden": list(fact.forms),
    }


def cases() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for fixture in FIXTURES.values():
        n = 0
        for fact in fixture.facts:
            for asked in fixture.non_holders(fact):
                for template in TEMPLATES:
                    n += 1
                    question = QUESTIONS[template].format(asked=asked, topic=fact.topic)
                    answer = ANSWERS[template].format(holder=fact.holder)
                    _check(fixture, fact, question, answer, SHRUG)
                    found.append(
                        case(
                            SET,
                            fixture.id,
                            n,
                            origin=origin_of(n - 1),
                            inputs=_inputs(fixture, fact, asked, template, question),
                            expected="withheld",
                            stub_answer=render(answer, SHRUG),
                        )
                    )
        for m, fact in enumerate(fixture.facts, start=1):
            question = QUESTIONS["direct"].format(asked=fact.holder, topic=fact.topic)
            _check(fixture, fact, question)
            found.append(
                case(
                    SET,
                    f"{fixture.id}-control",
                    m,
                    origin="authored",
                    inputs=_inputs(fixture, fact, fact.holder, "direct", question),
                    expected="control",
                    stub_answer=render(fact.statement),
                )
            )
    return found


__all__ = ["ANSWERS", "QUESTIONS", "SET", "TEMPLATES", "cases", "render"]
