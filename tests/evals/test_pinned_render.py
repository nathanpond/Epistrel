"""E-12 direct mode's render pin holds on an endpoint that is not in JSON-schema mode (#938).

A structured call sends no ``response_format`` there: the schema rides in the prompt and the
adapter's ``response_schema`` is ``None``. The pin (``runner.PINNED``) then reads the schema the
call is for from ``call_trace.STRUCTURED_MODEL``. Before the fix a live run's render went to
the model, so a direct case played the model's own draft, never the prepared one (found by
#938's live smoke on hosted Haiku).

The "endpoint" is the engine's own stub, as in ``test_routing``: the path a real-model run
takes, offline.

No test here carries a part marker: this is harness tooling, not a requirement's behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, runner
from epistrel.evals.runner import PINNED, pinned_call
from epistrel.evals.suites.e12_direct import DirectCase
from epistrel.evals.suites.scenario_play import user_line
from epistrel.orchestrator.compose.compose import ComposedMessage
from epistrel.orchestrator.models import structured
from epistrel.orchestrator.models.call_trace import STRUCTURED_MODEL
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.orchestrator.structured.render import RenderedBeats
from tests.eval.suite_support import SETS
from tests.evals.support import stub_engine

LIVE = AdapterConfig(adapter="openai", base_url="http://endpoint.invalid/v1", model="live-model")


class CharacterRender(RenderedBeats):
    """Stands in for a render's per-turn schema (``render.beat_schema``: a subclass)."""


def test_a_schema_in_the_prompt_is_still_pinned() -> None:
    pins = PINNED.set((RenderedBeats,))
    try:
        assert not pinned_call(None)
        token = STRUCTURED_MODEL.set(CharacterRender)
        try:
            assert pinned_call(None)
            assert not pinned_call(ComposedMessage)  # a schema sent wins over the prompt's
        finally:
            STRUCTURED_MODEL.reset(token)
        token = STRUCTURED_MODEL.set(ComposedMessage)
        try:
            assert not pinned_call(None)
        finally:
            STRUCTURED_MODEL.reset(token)
    finally:
        PINNED.reset(pins)
    token = STRUCTURED_MODEL.set(CharacterRender)
    try:
        assert not pinned_call(None)  # nothing pinned outside a direct case
    finally:
        STRUCTURED_MODEL.reset(token)


@pytest.mark.integration
async def test_a_live_direct_case_plays_its_prepared_draft(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct case on a routed run whose endpoint is not in JSON-schema mode: the render
    reaches the run's stub (which answers the prepared draft), never the endpoint."""
    made: list[StubAdapter] = []

    def stub_factory(*args: Any, **kwargs: Any) -> StubAdapter:
        found = StubAdapter(*args, **kwargs)
        made.append(found)
        return found

    monkeypatch.setattr(runner, "StubAdapter", stub_factory)
    monkeypatch.setattr(structured, "json_schema_mode", lambda *a, **k: False)
    suite = SUITES["e12_direct"]
    (case,) = [c for c in suite.load(SETS) if c.id == "direct-rung-1-0001"]
    assert isinstance(case, DirectCase)
    line = user_line(case.say)
    async with stub_engine(pg_url, engine) as lib:
        endpoint = lib.models.stub
        with runner.routed(lib, LIVE):
            job = runner.SuiteRun(
                engine=lib,
                config=LIVE,
                seeds=[20261019],
                run_id="pin-test",
                cases={suite.name: [case]},
            )
            await runner.run_suite(suite, job)
        sent = [c for c in endpoint.calls if c.purpose == "generation"]
    (routed_stub,) = made
    renders = [
        c for c in routed_stub.calls if c.purpose == "generation" and line in c.messages[-1].content
    ]
    assert renders, "the render never reached the run's stub"
    assert not any(
        line in c.messages[-1].content and "beats" in c.messages[0].content for c in sent
    )
