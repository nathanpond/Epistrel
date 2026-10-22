"""The harness smoke suite (#134): Lite turns end to end, on any model.

Each case is one turn of its own scratch story (a traveller and an innkeeper): the player asks
the innkeeper to use one given word in the reply. It is the live path the harness drives —
``Engine.respond`` with the input gate, one ``generation`` call and the output gate — and it
measures nothing NFR-QUAL floors cover; it shows that a run reaches the model, plays real turns
and scores them. The quality suites are #157's and #158's.

- ``smoke.turn_completed`` (rate, ≥ 95 %): the turn released a non-empty reply that is not the
  safe fallback, and kept its design (``tools.model_matrix.design_problems``: one generation
  call, no call outside it, within the turn's call cap).
- ``smoke.instruction_followed`` (rate, ≥ 80 %): the reply contains the requested word or a
  regular inflection of it ("candles" for "candle"; the owner's decision on #437, made before
  the re-run, not tuned to it). A compound ("candlestick") or another word does not count.

Its stub answers are scripted per case (the word included), so CI gates on its stub run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import Field

from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.orchestrator.lite.schemas import SAFE_FALLBACK, RespondRequest
from epistrel.orchestrator.models.registry import GENERATION
from epistrel.tools.model_matrix import TurnCalls, design_problems, designed_calls

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

PLAYER = "Wren"
INNKEEPER = "Maren"
CAST = (
    CharacterSpec(name=PLAYER, definition="A traveller, curious.", true_age=29, is_player=True),
    CharacterSpec(
        name=INNKEEPER,
        definition="The innkeeper: warm, plain-spoken, glad to help a guest.",
        true_age=46,
    ),
)
TURN_COMPLETED = "smoke.turn_completed"
INSTRUCTION_FOLLOWED = "smoke.instruction_followed"


class SmokeCase(CaseModel):
    say: str = Field(min_length=1, max_length=500)
    keyword: str = Field(pattern=r"^[a-z]{3,20}$")


VOWELS = frozenset("aeiou")


def inflections(word: str) -> frozenset[str]:
    """``word`` and its regular English inflections (#437): plural or third person (-s, -es,
    -ies, -ves) and past or participle (-ed, -d, -ing, with the final e dropped and the final
    consonant doubled). Deterministic and dependency-free; it may name a form that is not a
    real word, which costs nothing, since only the reply's words are matched against it."""
    w = word.lower()
    forms = {w, w + "s", w + "es", w + "ed", w + "ing"}
    if w.endswith("e"):
        forms |= {w + "d", w[:-1] + "ing"}
    if w.endswith("y") and len(w) > 1 and w[-2] not in VOWELS:
        forms |= {w[:-1] + "ies", w[:-1] + "ied"}
    if w.endswith("fe"):
        forms.add(w[:-2] + "ves")
    elif w.endswith("f"):
        forms.add(w[:-1] + "ves")
    consonant_vowel_consonant = (
        len(w) >= 3 and w[-3] not in VOWELS and w[-2] in VOWELS and w[-1] not in VOWELS | set("wxy")
    )
    if consonant_vowel_consonant:
        forms |= {w + w[-1] + "ed", w + w[-1] + "ing"}
    return frozenset(forms)


def contains_word(text: str, word: str) -> bool:
    """``text`` uses ``word`` or one of its ``inflections`` as a whole word, in any case."""
    alternatives = "|".join(re.escape(f) for f in sorted(inflections(word), key=len, reverse=True))
    return re.search(rf"\b(?:{alternatives})\b", text, re.IGNORECASE) is not None


@register
class SmokeSuite(Suite):
    name = "smoke"
    title = "Harness smoke: Lite turns end to end"
    metrics = (
        Metric(
            TURN_COMPLETED,
            Rule.rate,
            0.95,
            Direction.at_least,
            "the turn released a non-empty reply, not the safe fallback, on its designed calls",
        ),
        Metric(
            INSTRUCTION_FOLLOWED,
            Rule.rate,
            0.80,
            Direction.at_least,
            "the reply uses the word the player asked for, or an inflection of it "
            '("candles" for "candle")',
        ),
    )
    reading_notes = (
        "`smoke.instruction_followed` counts an inflected form of the requested word (plural "
        '-s/-es/-ies/-ves, -ed/-d/-ing: "candles" for "candle") as following the instruction; '
        'a compound ("candlestick"), another word, or no use of the word fails (#437).',
    )
    ci_gate = True
    case_model = SmokeCase

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, SmokeCase)  # noqa: S101 - load() validated it as case_model
        created = await ctx.story(StorySpec(characters=list(CAST)))
        actor = next(c.id for c in created.characters if c.name == PLAYER)
        request = RespondRequest.model_validate(
            {
                "actor_character_id": actor,
                "player_input": {"say": case.say},
                "prompt_params": {"length": "short"},
            }
        )
        result = await ctx.engine.respond(ctx.principal, created.story_id, request)
        reserved = sum(result.budget.reserved.values()) if result.budget else 0
        turn = TurnCalls(
            result.model_calls,
            designed_calls(reserved),
            [d.cause for d in result.degradation_details if d.step == SAFE_FALLBACK],
            result.budget.max_calls if result.budget else None,
        )
        problems = design_problems([turn], designed_calls(0))
        reply = result.message.text
        completed = bool(reply.strip()) and SAFE_FALLBACK not in result.degradation and not problems
        return Outcome(
            {
                TURN_COMPLETED: completed,
                INSTRUCTION_FOLLOWED: contains_word(reply, case.keyword),
            },
            output=reply,
            detail={"design_problems": problems, "degradation": list(result.degradation)},
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, SmokeCase)  # noqa: S101 - load() validated it as case_model
        return {GENERATION.name: f'{INNKEEPER} nods. "Of course: {case.keyword}, as you asked."'}


__all__ = [
    "CAST",
    "INSTRUCTION_FOLLOWED",
    "TURN_COMPLETED",
    "SmokeCase",
    "SmokeSuite",
    "contains_word",
    "inflections",
]
