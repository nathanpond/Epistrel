"""A configuration's per-purpose model routing reaches the calls it routes (#325; FR-PROF-5,
FR-PROF-16, FR-MODEL-3).

A profile, or a story's overrides, may route a story purpose (``models.purposes.<purpose>``:
a model and params). The Lite turn calls ``generation`` and ``guardrail_check`` with the routed
model, the ``lite_summary`` job calls its purpose the same way, and the configuration's
fingerprint, the turn's ``config_revisions.models`` and the effective-configuration read name
the routed model. A purpose left unrouted keeps the deployment's ``/admin/model-config`` model.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.jobs.handlers import lite_summary
from epistrel.orchestrator.models.access import ModelAccessLayer, routing
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import Route
from epistrel.orchestrator.models.stub import STUB_MODEL, StubCall
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, play_turn, rows
from tests.event_support import new_story as event_story
from tests.policy_support import AUTHOR, story_item
from tests.profile_support import PROFILES, WINDOW, merged, nest, profile_body, store_overrides
from tests.respond_support import FIRST, PLAYER, body, new_story, played, respond_client
from tests.story_support import WRITE, override
from tests.summary_support import run_due_jobs

OWLS = {"type": "model_assisted", "prompt": "Is this text about OWLS?", "on_uncertain": "pass"}
ROUTED = {
    "models": {
        "purposes": {
            "generation": {
                "model": "routed-generation",
                "params": {"temperature": 0.25, "top_p": 0.5, "max_tokens": 321},
            },
            "guardrail_check": {"model": "routed-guardrail"},
        }
    }
}
"""A profile that routes the turn's two purposes to models of its own, ``generation`` with
params; ``lite_summary`` is left to the deployment."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json() if response.content else {}
    return found


async def _routed_story(client: httpx.AsyncClient, lib: Engine) -> tuple[str, str]:
    """A two-character story on a profile that routes ``ROUTED``, with a model-assisted output
    policy (so every turn makes a ``guardrail_check``); its id and the actor's."""
    story, ids = await new_story(client, PLAYER, FIRST)
    profile = await ok(
        await client.post(PROFILES, json=profile_body("Routed", settings=ROUTED), headers=WRITE),
        201,
    )
    assigned = await client.put(
        f"/stories/{story}/engine-profile",
        json={"profile_id": profile["id"], "ignore_warnings": True},
        headers=WRITE,
    )
    await ok(assigned)
    await lib.policies_set_story(AUTHOR, story, [story_item(OWLS)])
    return story, ids[PLAYER]


async def _deployment_model(models: ModelAccessLayer, purpose: str) -> str:
    """The model the stub reports for an unrouted ``purpose``: the deployment's configured one."""
    return (await models.resolve(purpose)).model or STUB_MODEL


def _models(calls: list[StubCall]) -> dict[str, str]:
    return {call.purpose: call.model for call in calls}


async def _recorded(engine: AsyncEngine, story: str) -> dict[str, Any]:
    """The latest committed turn's ``config_revisions``."""
    found = await rows(
        engine,
        "SELECT config_revisions FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY committed_at DESC LIMIT 1",
        s=story,
    )
    return dict(found[0][0])


async def _fingerprint(client: httpx.AsyncClient, story: str) -> str:
    read = await ok(await client.get(f"/stories/{story}/effective-config", headers=WRITE))
    return str(read["fingerprint"])


