"""C-07: adapter swap and capability reporting (docs/04), the M2 parts: the swap (#51) and
protocol vs per-purpose capability (#54); and the M4 part (#138): the embedding adapter and the
lexical backend swap the same way, and the model matrix reports the embedding endpoint; and the
M6 part (#204): the external-provider adapters swap the same way."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.retrieval import LexicalFilters, LexicalSearch, TsvectorLexicalSearch, search
from epistrel.engine import Engine
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers.reembed import EMBED
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.capability import STRUCTURED_CALLS, assess
from epistrel.orchestrator.models.registry import DELIVERED
from epistrel.orchestrator.providers import registry as provider_registry
from epistrel.orchestrator.providers.http_manifest import HttpManifestProvider
from epistrel.orchestrator.providers.in_process import InProcessProvider
from epistrel.orchestrator.providers.port import (
    Binding,
    Effect,
    ExternalProvider,
    ProviderUnavailable,
    WriteOutcome,
)
from epistrel.tools import model_matrix as mm
from epistrel.tools.model_matrix import embedding_row
from tests import provider_support as ps
from tests.auth_support import auth_settings
from tests.conformance.suite import GENERATION_MODEL, run_suite
from tests.embedding_support import embedding_db, run_jobs, say, structured_story, vectors
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


class InMemoryLexicalSearch:
    """A test-only lexical backend (C-07 M4): reads the story's active hot memories and ranks
    them by how many of the query's words they contain, in Python."""

    name = "in_memory"

    async def search(
        self,
        conn: Any,
        story_id: str,
        query: str,
        k: int,
        filters: LexicalFilters | None = None,
    ) -> list[tuple[str, float]]:
        found = await conn.execute(
            text(
                "SELECT id, text FROM memories WHERE story_id = :s AND membership = 'active' "
                "AND retrieval_tier = 'hot'"
            ),
            {"s": story_id},
        )
        words = set(re.findall(r"\w+", query.lower()))
        scored = [
            (str(r.id), float(len(words & set(re.findall(r"\w+", r.text.lower()))))) for r in found
        ]
        return sorted(((i, s) for i, s in scored if s > 0), key=lambda h: (-h[1], h[0]))[:k]


LINES = (
    "The lighthouse keeper hid the brass key under the third step.",
    "Rain hammered the harbour all night long.",
)


def core_mentions_of_the_embedding_adapter() -> list[str]:
    """Modules under core that name the embedding adapter switch or its implementations."""
    found: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        source = path.read_text()
        for name in ("embedding_adapter", "EPISTREL_EMBEDDING_ADAPTER", "EmbeddingAdapter"):
            if name in source:
                found.append(f"{path.name}: names {name}")
    return found


@pytest.mark.part("M4", "NFR-PORT-2")
async def test_c_07__adapters(pg_url: str) -> None:
    """Swapping the embedding adapter (``EPISTREL_EMBEDDING_ADAPTER=openai|stub``) or the
    lexical backend (``LexicalSearch``: ``tsvector`` or a test-only in-memory one) changes no
    file under ``src/epistrel/core``, and the same embed-and-search run passes for each. The
    model matrix reports the embedding endpoint's protocol compatibility and dimension.

    Complements: the two runs really used different transports (the endpoint saw
    ``/embeddings`` requests only under ``openai``), and an endpoint that fails the probe is
    reported as a protocol failure with no dimension."""
    assert core_mentions_of_model_access() == []
    assert core_mentions_of_the_embedding_adapter() == []
    before = core_digest()
    seen: dict[str, int] = {}
    dimensions = {"openai": 24, "stub": 64}
    for kind in ("openai", "stub"):
        async with embedding_db(pg_url, embedding_adapter=kind) as s:
            with mock_openai(embedding_dimension=24) as server:
                if kind == "openai":
                    await model_config.write_config(
                        s.db,
                        "embedding",
                        keybox=s.lib.models.keybox,
                        principal="p-operator",
                        base_url=f"{server.root}/v1",
                        model="mock-embed",
                        api_key="k-embed",
                    )
                service = s.lib.models.embeddings
                assert service.adapter.name == kind
                story, cast = await structured_story(s.db)
                memory_ids = [await say(s.db, story, cast["Cal"], line) for line in LINES]
                assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]
                stored = await vectors(s.db, story)
                query = stored[memory_ids[0]].text
                for lexical in (TsvectorLexicalSearch(), InMemoryLexicalSearch()):
                    async with s.db.connect() as conn:
                        found = await search(
                            conn,
                            story,
                            query,
                            k=5,
                            lexical=lexical,
                            embed=service.query_embedder(),
                        )
                    assert found.degradation == ()
                    assert found.vector[0].memory_id == memory_ids[0]
                    assert found.lexical[0].memory_id == memory_ids[0]
                    assert isinstance(lexical, LexicalSearch)

                row = await embedding_row(service, tier=kind, model=kind, calls=3)
                assert row.protocol is True
                assert row.dimension == dimensions[kind]
                assert row.max_batch == 64
                assert row.p50_ms is not None
                seen[kind] = len([r for r in server.requests if r.path.endswith("/embeddings")])
                if kind == "openai":
                    server.fail_with = 500
                    failed = await embedding_row(service, tier=kind, model=kind, calls=1)
                    assert failed.protocol is False
                    assert failed.dimension is None
                    assert failed.protocol_reason == "http_error"
    assert core_digest() == before
    assert seen["openai"] > 0
    assert seen["stub"] == 0
    rendered = mm.render([], [dataclasses.asdict(row)])
    assert "## Embedding" in rendered
    assert "| stub: stub | ✓ | stub | 64 | 64 |" in rendered


# -- M6: the external-provider adapters (#204) ------------------------------------------------

