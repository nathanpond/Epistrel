"""`X-Epistrel-Expected-Revision`: a start-time precondition on story-scoped mutations (#47)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.errors import RevisionConflict, StoryNotFound
from epistrel.engine import Engine
from tests.auth_support import auth_settings, bearer, make_test_router
from tests.event_support import new_story, play_turn, scalar

HEADER = "X-Epistrel-Expected-Revision"
PRINCIPAL = {"X-Epistrel-Principal": "p-operator"}
COUNTS = (
    "SELECT (SELECT count(*) FROM operations WHERE story_id = :s), "
    "(SELECT count(*) FROM events WHERE story_id = :s), "
    "(SELECT timeline_revision FROM stories WHERE id = :s)"
)


def _with_engine[T](url: str, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def run() -> T:
        engine = make_engine(auth_settings(url))
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def _story(engine: AsyncEngine) -> str:
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    return story


async def _counts(engine: AsyncEngine, story: str) -> tuple[Any, ...]:
    async with engine.connect() as conn:
        return tuple((await conn.execute(text(COUNTS), {"s": story})).one())


def test_expected_revision(pg_url: str) -> None:
    """A stale expected revision is 409 `revision_conflict` carrying `current_revision`, and
    the call writes nothing. Complement: the matching revision, and no header, both proceed and
    the response carries `timeline_revision`."""
    story = _with_engine(pg_url, _story)
    before = _with_engine(pg_url, lambda e: _counts(e, story))
    assert before[2] == 2
    url = f"/admin/stories/{story}/rebuild"
    with TestClient(create_app(auth_settings(pg_url))) as client:
        stale = client.post(url, headers={**bearer(), **PRINCIPAL, HEADER: "1"})
        assert stale.status_code == 409
        assert stale.json()["error"] == {
            "code": "revision_conflict",
            "message": "expected timeline revision 1, the story is at 2",
            "details": {"current_revision": 2, "attempts": 0, "reason": "expected_revision"},
        }
        assert _with_engine(pg_url, lambda e: _counts(e, story)) == before

        fresh = client.post(url, headers={**bearer(), **PRINCIPAL, HEADER: "2"})
        assert fresh.status_code == 200
        assert fresh.json()["timeline_revision"] == 2
        unheaded = client.post(url, headers={**bearer(), **PRINCIPAL})
        assert unheaded.status_code == 200
        assert unheaded.json()["timeline_revision"] == 2
    operations, events, revision = _with_engine(pg_url, lambda e: _counts(e, story))
    assert (operations, events, revision) == (before[0] + 2, before[1], 2)


def test_expected_revision_is_ignored_on_deployment_calls(pg_url: str) -> None:
    """A deployment-scoped mutation has no timeline: a well-formed header is ignored (not 400
    or 409); a malformed one is still 422."""
    app = create_app(auth_settings(pg_url))
    include_protected(app, make_test_router())
    with TestClient(app) as client:
        ok = client.post(
            "/_test/write", json={"value": 1}, headers={**bearer(), **PRINCIPAL, HEADER: "99"}
        )
        assert ok.status_code == 200
        bad = client.post(
            "/_test/write", json={"value": 1}, headers={**bearer(), **PRINCIPAL, HEADER: "x"}
        )
        assert bad.status_code == 422


async def test_library_checks_the_expected_revision(engine: AsyncEngine) -> None:
    """The in-process Engine applies the same precondition before writing anything."""
    story = await _story(engine)
    library = Engine(auth_settings("postgresql+psycopg://unused/x"), db=engine)
    operations = "SELECT count(*) FROM operations WHERE story_id = :s"
    before = await scalar(engine, operations, s=story)
    with pytest.raises(RevisionConflict) as caught:
        await library.rebuild_projections(RequestContext(principal="p", expected_revision=7), story)
    assert caught.value.details["current_revision"] == 2
    assert await scalar(engine, operations, s=story) == before
    with pytest.raises(StoryNotFound):
        await library.check_expected_revision(RequestContext(principal="p"), "0" * 26)
    result = await library.rebuild_projections(
        RequestContext(principal="p", expected_revision=2), story
    )
    assert result.timeline_revision == 2
    async with engine.connect() as conn:
        assert await check_expected_revision(conn, story, 2) == 2
        assert not conn.in_transaction()
