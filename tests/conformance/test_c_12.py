"""C-12: the output viewpoint contract (FR-RESP-12, FR-AUTH-2) [M2] (#59).

Every respond and history read is produced for one output viewpoint, resolved the same way
over REST and in the library: ``test_c_12`` runs every check through both surfaces (the
``surface`` fixture in ``conftest.py``; NFR-SEC-5's parity half).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.settings_schema import StorySettings
from epistrel.core.viewpoint import PRIVATE_CHANNELS, validate_default_viewpoint
from tests.conformance.conftest import Surface
from tests.respond_support import FIRST, PLAYER, SECOND, body
from tests.story_support import character, count, story_body

PRIVATE_TEXT = "C12-PRIVATE-BEAT"
AUDIT_REASON = "c12-audit-record"
OPERATIONS = "SELECT count(*) FROM operations WHERE story_id = :s"


async def _story(surface: Surface, **fields: Any) -> tuple[str, dict[str, str]]:
    created = (await surface.create_story(story_body(PLAYER, FIRST, SECOND, **fields))).json
    return created["story_id"], {c["name"]: c["id"] for c in created["characters"]}


async def _private_records(engine: AsyncEngine, story_id: str, character_id: str) -> None:
    """A ``think`` beat and an audit record at turn 1, inserted directly (Lite never writes
    either; the filter's code path is exercised by this fixture)."""
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.MAINTENANCE, principal="p-test", turn=1
    ) as op:
        op.append(
            "think", {"character_id": character_id, "text": PRIVATE_TEXT}, character_id=character_id
        )
        op.append("guardrail_output_rejected", {"text": "withheld", "reason": AUDIT_REASON})
        await op.commit()


def _vp(character_id: str) -> str:
    return f"character:{character_id}"


def _no_filtering(result: dict[str, Any]) -> bool:
    """The result states perception ``none``: no audience filtering applies (M2)."""
    return bool(result["audience_filter"] == "none")


def _private_items(result: dict[str, Any]) -> list[tuple[str, str | None, Any]]:
    """The private-channel items of an omniscient read: text, character, true ages."""
    return [
        (i["text"], i["actor_character_id"], i["true_ages"])
        for i in result["items"]
        if i["channel"] in PRIVATE_CHANNELS
    ]


