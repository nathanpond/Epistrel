"""S-REBASE-VS-REGEN, M6 part: commit-time revalidation and pinned external reads (docs/04 §7;
FR-CONC-1,2,3,4,9; #205).

Cal's gold is owned by a provider that answers 300 first and 350 on any later read. While turn
A is held inside its first render (its gold already pinned at its input), a different operation
commits:

- **Variant 1** — an author fact about an unrelated lamp, outside A's read set: A rebases (its
  deterministic verify re-runs, its revision moves on, its read set is untouched) and commits
  with its recorded read: 300, and no second adapter call.
- **Variant 2** — the author edits the story's custom instructions, a member of A's read set
  (the prompt input it read): A regenerates against current state, reads the provider live
  again (one more call) and commits the new value, 350. With the retry budget spent
  (``regenerate_retry_budget`` 0) the same change is 409 ``revision_conflict`` and nothing is
  committed: no turn, no ``external_read``.
- **Variant 3** — a background consolidation commits during A: derived work bumps no timeline
  revision, so A commits as it was (no rebase, no regeneration, one read).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.events.operation import Operation
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.core.propositions.model import Interval, Proposition
from tests import canon_support as canon
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as perception
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.concurrency_support import set_instructions
from tests.event_support import execute_in_tx, rows, scalar
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import LOCATED_AT, beat, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _attempts(engine: AsyncEngine, story: str) -> list[str]:
    (row,) = await rows(
        engine,
        "SELECT attempts FROM operations WHERE story_id = :s AND kind = 'turn' "
        "ORDER BY operation_id DESC LIMIT 1",
        s=story,
    )
    return [a["outcome"] for a in row.attempts]


async def _revision(engine: AsyncEngine, story: str) -> int:
    return int(await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story))


async def _consolidation(engine: AsyncEngine, story: str) -> None:
    """A derived operation commits (it bumps no revision; FR-CONC-1)."""
    async with Operation(
        engine, story_id=story, kind=OperationKind.CONSOLIDATION, principal=canon.PRINCIPAL
    ) as op:
        await op.commit()


@pytest.mark.part("M6", "FR-CONC-1,2,3,4,9")
async def test_s_rebase_vs_regen__m6(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = ps.ScriptedProvider(name).attach(epistrel)

        async def story_with_gold() -> tuple[str, str]:
            story, ids = await structured_story(client, "Cal", "Mira")
            await ss.set_level(engine, story, "on")
            await ps.external_gold(engine, story, name)
            fake.script("gold", 300, 350, story_id=story)
            # Room in the model budget for one regeneration.
            await set_overrides(
                engine,
                story,
                ProfileOverridesPatch(
                    settings={"budgets": {"model_calls": 40}}, ignore_warnings=True
                ),
                principal="p-author",
                facts=perception.NO_FACTS,
            )
            return story, ids["Cal"]

        async def held(story: str, cal: str, during: Callable[[], Awaitable[Any]]) -> Any:
            stub.reset()
            es.narrator(stub, es.render(beat("narration", "Rain taps on the shutters.")))
            ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
            latch = stub.hold("generation")
            task = asyncio.ensure_future(respond(client, story, body(cal, None, do="sits")))
            entered = asyncio.ensure_future(latch.entered.wait())
            await asyncio.wait({entered, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            assert entered.done(), (await task).text
            await during()
            latch.release()
            return await task

        def reads_of(story: str) -> int:
            return len([r for r in fake.reads if r[1] == story])

        # Variant 1: a change outside the read set rebases with the recorded read.
        story, cal = await story_with_gold()
        lamp = await perception.place_entity(engine, story, "lamp")
        cellar = await perception.place_entity(engine, story, "cellar")
        before = await _revision(engine, story)

        async def unrelated() -> None:
            await canon.commit(
                engine,
                story,
                Proposition(
                    subject_id=lamp,
                    predicate_id=LOCATED_AT,
                    object_id=cellar,
                    valid_time=Interval.of(None, None),
                ),
                kind=OperationKind.CONTROL,
                provenance="author",
            )

        answer = await held(story, cal, unrelated)
        assert answer.status_code == 200, answer.text
        assert await _attempts(engine, story) == ["rebase"]
        assert await _revision(engine, story) == before + 2  # the fact's, then the turn's
        assert reads_of(story) == 1
        assert [r.payload["value"] for r in await ps.external_reads(engine, story)] == [300]

        # Variant 2: a change in the read set regenerates, reading live again.
        story, cal = await story_with_gold()
        answer = await held(story, cal, lambda: set_instructions(engine, story, "Keep it short."))
        assert answer.status_code == 200, answer.text
        assert await _attempts(engine, story) == ["regenerate", "commit"]
        assert reads_of(story) == 2
        assert [r.payload["value"] for r in await ps.external_reads(engine, story)] == [350]

        # ... and with the retry budget spent, a conflict with nothing committed.
        story, cal = await story_with_gold()
        await execute_in_tx(
            engine,
            (
                "UPDATE stories SET settings = settings || CAST(:p AS jsonb) WHERE id = :s",
                {"s": story, "p": json.dumps({"regenerate_retry_budget": 0})},
            ),
        )
        turns = await scalar(
            engine, "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'turn'", s=story
        )
        refused = await held(story, cal, lambda: set_instructions(engine, story, "Keep it short."))
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "revision_conflict"
        assert await ps.external_reads(engine, story) == []
        assert await ps.external_value_rows(engine, story) == []
        committed = await scalar(
            engine,
            "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND status = 'committed'",
            s=story,
        )
        assert committed == turns  # the gold's declaration only
        assert reads_of(story) == 1

        # Variant 3: a consolidation during the turn is neither a rebase nor a regeneration.
        story, cal = await story_with_gold()
        before = await _revision(engine, story)
        answer = await held(story, cal, lambda: _consolidation(engine, story))
        assert answer.status_code == 200, answer.text
        assert await _attempts(engine, story) == ["commit"]
        assert await _revision(engine, story) == before + 1
        assert reads_of(story) == 1
        assert [r.payload["value"] for r in await ps.external_reads(engine, story)] == [300]
