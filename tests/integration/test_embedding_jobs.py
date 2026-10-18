"""The embedding jobs' edges beyond C-05 (#138): a vector is derived data, so a job never writes
the vector of a text an erasure redacted or a truncation discarded after the job read it; a
fork keeps its source's vector space; and a configuration write that changes the embedding
model outside the REST route (the library's ``write_config``) still moves the pin and enqueues
the re-embed jobs.

These run on a database of their own (``embedding_support.embedding_db``): a pin retypes
``memories.embedding``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import text

from epistrel.core import clock
from epistrel.core.context import RequestContext
from epistrel.orchestrator.jobs import handlers, queue
from epistrel.orchestrator.jobs.handlers.reembed import EMBED, REEMBED
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.access import get_delivered
from epistrel.orchestrator.models.adapter import UnknownPurpose
from tests.embedding_support import (
    Scratch,
    embedding_db,
    jobs_of,
    pin_of,
    reset,
    run_jobs,
    say,
    story_version,
    structured_story,
    vectors,
)
from tests.event_support import execute_in_tx
from tests.fork_support import fork
from tests.model_support import OPERATOR_HEADERS
from tests.summary_support import LEASE_S

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def scratch(pg_url: str) -> AsyncIterator[Scratch]:
    async with embedding_db(pg_url) as s:
        yield s


@pytest.fixture
async def s(scratch: Scratch) -> Scratch:
    await reset(scratch.db)
    scratch.lib.models.embeddings.stub.reset()
    return scratch


async def _claimed(s: Scratch, kind: str, story: str) -> ClaimedJob:
    now = clock.now()
    async with s.db.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "UPDATE jobs SET status = 'running', lease_until = :lease, "
                    "attempt = attempt + 1, worker_id = 'w:test:0' "
                    "WHERE kind = :k AND story_id = :s AND status = 'queued' RETURNING *"
                ),
                {"k": kind, "s": story, "lease": now + timedelta(seconds=LEASE_S)},
            )
        ).one()
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        story_id=row.story_id,
        payload=dict(row.payload or {}),
        attempt=int(row.attempt),
        failures=int(row.failures),
        due_at=row.due_at,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        coalesce_key=row.coalesce_key,
    )


async def test_an_erasure_between_the_read_and_the_commit_drops_the_vectors(s: Scratch) -> None:
    """The job embedded the memory's text; an erasure then redacted it before the job
    committed. The commit's input recheck sees the text changed and writes nothing (the job is
    queued again), so no vector of the erased text exists; the next run embeds the redacted
    text. Complement: a memory the erasure did not touch is not written either (the output is
    dropped whole), and is embedded by that next run."""
    story, cast = await structured_story(s.db)
    named = await say(s.db, story, cast["Cal"], "Linda May hid the key.")
    other = await say(s.db, story, cast["Cal"], "The rain stopped.")
    job = await _claimed(s, EMBED, story)
    handler = handlers.lookup(EMBED)
    assert handler is not None
    ctx = JobContext(db=s.db, job=job, payload=handler.payload())
    output = await handler.run(ctx)  # the model call happened: vectors of the old text
    await execute_in_tx(
        s.db,
        ("SET LOCAL epistrel.erasure = 'on'", {}),
        ("UPDATE memories SET text = '[erased] hid the key.' WHERE id = :m", {"m": named}),
    )

    outcome = await queue.commit_job(s.db, job, handler, output, ctx.input_versions)

    assert outcome == queue.CommitOutcome.DROPPED
    stored = await vectors(s.db, story)
    assert stored[named].vector is None
    assert stored[other].vector is None
    assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]
    stored = await vectors(s.db, story)
    assert stored[named].text == "[erased] hid the key."
    assert stored[named].vector is not None
    assert stored[other].vector is not None


async def test_a_fork_keeps_its_source_vector_space(s: Scratch) -> None:
    """A fork copies the memories with their vectors and the story's version, so it is not
    pending and needs no re-embed."""
    story, cast = await structured_story(s.db)
    await say(s.db, story, cast["Cal"], "One.")
    assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]
    forked = await fork(s.db, story, at_turn=1)
    version = await story_version(s.db, story)
    assert version is not None
    assert await story_version(s.db, forked.story_id) == version
    (copied,) = (await vectors(s.db, forked.story_id)).values()
    assert copied.version == version


async def test_a_library_write_of_the_model_moves_the_pin(s: Scratch) -> None:
    """``model_config.write_config`` (no REST route, no probe) changing the embedding model
    moves the pin to the new version at the pinned dimension and enqueues the re-embed job;
    removing the override (the model unset) moves nothing. The port is not a chat purpose:
    a chat call naming it is refused (``reason: port``)."""
    story, cast = await structured_story(s.db)
    await say(s.db, story, cast["Cal"], "One.")
    assert await run_jobs(s.db, EMBED, story) == [queue.CommitOutcome.COMMITTED]
    keybox = s.lib.models.keybox
    await model_config.write_config(
        s.db, "embedding", keybox=keybox, principal="p-operator", model="embed-lib"
    )
    pin = await pin_of(s.db)
    assert (pin.version, pin.dimension) == ("stub:embed-lib:sha256:64", 64)
    assert [j.status for j in await jobs_of(s.db, REEMBED, story)] == ["queued"]
    await model_config.remove_override(s.db, "embedding", keybox=keybox, principal="p-operator")
    assert (await pin_of(s.db)).version == "stub:embed-lib:sha256:64"
    with pytest.raises(UnknownPurpose) as refused:
        get_delivered("embedding")
    assert refused.value.details["reason"] == "port"


async def test_the_chat_parameters_are_refused_for_the_port(s: Scratch) -> None:
    """The ``embedding`` row takes ``timeout`` only: a chat parameter is 422 and nothing is
    written; a timeout is stored."""
    refused = await s.client.put(
        "/admin/model-config/embedding",
        json={"params": {"temperature": 0.2}},
        headers=OPERATOR_HEADERS,
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["reason"] == "not_a_port_param"
    _, entry = await s.lib.model_config_entry(RequestContext(principal="p-operator"), "embedding")
    assert entry.params == {}
    assert entry.model is None
    assert entry.sources["model"] == "own"  # never the default row's
