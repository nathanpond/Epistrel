"""S-WORKER-AMEND-RACE: a background job versus an amendment (docs/04 §7; FR-CONC-8,
FR-STORE-27; #236).

A ``consolidation`` job over a window of turns declares every input it reads (each turn's
operation and each memory); while it calls the model, an amendment corrects one of those turns
without discarding it: an overlay on its event and an ``in_place`` correction of its memory's
text. The operation's version now carries the amendment's record part (``record_version``,
``core.input_versions``) and the memory's its new text, so ``stale_inputs`` at the job's commit
names them: the output is dropped and the same job put back, due now, with no failure counted.
When it runs again it reads the amended text, and its reflection commits. Scaled down from the
spec's turns 700-800 to 100-140 (the mechanism is the same per input).

The re-embed race (#138's ``memories`` reader): an ``embed`` job that read a memory's text
before an amendment corrected it drops its vector. Complements: a job whose inputs no amendment
touched commits normally, and a dropped output appears in no view.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.amendments.store import RecordPartDraft
from epistrel.core.input_versions import input_key, read_version
from epistrel.core.memory.consolidation import Window
from epistrel.core.tables import operations
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import consolidation
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import amendment_support as am
from tests import consolidation_support as cs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.summary_support import worker

pytestmark = pytest.mark.integration

AMENDED = "Bobby does: line 120, but quietly"


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    return ModelAccessLayer(auth_settings(pg_url, model_adapter="stub"))


@pytest.mark.part("M7", "FR-CONC-8; FR-STORE-27")
async def test_s_worker_amend_race(
    engine: AsyncEngine, models: ModelAccessLayer, drained_embedding_jobs: None
) -> None:
    w = await cs.story(engine, cs.dials(window_memories=10_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    turns = await cs.bulk_turns(engine, story, 140, actor=bobby, name="Bobby", does=True)
    target = next(t for t in turns if t.number == 120)

    async with engine.begin() as conn:
        await consolidation.enqueue_window(conn, story, Window(100, 140))
    job = await cs.claim(engine, story)
    assert job is not None
    key = input_key(operations.name, target.operation_id)
    consolidation.bind(models)
    latch = models.stub.hold("consolidation")
    run = asyncio.create_task(worker(engine).execute(job))
    await asyncio.wait_for(latch.entered.wait(), 10)
    await am.amend(
        engine,
        story,
        [
            RecordPartDraft("events", target.event_id, "replace", {"text": "quietly"}),
            RecordPartDraft("memories", target.memory_id, "replace", {"text": AMENDED}),
        ],
    )
    latch.release()
    models.stub.reset()
    assert await run is queue.CommitOutcome.DROPPED

    (row,) = await rows(engine, "SELECT * FROM jobs WHERE id = :j", j=job.id)
    assert (row.status, row.failures) == ("queued", 0)
    read = row.input_versions[key]
    assert read[2] is None  # the job read it before the amendment
    async with engine.connect() as conn:
        now = await read_version(conn, key)
    assert now is not None
    assert now[:2] == read[:2]
    assert now[2] is not None  # the record part's tx_from
    # The dropped output appears in no view.
    assert await cs.outputs(engine, story) == []
    assert await cs.windows(engine, story) == []

    models.stub.script(
        "consolidation", [cs.answer(facts=[("Bobby keeps quiet.", "observation", [0, 20])])]
    )
    assert await cs.run_jobs(engine, models, story) == ["committed"]
    prompts = [
        "\n".join(m.content for m in call.messages)
        for call in models.stub.calls
        if call.purpose == "consolidation"
    ]
    assert prompts
    assert AMENDED in prompts[-1]
    (made,) = await cs.outputs(engine, story)
    assert made.text == "Bobby keeps quiet."

    # Complement: a window no amendment touched commits at once.
    other = await cs.story(engine, cs.dials(window_memories=10_000, cadence_turns=1_000))
    await cs.bulk_turns(
        engine, other.story_id, 20, actor=other.characters["Bobby"], name="Bobby", does=True
    )
    models.stub.script(
        "consolidation", [cs.answer(facts=[("Bobby keeps busy.", "observation", [0])])]
    )
    done = await consolidation.consolidate_window(
        engine, other.story_id, Window(1, 20), models=models
    )
    assert str(done.outcome) == "committed"


@pytest.mark.part("M7", "FR-CONC-8; FR-STORE-27")
async def test_s_worker_amend_race__reembed(
    engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The re-embed race: the ``memories`` reader (#138) versions a memory by its text, so an
    in-place correction (#221) of a memory an embed job read makes that input stale, and the
    job's vector is dropped; an untouched memory's input is not."""
    from epistrel.core.input_versions import stale_inputs
    from epistrel.core.tables import memories

    w = await cs.story(engine)
    story, bobby = w.story_id, w.characters["Bobby"]
    first, second = await cs.bulk_turns(engine, story, 2, actor=bobby, name="Bobby")
    keys = [input_key(memories.name, t.memory_id) for t in (first, second)]
    async with engine.connect() as conn:
        declared = {k: await read_version(conn, k) for k in keys}
    await am.amend(
        engine, story, [RecordPartDraft("memories", first.memory_id, "replace", {"text": "x"})]
    )
    async with engine.connect() as conn:
        assert await stale_inputs(conn, declared) == (keys[0],)
