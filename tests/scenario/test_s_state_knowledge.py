"""S-STATE-KNOWLEDGE: values are not knowledge (docs/04 §7; FR-STATE-8, FR-EPI-4; #191).

Cal (the player) and Mira the smith share the forge, under per-character partitions (testimony
``defaulted``, perception ``co_location``). Cal holds 500 gold, a player-visible State. Played
end to end through ``respond`` with the stub adapter, Mira's render scripted by name and the
narrator's by its contract line.

- Before Mira has any acquisition path, her sincere "You have 500 gold" is true but withheld:
  the objective relation is ``true`` (the State's fold), her belief relation ``unknown``, and
  #165's ``could_have_known`` finds no path, so it is a hallucination, repaired (#151 narrows
  the sentence away); no belief is written.
- Then the narrator renders Cal counting his coins on the counter, "500 gold in all": Mira
  perceives it at ``content`` fidelity, so its ``state_value`` claim (supported against the
  fold) is her observation (``observation.observe`` through ``RouteBeat.claims``): she now
  knows Cal's gold is 500, and her Beliefs section says so.
- The same line is then legitimate: ``sincere``, belief-consistent, standing.
- Cal's gold drops to 400 through the consumer API while Mira is not watching: her "500" is a
  ``sincere_error`` (an established false belief), standing and never corrected; a deceptive
  "900" against her belief of 500 is a ``lie``. Neither is a hallucination.

Complements: the omniscient director context (the narrator's ``## Tracked values``) holds the
value from the start; Mira's render context before her observation holds no value at all, and
after it only her belief. With ``partitions`` off (a second story): ``could_have_known`` is
``NotApplicable``, no State belief is written, and her claim takes the objective path (no
speech classification) and stands as true.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind, StateChangeSource
from epistrel.core.epistemic import replay
from epistrel.core.membership import Position
from epistrel.core.propositions.vocabulary import STATE_VALUE
from epistrel.core.states import verify as sv
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import state_support as ss
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

KNOWS = "You have 500 gold."
GREETING = "Welcome back to the forge."
COUNTED = "Cal counts his coins on the counter: 500 gold in all."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _forge(
    client: Any, engine: AsyncEngine, *, partitions: bool = True
) -> tuple[str, dict[str, str], str]:
    """The story: Cal and Mira at the forge, Cal holding 500 gold. Answers the story, the
    cast and the gold State's id."""
    story, ids = await structured_story(client, "Cal", "Mira")
    await ts.enable_testimony(
        engine, story, "defaulted", level="co_location", partitions=partitions
    )
    await ss.set_level(engine, story, "on")
    (gold,) = await ss.define_all(engine, story, es.gold())
    forge = await ps.place_entity(engine, story, "forge")
    for character in ids.values():
        await ps.put(engine, story, character, forge)
    await ss.change(
        engine,
        story,
        ss.set_to(
            "gold", 500, scope_key=ids["Cal"], reason="a purse", source=StateChangeSource.CONSUMER
        ),
    )
    assert gold.id is not None
    return story, ids, gold.id


async def _say(
    client: Any, stub: Any, story: str, cal: str, *beats: dict[str, Any], narration: str
) -> dict[str, Any]:
    """A turn: Cal leans on the counter (an action naming no one), the narrator narrates
    ``narration`` and Mira answers ``beats``."""
    stub.reset()
    ms.renders_for(stub, "Mira", es.render(*beats))
    es.narrator(stub, es.render(beat("narration", narration)))
    answer = await respond(client, story, body(cal, None, do="leans on the counter"))
    assert answer.status_code == 200, answer.text
    played: dict[str, Any] = answer.json()
    return played


async def _speech(engine: AsyncEngine, story: str) -> dict[str, Any]:
    """The speech dimensions of the latest turn's ``state_value`` claim."""
    report = await turn_report(engine, story)
    found = [v for v in report["verify"]["verdicts"] if "speech" in v]
    assert len(found) == 1, found
    speech: dict[str, Any] = found[0]["speech"]
    return {**speech, "kind": found[0]["kind"], "repairs": report.get("repairs", [])}


async def _state_beliefs(engine: AsyncEngine, story: str, holder: str) -> list[Any]:
    """``holder``'s open ``state_value`` beliefs, with their value and producing transition."""
    return await rows(
        engine,
        "SELECT b.id, b.confidence_level, p.literal, t.kind FROM beliefs b "
        "JOIN propositions p ON p.id = b.proposition_id "
        "JOIN belief_transitions t ON t.id = b.provenance "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND p.predicate_id = :v "
        "AND b.held_to_turn IS NULL AND b.tx_to IS NULL",
        s=story,
        h=holder,
        v=STATE_VALUE,
    )


