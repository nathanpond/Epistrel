"""The tavern fixture of the claim-detection tests (#148): a structured story with Cal (the
player), Linda and Marcus, a key canon says Linda does not have, a ring and the tavern; and the
steps a structured turn takes with detection (#150 makes them in its pipeline): build the
environment, detect a render and the composed message, verify every detected claim, then store
the turn's report (``turn_report.detection``) with its beats and enqueue the shadow sample it
owes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import StructuredReadSet
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.core.tables import operations
from epistrel.core.verify.detect import DetectBeat
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs.types import ClaimedJob
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.verify import extraction
from epistrel.orchestrator.verify.extraction import (
    CheckedClaim,
    ClaimDetector,
    DetectionConfig,
    DetectionOutcome,
)
from epistrel.orchestrator.verify.verifier import story_verifier
from tests.canon_support import PRINCIPAL, commit
from tests.memory_support import use_structured
from tests.profile_support import store_overrides
from tests.summary_support import LEASE_S, worker

HOLDS = CORE_BY_NAME["holds"].id


@dataclass(frozen=True)
class Tavern:
    story_id: str
    cal: str
    linda: str
    marcus: str
    key: str
    ring: str
    tavern: str


async def tavern_story(engine: AsyncEngine, *, mode: str = "always") -> Tavern:
    """The fixture story on ``Structured (M4)`` (``extraction_mode`` overridden to ``mode``
    when it is not the profile's ``always``), where Linda has not had the key since minute 0."""
    spec = StorySpec(
        name="tavern",
        characters=[
            CharacterSpec(name="Cal", definition="The player.", true_age=30, is_player=True),
            CharacterSpec(name="Linda", definition="A thief.", true_age=30),
            CharacterSpec(name="Marcus", definition="The innkeeper.", true_age=50),
        ],
    )
    created = await create_story_from(engine, spec, principal=PRINCIPAL)
    story = created.story_id
    ids = {c.name: c.id for c in created.characters}
    async with engine.begin() as conn:
        for name, character in ids.items():
            gender = {"Linda": "feminine", "Marcus": "masculine"}.get(name)
            await store.add_entity(
                conn, story, "person", name, character_id=character, gender=gender
            )
        key = await store.add_entity(conn, story, "item", "key")
        ring = await store.add_entity(conn, story, "item", "ring")
        tavern = await store.add_entity(conn, story, "place", "tavern")
    await use_structured(engine, story)
    if mode != "always":
        await store_overrides(engine, story, {"verification": {"extraction_mode": mode}})
    # Marcus keeps the key from Linda: canon says she does not have it (#439: "Marcus holds
    # it" alone would not contradict her holding it, the inverse's cardinality is unchecked).
    await commit(
        engine,
        story,
        Proposition(
            subject_id=ids["Linda"],
            predicate_id=HOLDS,
            object_id=key,
            polarity=Polarity.NEGATED,
            valid_time=Interval.of(0, None),
        ),
    )
    return Tavern(story, ids["Cal"], ids["Linda"], ids["Marcus"], key, ring, tavern)


def claims_answer(*claims: dict[str, Any]) -> str:
    """A scripted ``extraction_backstop`` answer."""
    return json.dumps({"claims": list(claims)})


@dataclass
class DetectedTurn:
    """What one turn's detection found and verified, before its commit."""

    outcomes: list[DetectionOutcome]
    checked: list[CheckedClaim]
    report: dict[str, Any]
    verifier: Any


async def detect_turn(
    engine: AsyncEngine,
    story_id: str,
    models: ModelAccessLayer,
    beats: Sequence[DetectBeat],
    *,
    message: str | None = None,
    turn: int = 2,
    shadow_rate: float | None = None,
    scene_entity: str | None = None,
) -> DetectedTurn:
    """The read phase of a structured turn at ``turn``: the environment, the render's
    detection, the composed message's, and the verification of every detected claim."""
    read_set = StructuredReadSet()
    speakers = [b.speaker for b in beats]
    async with engine.connect() as conn:
        config = await DetectionConfig.load(conn, story_id)
        if shadow_rate is not None:
            config = replace(config, shadow_rate=shadow_rate)
        env = await extraction.load_env(
            conn, story_id, read_set, position=(turn, 0), speakers=speakers, config=config
        )
        if scene_entity is not None:
            env = replace(env, scene_entity=scene_entity)
        budget = TurnBudget()
        detector = ClaimDetector(config, env, models=models, budget=budget)
        outcomes = [await detector.render(beats, (turn, 0), operation_id=f"turn-{turn}")]
        if message is not None:
            outcomes.append(await detector.message(message, (turn, 0), operation_id=f"turn-{turn}"))
        verifier = await story_verifier(conn, read_set, story_id, models=models, budget=budget)
        checked: list[CheckedClaim] = []
        for outcome in outcomes:
            checked += await extraction.verify_detected(
                verifier, outcome, (turn, 0), operation_id=f"turn-{turn}", start=len(checked)
            )
    return DetectedTurn(outcomes, checked, extraction.detection_report(outcomes, checked), verifier)


async def commit_turn(
    engine: AsyncEngine,
    story_id: str,
    found: DetectedTurn,
    beats: Sequence[DetectBeat],
    *,
    message: str | None = None,
) -> Operation:
    """The turn's commit: its beats as events, its ``turn_report.detection``, its metrics and
    the shadow sample it owes, enqueued in the same transaction."""
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER)
        for beat in beats:
            if beat.channel == "narration":
                op.append("message_rendered", {"text": beat.text}, beat=beat.index)
        if message is not None:
            op.append("message_rendered", {"text": message}, beat=99)

        async def step(conn: AsyncConnection) -> None:
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id == op.operation_id)
                .values(turn_report={"detection": found.report})
            )
            extraction.record_metrics(found.outcomes, found.checked)
            if extraction.shadow_owed(found.outcomes):
                await extraction.enqueue_shadow(conn, story_id, op.operation_id)

        op.add_commit_step(step)
        await op.commit()
    return op


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:test:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = 'extraction_shadow' AND story_id = :s "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def run_shadow_jobs(
    engine: AsyncEngine, models: ModelAccessLayer, story_id: str, *, limit: int = 5
) -> list[Any]:
    """Run the story's due ``extraction_shadow`` jobs one at a time through the worker's own
    ``execute`` with ``models`` bound; the outcomes in order."""
    extraction.bind(models)
    outcomes: list[Any] = []
    try:
        for _ in range(limit):
            now = clock.now()
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        _CLAIM,
                        {"s": story_id, "now": now, "lease": now + timedelta(seconds=LEASE_S)},
                    )
                ).one_or_none()
            if row is None:
                break
            job = ClaimedJob(
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
            outcomes.append(await worker(engine).execute(job))
    finally:
        extraction.bind(None)
    return outcomes


async def shadow_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT * FROM extraction_shadow WHERE story_id = :s ORDER BY id"),
            {"s": story_id},
        )
        return list(found)


__all__ = [
    "HOLDS",
    "DetectedTurn",
    "Tavern",
    "claims_answer",
    "commit_turn",
    "detect_turn",
    "run_shadow_jobs",
    "shadow_rows",
    "tavern_story",
]
