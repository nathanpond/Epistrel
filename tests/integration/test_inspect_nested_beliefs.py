"""#606: ``GET /stories/{id}/inspect/beliefs?kind=`` serves a character's nested and speech-act
beliefs: Jenni heard Bobby say Marcus knocked (``speech_act``) and thinks Bobby believes it
(``nested``, suspected), beside her own first-order belief. Each kind lists its own rows with
its fields (``attributed_holder_id``, ``attributed_level``, ``depth``, ``speaker_character_id``);
the default stays the first-order projection. Paged by belief id."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import BeliefConfidenceLevel, OperationKind
from epistrel.core.epistemic import nested, testimony, transitions
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from tests import canon_support as c
from tests import nested_support as ns
from tests import partition_support as parts
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.story_support import READ

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _at(op: Operation) -> Position:
    return testimony.witness_position(op.narrative_turn or 0, op.turn_variant or 0, 2)


async def _beliefs(client: Any, story: str, character: str, **params: Any) -> dict[str, Any]:
    found = await client.get(
        f"/stories/{story}/inspect/beliefs",
        params={"character": character, "viewpoint": "omniscient", **params},
        headers=READ,
    )
    assert found.status_code == 200, found.text
    body: dict[str, Any] = found.json()
    return body


async def test_nested_and_speech_act_beliefs_are_served(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    try:
        async with respond_client(stub_settings, engine) as (client, _epistrel):
            w = await ns.knock_world(client, engine, "Cal", "Bobby", "Jenni")
            story = w.story_id
            bobby, jenni = w.ids["Bobby"], w.ids["Jenni"]
            await ns.enable_nested(engine, story)
            settings = await ns.nested_settings(engine, story)
            await c.commit(
                engine, story, w.knocked(w.george), kind=OperationKind.CONTROL, provenance="author"
            )
            await parts.believe(engine, story, jenni, w.knocked(w.driver))

            async def heard(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
                cited = transitions.Antecedent.record("events", event_id)
                await nested.write_speech_act(
                    conn,
                    op,
                    holder=jenni,
                    speaker=bobby,
                    proposition=w.knocked(w.marcus),
                    antecedent=cited,
                    position=_at(op),
                    perceived_intent="sincere",
                    settings=settings,
                )
                return await nested.write_nested(
                    conn,
                    op,
                    holder=jenni,
                    attributed_holder=bobby,
                    proposition=w.knocked(w.marcus),
                    cause=nested.Cause(transitions.Kind.INFERENCE, cited),
                    position=_at(op),
                    confidence=transitions.Confidence(BeliefConfidenceLevel.SUSPECTED),
                    settings=settings,
                )

            await ns.in_operation(engine, story, heard, say=(bobby, "Marcus knocked.", "sincere"))

            plain = await _beliefs(client, story, jenni)
            assert [b["kind"] for b in plain["beliefs"]] == ["first_order"]
            assert all("depth" not in b for b in plain["beliefs"])

            deep = await _beliefs(client, story, jenni, kind="nested")
            (item,) = deep["beliefs"]
            assert (item["kind"], item["attributed_holder_id"], item["depth"]) == (
                "nested",
                bobby,
                2,
            )
            assert item["current_level"] == "suspected"
            assert "speaker_character_id" not in item

            said = await _beliefs(client, story, jenni, kind="speech_act")
            (item,) = said["beliefs"]
            assert (item["kind"], item["speaker_character_id"]) == ("speech_act", bobby)
            assert item["depth"] >= 2

            # Paged by belief id: a page of one continues after it, and ends.
            first = await _beliefs(client, story, jenni, kind="nested", limit=1)
            assert first["next_after"] is None
            bad = await client.get(
                f"/stories/{story}/inspect/beliefs",
                params={"character": jenni, "viewpoint": "omniscient", "kind": "attributed"},
                headers=READ,
            )
            assert bad.status_code == 422
    finally:
        await ts.forget(engine)
