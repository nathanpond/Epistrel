"""A render's permitted claim naming an untracked place or object introduces it (#479;
FR-VERIFY-11, FR-PROP-6; docs/02 §4.9).

The Structured (M4) profile runs the extraction backstop on every render. Its claims often name
scene texture the story has no entity for ("the cellar"). Before #479 each such claim was
``uncertain(unresolved_entity)``, took the safe disposition (``repair``) and pushed the beat
down the repair ladder to the safe fallback. Now an argument in a ``place`` or ``item`` slot
that nothing in the story names gets a minted id. The claim is ``novel`` and passes the
permission check. When it is canonized, the commit creates the entity. A later turn's claim
about it resolves to that entity, and a contradiction is caught like any other.

Negatives: a literal slot never mints, a denied claim creates nothing (and is still repaired),
and a name an entity already holds reuses that entity.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.detection_support import claims_answer
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, place, render, structured_story, turn_report

pytestmark = pytest.mark.integration

FIRST = "Maren carries the crate down into the cellar."
LATER = "Maren has never set foot in the cellar."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def extracted(
    span: str, subject: str, predicate: str, obj: str | None, **more: Any
) -> dict[str, Any]:
    return {
        "beat": 0,
        "span": span,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "time": None,
        "polarity": "affirmed",
        "confidence": 1.0,
        **more,
    }


async def entities_named(engine: AsyncEngine, story: str, name: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT id, kind, canonical_name FROM entities WHERE story_id = :s "
        "AND lower(canonical_name) = :n",
        s=story,
        n=name,
    )


async def facts_about(engine: AsyncEngine, story: str, entity: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.id, f.subject_id, p.object_id, f.provenance FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id WHERE f.story_id = :s "
        "AND (f.subject_id = :e OR p.object_id = :e) AND f.tx_to IS NULL",
        s=story,
        e=entity,
    )


async def _turn(
    client: Any, stub: Any, story: str, player: str, text: str, *claims: dict[str, Any]
) -> dict[str, Any]:
    """One turn whose render is a single ``do`` beat and whose extraction answers ``claims``;
    its response body."""
    stub.reset()
    stub.script("generation", [render(beat("do", text))])
    stub.script("extraction_backstop", [claims_answer(*claims)])
    response = await respond(client, story, body(player, "And then?"))
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def test_a_permitted_claim_introduces_an_untracked_place(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The extracted claim naming an untracked place canonizes it: the entity exists, the fact
    names it, and a later contradicting claim about it is caught."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        stub = epistrel.models.stub
        played = await _turn(
            client,
            stub,
            story,
            ids["Wren"],
            FIRST,
            extracted("into the cellar", "Maren", "located_at", "the_cellar"),
        )
        # Released as rendered: nothing went to repair.
        assert played["message"]["text"] == f"*{FIRST}*"
        report = await turn_report(engine, story)
        assert "repairs" not in report
        ((verdict),) = report["verify"]["verdicts"]
        assert (verdict["kind"], verdict["permission"]) == ("novel", "permitted:own")
        assert report["commit"]["canonized"] == 1
        # The snake_case name is read with spaces: one place entity, named by the fact.
        (cellar,) = await entities_named(engine, story, "the cellar")
        assert cellar.kind == "place"
        (fact,) = await facts_about(engine, story, str(cellar.id))
        assert (fact.subject_id, fact.object_id, fact.provenance) == (
            ids["Maren"],
            cellar.id,
            "render",
        )

        # A later render contradicts it: the name resolves to the entity, the claim is
        # contradicted against the canonized fact and repaired, and no second entity appears.
        played = await _turn(
            client,
            stub,
            story,
            ids["Wren"],
            LATER,
            extracted(
                "never set foot in the cellar",
                "Maren",
                "located_at",
                "the cellar",
                polarity="negated",
            ),
        )
        report = await turn_report(engine, story)
        ((verdict),) = report["verify"]["verdicts"]
        assert (verdict["kind"], verdict["rule"]) == ("contradicted", "polarity")
        (repair,) = report["repairs"]
        assert repair["contradicted_facts"] == [fact.id]
        assert played["message"]["text"] != f"*{LATER}*"
        assert len(await entities_named(engine, story, "the cellar")) == 1


async def test_a_denied_claim_creates_nothing(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Permission is unchanged (FR-VERIFY-11): Maren's render may not say what the player's
    character holds. The claim is denied and repaired, and the key it named is never created."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        text = "Wren pockets the brass key."
        played = await _turn(
            client,
            epistrel.models.stub,
            story,
            ids["Wren"],
            text,
            extracted("pockets the brass key", "Wren", "holds", "the_brass_key"),
        )
        report = await turn_report(engine, story)
        assert [d["reason"] for d in report["verify"]["denied"]] == ["player_character"]
        assert report["repairs"]
        assert played["message"]["text"] != f"*{text}*"
        assert report["commit"]["canonized"] == 0
        assert await entities_named(engine, story, "the brass key") == []


async def test_a_name_the_story_holds_reuses_its_entity(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Reuse before minting: a name an entity already holds resolves to it; no second entity."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        cellar = await place(engine, story, "the cellar")
        await _turn(
            client,
            epistrel.models.stub,
            story,
            ids["Wren"],
            FIRST,
            extracted("into the cellar", "Maren", "located_at", "the_cellar"),
        )
        (only,) = await entities_named(engine, story, "the cellar")
        assert only.id == cellar
        (fact,) = await facts_about(engine, story, cellar)
        assert fact.subject_id == ids["Maren"]


# -- #479's second part (ADR-163): a weak relation naming something untracked is surfaced -----

TEXTURE = "Maren looks up from wiping down the bar."


async def test_a_weak_relation_naming_something_untracked_is_surfaced(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The model's own relation (``looks_up_from``) about an untracked thing: released as
    rendered, listed in ``uncertain_claims``, never repaired and never canonized."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        played = await _turn(
            client,
            epistrel.models.stub,
            story,
            ids["Wren"],
            TEXTURE,
            extracted(
                "looks up from wiping down the bar", "Maren", "looks_up_from", "wiping_down_the_bar"
            ),
        )
        assert played["message"]["text"] == f"*{TEXTURE}*"
        report = await turn_report(engine, story)
        assert "repairs" not in report
        ((verdict),) = report["verify"]["verdicts"]
        assert (verdict["kind"], verdict["reason"], verdict["disposition"]) == (
            "uncertain",
            "unresolved_entity",
            "surface",
        )
        ((listed),) = report["uncertain_claims"]
        assert (listed["claim_id"], listed["reason"]) == (verdict["claim_id"], "unresolved_entity")
        assert "uncertain_surfaced" in report["degradation"]
        assert report["commit"]["canonized"] == 0


@pytest.mark.parametrize(
    ("span", "subject", "predicate", "obj"),
    [
        # an unknown proper name
        ("talks about the Crossroads Inn", "Maren", "talks_about", "the Crossroads Inn"),
        # the player's character is an argument
        ("Wren looks up from the bar", "Wren", "looks_up_from", "the_bar"),
        # a core relation: option 2 governs it, and a person slot never mints
        ("knows the ferryman", "Maren", "knows", "the_ferryman"),
    ],
)
async def test_other_unresolved_claims_are_still_repaired(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    span: str,
    subject: str,
    predicate: str,
    obj: str,
) -> None:
    """Each case keeps the safe disposition (``repair``): the beat goes to the ladder."""
    text = f"Maren {span}." if not span.startswith("Wren") else f"{span}."
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        played = await _turn(
            client,
            epistrel.models.stub,
            story,
            ids["Wren"],
            text,
            extracted(span, subject, predicate, obj),
        )
        report = await turn_report(engine, story)
        ((verdict),) = report["verify"]["verdicts"]
        assert (verdict["kind"], verdict["disposition"]) == ("uncertain", "repair")
        assert report["repairs"]
        assert report["uncertain_claims"] == []
        assert played["message"]["text"] != f"*{text}*"
