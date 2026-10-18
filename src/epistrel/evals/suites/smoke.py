"""The harness smoke suite (#134): Lite turns end to end, on any model.

Each case is one turn of its own scratch story (a traveller and an innkeeper): the player asks
the innkeeper to use one given word in the reply. It is the live path the harness drives —
``Engine.respond`` with the input gate, one ``generation`` call and the output gate — and it
measures nothing NFR-QUAL floors cover; it shows that a run reaches the model, plays real turns
and scores them. The quality suites are #157's and #158's.

- ``smoke.turn_completed`` (rate, ≥ 95 %): the turn released a non-empty reply that is not the
  safe fallback, and kept its design (``tools.model_matrix.design_problems``: one generation
  call, no call outside it, within the turn's call cap).
- ``smoke.instruction_followed`` (rate, ≥ 80 %): the reply contains the requested word.

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


def contains_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


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
            "the reply uses the word the player asked for",
        ),
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


__all__ = ["CAST", "INSTRUCTION_FOLLOWED", "TURN_COMPLETED", "SmokeCase", "SmokeSuite"]
