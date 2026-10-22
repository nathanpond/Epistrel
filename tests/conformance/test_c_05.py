"""C-05 (M4): embedding-adapter conformance (docs/04; FR-EMBED-1..4; #138).

Any OpenAI-compatible ``/embeddings`` endpoint works (the stub, and the ``openai`` SDK adapter
against a local mock serving ``/embeddings``); every vector is stamped with its
embedding-model version; the dimension is sticky (a vector of another dimension is refused and
nothing is stored); a search never compares vectors of two versions; and a model change
enqueues the background re-embed job, which completes and re-stamps.

Every test runs on a database of its own (``embedding_support.embedding_db``): the first
embedding retypes ``memories.embedding`` and builds the HNSW index, a schema change the shared
session database must not see. ``reset`` puts it back to unpinned before each test.

C-05 (M4) has exactly this file (traceability allows one file per test ID and milestone).
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any

import pytest

from epistrel.core import embeddings
from epistrel.core.config_changes import read_settings
from epistrel.core.retrieval import (
    EMBEDDING_PENDING,
    EMBEDDING_UNAVAILABLE,
    SearchResult,
    TsvectorLexicalSearch,
    search,
)
from epistrel.orchestrator.embeddings.adapter import stub_vector
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers.reembed import EMBED, REEMBED, coalesce_key
from epistrel.orchestrator.models import metrics
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.registry import EMBEDDING as PORT
from tests.embedding_support import (
    Scratch,
    column_type,
    embedding_db,
    hnsw_index,
    ids,
    jobs_of,
    pin_of,
    reset,
    run_jobs,
    say,
    story_version,
    structured_story,
    vectors,
)
from tests.event_support import execute_in_tx, rows
from tests.live_support import live_embedding
from tests.memory_support import turn
from tests.model_support import OPERATOR_HEADERS, mock_openai, put_key

pytestmark = pytest.mark.integration

STUB_VERSION = "stub:sha256:64"
LEXICAL = TsvectorLexicalSearch()
LINES = (
    "The lighthouse keeper hid the brass key under the third step.",
    "Rain hammered the harbour all night long.",
)


@pytest.fixture(scope="module")
async def scratch(pg_url: str) -> AsyncIterator[Scratch]:
    async with embedding_db(pg_url) as s:
        yield s


@pytest.fixture
async def s(scratch: Scratch) -> Scratch:
    await reset(scratch.db)
    scratch.lib.models.embeddings.stub.reset()
    scratch.lib.models.embeddings.stub.dimension = 64
    return scratch


async def _embedded_story(s: Scratch) -> tuple[str, dict[str, str], list[str]]:
    story, cast = await structured_story(s.db)
    memory_ids = [await say(s.db, story, cast["Cal"], line) for line in LINES]
    assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]
    return story, cast, memory_ids


async def _search(s: Scratch, story: str, query: str) -> SearchResult:
    async with s.db.connect() as conn:
        return await search(
            conn,
            story,
            query,
            k=5,
            lexical=LEXICAL,
            embed=s.lib.models.embeddings.query_embedder(),
        )


def _close(stored: str, expected: list[float]) -> bool:
    values = [float(v) for v in stored.strip("[]").split(",")]
    return len(values) == len(expected) and all(
        math.isclose(a, b, abs_tol=1e-6) for a, b in zip(values, expected, strict=True)
    )


@pytest.mark.part("M4", "FR-EMBED-1,2,3,4")
async def test_c_05(s: Scratch) -> None:
    """The stub endpoint works: a structured turn's memories get vectors from the background
    ``embed`` job (never the turn), each the stub's SHA-256 vector of its text, stamped with
    the stub's version. The first embedding pins the deployment (version, dimension, a
    ``config_changes`` row), retypes the column to ``vector(64)`` and builds the HNSW index, and
    sets the story's version. A search compares the query's vector with the pinned version's
    vectors only.

    Complement: a vector stamped with another version is never compared (absent from the vector
    hits even when its text is the query), while the lexical channel still finds it."""
    story, cast = await structured_story(s.db)
    first = await say(s.db, story, cast["Cal"], LINES[0])
    # The turn wrote the memory and enqueued its job; nothing is embedded yet.
    assert (await vectors(s.db, story))[first].vector is None
    (job,) = await jobs_of(s.db, EMBED, story)
    assert job.coalesce_key == coalesce_key(EMBED, story)
    assert s.lib.models.embeddings.stub.calls == []
    second = await say(s.db, story, cast["Cal"], LINES[1])
    assert len(await jobs_of(s.db, EMBED, story)) == 1  # coalesced
    before = metrics.sample(
        "epistrel_model_calls_total", purpose="embedding", model="stub", outcome="ok"
    )

    assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]

    assert s.lib.models.embeddings.stub.calls == [2]  # one batch
    after = metrics.sample(
        "epistrel_model_calls_total", purpose="embedding", model="stub", outcome="ok"
    )
    assert after == before + 1
    stored = await vectors(s.db, story)
    for memory_id in (first, second):
        row = stored[memory_id]
        assert row.version == STUB_VERSION
        assert row.dims == 64
        assert _close(row.vector, stub_vector(row.text, 64))
    pin = await pin_of(s.db)
    assert (pin.version, pin.dimension) == (STUB_VERSION, 64)
    assert pin.pinned_at is not None
    assert await column_type(s.db) == "vector(64)"
    index = await hnsw_index(s.db)
    assert index is not None
    assert "USING hnsw" in index
    assert "vector_cosine_ops" in index
    assert await story_version(s.db, story) == STUB_VERSION
    (pinned,) = await rows(
        s.db,
        "SELECT actor, class AS change_class, new FROM config_changes WHERE setting = :s",
        s=embeddings.PIN_SETTING,
    )
    assert (pinned.actor, pinned.change_class) == ("system", "prospective")
    assert pinned.new == {"version": STUB_VERSION, "dimension": 64}

    found = await _search(s, story, stored[first].text)
    assert found.degradation == ()
    assert found.embedding_model_version == STUB_VERSION
    assert ids(found.vector)[0] == first
    assert math.isclose(found.vector[0].score, 1.0, abs_tol=1e-5)
    assert set(ids(found.vector)) == {first, second}

    # Complement: the same text in another vector space is never compared.
    await execute_in_tx(
        s.db,
        (
            "UPDATE memories SET embedding_model_version = 'elsewhere/old-model' WHERE id = :m",
            {"m": first},
        ),
    )
    mixed = await _search(s, story, stored[first].text)
    assert first not in ids(mixed.vector)
    assert ids(mixed.vector) == [second]
    assert first in ids(mixed.lexical)


@pytest.mark.part("M4", "FR-EMBED-1,2,3,4")
async def test_c_05__dimension(s: Scratch) -> None:
    """Dimension stickiness: once pinned at 64, an embedding of another dimension is refused,
    503 ``embedding_dimension_mismatch`` with ``{expected, actual, model, pinned_version}``,
    and nothing is stored (the job fails; the memory keeps no vector).

    Complement: with the model answering 64 again, the same memory is embedded."""
    story, cast, _ = await _embedded_story(s)
    late = await say(s.db, story, cast["Cal"], "A gull stole the fisherman's bread.")
    stub = s.lib.models.embeddings.stub
    stub.dimension = 32
    await execute_in_tx(
        s.db,
        ("UPDATE jobs SET failures = 2 WHERE kind = 'embed' AND story_id = :s", {"s": story}),
    )

    assert await run_jobs(s.db, EMBED, story) == [queue.FAILED]

    (job,) = [j for j in await jobs_of(s.db, EMBED, story) if j.status == queue.FAILED]
    assert job.result == {"error": "EmbeddingDimensionMismatch"}
    assert (await vectors(s.db, story))[late].vector is None
    pin = await pin_of(s.db)
    assert (pin.version, pin.dimension) == (STUB_VERSION, 64)
    mismatch = embeddings.EmbeddingDimensionMismatch(
        expected=64, actual=32, model="stub", pinned_version=STUB_VERSION
    )
    assert (mismatch.code, mismatch.http_status) == ("embedding_dimension_mismatch", 503)
    async with s.db.begin() as conn:
        with pytest.raises(embeddings.EmbeddingDimensionMismatch) as refused:
            await embeddings.stamp(conn, story, {late: [0.0] * 32}, await embeddings.read_pin(conn))
    assert refused.value.details == {
        "expected": 64,
        "actual": 32,
        "model": STUB_VERSION,
        "pinned_version": STUB_VERSION,
    }
    assert (await vectors(s.db, story))[late].vector is None

    stub.dimension = 64
    async with s.db.begin() as conn:
        await queue.enqueue(
            conn, EMBED, {}, story_id=story, coalesce_key=coalesce_key(EMBED, story)
        )
    assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]
    assert (await vectors(s.db, story))[late].dims == 64


async def _put_model(s: Scratch, model: str) -> dict[str, object]:
    response = await s.client.put(
        "/admin/model-config/embedding", json={"model": model}, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def _status(s: Scratch) -> dict[str, Any]:
    response = await s.client.get("/admin/reembed/status", headers=OPERATOR_HEADERS)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.part("M4", "FR-EMBED-1,2,3,4")
async def test_c_05__model_change(s: Scratch) -> None:
    """A model change through ``PUT /admin/model-config/embedding`` records the change, moves
    the pin to the new version and enqueues one ``reembed`` job for the story; until it
    completes the story's searches are lexical-only with ``embedding_pending``, and
    ``GET /admin/reembed/status`` shows the job queued. The job re-embeds the active hot
    memories in the new space (new vectors, new version), advances the story and the status
    reports it done and finished; the search compares vectors again.

    Complements: a retired memory's retained vector is nulled and not re-embedded; a PUT naming
    the same model again enqueues nothing; a change of dimension nulls every vector and retypes
    the column in the PUT's own transaction."""
    story, _, memory_ids = await _embedded_story(s)
    retired = memory_ids[1]
    await execute_in_tx(
        s.db, ("UPDATE memories SET retrieval_tier = 'retired' WHERE id = :m", {"m": retired})
    )
    old = (await vectors(s.db, story))[memory_ids[0]].vector

    body = await _put_model(s, "embed-v2")
    assert body["model"] == "embed-v2"
    new_version = "stub:embed-v2:sha256:64"
    pin = await pin_of(s.db)
    assert (pin.version, pin.dimension) == (new_version, 64)
    assert pin.reembed_state["from_version"] == STUB_VERSION
    assert pin.reembed_state["to_version"] == new_version
    settings_changed = {
        r.setting
        for r in await rows(s.db, "SELECT setting FROM config_changes WHERE actor = 'p-operator'")
    }
    assert {"model_config:embedding.model", embeddings.PIN_SETTING} <= settings_changed
    (job,) = await jobs_of(s.db, REEMBED, story)
    assert job.status == queue.QUEUED
    stored = await vectors(s.db, story)
    assert stored[retired].vector is None  # a retired vector is nulled, not re-embedded
    assert stored[memory_ids[0]].version == STUB_VERSION  # until the job, still the old space

    pending = await _search(s, story, stored[memory_ids[0]].text)
    assert pending.degradation == (EMBEDDING_PENDING,)
    assert pending.vector == ()
    assert memory_ids[0] in ids(pending.lexical)
    status = await _status(s)
    assert (status["from_version"], status["to_version"]) == (STUB_VERSION, new_version)
    assert status["finished_at"] is None
    assert status["stories"] == [
        {"story_id": story, "total": 1, "done": 0, "status": "queued", "error": None}
    ]

    assert await run_jobs(s.db, REEMBED, story) == [queue.CommitOutcome.COMMITTED]

    stored = await vectors(s.db, story)
    assert stored[memory_ids[0]].version == new_version
    assert stored[memory_ids[0]].vector != old
    assert _close(
        stored[memory_ids[0]].vector, stub_vector(stored[memory_ids[0]].text, 64, "embed-v2")
    )
    assert stored[retired].vector is None
    assert await story_version(s.db, story) == new_version
    status = await _status(s)
    assert status["finished_at"] is not None
    assert status["stories"] == [
        {"story_id": story, "total": 1, "done": 1, "status": "done", "error": None}
    ]
    again = await _search(s, story, stored[memory_ids[0]].text)
    assert again.degradation == ()
    assert ids(again.vector) == [memory_ids[0]]

    # The same model again: nothing moves, nothing is enqueued.
    revision = (await read_settings(s.db)).config_revision
    await _put_model(s, "embed-v2")
    assert (await read_settings(s.db)).config_revision == revision
    assert len(await jobs_of(s.db, REEMBED, story)) == 1

    # A model of another dimension: the PUT probes it (48), nulls every vector and retypes.
    s.lib.models.embeddings.stub.dimension = 48
    await _put_model(s, "embed-v3")
    pin = await pin_of(s.db)
    assert (pin.version, pin.dimension) == ("stub:embed-v3:sha256:48", 48)
    assert await column_type(s.db) == "vector(48)"
    assert all(r.vector is None for r in (await vectors(s.db, story)).values())
    assert await run_jobs(s.db, REEMBED, story) == [queue.CommitOutcome.COMMITTED]
    assert (await vectors(s.db, story))[memory_ids[0]].dims == 48


