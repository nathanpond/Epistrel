"""S-COIN — lazy commitment and regeneration stability (FR-COMMIT-5,8; docs/04 §7; #154).

Structured turns on the stub: Wren (the player) asks Ada to flip a coin, and Ada's scripted
renders catch it. The coin's face is ``coin_face``, an author predicate that is functional (one
face at a time), so a regenerated message that read the replaced variant's result would see it
contradicted: the three cases prove the regeneration reads the story as it stood before the
message, and that a later message's regeneration does not.

- (a) *undetermined*: the coin is caught, its face unstated, no reaction → a thunk (open, no
  fact); regenerating that message may yield a result: the thunk leaves with the replaced
  variant and the new one commits "tails".
- (b) *same-message commit*: "catches it, sees heads, smiles" commits the face and the reaction
  together; regenerating this message re-rolls it to tails (scripted) and the reaction
  regenerates to match: the face is "tails" in canon and in the reply, "heads" is ``revised``
  and out of the timeline. The regenerate writes ``turn_report.regenerated_variant``, and the
  feedback aggregates count it as a regeneration (FR-FB-3).
- (c) *forward stickiness*: heads committed in message M, message M+1 reacts; regenerating
  M+1 with a render that claims tails leaves heads in force (its row byte-identical, the tails
  claim contradicted and repaired away), while rewinding to M and regenerating M itself
  re-rolls it.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import registry, store
from epistrel.core.tables.operations import REGENERATED_VARIANT
from epistrel.orchestrator.models.registry import SalienceSketch
from tests.auth_support import auth_settings
from tests.commit_support import salience_answer
from tests.event_support import rows
from tests.feedback_support import aggregates, rate
from tests.respond_support import body, played, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, render, structured_story

PLAYER_SAYS = "Ada, flip the coin."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _coin_story(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], str]:
    story, ids = await structured_story(client, "Wren", "Ada")
    async with engine.begin() as conn:
        coin = await store.add_entity(conn, story, "item", "coin")
        await registry.register_author(
            conn,
            story,
            registry.ScenarioRules.parse(
                {
                    "predicates": [
                        {
                            "name": "coin_face",
                            "object_type": "str",
                            "cardinality": "functional",
                            "docs": "The face a coin shows.",
                        }
                    ]
                }
            ),
        )
    return story, ids, coin


def _caught(face: str | None, reaction: str) -> str:
    """Ada's render: she catches the coin (its face claimed, or left unstated) and reacts."""
    if face is None:
        return render(beat("do", HIDDEN), beat("say", reaction, intent="sincere"))
    return render(
        beat(
            "do",
            f"Ada catches the coin and looks: {face}.",
            propositions=[claim("coin", "coin_face", face)],
        ),
        beat("say", reaction, intent="sincere"),
    )


HIDDEN = "Ada catches the coin and keeps its face hidden."
UNSTATED = salience_answer(
    (
        2,
        HIDDEN,
        SalienceSketch(subject="the coin", relation="coin_face", object="something"),
    )
)
"""The salience answer for ``HIDDEN``: the coin's face is a salient open reference."""


async def _faces(engine: AsyncEngine, story: str) -> list[Any]:
    """The coin's face facts, with their state and membership."""
    return await rows(
        engine,
        "SELECT f.id, f.commit_state, f.membership, f.committed_at_position, p.literal "
        "FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "JOIN predicates q ON q.id = f.predicate_id WHERE f.story_id = :s "
        "AND q.name = 'coin_face' ORDER BY f.id",
        s=story,
    )


def _active(faces: list[Any]) -> list[str]:
    return [
        f.literal["value"]
        for f in faces
        if f.membership == "active" and str(f.commit_state) == "committed"
    ]


async def _regenerate(client: Any, story: str) -> dict[str, Any]:
    response = await client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def _thunks(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine, "SELECT id, membership, state FROM thunks WHERE story_id = :s", s=story
    )


@pytest.mark.part("M4", "FR-COMMIT-5,8")
async def test_s_coin(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring: cases (a), (b) and (c), each with its complement."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # (a) undetermined: a thunk, and the regeneration may resolve it differently.
        story, ids, _ = await _coin_story(client, engine)
        stub.script("generation", [_caught(None, "Well?")])
        stub.script("salience_detection", [UNSTATED])
        await played(client, story, body(ids["Wren"], PLAYER_SAYS))
        (open_,) = await _thunks(engine, story)
        assert (open_.membership, str(open_.state)) == ("active", "open")
        assert await _faces(engine, story) == []  # no world value
        stub.script("generation", [_caught("tails", "Tails.")])
        await _regenerate(client, story)
        (left,) = await _thunks(engine, story)
        assert left.membership == "dormant"  # the open thunk left with its variant
        assert _active(await _faces(engine, story)) == ["tails"]

        # (b) same-message commit: the face and the reaction regenerate together.
        story, ids, _ = await _coin_story(client, engine)
        stub.script("generation", [_caught("heads", "Heads! I win.")])
        first = await played(client, story, body(ids["Wren"], PLAYER_SAYS))
        assert "heads" in first["message"]["text"]
        rated = await rate(client, story, first["message"]["message_id"], {"rating": 2})
        assert rated.status_code == 201, rated.text
        (heads,) = await _faces(engine, story)
        assert _active([heads]) == ["heads"]
        stub.script("generation", [_caught("tails", "Tails. You win.")])
        again = await _regenerate(client, story)
        assert (again["previous_variant"], again["variant"]) == (1, 2)
        faces = await _faces(engine, story)
        assert _active(faces) == ["tails"]  # re-rolled on its own regeneration
        assert "tails" in again["message"]["text"]
        assert "Tails. You win." in again["message"]["text"]  # the reaction matches the result
        old = next(f for f in faces if f.id == heads.id)
        assert (str(old.commit_state), old.membership) == ("revised", "dormant")
        report = (
            await rows(
                engine,
                "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
                "AND turn_variant = 2",
                s=story,
            )
        )[0].turn_report
        assert report[REGENERATED_VARIANT] == 1
        assert report["commit"]["reverted_facts"] >= 1
        totals = (await aggregates(client, "profile_version", story_id=story))["totals"]
        assert totals["signals"]["regenerations"] == 1  # FR-FB-3 counts it (#126)

        # (c) forward stickiness: a later message's regeneration keeps the result.
        story, ids, _ = await _coin_story(client, engine)
        stub.script("generation", [_caught("heads", "Heads.")])
        await played(client, story, body(ids["Wren"], PLAYER_SAYS))
        stub.script(
            "generation", [render(beat("say", "Heads means I go first.", intent="sincere"))]
        )
        await played(client, story, body(ids["Wren"], "So who goes first?"))
        before = await _faces(engine, story)
        assert _active(before) == ["heads"]
        stub.script(
            "generation",
            [
                render(
                    beat("say", "I go first.", intent="sincere"),
                    beat(
                        "do",
                        "Ada glances at the coin: tails.",
                        propositions=[claim("coin", "coin_face", "tails")],
                    ),
                )
            ],
        )
        await _regenerate(client, story)  # message M+1
        after = await _faces(engine, story)
        assert [dict(f._mapping) for f in after if f.id == before[0].id] == [
            dict(before[0]._mapping)
        ]  # the result is identical before and after
        assert _active(after) == ["heads"]
        # Regenerating M itself may re-roll it: rewind to M, then regenerate M.
        rewound = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE
        )
        assert rewound.status_code == 200, rewound.text
        stub.script("generation", [_caught("tails", "Tails.")])
        await _regenerate(client, story)
        assert _active(await _faces(engine, story)) == ["tails"]
