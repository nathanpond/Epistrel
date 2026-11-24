"""An extraction claim about an everyday thing the story has no entity for passes silently
(#978, ADR-219; FR-VERIFY-8, FR-VERIFY-11; the owner's option (a)).

E-06 on hosted ``claude-haiku-5-5`` (``docs/evals/full-bca1e50d0af4-2026-11-21.md``) ended 125
of 600 case-seeds in the safe fallback through one path: the extraction backstop names scene
texture in a core relation ("ring of keys" ``located_at`` "by the hearth"), the untracked
subject does not normalize (``unresolved_entity``), the claim takes the safe disposition, and
the ladder's only constraint for it ("Do not assert what this beat could not support.") names
nothing the rewrite can drop. Now such a claim takes ``pass``: the beat commits as rendered with
no repair, the verdict records ``uncertain`` / ``unresolved_entity`` / ``pass``, and nothing is
listed in ``uncertain_claims``.

Complements keep today's behaviour: a claim touching the player's character, a tracked relation
(possession), another ``Unnormalizable`` reason, and (``test_introduced_entities``) a claim
contradicting a canonized fact.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.detection_support import claims_answer
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, render, structured_story, text_of, turn_report

pytestmark = pytest.mark.integration

KEYS = "Maren reaches for a ring of keys by the hearth."
GENERIC = "Do not assert what this beat could not support."


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


def _regenerations(stub: Any) -> list[Any]:
    return [c for c in stub.calls if c.purpose == "regeneration"]


@pytest.mark.parametrize(
    ("span", "subject", "predicate", "obj"),
    [
        # a core relation with an untracked subject (the E-06 evidence): ``any`` never mints
        ("ring of keys by the hearth", "ring_of_keys", "located_at", "by_the_hearth"),
        # a core relation's person slot, which never mints
        ("reaches for a ring of keys", "Maren", "knows", "the_ferryman"),
        # the model's own relation: surfaced before #978, silent now
        ("reaches for a ring of keys", "Maren", "reaches_for", "a_ring_of_keys"),
    ],
)
async def test_an_untracked_mention_passes_with_no_repair(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    span: str,
    subject: str,
    predicate: str,
    obj: str,
) -> None:
    """The beat commits as rendered with zero regeneration calls; the verdict records
    ``unresolved_entity`` with disposition ``pass``; nothing is surfaced or canonized."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        stub = epistrel.models.stub
        played = await _turn(
            client, stub, story, ids["Wren"], KEYS, extracted(span, subject, predicate, obj)
        )
        assert _regenerations(stub) == []
        assert played["message"]["text"] == f"*{KEYS}*"
        assert played.get("uncertain_claims", []) == []
        report = await turn_report(engine, story)
        assert "repairs" not in report
        ((verdict),) = report["verify"]["verdicts"]
        assert (verdict["kind"], verdict["reason"], verdict["disposition"]) == (
            "uncertain",
            "unresolved_entity",
            "pass",
        )
        assert report["uncertain_claims"] == []
        assert "uncertain_surfaced" not in report["degradation"]
        assert report["commit"]["canonized"] == 0


@pytest.mark.parametrize(
    ("span", "subject", "predicate", "obj", "reason"),
    [
        # the player's character is the other argument, under a core relation
        ("reaches for a ring of keys", "the_stranger", "knows", "Wren", "unresolved_entity"),
        # a tracked relation (possession) with an untracked subject
        (
            "reaches for a ring of keys",
            "the_stable",
            "holds",
            "plenty_of_straw",
            "unresolved_entity",
        ),
        # another reason: an object on a predicate that takes none
        ("reaches for a ring of keys", "Maren", "dead", "the_world", "argument_type"),
    ],
)
async def test_other_unnormalizable_claims_still_repair(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    span: str,
    subject: str,
    predicate: str,
    obj: str,
    reason: str,
) -> None:
    """Each keeps the safe disposition (``repair``): the beat goes to the ladder."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        played = await _turn(
            client,
            epistrel.models.stub,
            story,
            ids["Wren"],
            KEYS,
            extracted(span, subject, predicate, obj),
        )
        report = await turn_report(engine, story)
        ((verdict),) = report["verify"]["verdicts"]
        assert (verdict["kind"], verdict["reason"], verdict["disposition"]) == (
            "uncertain",
            reason,
            "repair",
        )
        assert report["repairs"]
        assert played["message"]["text"] != f"*{KEYS}*"


async def test_a_passed_claim_adds_no_constraint_to_its_beats_repair(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A beat with a passed claim and a repaired one still goes to the ladder for the other
    claim; the passed one contributes no constraint, so the generic one is never sent."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("do", KEYS))])
        stub.script(
            "extraction_backstop",
            [
                claims_answer(
                    extracted("ring of keys by the hearth", "ring_of_keys", "located_at", "hearth"),
                    # denied (FR-VERIFY-11): its constraint names the denial, not the generic
                    extracted("reaches for a ring of keys", "Wren", "holds", "the_brass_key"),
                )
            ],
        )
        response = await respond(client, story, body(ids["Wren"], "And then?"))
        assert response.status_code == 200, response.text
        report = await turn_report(engine, story)
        passed = [v for v in report["verify"]["verdicts"] if v["disposition"] == "pass"]
        assert [v["reason"] for v in passed] == ["unresolved_entity"]
        assert [d["reason"] for d in report["verify"]["denied"]] == ["player_character"]
        assert report["repairs"]
        regenerations = _regenerations(stub)
        assert regenerations, "the denied claim is still repaired"
        assert all(GENERIC not in text_of(call) for call in regenerations)
