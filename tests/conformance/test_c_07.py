"""C-07: adapter swap and capability reporting (docs/04), the M2 parts: the swap (#51) and
protocol vs per-purpose capability (#54)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.capability import STRUCTURED_CALLS, assess
from epistrel.orchestrator.models.registry import DELIVERED
from tests.auth_support import auth_settings
from tests.conformance.suite import GENERATION_MODEL, run_suite
from tests.model_support import config_key, mock_openai, restored_model_config

CORE = Path(__file__).resolve().parents[2] / "src" / "epistrel" / "core"


def core_digest() -> str:
    """One hash over every file under ``src/epistrel/core``."""
    digest = hashlib.sha256()
    for path in sorted(CORE.rglob("*.py")):
        digest.update(path.relative_to(CORE).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def core_mentions_of_model_access() -> list[str]:
    """Modules under core that name the Model Access Layer or the adapter switch."""
    found: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(n.startswith(("epistrel.orchestrator", "openai")) for n in names):
                found.append(f"{path.name}: {names}")
        if "model_adapter" in source or "EPISTREL_MODEL_ADAPTER" in source:
            found.append(f"{path.name}: names the adapter setting")
    return found


@pytest.mark.integration
@pytest.mark.part("M2", "NFR-PORT-1,2")
async def test_c_07__swap(pg_url: str, engine: AsyncEngine) -> None:
    """Swapping ``EPISTREL_MODEL_ADAPTER`` between ``openai`` and ``stub`` changes nothing under
    ``src/epistrel/core`` and the same conformance suite passes for both.

    Complement asserted: the two runs really used different transports (the endpoint saw
    requests only under ``openai``).
    """
    assert core_mentions_of_model_access() == []
    before = core_digest()
    seen: dict[str, int] = {}
    for kind in ("openai", "stub"):
        settings = auth_settings(pg_url, model_adapter=kind, config_key=config_key())
        async with restored_model_config(engine):
            with mock_openai() as server:
                epistrel = Engine.from_settings(settings)
                epistrel.bind(engine)
                try:
                    observed = await run_suite(epistrel, settings, server)
                finally:
                    await epistrel.models.aclose()
                assert epistrel.models.adapter.name == kind
                assert observed.results["generation"].model == GENERATION_MODEL
                seen[kind] = len(server.requests)
    assert core_digest() == before
    assert seen["openai"] > 0
    assert seen["stub"] == 0


@pytest.mark.part("M2", "NFR-PORT-3")
async def test_c_07__capability(dummy_dsn: str) -> None:
    """Protocol compatibility and per-purpose capability are separate results. The stub speaks
    the protocol and meets every floor; with garbage on the structured-output floor of
    ``guardrail_check`` it still passes protocol, and only that purpose is reported incapable.

    Complements asserted: an endpoint that fails the protocol probe is reported as a protocol
    failure with no capability results at all (not as N incapable purposes); a purpose whose
    floor does not need structured output skips that check rather than passing it."""
    models = ModelAccessLayer(auth_settings(dummy_dsn, model_adapter="stub"))
    models.bind(None)
    stub = models.stub

    clean = await assess(models)
    assert clean.protocol.ok
    assert set(clean.purposes) == set(DELIVERED)
    assert all(result.capable for result in clean.purposes.values())
    structured = {c.name: c for c in clean.purposes["guardrail_check"].checks}["structured_output"]
    assert (structured.passed, structured.valid, structured.mode) == (True, 20, "json_schema")
    skipped = {c.name: c for c in clean.purposes["generation"].checks}["structured_output"]
    assert skipped.passed is None

    stub.fault("guardrail_check", "garbage", n=STRUCTURED_CALLS)
    degraded = await assess(models)
    assert degraded.protocol.ok
    assert degraded.purposes["generation"].capable
    assert degraded.purposes["lite_summary"].capable
    guardrail = degraded.purposes["guardrail_check"]
    assert not guardrail.capable
    checks = {c.name: c for c in guardrail.checks}
    assert checks["context"].passed is True
    assert checks["structured_output"].passed is False
    assert checks["structured_output"].valid == 0
    assert checks["structured_output"].mode == "parse_validate_retry"

    stub.reset()
    stub.fault("generation", "connection")
    unreachable = await assess(models)
    assert not unreachable.protocol.ok
    assert unreachable.protocol.reason == "connection"
    assert unreachable.purposes == {}
