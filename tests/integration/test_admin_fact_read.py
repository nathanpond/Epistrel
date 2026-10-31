"""``GET /admin/stories/{id}/facts/{fact_id}``: one canon fact version by id (#535; FR-BITEMP-3).

On #133's world on ``Structured (M4)``: an author fact and its supersede, each version read by
id over REST (the superseded one included, closed in transaction-time and naming its
successor), the same answer from ``Engine.get_fact``; an unknown id and another story's fact id
are 404 ``fact_not_found``; the read needs the service credential (401) and the omniscient
viewpoint (403)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.routes.admin import ADMIN_PREFIX
from epistrel.api.routes.admin.mutation import FactBody
from epistrel.core.context import RequestContext
from tests import canon_support as c
from tests import consolidation_support as cs
from tests.auth_support import auth_settings, bearer
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-author"}
OMNISCIENT = {"viewpoint": "omniscient"}
UNKNOWN = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"


def _at(w: c.World, place: str) -> dict[str, Any]:
    return c.at(w.people["Bobby"], w.places[place], 0, None).model_dump(mode="json")


async def test_one_fact_version_by_id(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Found, superseded, unknown, another story's, auth and the library's answer (see the
    module docstring)."""
    w = await cs.story(engine)
    other = await cs.story(engine)
    story = w.story_id
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
        client,
        epistrel,
    ):

        def path(story_id: str, fact_id: str) -> str:
            return f"{ADMIN_PREFIX}/stories/{story_id}/facts/{fact_id}"

        committed = await client.post(
            f"{ADMIN_PREFIX}/stories/{story}/facts",
            json={"proposition": _at(w, "school")},
            headers=WRITE,
        )
        assert committed.status_code == 201, committed.text
        first = committed.json()["fact"]
        superseded = await client.post(
            f"{path(story, first['id'])}/supersede",
            json={"proposition": _at(w, "garden")},
            headers=WRITE,
        )
        assert superseded.status_code == 201, superseded.text
        second = superseded.json()["fact"]

        # Found: the current version, as the write answered it.
        found = await client.get(path(story, second["id"]), params=OMNISCIENT, headers=bearer())
        assert found.status_code == 200, found.text
        assert found.json() == second
        assert found.json()["tx_to"] is None

        # Superseded: the closed version is still read by id, naming its successor.
        old = await client.get(path(story, first["id"]), params=OMNISCIENT, headers=bearer())
        assert old.status_code == 200, old.text
        body = old.json()
        assert body["id"] == first["id"]
        assert body["proposition"]["object_id"] == w.places["school"]
        assert body["tx_to"] is not None
        assert body["superseded_by"] == second["id"]
        assert first["id"] in second["supersedes"]
        assert body["lineage_id"] == second["lineage_id"]

        # The library answers what REST does.
        ctx = RequestContext(principal="p-author")
        library = await epistrel.get_fact(ctx, story, first["id"], viewpoint="omniscient")
        assert FactBody.of(library).model_dump(mode="json") == body

        # Unknown id, and a fact of another story read through this one: 404 fact_not_found.
        theirs = await client.post(
            f"{ADMIN_PREFIX}/stories/{other.story_id}/facts",
            json={"proposition": _at(other, "kitchen")},
            headers=WRITE,
        )
        assert theirs.status_code == 201, theirs.text
        for fact_id in (UNKNOWN, theirs.json()["fact"]["id"]):
            missing = await client.get(path(story, fact_id), params=OMNISCIENT, headers=bearer())
            assert missing.status_code == 404, missing.text
            assert missing.json()["error"]["code"] == "fact_not_found"
        # Complement: that fact is found through its own story.
        own = await client.get(
            path(other.story_id, theirs.json()["fact"]["id"]), params=OMNISCIENT, headers=bearer()
        )
        assert own.status_code == 200, own.text

        # Auth: no credential is 401; a non-omniscient viewpoint is 403.
        anonymous = await client.get(path(story, first["id"]), params=OMNISCIENT)
        assert anonymous.status_code == 401
        private = await client.get(path(story, first["id"]), headers=bearer())
        assert private.status_code == 403
        assert private.json()["error"]["code"] == "omniscient_required"
