"""#979 (FR-VERIFY-11, ADR-218): a character's observable action toward the player's character,
or toward another modeled character present at the same place, commits in a structured turn on
the stub with no ``player_character``/``other_character_secret`` denial, no repair and no safe
fallback. The claims are injected in the extraction backstop's shape (a free relation the
backstop names, auto-registered with the weakest semantics), as E-06's trace showed them
(``docs/evals/full-bca1e50d0af4-2026-11-21.md``). They are texture: never canonized."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import multi_support as ms
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.detection_support import claims_answer
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, render, structured_story, turn_report

pytestmark = pytest.mark.integration

SLIDE = "Maren slides a mug of cider toward Wren."
LEAN = "Malik leans toward Ethan."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _backstop(span: str, subject: str, relation: str, obj: str) -> str:
    """The extraction backstop's answer naming ``subject relation obj`` on the render's beat."""
    return claims_answer(
        {"beat": 1, "span": span, "subject": subject, "predicate": relation, "object": obj}
    )


def _no_denial(report: dict[str, Any], relation_code: str) -> None:
    assert [d["reason"] for d in report["verify"]["denied"]] == []
    assert not report.get("repairs")
    verdicts = report["verify"]["verdicts"]
    assert any(v.get("permission") == relation_code for v in verdicts), verdicts


async def test_an_action_toward_the_player_commits(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Maren (at the inn with Wren, the player) slides him a mug of cider: the backstop's
    ``slides_cup_to(Maren, Wren)`` is her own action toward him (``action_texture``). Red
    before the fix: ``denied:player_character``, repaired, and the beat fell back."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        inn = await ps.place_entity(engine, story, "the inn")
        for name in ("Wren", "Maren"):
            await ps.put(engine, story, ids[name], inn)
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("do", SLIDE))])
        stub.script("extraction_backstop", [_backstop(SLIDE, "Maren", "slides_cup_to", "Wren")])
        response = await respond(client, story, body(ids["Wren"], "Evening, Maren."))
        assert response.status_code == 200, response.text
        played = response.json()
        report = await turn_report(engine, story)
        _no_denial(report, "permitted:action_texture")
        assert SLIDE in played["message"]["text"]
        assert played.get("degradation_details", []) == []
        assert report["commit"]["canonized"] == 0  # texture, never canon


async def test_an_action_toward_a_present_character_commits(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Malik and Ethan (two AI characters) are at camp with Nathan (the player); Malik's render
    leans toward Ethan: the backstop's ``leans_toward(Malik, Ethan)`` commits as texture. Red
    before the fix: ``denied:other_character_secret``, repaired, and the beat fell back."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Nathan", "Malik", "Ethan")
        camp = await ps.place_entity(engine, story, "camp")
        for name in ("Nathan", "Malik", "Ethan"):
            await ps.put(engine, story, ids[name], camp)
        await ms.per_character(engine, story)
        stub = epistrel.models.stub
        stub.reset()
        ms.renders_for(stub, "Malik", render(beat("do", LEAN)))
        stub.script("extraction_backstop", [_backstop(LEAN, "Malik", "leans_toward", "Ethan")])
        response = await respond(
            client,
            story,
            body(ids["Nathan"], "Long day.", render_character_ids=[ids["Malik"]]),
        )
        assert response.status_code == 200, response.text
        played = response.json()
        report = await turn_report(engine, story)
        _no_denial(report, "permitted:action_texture")
        assert LEAN in played["message"]["text"]
        assert played.get("degradation_details", []) == []
        assert report["commit"]["canonized"] == 0
