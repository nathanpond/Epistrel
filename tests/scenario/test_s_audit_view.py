"""S-AUDIT-VIEW (FR-STORE-23): the as-recorded view survives a later amendment and truncation,
shows the erasure markers, and no player-facing path reads it (#222)."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import views
from epistrel.core.amendments.store import RecordPartDraft
from epistrel.core.context import RequestContext
from epistrel.core.errors import OmniscientRequired
from epistrel.core.timeline import TruncateRequest
from epistrel.engine import Engine
from tests import amendment_support as am
from tests import canon_support as c
from tests.auth_support import auth_settings, bearer
from tests.respond_support import body as respond_body
from tests.respond_support import new_story, respond_client

pytestmark = pytest.mark.integration


def _texts(events: list[Any]) -> set[str]:
    return {str(getattr(e.payload, "text", "")) for e in events}


@pytest.mark.part("M7", "FR-STORE-23")
async def test_s_audit_view(pg_url: str, engine: AsyncEngine) -> None:
    """Turn 20's reply is later corrected and the story truncated to 10: as recorded at 25 the
    original reply is there and nothing reads as discarded; the corrected view has neither
    turn 20 nor its correction; the view carries the story's erasure markers (none here)."""
    w = await c.world(engine)
    replies = await am.play(engine, w.story_id, 30)
    await am.amend(
        engine,
        w.story_id,
        [RecordPartDraft("events", replies[20], "replace", {"text": "a later fix"})],
    )
    lib = Engine(auth_settings(pg_url, model_adapter="stub"), db=engine)
    await lib.truncate(RequestContext(principal=am.AUTHOR), w.story_id, TruncateRequest(to_turn=10))
    recorded = await views.as_originally_recorded(engine, w.story_id, views.Checkpoint(25))
    texts = _texts(recorded.events)
    assert "reply 20" in texts
    assert "a later fix" not in texts
    assert {"reply 21", "reply 25"} <= texts
    assert "reply 26" not in texts
    assert recorded.erasure_markers == []
    corrected = _texts(await views.corrected_history(engine, w.story_id))
    assert not {"reply 20", "a later fix"} & corrected


@pytest.mark.part("M7", "FR-STORE-23")
async def test_s_audit_view__spy(pg_url: str, engine: AsyncEngine, monkeypatch: Any) -> None:
    """No player-facing path reads the as-recorded view: a spy on it records zero calls during
    respond, regenerate and fork. Complement: the history read with ``view=as_recorded`` is a
    call (the spy sees it)."""
    calls: list[str] = []
    original = views.as_originally_recorded

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append("called")
        return await original(*args, **kwargs)

    monkeypatch.setattr(views, "as_originally_recorded", spy)
    write = {**bearer(), "X-Epistrel-Principal": "p-spy"}
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (client, _):
        story, ids = await new_story(client)
        actor = next(iter(ids.values()))
        for path, body in (
            (f"/stories/{story}/respond", respond_body(actor)),
            (f"/stories/{story}/regenerate", {}),
            (f"/stories/{story}/fork", {"at_turn": 1}),
        ):
            answer: httpx.Response = await client.post(path, json=body, headers=write)
            assert answer.status_code in (200, 201), answer.text
        assert calls == []
        read = await client.get(
            f"/stories/{story}/history",
            params={"view": "as_recorded", "viewpoint": "omniscient", "as_of": "1"},
            headers=bearer(),
        )
        assert read.status_code == 200, read.text
    assert calls == ["called"]


@pytest.mark.part("M7", "FR-STORE-23")
async def test_s_audit_view__omniscient_only(pg_url: str, engine: AsyncEngine) -> None:
    """``view=as_recorded`` without ``omniscient`` (a character, or none) is 403
    ``omniscient_required`` with no records in the body, over REST and the library, under two
    principals; with it, 200 with ``view``, ``boundary_tx`` and ``erasure_markers``; an unknown
    view is 422 ``invalid_view``; an unknown checkpoint 404 ``checkpoint_not_found``."""
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
        client,
        lib,
    ):
        story, ids = await new_story(client)
        actor = next(iter(ids.values()))
        write = {**bearer(), "X-Epistrel-Principal": "p-one"}
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(actor), headers=write
        )
        assert played.status_code == 200, played.text
        for principal in ("p-one", "p-two"):
            headers = {**bearer(), "X-Epistrel-Principal": principal}
            for params in (
                {"view": "as_recorded", "as_of": "1"},
                {"view": "as_recorded", "as_of": "1", "viewpoint": f"character:{actor}"},
            ):
                refused = await client.get(
                    f"/stories/{story}/history", params=params, headers=headers
                )
                assert refused.status_code == 403, refused.text
                assert set(refused.json()) == {"error"}
                assert refused.json()["error"]["code"] == "omniscient_required"
            with pytest.raises(OmniscientRequired):
                await lib.history(
                    RequestContext(principal=principal), story, view="as_recorded", as_of="1"
                )
        shown = await client.get(
            f"/stories/{story}/history",
            params={"view": "as_recorded", "as_of": "1", "viewpoint": "omniscient"},
            headers=bearer(),
        )
        assert shown.status_code == 200, shown.text
        body = shown.json()
        assert (body["view"], body["erasure_markers"]) == ("as_recorded", [])
        assert body["boundary_tx"]
        assert body["items"]
        # #222: the derived sections as they stood at the boundary.
        assert set(body["derived"]) == {"facts", "beliefs", "memories", "states"}
        unknown = await client.get(
            f"/stories/{story}/history",
            params={"view": "sideways", "viewpoint": "omniscient"},
            headers=bearer(),
        )
        assert unknown.status_code == 422, unknown.text
        assert unknown.json()["error"]["code"] == "invalid_view"
        missing = await client.get(
            f"/stories/{story}/history",
            params={"view": "as_recorded", "as_of": "9", "viewpoint": "omniscient"},
            headers=bearer(),
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "checkpoint_not_found"