LEDGER = {"p-1": 640, "p-2": 75}
"""The game's balances, by player id: what both adapters read."""
OUTBOX_KEY = "01OUTBOX0000000000000000AA"


def core_mentions_of_provider_adapters() -> list[str]:
    """Modules under core that import the Orchestrator's provider package or name an adapter."""
    found: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(n.startswith("epistrel.orchestrator.providers") for n in names):
                found.append(f"{path.name}: {names}")
        source = path.read_text()
        if "HttpManifestProvider" in source or "InProcessProvider" in source:
            found.append(f"{path.name}: names an adapter")
    return found


async def _conformance(provider: Any, name: str, story: str) -> tuple[list[Any], list[str]]:
    """The C-06 adapter assertions both adapters must pass: the port's protocol, each
    binding's own typed value, a confirmed write with its resulting values under the effect's
    idempotency key, the same key's resend answered alike, and an unanswerable read as the
    port's adapter error. Answers the values read and the outcomes."""
    assert isinstance(provider, ExternalProvider)
    assert provider.name == name
    values = [
        (await provider.read("gold", Binding(story, name, {"player": p}), None)).value
        for p in ("p-1", "p-2")
    ]
    effect = Effect("gold", OUTBOX_KEY, delta=Decimal(-40))
    binding = Binding(story, name, {"player": "p-1"})
    first = await provider.write(effect, binding)
    resent = await provider.write(effect, binding)
    with pytest.raises(ProviderUnavailable) as down:
        await provider.read("gold", Binding(story, name, {"player": "down"}), None)
    assert down.value.provider == name
    return values, [first.status, resent.status, str((first.resulting_values or {})["gold"])]


@pytest.mark.integration
@pytest.mark.part("M6", "NFR-PORT-2")
async def test_c_07__m6(pg_url: str, engine: AsyncEngine) -> None:
    """The external-provider adapters swap with no Engine Core change: the same C-06
    conformance run passes for a declarative HTTP manifest (``HttpManifestProvider`` over a
    loopback stub) and for in-process callables (``InProcessProvider``), each resolved by name
    from its registration through the same ``provider_for``, and nothing under
    ``src/epistrel/core`` changes or names an adapter. (No model-matrix row: the owner's
    decision of 2026-10-30.)

    Complements asserted: the two runs really used different transports (the stub saw the
    HTTP run's requests only, the callables the in-process run's only), and an in-process
    provider with no callable registered in this process is the port's adapter error."""
    assert core_mentions_of_provider_adapters() == []
    before = core_digest()
    story = new_ulid()
    results: dict[str, tuple[list[Any], list[str]]] = {}
    calls: dict[str, int] = {}
    epistrel = Engine.from_settings(auth_settings(pg_url))

    # The HTTP manifest over a loopback stub.
    with ps.stub_server() as stub:
        for player, balance in LEDGER.items():
            stub.route("GET", f"/players/{player}/gold", ps.Reply(200, {"balance": balance}))
        stub.route("GET", "/players/down/gold", ps.Reply(503, {"error": "down"}))
        stub.route(
            "POST", "/players/p-1/spend", ps.Reply(200, {"status": "ok", "values": {"gold": 600}})
        )
        async with ps.allowed_hosts(engine, [stub.host]):
            http_name = ps.unique_name("http")
            await ps.register(
                engine,
                ps.http_spec(stub.base_url, http_name, write=ps.write_template(stub.base_url)),
            )
            async with engine.connect() as conn:
                provider = await provider_registry.provider_for(
                    conn, http_name, in_process=epistrel.in_process_providers
                )
            assert isinstance(provider, HttpManifestProvider)
            results["http"] = await _conformance(provider, http_name, story)
        sent = [r for r in stub.received if r.method == "POST"]
        assert {r.headers["Idempotency-Key"] for r in sent} == {OUTBOX_KEY}
        assert sent[0].body == {"amount": -40, "note": "spend -40"}
        calls["http"] = len(stub.received)

    # In-process callables over the same ledger.
    in_process_name = ps.unique_name("inproc")
    await ps.register(engine, ps.in_process_spec(in_process_name, writable=["gold"]))
    seen: list[str] = []
    first_outcomes: dict[str, WriteOutcome] = {}

    def read(value: str, binding: Binding, scope_key: str | None) -> Any:
        seen.append(binding.external_ids["player"])
        if binding.external_ids["player"] == "down":
            raise ConnectionError("the game is down")
        return LEDGER[binding.external_ids["player"]]

    async def write(effect: Effect, binding: Binding) -> WriteOutcome:
        seen.append(effect.idempotency_key)
        return first_outcomes.setdefault(
            effect.idempotency_key, WriteOutcome("confirmed", resulting_values={"gold": 600})
        )

    async with engine.connect() as conn:
        orphan = await provider_registry.provider_for(
            conn, in_process_name, in_process=epistrel.in_process_providers
        )
    with pytest.raises(ProviderUnavailable) as unregistered:
        await orphan.read("gold", Binding(story, in_process_name, {"player": "p-1"}), None)
    assert unregistered.value.reason == "not_registered"
    epistrel.register_in_process_provider(in_process_name, read=read, write=write)
    async with engine.connect() as conn:
        provider = await provider_registry.provider_for(
            conn, in_process_name, in_process=epistrel.in_process_providers
        )
    assert isinstance(provider, InProcessProvider)
    results["in_process"] = await _conformance(provider, in_process_name, story)
    calls["in_process"] = len(seen)

    expected = ([Decimal(640), Decimal(75)], ["confirmed", "confirmed", "600"])
    assert results["http"] == expected
    assert results["in_process"] == expected
    assert calls == {"http": 5, "in_process": 5}
    assert core_digest() == before