async def test_a_turn_calls_each_purpose_with_its_routed_model(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A profile's routes reach the turn (FR-PROF-5, FR-PROF-16). Its ``generation`` call goes
    to the routed model with the routed temperature, top_p and token cap (recorded in the turn's
    ``generation_settings``), and its ``guardrail_check`` call goes to the routed model. The
    turn's ``model_calls`` name both models. The turn's ``config_revisions.models`` names them
    too, with ``lite_summary`` (unrouted) at the deployment's model. The turn's fingerprint is
    the effective-configuration read's.

    A story override that routes ``generation`` elsewhere wins over the profile: the next turn
    calls that model, and the fingerprint changes. Removing the override restores the profile's
    model and the fingerprint. A request's ``prompt_params`` still win over the routed params.
    Complement: a story on the Lite preset (nothing routed) calls the deployment's models."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        summary_model = (await lib.models.resolve("lite_summary")).model
        # A purpose the turn does not call (#135's importance_scoring) records its resolved model.
        scoring_model = (await lib.models.resolve("importance_scoring")).model
        entailment_model = (await lib.models.resolve("entailment")).model
        story, actor = await _routed_story(client, lib)

        stub.reset()
        turn = await played(client, story, body(actor, "Hello."))
        assert _models(stub.calls) == {
            "generation": "routed-generation",
            "guardrail_check": "routed-guardrail",
        }
        assert {c["purpose"]: c["model"] for c in turn["model_calls"]} == _models(stub.calls)
        (generation,) = [c for c in stub.calls if c.purpose == "generation"]
        assert (generation.params.temperature, generation.params.top_p) == (0.25, 0.5)
        assert generation.params.max_tokens == 321
        recorded = await _recorded(engine, story)
        assert recorded["generation_settings"] == {
            "temperature": 0.25,
            "top_p": 0.5,
            "length": 321,
            "max_tokens": 321,
        }
        assert recorded["models"] == {
            "generation": {"id": "routed-generation", "reported_version": None},
            "guardrail_check": {"id": "routed-guardrail", "reported_version": None},
            "lite_summary": {"id": summary_model, "reported_version": None},
            "importance_scoring": {"id": scoring_model, "reported_version": None},
            "entailment": {"id": entailment_model, "reported_version": None},
        }
        assert turn["fingerprint"] == recorded["fingerprint"] == await _fingerprint(client, story)
        profiled = turn["fingerprint"]

        # A story override routes generation elsewhere: it wins, and the fingerprint follows.
        await override(client, story, nest("models.purposes.generation.model", "story-generation"))
        overridden = await _fingerprint(client, story)
        assert overridden != profiled
        stub.reset()
        turn = await played(client, story, body(actor, "Again."))
        assert _models(stub.calls)["generation"] == "story-generation"
        assert _models(stub.calls)["guardrail_check"] == "routed-guardrail"
        assert turn["fingerprint"] == overridden
        assert (await _recorded(engine, story))["models"]["generation"]["id"] == "story-generation"

        # The override removed: the profile's route again, and its fingerprint.
        await override(client, story, nest("models.purposes.generation.model", None))
        assert await _fingerprint(client, story) == profiled

        # A request's prompt params win over the routed ones.
        stub.reset()
        prompt_params = {"temperature": 0.9, "length": "short"}
        await played(client, story, body(actor, "Once more.", prompt_params=prompt_params))
        (generation,) = [c for c in stub.calls if c.purpose == "generation"]
        assert (generation.model, generation.params.temperature) == ("routed-generation", 0.9)
        assert (generation.params.top_p, generation.params.max_tokens) == (0.5, 150)

        # Complement: nothing routed, the deployment's models.
        plain, ids = await new_story(client, PLAYER, FIRST)
        await lib.policies_set_story(AUTHOR, plain, [story_item(OWLS)])
        stub.reset()
        await played(client, plain, body(ids[PLAYER], "Hello."))
        assert _models(stub.calls) == {
            "generation": await _deployment_model(lib.models, "generation"),
            "guardrail_check": await _deployment_model(lib.models, "guardrail_check"),
        }
        assert await _fingerprint(client, plain) != profiled


async def test_routing_lays_a_route_over_the_deployment_configuration(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``ModelAccessLayer.resolve`` under ``routing``: a routed purpose's model replaces the
    deployment's, and its params are merged over the deployment's key by key. A purpose the
    routes leave out, and every purpose outside the block, keep the deployment's configuration
    (FR-PROF-5, FR-MODEL-3)."""
    models = ModelAccessLayer.from_settings(stub_settings)
    models.bind(engine)
    deployment = await models.resolve("guardrail_check")
    generation = await models.resolve("generation")
    route = Route(model="routed-guardrail", params=ModelParams(temperature=0.1, max_tokens=64))
    with routing({"guardrail_check": route}):
        inside = await models.resolve("guardrail_check")
        assert inside.model == "routed-guardrail"
        assert (inside.params.temperature, inside.params.max_tokens) == (0.1, 64)
        assert inside.params.top_p == deployment.params.top_p
        assert (inside.base_url, inside.revision) == (deployment.base_url, deployment.revision)
        assert await models.resolve("generation") == generation
    assert await models.resolve("guardrail_check") == deployment
    with routing({"guardrail_check": Route(params=ModelParams(top_p=0.3))}):
        params_only = await models.resolve("guardrail_check")
        assert (params_only.model, params_only.params.top_p) == (deployment.model, 0.3)


async def test_the_summary_job_uses_the_story_routing(engine: AsyncEngine, pg_url: str) -> None:
    """The ``lite_summary`` job calls its purpose with the story's routed model, and the
    route's params win over the call's own temperature and token cap (FR-PROF-5). The segment
    records the routed model. Complement: a story that routes nothing calls the deployment's
    model with the job's own params."""
    models = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    models.bind(engine)
    stub = models.stub

    async def summarised(overrides: dict[str, Any]) -> tuple[StubCall, list[Any]]:
        story = await event_story(engine)
        await execute_settings(engine, story)
        await store_overrides(engine, story, merged(nest(WINDOW, 2), overrides))
        for n in range(1, 4):
            await play_turn(engine, story, f"line {n}")
        stub.reset()
        await run_due_jobs(engine, models, story)
        calls = [c for c in stub.calls if c.purpose == "lite_summary"]
        assert calls, "the summary job made no lite_summary call"
        found = await rows(
            engine, "SELECT model FROM lite_summary_segments WHERE story_id = :s", s=story
        )
        return calls[0], [r[0] for r in found]

    route = {"model": "routed-summary", "params": {"temperature": 0.7, "max_tokens": 222}}
    call, segment_models = await summarised(nest("models.purposes.lite_summary", route))
    assert call.model == "routed-summary"
    assert (call.params.temperature, call.params.max_tokens) == (0.7, 222)
    assert segment_models
    assert set(segment_models) == {"routed-summary"}

    call, _ = await summarised({})
    assert call.model == await _deployment_model(models, "lite_summary")
    assert (call.params.temperature, call.params.max_tokens) == (
        lite_summary.BUILT_IN_PARAMS.temperature,
        lite_summary.BUILT_IN_PARAMS.max_tokens,
    )


async def execute_settings(engine: AsyncEngine, story: str) -> None:
    """One turn per level-0 segment (the story's ``lite_summary_chunk``)."""
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = settings || "
            "CAST('{\"lite_summary_chunk\": 2}' AS jsonb) WHERE id = :s",
            {"s": story},
        ),
    )
