"""Which capabilities a story's gap fill accepts (#790; nathanpond/Epistrel-Console#340).

``GET /stories/{id}/effective-config`` carries ``gap_fillable``: sorted, exactly the keys of
``coverage.capabilities_of`` over the story's effective settings, with no ``:mode`` suffix.
Every name in it is accepted by a gap-fill preview (``POST /stories/{id}/backfill`` with
``capabilities``), and every other capability the effective configuration lists is refused with
422 ``validation_error`` naming it in ``details.not_enabled``, over the whole set, on a Full
story and a Lite story. The refusal's message says what it means.

Each story gets a few turns of history (direct commits, ``tests/fixtures/long_lite_story``) and
its ``capability_coverage`` rows are removed, so every gap-fillable capability has a gap and a
preview answers 200 rather than 409 ``nothing_to_backfill``. A preview writes nothing.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.backfill import coverage
from epistrel.core.profiles import effective
from epistrel.core.seeds.profiles import FULL_PRESET_ID, LITE_PRESET_ID
from tests.auth_support import auth_settings, bearer
from tests.event_support import scalar
from tests.fixtures import long_lite_story as lls
from tests.respond_support import new_story, respond_client
from tests.story_support import READ

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-gap-fillable"}
MESSAGE = "a gap fill names capabilities it can't fill"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json() if response.content else {}
    return found


async def _story(client: httpx.AsyncClient, db: AsyncEngine, profile: str) -> str:
    """A story on ``profile`` with three turns of history and no capability coverage."""
    story, ids = await new_story(client, "Ada", "Bobby", "Cal")
    await ok(
        await client.put(
            f"/stories/{story}/engine-profile",
            json={"profile_id": profile, "ignore_warnings": True},
            headers=WRITE,
        )
    )
    await lls.long_lite_story(db, story, ids["Ada"], 6)
    async with db.begin() as conn:
        await conn.execute(
            text("DELETE FROM capability_coverage WHERE story_id = :s"), {"s": story}
        )
    return story


async def _expected(db: AsyncEngine, story: str) -> list[str]:
    """``capabilities_of`` over the story's effective settings, sorted."""
    async with db.connect() as conn:
        found = await effective.effective_configuration(conn, story, validate=False)
    return sorted(coverage.capabilities_of(found.settings))


async def _preview(
    client: httpx.AsyncClient, story: str, capabilities: list[str]
) -> httpx.Response:
    return await client.post(
        f"/stories/{story}/backfill", json={"capabilities": capabilities}, headers=WRITE
    )


async def _check(client: httpx.AsyncClient, db: AsyncEngine, story: str) -> dict[str, Any]:
    shown = await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))
    fillable: list[str] = shown["gap_fillable"]
    assert fillable == sorted(fillable)
    assert fillable == await _expected(db, story)
    assert all(":" not in name for name in fillable)

    backfills = "SELECT count(*) FROM story_backfills WHERE story_id = :s"
    for name in fillable:
        preview = await ok(await _preview(client, story, [name]))
        assert preview["applied"] is False, preview
        assert preview["backfill"]["needed"] is True, preview
        assert preview["backfill"]["capabilities"] == [name], preview
    if fillable:
        whole = await ok(await _preview(client, story, fillable))
        assert sorted(whole["backfill"]["capabilities"]) == fillable

    # Complement: every effective-config capability outside gap_fillable is refused.
    others = sorted(set(shown["capabilities"]) - set(fillable))
    assert others
    for name in others:
        refused = await _preview(client, story, [name])
        assert refused.status_code == 422, (name, refused.text)
        error = refused.json()["error"]
        assert error["code"] == "validation_error", error
        assert error["message"] == MESSAGE, error
        details = error["details"]
        assert (details["field"], details["not_enabled"]) == ("body.capabilities", [name]), error
    # ... also beside an accepted one, naming only the refused.
    if fillable:
        mixed = await _preview(client, story, [fillable[0], *others])
        assert mixed.status_code == 422, mixed.text
        assert mixed.json()["error"]["details"]["not_enabled"] == others
    assert await scalar(db, backfills, s=story) == 0
    return shown


async def test_gap_fillable_full_story(stub_settings: Settings, engine: AsyncEngine) -> None:
    """On a Full story ``gap_fillable`` is exactly ``capabilities_of``'s keys, sorted, with no
    ``:mode`` suffix; each previews, and each other effective-config capability (``episodic``,
    ``perception:full``, …) is 422 ``not_enabled`` with the corrected message."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await _story(client, engine, FULL_PRESET_ID)
        shown = await _check(client, engine, story)
        fillable = shown["gap_fillable"]
        assert fillable
        assert "episodic" in shown["capabilities"]
        assert "episodic" not in fillable
        assert any(":" in name for name in shown["capabilities"])


async def test_gap_fillable_lite_story(stub_settings: Settings, engine: AsyncEngine) -> None:
    """On a Lite story ``gap_fillable`` is whatever ``capabilities_of`` says (empty or minimal),
    and every other effective-config capability is refused the same way."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await _story(client, engine, LITE_PRESET_ID)
        await _check(client, engine, story)
