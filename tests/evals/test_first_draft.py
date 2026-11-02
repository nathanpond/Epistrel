"""#647: ``scenario_play.first_draft`` reads a played turn's report even when every render beat
was dropped (its result keeps only the input and message beats): the operation comes from any
beat, so E-09's removed skips and E-12's interventions see the turn."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.evals import runner
from epistrel.evals.suites import scenario_play
from epistrel.orchestrator.lite.schemas import RespondRequest
from tests.auth_support import auth_settings
from tests.respond_support import body, respond_client
from tests.structured_turn_support import beat, render, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_a_turn_with_no_render_beat_left_has_its_report(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await structured_story(client, "Wren", "Ada")
        lib.models.stub.script("generation", [render(beat("say", "Hello."))])
        result = await lib.respond(
            RequestContext(principal="p-lib"),
            story,
            RespondRequest.model_validate(body(ids["Wren"], "Ada?")),
        )
        dropped = dataclasses.replace(
            result,
            beats=[b for b in result.beats if not b.kind.startswith(scenario_play.RENDER_PREFIX)],
        )
        ctx = runner.CaseContext(lib, runner.Scratch(lib, "x"), 1, "x")
        *_, report = await scenario_play.first_draft(ctx, dropped, {})
        assert report, "the turn report is read"
        assert "verify" in report
