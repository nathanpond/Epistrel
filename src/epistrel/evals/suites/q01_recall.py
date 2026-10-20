"""Q-01: recall@10 and MRR over the S-THOSEGUYS-style fixture (docs/04 §9; NFR-QUAL-6; #157).

``setup`` builds the fixture once per run (``build_fixture``; content and labels:
``authoring.retrieval``, version ``FIXTURE_VERSION``): a scratch story on ``Structured (M4)``
with its cast and people, one real turn, then 5,000 filler episodes and the 100 planted
occasions' beats written in bulk through #153's memory row builder
(``testing.fixtures.scale.memory_row``/``insert_memories``: building 5,000 turns through the
pipeline would take far longer), and the head moved to turn 5,000, as #141's S-THOSEGUYS fixture
does. When the deployment names an embedding model (``--embedding-model``), the story's
vectors are written by #138's ``embed`` job, run in process before the head moves
(``scenes.drain_embeddings``);
without one (CI's stub run), retrieval runs its lexical and alias arms only.

Each case asks one query through ``Engine.retrieve`` (the omniscient debugging read of what a
render's context would retrieve, as the player). The **ranking** is what the context receives
in order: the located occasion's episodes, the assessments, then the ranked ``memories`` by
score (an occasion the occasion arm found is not repeated among the ranked memories).

- ``nfr_qual_6.recall_at_10`` (rate, ≥ 0.9): per seed, the share of every query's relevant beats
  found in the first ten (micro-averaged: ``Suite.aggregate``).
- ``q01.mrr`` (report-only): the mean reciprocal rank of the first relevant beat.
- ``q01.all_found`` (report-only): every relevant beat of the query in the first ten.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

from epistrel.core.enums import MemoryKind, OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.tables import stories
from epistrel.evals import scenes
from epistrel.evals.authoring import retrieval as content
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.metrics import reciprocal_rank
from epistrel.evals.suites import Aggregate, CaseModel, Counts, Outcome, Suite, Value, register
from epistrel.testing.fixtures import scale

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.core.retrieval.pipeline import RetrievalResult
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext, Scratch

RECALL = "nfr_qual_6.recall_at_10"
MRR = "q01.mrr"
ALL_FOUND = "q01.all_found"
K = 10

CAST = tuple(
    CharacterSpec(name=n, definition=d, true_age=a, is_player=n == "Cal")
    for n, d, a in (("Cal", "The player.", 30), ("Bobby", "A boy.", 12), ("Ann", "His aunt.", 40))
)


class Fixture:
    """A built fixture: its story, people by name and the builder key of each planted beat."""

    def __init__(self, story_id: str, people: dict[str, str], player: str) -> None:
        self.story_id = story_id
        self.people = people
        self.player = player
        self.keys: dict[str, str] = {}
        """Memory id → builder key (``occasion:<k>:<beat>``)."""


async def _first_turn(engine: Engine, story_id: str, actor: str) -> str:
    async with Operation(
        engine.db, story_id=story_id, kind=OperationKind.TURN, principal=scenes.PRINCIPAL
    ) as op:
        op.append(
            "player_input",
            {"text": "Hello.", "say": "Hello.", "do": None},
            provenance=Provenance.PLAYER,
            character_id=actor,
            beat=1,
        )
        await op.commit()
    return op.operation_id


async def build_fixture(engine: Engine, ctx: RequestContext, scratch: Scratch) -> Fixture:
    """The fixture (module docstring) in a new scratch story of ``scratch``."""
    import random

    created = await scratch.create(StorySpec(characters=list(CAST)))
    story = created.story_id
    await scenes.structured(engine, ctx, story)
    people = await scenes.add_entities(
        engine, created, [scenes.EntitySpec(n) for n in content.PEOPLE]
    )
    player = next(c.id for c in created.characters if c.name == "Cal")
    operation = await _first_turn(engine, story, player)
    fixture = Fixture(story, people, player)
    rnd = random.Random(f"{content.FIXTURE_VERSION}:fillers")  # noqa: S311 - a fixture
    rows: list[dict[str, Any]] = []
    for n in range(content.FILLERS):
        actor, other = rnd.sample(content.EVERYONE, 2)
        rows.append(
            scale.memory_row(
                story_id=story,
                operation_id=operation,
                line=rnd.choice(content.FILLER_LINES).format(a=actor),
                turn=1 + (n * content.TURNS) // content.FILLERS,
                event_type=rnd.choice(content.FILLER_TYPES),
                participants=(people[actor], people[other]),
                importance=round(rnd.random(), 3),
            )
        )
    for occasion in content.occasions():
        for b, beat in enumerate(occasion["beats"], start=1):
            row = scale.memory_row(
                story_id=story,
                operation_id=operation,
                line=beat["text"],
                turn=occasion["turn"],
                beat=b,
                kind=MemoryKind.EPISODIC,
                event_type=occasion["event"],
                participants=[people[p] for p in occasion["participants"]],
                importance=0.7,
            )
            rows.append(row)
            fixture.keys[str(row["id"])] = beat["key"]
    await scale.insert_memories(engine.db, rows)
    if await scenes.embedding_configured(engine):
        # Before the head moves: the embed job commits an operation, and none can commit once
        # the head is past the story's last real turn (the chain would not hold).
        await scenes.drain_embeddings(engine, story)
    async with engine.db.begin() as conn:
        await conn.execute(
            update(stories).where(stories.c.id == story).values(next_turn_index=content.TURNS + 1)
        )
    return fixture


def ranking(result: RetrievalResult) -> list[str]:
    """What the context receives, in order: episodes, assessments, then ranked memories."""
    seen: dict[str, None] = {}
    for memory in (*result.episodes, *result.assessments, *result.memories):
        seen.setdefault(memory.id, None)
    return list(seen)


class RecallInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture: Literal["thoseguys-v1"]
    query: str = Field(min_length=1, max_length=500)
    asker: Literal["Cal"]


class RecallLabels(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relevant: list[str] = Field(min_length=1)


class RecallCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: RecallInputs
    expected: RecallLabels


@register
class Q01Suite(Suite):
    name = "q01"
    title = "Q-01: recall@10 and MRR on the S-THOSEGUYS-style fixture"
    set_path = "retrieval/recall.jsonl"
    min_cases = 100
    guarantees = ("embedding",)
    ci_gate = True
    case_model = RecallCase
    metrics = (
        Metric(
            RECALL,
            Rule.rate,
            0.9,
            Direction.at_least,
            "a relevant beat is among the first ten the context receives",
        ),
        Metric(MRR, Rule.informational, description="mean reciprocal rank of the first hit"),
        Metric(ALL_FOUND, Rule.informational, description="every relevant beat in the top ten"),
    )
    reading_notes = (
        "Q-01's ranking is what a render's context receives in order: the located occasion's "
        "episodes, the assessments, then the ranked memories. Recall@10 is micro-averaged over "
        "every query's relevant beats. Without an embedding model (`--embedding-model`) the "
        "vector arm does not run and recall rests on the lexical and alias arms.",
    )

    def __init__(self) -> None:
        self.fixture: Fixture | None = None
        self.scratch: Scratch | None = None

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        from epistrel.evals.runner import Scratch

        self.scratch = Scratch(engine, "q01-fixture")
        self.fixture = await build_fixture(engine, ctx, self.scratch)

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        if self.scratch is not None:
            await self.scratch.delete_all()
        self.fixture, self.scratch = None, None

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, RecallCase)  # noqa: S101 - load() validated it
        fixture = self.fixture
        assert fixture is not None  # noqa: S101 - setup built it
        result = await ctx.engine.retrieve(
            ctx.principal,
            fixture.story_id,
            q=case.inputs.query,
            viewpoint="omniscient",
            actor_character_id=fixture.player,
        )
        ranked = [fixture.keys.get(i, i) for i in ranking(result)]
        relevant = set(case.expected.relevant)
        top = set(ranked[:K])
        found = len(relevant & top)
        return Outcome(
            {ALL_FOUND: found == len(relevant)},
            output=json.dumps(ranked[:K]),
            detail={
                "found": found,
                "missed": len(relevant) - found,
                "rr": reciprocal_rank(ranked, relevant),
                "degradation": list(result.degradation),
                "skipped": list(result.skipped),
            },
        )

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        found = sum(int(o.detail["found"]) for o in outcomes)
        missed = sum(int(o.detail["missed"]) for o in outcomes)
        mrr = sum(float(o.detail["rr"]) for o in outcomes) / len(outcomes) if outcomes else None
        return {RECALL: Counts(found, missed), MRR: Value(mrr, len(outcomes))}


__all__ = ["ALL_FOUND", "MRR", "RECALL", "Q01Suite", "build_fixture", "ranking"]