@pytest.mark.part("M4", "FR-EMBED-1,2,3,4")
async def test_c_05__retry(s: Scratch) -> None:
    """A re-embed job that fails for good is reported ``failed`` with its exception class, and
    ``POST /admin/reembed/retry`` enqueues a new one, which completes; the retry names the
    stories it retried. Complement: with nothing failed, a retry retries nothing."""
    story, _, _ = await _embedded_story(s)
    await _put_model(s, "embed-v2")
    s.lib.models.embeddings.stub.fault("connection")
    await execute_in_tx(
        s.db,
        ("UPDATE jobs SET failures = 2 WHERE kind = 'reembed' AND story_id = :s", {"s": story}),
    )
    assert await run_jobs(s.db, REEMBED, story) == [queue.FAILED]
    status = await _status(s)
    assert status["stories"] == [
        {"story_id": story, "total": 2, "done": 0, "status": "failed", "error": "ModelUnavailable"}
    ]
    assert status["finished_at"] is not None  # nothing queued or running

    response = await s.client.post("/admin/reembed/retry", headers=OPERATOR_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["retried"] == [story]
    assert (await _status(s))["stories"][0]["status"] == "queued"
    assert await run_jobs(s.db, REEMBED, story) == [queue.CommitOutcome.COMMITTED]
    assert (await _status(s))["stories"][0] == {
        "story_id": story,
        "total": 2,
        "done": 2,
        "status": "done",
        "error": None,
    }
    nothing = await s.client.post("/admin/reembed/retry", headers=OPERATOR_HEADERS)
    assert nothing.json()["retried"] == []


@pytest.mark.part("M4", "FR-EMBED-1,2,3,4")
async def test_c_05__unavailable(s: Scratch) -> None:
    """An endpoint that fails when a query is embedded leaves the search lexical-only with
    ``embedding_unavailable`` (never an error); a query embedding of another dimension than the
    pin's is the same. Complements: with no embedder (the story's ``episodic`` module off) the
    search is lexical-only with no marker, and a Lite story's turn enqueues no embed job."""
    story, _, memory_ids = await _embedded_story(s)
    text = (await vectors(s.db, story))[memory_ids[0]].text
    stub = s.lib.models.embeddings.stub
    stub.fault("connection")
    down = await _search(s, story, text)
    assert down.degradation == (EMBEDDING_UNAVAILABLE,)
    assert down.vector == ()
    assert memory_ids[0] in ids(down.lexical)
    stub.dimension = 16
    wrong = await _search(s, story, text)
    assert wrong.degradation == (EMBEDDING_UNAVAILABLE,)
    stub.dimension = 64

    async with s.db.connect() as conn:
        off = await search(conn, story, text, k=5, lexical=LEXICAL, embed=None)
    assert off.degradation == ()
    assert off.vector == ()
    assert memory_ids[0] in ids(off.lexical)

    lite, cast = await structured_story(s.db)
    await execute_in_tx(
        s.db,
        (
            "UPDATE stories SET engine_profile_id = (SELECT default_engine_profile_id "
            "FROM deployment_state) WHERE id = :s",
            {"s": lite},
        ),
    )
    _, written = await turn(s.db, lite, actor=cast["Cal"], say="Hello.", reply=None)
    assert written == []
    assert await jobs_of(s.db, EMBED, lite) == []


@pytest.mark.part("M4", "FR-EMBED-1,2,3,4")
async def test_c_05__any_endpoint(pg_url: str) -> None:
    """Any OpenAI-compatible ``/embeddings`` endpoint works: the ``openai`` SDK adapter against
    a local mock embeds the story's memories in one ``/embeddings`` request
    (``encoding_format: float``, the texts as ``input``), configured by the ``embedding`` row
    alone, and pins ``<host>/<model>`` at the mock's dimension.

    Complements: without its own row the port is not configured (no fallback to ``default``),
    and an endpoint failure is ``model_unavailable`` naming the ``embedding`` purpose."""
    async with embedding_db(pg_url, embedding_adapter="openai") as s:
        with mock_openai(embedding_dimension=24) as server:
            service = s.lib.models.embeddings
            assert service.adapter.name == "openai"
            with pytest.raises(ModelUnavailable) as unset:
                await service.embed(["anything"])
            assert unset.value.details == {"purpose": "embedding", "reason": "not_configured"}

            response = await s.client.put(
                "/admin/model-config/embedding",
                json={"base_url": f"{server.root}/v1", "model": "mock-embed"},
                headers=OPERATOR_HEADERS,
            )
            # No key yet: the probe cannot reach the endpoint, so nothing is configured.
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "embedding_unreachable"
            await put_key(s.client, "embedding", "k-embed")
            response = await s.client.put(
                "/admin/model-config/embedding",
                json={"base_url": f"{server.root}/v1", "model": "mock-embed"},
                headers=OPERATOR_HEADERS,
            )
            assert response.status_code == 200, response.text

            story, cast = await structured_story(s.db)
            memory_ids = [await say(s.db, story, cast["Cal"], line) for line in LINES]
            assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]

            calls = [r for r in server.requests if r.path.endswith("/embeddings")]
            # The PUT's probe embedded one text; the job one batch of the two memories.
            assert len(calls) == 2
            batch = calls[-1].body
            assert batch["model"] == "mock-embed"
            assert batch["encoding_format"] == "float"
            stored = await vectors(s.db, story)
            assert batch["input"] == [stored[m].text for m in memory_ids]
            assert calls[-1].authorization == "Bearer k-embed"
            host = server.root.removeprefix("http://")
            pin = await pin_of(s.db)
            assert (pin.version, pin.dimension) == (f"{host}/mock-embed", 24)
            assert all(stored[m].version == f"{host}/mock-embed" for m in memory_ids)
            assert _close(
                stored[memory_ids[0]].vector, stub_vector(stored[memory_ids[0]].text, 24, "mock")
            )

            server.fail_with = 500
            with pytest.raises(ModelUnavailable) as failed:
                await service.embed(["anything"])
            assert failed.value.details["purpose"] == "embedding"
            assert failed.value.details["provider_status"] == 500


@pytest.mark.live_model
@pytest.mark.part("M4", "FR-EMBED-1,2,3,4")
async def test_c_05__live() -> None:
    """A live OpenAI-compatible ``/embeddings`` endpoint (``EPISTREL_LIVE_EMBEDDING_*``; run only
    with ``-m live_model``): the probe passes and reports a dimension within the floor, a batch
    answers one vector per text all of that dimension, and the same text embeds to the same
    direction twice. Complement: two different texts are not the same vector."""
    service, config = live_embedding()
    try:
        probe = await service.probe(config)
        assert probe.protocol_ok
        assert PORT.floor.min_dimension <= probe.dimension <= PORT.floor.max_dimension
        texts = [LINES[0], LINES[1], LINES[0]]
        embedded = await service.embed(texts, config=config)
        assert len(embedded.vectors) == 3
        assert {len(v) for v in embedded.vectors} == {probe.dimension}

        def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

        assert cosine(embedded.vectors[0], embedded.vectors[2]) > 0.999
        assert cosine(embedded.vectors[0], embedded.vectors[1]) < 0.999
    finally:
        await service.aclose()
