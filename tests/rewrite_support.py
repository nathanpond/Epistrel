"""Shared setup for the rewrite side-channel's tests (#223, #224): the Bobby education fixture
with a turn whose records depend on Bobby's enrollment at OSU through #220's typed edges.

- ``names``: a reply that names OSU (presupposes it: a cascaded ``fix`` by substitution);
- ``assumes``: a reply that assumes it without naming OSU (an irreducible conflict, ``high``: a
  delivered message);
- ``player``: the player's own line (an irreducible conflict, ``player_authored``, ``high``);
- ``reflection``: a memory no player holds that assumes it without naming OSU (an irreducible
  conflict, ``low``: confined to a reflection);
- ``memory``: a memory id that mentions OSU (incidental);
- ``utterance``: a speech act that asserted it (relabelled).

``UCLA`` is the scripted ``rewrite_parse`` answer to "Bobby actually went to UCLA".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import edges
from epistrel.core.enums import (
    DependencyEdgeType,
    DependencyTarget,
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
from tests.event_support import rows
from tests.fixtures.bobby_education import Education, bobby_education
from tests.story_support import WRITE

OMNISCIENT = {"viewpoint": "omniscient"}
UCLA = json.dumps(
    {
        "changes": [
            {
                "kind": "proposition",
                "subject": "Bobby",
                "predicate": "enrolled_at",
                "object": "UCLA",
            }
        ]
    }
)
INSTRUCTION = "Bobby actually went to UCLA."
LINES = {
    "player": "Tell me about OSU.",
    "names": "Bobby says OSU was a fine school.",
    "assumes": "Bobby misses the cold walks across his old campus.",
    "reflection": "Bobby still dreams of the frozen quad.",
}
REWRITTEN = "Bobby misses the warm walks across his old campus."


@dataclass(frozen=True)
class Dependents:
    ed: Education
    records: dict[str, tuple[str, str]]

    @property
    def story_id(self) -> str:
        return self.ed.story_id


async def story(client: httpx.AsyncClient, engine: AsyncEngine) -> Dependents:
    ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
    return Dependents(ed, await dependents(engine, ed))


async def dependents(engine: AsyncEngine, ed: Education) -> dict[str, tuple[str, str]]:
    """A turn whose records depend on the enrollment (see the module docstring)."""
    story_id = ed.story_id
    made: dict[str, tuple[str, str]] = {}
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal="p-test"
    ) as op:
        op.append("player_input", {"text": LINES["player"]}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": LINES["names"]})
        op.append("message_rendered", {"text": LINES["assumes"]})

        async def step(conn: AsyncConnection) -> None:
            found = [
                (str(r.id), str(r.type), dict(r.payload))
                for r in await conn.execute(
                    text(
                        "SELECT id, type, payload FROM events WHERE operation_id = :o ORDER BY seq"
                    ),
                    {"o": op.operation_id},
                )
            ]
            player = next(i for i, t, _ in found if t == "player_input")
            names = next(i for i, _, p in found if p.get("text") == LINES["names"])
            assumes = next(i for i, _, p in found if p.get("text") == LINES["assumes"])
            reflection = new_ulid()
            await insert_derived(
                conn,
                memories,
                [
                    {
                        "id": reflection,
                        "story_id": story_id,
                        "kind": MemoryKind.EPISODIC.value,
                        "text": LINES["reflection"],
                        "holder_character_id": None,
                        "character_id": ed.bobby,
                        "visibility": {"scope": "public"},
                        "provenance": RecordProvenance.RENDER.value,
                        "trust_tier": records.trust_tier_of(RecordProvenance.RENDER).value,
                        "operation_id": op.operation_id,
                        "source_event_ids": [assumes],
                        "narrative_turn": op.narrative_turn,
                        "turn_variant": op.turn_variant,
                        "beat": 1,
                        "proposition_ids": [],
                        "participants": [ed.bobby],
                    }
                ],
            )
            memory = new_ulid()
            utterance = new_ulid()
            at = Position(op.narrative_turn or 0, 0, 0, 1, 0)
            specs = []
            for table, dependent in (
                ("events", player),
                ("events", names),
                ("events", assumes),
                ("memories", reflection),
            ):
                specs += await edges.fact_edges(
                    conn,
                    story_id,
                    DependencyEdgeType.PRESUPPOSES,
                    table,
                    dependent,
                    [ed.enrollment.id],
                    position=at,
                )
            specs += await edges.fact_edges(
                conn,
                story_id,
                DependencyEdgeType.ASSERTS,
                "utterances",
                utterance,
                [ed.enrollment.id],
                position=at,
            )
            specs.append(
                edges.EdgeSpec(
                    DependencyEdgeType.MENTIONS,
                    "memories",
                    memory,
                    DependencyTarget.ENTITY,
                    ed.osu,
                    position=at,
                )
            )
            await edges.record_edges(conn, op, specs)
            made.update(
                {
                    "player": ("events", player),
                    "names": ("events", names),
                    "assumes": ("events", assumes),
                    "reflection": ("memories", reflection),
                    "memory": ("memories", memory),
                    "utterance": ("utterances", utterance),
                }
            )

        op.add_commit_step(step)
        await op.commit()
    return made


async def open_session(client: httpx.AsyncClient, story_id: str, **body: Any) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/rewrites",
        params=OMNISCIENT,
        json={"instruction": INSTRUCTION, **body},
        headers=WRITE,
    )


async def apply(
    client: httpx.AsyncClient, story_id: str, session_id: str, base_revision: int | None
) -> httpx.Response:
    body = {} if base_revision is None else {"base_revision": base_revision}
    return await client.post(
        f"/stories/{story_id}/rewrites/{session_id}/apply",
        params=OMNISCIENT,
        json=body,
        headers=WRITE,
    )


async def answer_all(
    client: httpx.AsyncClient, story_id: str, session: dict[str, Any]
) -> httpx.Response:
    """Accept the recommendation of every unanswered required question."""
    pending = [
        {"question_id": q["id"], "accept_recommended": True}
        for q in session["questions"]
        if q["required"] and q["answered_at"] is None
    ]
    return await client.post(
        f"/stories/{story_id}/rewrites/{session['id']}/answers",
        params=OMNISCIENT,
        json={"answers": pending},
        headers=WRITE,
    )


async def revision(engine: AsyncEngine, story_id: str) -> int:
    found = await rows(
        engine, "SELECT timeline_revision AS r FROM stories WHERE id = :s", s=story_id
    )
    return int(found[0].r)


async def count(engine: AsyncEngine, table: str, story_id: str) -> int:
    found = await rows(
        engine,
        f"SELECT count(*) AS n FROM {table} WHERE story_id = :s",  # noqa: S608
        s=story_id,
    )
    return int(found[0].n)
