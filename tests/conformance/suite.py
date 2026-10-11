"""The adapter conformance suite (docs/04 C-04, M2 parts): one routine every model adapter must
pass, run by ``test_c_04`` for each adapter and by ``test_c_07__swap`` across a swap.

The suite configures a ``default`` row and two overrides, calls every delivered purpose through
the Model Access Layer, and returns what it observed so the tests can assert on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx2 as httpx

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.access import declared_fields
from epistrel.orchestrator.models.adapter import ModelResult, ModelUnavailable
from epistrel.orchestrator.models.prompts import example_instance
from epistrel.orchestrator.models.registry import DELIVERED
from tests.auth_support import bearer
from tests.model_support import MockOpenAI

OPERATOR = "p-operator"
DEFAULT_KEY = "sk-conformance-default-0001"
HOUSE_MODEL = "house-model"
GENERATION_MODEL = "generation-model"


@dataclass
class Observed:
    unconfigured_error: str | None = None
    results: dict[str, ModelResult] = field(default_factory=dict)
    repeat: dict[str, ModelResult] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)
    authorizations: set[str | None] = field(default_factory=set)
    fields_outside_declared: dict[str, set[str]] = field(default_factory=dict)
    listing: dict[str, object] = field(default_factory=dict)


async def configure(engine: Engine, mock: MockOpenAI) -> None:
    """``default`` → the mock's ``/v1`` with a key; ``generation`` overrides the model;
    ``guardrail_check`` overrides the base URL (a gateway path on the same server)."""
    write = model_config.write_config
    keybox = engine.models.keybox
    await write(
        engine.db,
        "default",
        keybox=keybox,
        principal=OPERATOR,
        base_url=f"{mock.root}/v1/",
        model=HOUSE_MODEL,
        api_key=DEFAULT_KEY,
    )
    await write(engine.db, "generation", keybox=keybox, principal=OPERATOR, model=GENERATION_MODEL)
    await write(
        engine.db,
        "guardrail_check",
        keybox=keybox,
        principal=OPERATOR,
        base_url=f"{mock.root}/gateway/v1",
    )


async def run_suite(engine: Engine, settings: Settings, mock: MockOpenAI) -> Observed:
    observed = Observed()
    probe = example_instance(DELIVERED["generation"].input_schema)
    try:
        await engine.models.call("generation", probe)
    except ModelUnavailable as exc:
        observed.unconfigured_error = str(exc.details["reason"])
    await configure(engine, mock)
    for name, purpose in DELIVERED.items():
        inputs = example_instance(purpose.input_schema)
        before = len(mock.requests)
        observed.results[name] = await engine.models.call(name, inputs)
        observed.repeat[name] = await engine.models.call(name, inputs)
        if len(mock.requests) > before:
            observed.paths[name] = mock.requests[before].path
    observed.authorizations = {r.authorization for r in mock.requests}
    if engine.models.adapter.name == "stub":
        for call in engine.models.stub.calls:
            outside = set(call.fields or ()) - declared_fields(DELIVERED[call.purpose])
            observed.fields_outside_declared[call.purpose] = outside
    app = create_app(settings)
    app.state.epistrel = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/model-config", headers=bearer())
        assert response.status_code == 200, response.text
        observed.listing = response.json()
    return observed
