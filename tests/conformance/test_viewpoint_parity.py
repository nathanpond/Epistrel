"""The same request gives the same answer over REST and in-process (NFR-SEC-5's parity half,
#59): one story, read and played through both surfaces of one app."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conformance.conftest import Surface
from tests.respond_support import FIRST, PLAYER, SECOND, body
from tests.story_support import story_body


def _shared(result: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """A respond body without the per-story ids (the viewpoint names its character)."""
    viewpoint = result["output_viewpoint"]
    for name, character_id in ids.items():
        viewpoint = viewpoint.replace(character_id, name)
    return {
        "beats": [(b["beat"], b["kind"], b["channel"], b["text"]) for b in result["beats"]],
        "output_viewpoint": viewpoint,
        "audience_filter": result["audience_filter"],
        "message": result["message"]["text"],
    }


@pytest.mark.integration
async def test_rest_and_library_agree(both: dict[str, Surface]) -> None:
    """History reads under every viewpoint, story reads, and respond results are equal
    across the surfaces; a refusal has the same status and code on both."""
    rest, library = both["rest"], both["library"]
    played: dict[str, list[dict[str, Any]]] = {}
    for surface in (rest, library):
        created = (await surface.create_story(story_body(PLAYER, FIRST, SECOND))).json
        story = created["story_id"]
        ids = {c["name"]: c["id"] for c in created["characters"]}
        played[surface.name] = [
            _shared((await surface.respond(story, body(ids[PLAYER], line), viewpoint=vp)).json, ids)
            for line, vp in (("Same words.", None), ("More words.", "omniscient"))
        ]
    assert played["rest"] == played["library"]

    for params in (
        {},
        {"viewpoint": "omniscient", "include_audit": True},
        {"viewpoint": f"character:{ids[FIRST]}"},
        {"actor_character_id": ids[SECOND], "limit": 3},
        {"as_of": 1},
        {"viewpoint": "nobody"},
        {"viewpoint": f"character:{ids[FIRST]}", "include_audit": True},
    ):
        assert await rest.history(story, **params) == await library.history(story, **params)
    for vp in (None, "omniscient", "character:nobody"):
        over_rest, in_process = await rest.get_story(story, vp), await library.get_story(story, vp)
        assert (over_rest.status, over_rest.code) == (in_process.status, in_process.code)
        assert (over_rest.body or {}).get("characters") == (in_process.body or {}).get("characters")