@pytest.mark.integration
@pytest.mark.part("M2", "FR-RESP-12; FR-AUTH-2")
async def test_c_12(surface: Surface, engine: AsyncEngine) -> None:
    """Echo; defaults (actor, then the story's default); an ``omniscient`` default refused;
    ``omniscient`` only when named; a key reused for another viewpoint is a conflict with no
    cached result; audit reads without ``omniscient`` refused; two principals get the same
    result; a Lite character viewpoint gets the whole message. Run over REST and the library.

    Complement asserted: with ``omniscient`` named, the audit records **are** returned (and the
    private beat, labelled by its channel, and the true ages)."""
    story, ids = await _story(surface)
    player, first, second = ids[PLAYER], ids[FIRST], ids[SECOND]

    # Echo and the actor default; a Lite character viewpoint returns the whole message.
    played = (await surface.respond(story, body(player, "Who's there?", do="knocks"))).json
    assert played["output_viewpoint"] == _vp(player)
    assert _no_filtering(played)
    assert [(b["channel"], b["text"]) for b in played["beats"]] == [
        ("say", "Who's there?"),
        ("do", "knocks"),
        ("message", played["message"]["text"]),
    ]
    assert all("true_ages" not in b for b in played["beats"])
    as_first = (await surface.respond(story, body(first, "Only me."))).json
    assert as_first["output_viewpoint"] == _vp(first)
    named = (await surface.respond(story, body(player, "Hm.", output_viewpoint=_vp(second)))).json
    assert named["output_viewpoint"] == _vp(second)
    via_query = (await surface.respond(story, body(player, "Hm?"), viewpoint="omniscient")).json
    assert via_query["output_viewpoint"] == "omniscient"
    assert {b["true_ages"][player] for b in via_query["beats"] if b["channel"] != "message"} == {20}

    # History: the story's default (the sole player), the actor, the named viewpoint.
    history = (await surface.history(story)).json
    assert history["output_viewpoint"] == _vp(player)
    assert _no_filtering(history)
    assert (await surface.history(story, actor_character_id=first)).json["output_viewpoint"] == _vp(
        first
    )

    # Unresolved: no name, no actor, no default -> 422 with nothing written.
    cast = [character(n, 30) for n in (PLAYER, FIRST)]
    bare = (await surface.create_story({"name": "No default", "characters": cast})).json
    bare_id = bare["story_id"]
    assert (await surface.history(bare_id)).code == "viewpoint_unresolved"
    before = await count(engine, OPERATIONS, s=bare_id)
    actor = bare["characters"][0]["id"]
    wrong = await surface.respond(bare_id, body(actor, output_viewpoint="character:nobody"))
    assert (wrong.status, wrong.code) == (422, "viewpoint_unresolved")
    malformed = await surface.respond(bare_id, body(actor), viewpoint="everyone")
    assert malformed.code == "viewpoint_unresolved"
    assert await count(engine, OPERATIONS, s=bare_id) == before

    # An omniscient default is refused at creation and by every settings write (#63's PATCH).
    refused = await surface.create_story(
        story_body(PLAYER, settings={"default_output_viewpoint": "omniscient"})
    )
    assert (refused.status, refused.code) == (422, "validation_error")
    with pytest.raises(ValueError, match="must be a character"):
        StorySettings.model_validate({"default_output_viewpoint": "omniscient"})
    with pytest.raises(InvalidRequest):
        validate_default_viewpoint("omniscient", ids.values())

    # Omniscient only when named: the private beat and the audit records.
    await _private_records(engine, story, first)
    for read in (await surface.history(story), await surface.history(story, viewpoint=_vp(first))):
        assert PRIVATE_TEXT not in str(read.json)
        assert "audit" not in read.json
        assert all("true_ages" not in i for i in read.json["items"])
    denied = await surface.history(story, viewpoint=_vp(first), include_audit=True)
    assert (denied.status, denied.code) == (403, "omniscient_required")
    assert (await surface.history(story, viewpoint="bogus", include_audit=True)).code == (
        "viewpoint_unresolved"
    )
    omniscient = (await surface.history(story, viewpoint="omniscient", include_audit=True)).json
    assert _private_items(omniscient) == [(PRIVATE_TEXT, first, {first: 21})]
    assert [(a["kind"], AUDIT_REASON in a["text"]) for a in omniscient["audit"]] == [
        ("guardrail_output_rejected", True)
    ]

    # A key reused for another viewpoint: 409, no cached result, nothing written.
    first_try = await surface.respond(story, body(player, "Again?"), key="c12-key")
    assert first_try.status == 200
    before = await count(engine, OPERATIONS, s=story)
    conflict = await surface.respond(
        story, body(player, "Again?"), key="c12-key", viewpoint="omniscient"
    )
    assert (conflict.status, conflict.code, conflict.body) == (409, "idempotency_conflict", None)
    assert await count(engine, OPERATIONS, s=story) == before
    same = await surface.respond(
        story, body(player, "Again?"), key="c12-key", viewpoint=_vp(player)
    )
    assert same.code != "idempotency_conflict"

    # No outcome depends on the principal.
    one = await surface.history(story, principal="u-one", viewpoint="omniscient")
    two = await surface.history(story, principal="u-two", viewpoint="omniscient")
    assert one == two

    # True ages only for an omniscient story read.
    characters = (await surface.get_story(story, viewpoint="omniscient")).json["characters"]
    assert [c["true_age"] for c in characters] == [20, 21, 22]
    for viewpoint in (None, _vp(first)):
        shown = (await surface.get_story(story, viewpoint=viewpoint)).json["characters"]
        assert all("true_age" not in c for c in shown)
    assert (await surface.get_story(story, viewpoint="character:nobody")).code == (
        "viewpoint_unresolved"
    )
