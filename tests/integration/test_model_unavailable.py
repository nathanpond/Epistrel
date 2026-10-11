"""A provider failure fails the turn cleanly over REST (#53; NFR-REL-3): 503
``model_unavailable`` naming the purpose and the reason, the operation marked ``failed`` with
no event, and the story's revision unchanged. A 429's ``Retry-After`` passes through; no other
status carries one."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.models.stub import RETRY_AFTER, FaultKind
from epistrel.testing.ops import TEST_EVENT, make_router
from tests.auth_support import auth_settings
from tests.event_support import new_story
from tests.story_support import WRITE
from tests.structured_support import POLICY_PROMPT, story_state


@asynccontextmanager
async def guarded_client(
    settings: Settings, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, Engine]]:
    app = create_app(settings)
    include_protected(app, make_router())
    epistrel: Engine = app.state.epistrel
    epistrel.bind(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, epistrel


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


FAILURES: list[tuple[str, FaultKind, dict[str, object]]] = [
    ("guardrail_check", "timeout", {"reason": "timeout", "attempts": 1}),
    ("guardrail_check", "connection", {"reason": "connection", "attempts": 1}),
    ("guardrail_check", "http_500", {"reason": "http_error", "provider_status": 500}),
    ("guardrail_check", "http_429", {"reason": "rate_limited", "provider_status": 429}),
    ("guardrail_check", "http_401", {"reason": "auth", "provider_status": 401}),
    ("guardrail_check", "bad_body", {"reason": "garbage", "attempts": 1}),
    ("generation", "http_500", {"reason": "http_error", "provider_status": 500}),
    ("generation", "timeout", {"reason": "timeout"}),
    ("generation", "empty", {"reason": "empty"}),
    ("generation", "bad_body", {"reason": "garbage"}),
]


@pytest.mark.parametrize(("purpose", "kind", "expected"), FAILURES)
async def test_provider_failure_is_a_clean_503(
    stub_settings: Settings,
    engine: AsyncEngine,
    purpose: str,
    kind: FaultKind,
    expected: dict[str, object],
) -> None:
    """Complement asserted: the same request without the fault commits one event and moves
    the revision, so "nothing changed" is the failure's doing."""
    story = await new_story(engine)
    body = {"said": "a quiet road", "model_prompt": POLICY_PROMPT}
    url = f"/_test/stories/{story}/guarded-reply"
    async with guarded_client(stub_settings, engine) as (client, epistrel):
        before = await story_state(engine, story)
        epistrel.models.stub.fault(purpose, kind)
        failed = await client.post(url, json=body, headers=WRITE)
        after = await story_state(engine, story)

        assert failed.status_code == 503, failed.text
        error = failed.json()["error"]
        assert error["code"] == "model_unavailable"
        assert error["details"]["purpose"] == purpose
        assert expected.items() <= error["details"].items()
        assert ("Retry-After" in failed.headers) == (kind == "http_429")
        if kind == "http_429":
            assert failed.headers["Retry-After"] == RETRY_AFTER
        assert "stub fault" not in failed.text  # provider text redacted to its class

        assert after.timeline_revision == before.timeline_revision
        (new_op,) = set(after.statuses) - set(before.statuses)
        assert after.statuses[new_op] == "failed"
        assert new_op not in after.events

        ok = await client.post(url, json=body, headers=WRITE)
        assert ok.status_code == 200, ok.text
        done = await story_state(engine, story)
        assert (
            ok.json()["timeline_revision"] == done.timeline_revision == after.timeline_revision + 1
        )
        assert done.events[ok.json()["operation_id"]] == [TEST_EVENT]
