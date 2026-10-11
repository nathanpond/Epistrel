"""C-07: adapter swap (docs/04), the M2 swap half; the capability-reporting half is #54's."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
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
