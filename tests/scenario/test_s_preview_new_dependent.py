"""S-PREVIEW-NEW-DEPENDENT (docs/04 §7; FR-CONC-13; #237): a dependent formed after the preview
makes the plan stale.

The OSU fixture, "Bobby actually went to UCLA" previewed and answered at revision R. Then a turn
has Marcus form a memory derived from Bobby's old school (a ``derives`` edge onto the
enrollment fact), touching no planned record and writing no fact. The apply re-runs the
dependency search under the story lock, finds the memory, and the plan is 409 ``rewrite_stale``
with reason ``new_dependent`` naming it; no amendment is written.

Complement: with the re-search skipped (``faults.rewrite_research_skipped``, the fault point
``rewrite.revalidate.skip_research``) the same apply wrongly succeeds: nothing else sees it.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import edges
from epistrel.core.enums import (
    DependencyEdgeType,
    MemoryKind,
    OperationKind,
    Provenance,
    RecordProvenance,
)
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, insert_derived
from epistrel.core.memory import records
from epistrel.core.tables.memories import memories
from epistrel.testing import faults
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


async def _marcus_remembers(engine: AsyncEngine, ed: Any) -> str:
    """A turn in which Marcus forms a memory derived from Bobby's enrollment at OSU."""
    memory = new_ulid()
    async with Operation(
        engine, story_id=ed.story_id, kind=OperationKind.TURN, principal="p-test"
    ) as op:
        op.append("player_input", {"text": "Marcus?"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "Marcus nods along."})

        async def step(conn: AsyncConnection) -> None:
            await insert_derived(
                conn,
                memories,
                [
                    {
                        "id": memory,
                        "story_id": ed.story_id,
                        "kind": MemoryKind.EPISODIC.value,
                        "text": "Marcus recalls that Bobby went to OSU.",
                        "holder_character_id": ed.marcus,
                        "character_id": ed.marcus,
                        "visibility": {"scope": "public"},
                        "provenance": RecordProvenance.RENDER.value,
                        "trust_tier": records.trust_tier_of(RecordProvenance.RENDER).value,
                        "operation_id": op.operation_id,
                        "source_event_ids": [],
                        "narrative_turn": op.narrative_turn,
                        "turn_variant": op.turn_variant,
                        "beat": 1,
                        "proposition_ids": [],
                        "participants": [ed.marcus, ed.bobby],
                    }
                ],
            )
            specs = await edges.fact_edges(
                conn,
                ed.story_id,
                DependencyEdgeType.DERIVES,
                "memories",
                memory,
                [ed.enrollment.id],
                position=Position(op.narrative_turn or 0, 0, 0, 1, 0),
            )
            await edges.record_edges(
                conn, op, [dataclasses.replace(e, acquisition_kind="testimony") for e in specs]
            )

        op.add_commit_step(step)
        await op.commit()
    return memory


async def _answered(client: Any, story: str) -> dict[str, Any]:
    opened = await rs.open_session(client, story)
    assert opened.status_code == 201, opened.text
    answered = await rs.answer_all(client, story, opened.json())
    assert answered.status_code == 200, answered.text
    session: dict[str, Any] = answered.json()
    assert session["state"] == "awaiting_approval"
    return session


@pytest.mark.part("M7", "FR-CONC-13")
async def test_s_preview_new_dependent(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        stub.when("", rs.UCLA, purpose="rewrite_parse")
        stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")

        # The complement first: without the re-search the apply wrongly succeeds.
        blind = await rs.story(client, engine)
        session = await _answered(client, blind.story_id)
        await _marcus_remembers(engine, blind.ed)
        with faults.rewrite_research_skipped():
            wrong = await rs.apply(client, blind.story_id, session["id"], session["base_revision"])
        assert wrong.status_code == 200, wrong.text

        deps = await rs.story(client, engine)
        story = deps.story_id
        session = await _answered(client, story)
        memory = await _marcus_remembers(engine, deps.ed)
        stale = await rs.apply(client, story, session["id"], session["base_revision"])
        assert stale.status_code == 409, stale.text
        details = stale.json()["error"]["details"]
        kinds = {r["kind"]: r["refs"] for r in details["reasons"]}
        assert list(kinds) == ["new_dependent"]
        assert {"table": "memories", "id": memory} in kinds["new_dependent"]
        assert await rs.count(engine, "amendments", story) == 0
        planned = details["preview"]["blast_radius"]
        assert memory in {entry["record"]["id"] for bucket in planned.values() for entry in bucket}
