"""#658: shutdown waits for the background staged-turn resumes this process started
(``staged.drain_kicked``), up to its bound; one still running then is cancelled at a stage
boundary (the next start's recovery resumes it). ``Engine.stop_workers`` drains them first."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.structured import staged
from tests.auth_support import auth_settings
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _resumes(monkeypatch: pytest.MonkeyPatch, delay: float, done: list[str]) -> None:
    """``staged.resume`` replaced by one that takes ``delay`` seconds and records its end."""

    async def resume(db: Any, operation_id: str, **_: Any) -> staged.Resumed:
        await asyncio.sleep(delay)
        done.append(operation_id)
        return staged.Resumed(operation_id, None)

    monkeypatch.setattr(staged, "resume", resume)


async def test_drain_waits_then_cancels_at_its_bound(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    done: list[str] = []
    _resumes(monkeypatch, 0.05, done)
    staged.kick(engine, "s", "quick")
    assert await staged.drain_kicked(5.0) == 0
    assert done == ["quick"]

    _resumes(monkeypatch, 60.0, done)
    staged.kick(engine, "s", "stuck")
    assert await staged.drain_kicked(0.1) == 1
    assert done == ["quick"]
    assert not staged._kicked


async def test_stop_workers_waits_for_a_background_resume(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    done: list[str] = []
    async with respond_client(stub_settings, engine) as (_client, epistrel):
        _resumes(monkeypatch, 0.2, done)
        staged.kick(epistrel.db, "s", "resuming")
        await epistrel.stop_workers()
        assert done == ["resuming"]