@pytest.mark.part("M6", "FR-STATE-8; FR-EPI-4")
async def test_s_state_knowledge(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, gold = await _forge(client, engine)
        cal, mira = ids["Cal"], ids["Mira"]
        claim = sv.proposition(gold, cal, sv.number_literal(500))

        # -- No acquisition path: a true value she cannot know is a hallucination, repaired.
        played = await _say(
            client,
            stub,
            story,
            cal,
            beat("say", f"{KNOWS} {GREETING}", intent="sincere"),
            narration="The forge glows.",
        )
        found = await _speech(engine, story)
        assert (found["classification"], found["belief_relation"], found["licence"]) == (
            "hallucination_repaired",
            "unknown",
            None,
        )
        assert found["kind"] == "supported"  # the objective relation: the fold says 500
        assert [r["rung"] for r in found["repairs"]] == [1]
        assert "500" not in played["message"]["text"]
        assert GREETING in played["message"]["text"]
        assert await _state_beliefs(engine, story, mira) == []
        async with engine.connect() as conn:
            path = await replay.could_have_known(
                conn, story, mira, claim, Position.end_of(played["turn"])
            )
        assert isinstance(path, replay.NoPath)
        # The director (narrator) context holds the value; Mira's holds none at all.
        (narrator_prompt,) = es.prompts(stub, "NarratorRender")
        assert f"Cal (id {cal}) 500 gp" in narrator_prompt
        (before,) = ms.render_prompts(stub, "Mira")
        assert "500" not in before

        # -- She watches Cal count his coins: an observation of the counted value.
        await _say(client, stub, story, cal, beat("say", GREETING), narration=COUNTED)
        (belief,) = await _state_beliefs(engine, story, mira)
        assert (belief.kind, belief.confidence_level, belief.literal["value"]) == (
            "observation",
            "known",
            500,
        )
        assert await _state_beliefs(engine, story, cal) == []  # the player is never modeled

        # -- Now the same reference is legitimate, and her context carries her belief.
        played = await _say(
            client,
            stub,
            story,
            cal,
            beat("say", f"{KNOWS} {GREETING}", intent="sincere"),
            narration="The forge glows.",
        )
        found = await _speech(engine, story)
        assert (found["classification"], found["belief_relation"]) == ("sincere", "consistent")
        assert found["repairs"] == []
        assert KNOWS in played["message"]["text"]
        (after,) = ms.render_prompts(stub, "Mira")
        assert "you know Cal's gold is 500" in after
        es.no_value(after, 500)  # the Tracked values section still lists no value

        # -- Spent out of her sight: her stale "500" is a sincere error, standing.
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold",
                400,
                scope_key=cal,
                reason="a debt",
                source=StateChangeSource.CONSUMER,
                position=Position(played["turn"], 0, 1),  # the inter-turn slot after it
            ),
            kind=OperationKind.CONTROL,
        )
        played = await _say(
            client,
            stub,
            story,
            cal,
            beat("say", f"{KNOWS} {GREETING}", intent="sincere"),
            narration="The forge glows.",
        )
        found = await _speech(engine, story)
        assert (found["classification"], found["belief_relation"], found["kind"]) == (
            "sincere_error",
            "consistent",
            "contradicted",
        )
        assert found["repairs"] == []
        assert KNOWS in played["message"]["text"]

        # -- A deceptive "900" against her belief of 500 is a lie, not a hallucination.
        played = await _say(
            client,
            stub,
            story,
            cal,
            beat("say", "You have 900 gold.", intent="deceptive"),
            narration="The forge glows.",
        )
        found = await _speech(engine, story)
        assert (found["classification"], found["belief_relation"]) == ("lie", "inconsistent")
        assert found["contrary_belief_ids"] == [str(belief.id)]
        assert found["repairs"] == []
        assert await ss.value(engine, story, "gold", cal) == 400  # a claim never changes it

        # -- Partitions off: no could-have-known, no State belief, the objective path.
        shared, shared_ids, shared_gold = await _forge(client, engine, partitions=False)
        await _say(
            client, stub, shared, shared_ids["Cal"], beat("say", GREETING), narration=COUNTED
        )
        played = await _say(
            client,
            stub,
            shared,
            shared_ids["Cal"],
            beat("say", f"{KNOWS} {GREETING}", intent="sincere"),
            narration="The forge glows.",
        )
        report = await turn_report(engine, shared)
        verdicts = [
            v
            for v in report["verify"]["verdicts"]
            if v.get("kind") in ("supported", "contradicted")
        ]
        assert [v["kind"] for v in verdicts] == ["supported"]
        assert "speech" not in verdicts[0]
        assert "repairs" not in report
        assert KNOWS in played["message"]["text"]
        assert await _state_beliefs(engine, shared, shared_ids["Mira"]) == []
        async with engine.connect() as conn:
            path = await replay.could_have_known(
                conn,
                shared,
                shared_ids["Mira"],
                sv.proposition(shared_gold, shared_ids["Cal"], sv.number_literal(500)),
                Position.end_of(played["turn"]),
            )
        assert isinstance(path, replay.NotApplicable)
