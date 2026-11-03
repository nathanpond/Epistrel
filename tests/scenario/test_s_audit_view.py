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


async def _lazy_resolution(engine: AsyncEngine) -> Any:
    """A turn exposing Ann to Bobby's claim, then a later turn resolving it lazily."""
    from tests import exposure_support as xs
    from tests.memory_support import use_structured

    w = await c.world(engine)
    await use_structured(engine, w.story_id)
    claim = c.at(w.people["Cal"], w.places["garden"], None, None)
    exposure_id, _ = await xs.expose(
        engine, w.story_id, w.characters["Ann"], claim, source=w.characters["Bobby"]
    )
    resolution, _ = await xs.resolve(engine, w.story_id, exposure_id)
    return resolution


@pytest.mark.part("M7", "FR-STORE-23")
async def test_s_audit_view__spy(pg_url: str, engine: AsyncEngine, monkeypatch: Any) -> None:
    """No player-facing path reads the as-recorded view: a spy on it records zero calls during
    respond, regenerate, fork and a lazy belief resolution. Complement: the history read with
    ``view=as_recorded`` is a call (the spy sees it)."""
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
        # The fourth player-facing path: a lazy belief resolution inside a later turn.
        resolution = await _lazy_resolution(engine)
        assert resolution.belief is not None
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


@pytest.mark.part("M7", "FR-STORE-23")
async def test_s_audit_view__beliefs(engine: AsyncEngine) -> None:
    """#222 AC1, the beliefs section (FR-STORE-26: only if created at or before the boundary).
    Ann comes to believe Cal is in the garden (turn 2) and that Bobby is in the kitchen (turn 3),
    then sees Cal is not in the garden (turn 4), which closes the first belief. Turn 5 exposes
    her to Bobby saying Cal is at school, and turn 6 resolves it lazily: the belief is held
    from turn 5 but recorded by turn 6. As recorded: at 2 only the garden belief; at 3 both;
    at 5 the kitchen belief and the correction, not the closed garden belief, and not the
    lazily resolved one (recorded after 5's boundary); at 6 the lazy one too. Complement: the
    current partition still holds the corrected garden belief as a closed row."""
    from tests import exposure_support as xs
    from tests import partition_support as parts
    from tests.memory_support import use_structured

    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    cal = w.people["Cal"]
    garden = c.at(cal, w.places["garden"], None, None)
    kitchen = c.at(w.people["Bobby"], w.places["kitchen"], None, None)
    school = c.at(cal, w.places["school"], None, None)
    await xs.plain_turn(engine, story)
    first = await parts.believe(engine, story, ann, garden)
    second = await parts.believe(engine, story, ann, kitchen)
    corrected = await parts.believe(engine, story, ann, garden.negation())
    exposure_id, _ = await xs.expose(engine, story, ann, school, source=bobby)
    resolution, _ = await xs.resolve(engine, story, exposure_id)
    assert resolution.belief is not None
    lazy = resolution.belief
    assert (first.held_from.turn, second.held_from.turn, corrected.held_from.turn) == (2, 3, 4)
    assert lazy.held_from.turn == 5

    async def held(turn: int) -> set[str]:
        view = await views.as_originally_recorded(engine, story, views.Checkpoint(turn))
        return {b["id"] for b in view.beliefs}

    assert await held(1) == set()
    assert await held(2) == {first.id}
    assert await held(3) == {first.id, second.id}
    at_5 = await held(5)
    assert {second.id, corrected.id} <= at_5
    assert first.id not in at_5  # closed (corrected) before the boundary
    assert lazy.id not in at_5  # held from 5, recorded after 5's boundary
    assert lazy.id in await held(6)
    closed = {r.id: r for r in await parts.belief_rows(engine, story)}[first.id]
    assert closed.tx_to is not None
