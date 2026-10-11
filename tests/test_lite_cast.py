"""The cast rules of a Lite turn (#62, FR-AUTH-3): who may act, who is rendered and in which
order, and how unknown ids are refused. Pure: no database, no model."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from epistrel.core.errors import InvalidRequest
from epistrel.core.settings_schema import StorySettings
from epistrel.core.turns import CastEntry, StoryCast
from epistrel.orchestrator.lite.cast import resolve_cast
from epistrel.orchestrator.lite.schemas import RespondRequest

WREN = CastEntry(id="01HZY000000000000000000WRN", name="Wren", definition="-", is_player=True)
ADA = CastEntry(id="01HZY000000000000000000ADA", name="Ada", definition="-", is_player=False)
BO = CastEntry(id="01HZY0000000000000000000B0", name="Bo", definition="-", is_player=False)
STRANGER = "01HZY00000000000000000STRA"


def _story(*cast: CastEntry) -> StoryCast:
    return StoryCast(status="active", settings=StorySettings(), cast=cast)


def _request(actor: str, **fields: Any) -> RespondRequest:
    return RespondRequest.model_validate(
        {"actor_character_id": actor, "player_input": {"say": "hi"}, **fields}
    )


def test_default_render_set_excludes_the_actor_in_cast_order() -> None:
    """Complement asserted: an NPC actor is accepted too, and the player is then rendered."""
    story = _story(WREN, ADA, BO)
    cast = resolve_cast(story, _request(WREN.id))
    assert (cast.actor, cast.render, cast.actor_rendered) == (WREN, (ADA, BO), False)
    npc = resolve_cast(story, _request(BO.id))
    assert npc.render_ids == [WREN.id, ADA.id]
    assert npc.noted == (BO, WREN, ADA)


def test_a_cast_of_one_renders_the_actor() -> None:
    cast = resolve_cast(_story(WREN), _request(WREN.id))
    assert cast.render == (WREN,)
    assert cast.actor_rendered
    assert cast.noted == (WREN,)


def test_listed_renders_keep_request_order_drop_duplicates_and_may_include_the_actor() -> None:
    story = _story(WREN, ADA, BO)
    cast = resolve_cast(story, _request(WREN.id, render_character_ids=[BO.id, WREN.id, BO.id]))
    assert cast.render_ids == [BO.id, WREN.id]
    assert cast.actor_rendered
    assert cast.noted == (WREN, BO)
    reordered = resolve_cast(story, _request(WREN.id, render_character_ids=[BO.id, ADA.id]))
    assert reordered.noted == (WREN, BO, ADA)


def test_unknown_actor_and_renders_are_reported_together() -> None:
    story = _story(WREN, ADA)
    request = _request(
        STRANGER,
        render_character_ids=[ADA.id, BO.id],
        scene_context={"characters_present": [BO.id]},
    )
    with pytest.raises(InvalidRequest) as refused:
        resolve_cast(story, request)
    details = dict(refused.value.details)
    assert [e["field"] for e in details.pop("errors")] == [
        "body.actor_character_id",
        "body.render_character_ids",
        "body.scene_context.characters_present",
    ]
    assert details == {
        "field": "body.actor_character_id",
        "unknown_actor": STRANGER,
        "unknown_render_ids": [BO.id],
        "unknown_present_ids": [BO.id],
    }
    with pytest.raises(InvalidRequest) as render_only:
        resolve_cast(story, _request(WREN.id, render_character_ids=[BO.id]))
    assert render_only.value.details["unknown_render_ids"] == [BO.id]
    assert render_only.value.details["field"] == "body.render_character_ids"


def test_malformed_ids_and_empty_renders_are_refused_by_the_model() -> None:
    cases: tuple[dict[str, Any], ...] = (
        {"render_character_ids": []},
        {"render_character_ids": ["ada"]},
        {"scene_context": {"characters_present": ["01hzy000000000000000000ada"]}},
    )
    for fields in cases:
        with pytest.raises(ValidationError):
            _request(WREN.id, **fields)
    with pytest.raises(ValidationError):
        _request("Wren")
