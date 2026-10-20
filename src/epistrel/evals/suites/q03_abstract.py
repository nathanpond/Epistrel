"""Q-03: abstract-query recall with and without the reflection tier (docs/04 §9; FR-MEM-3;
#157; it extends #143's stub fixture ``tests/eval/test_q_03.py``).

Each case is one window (``retrieval/abstract.jsonl``: one character's three concrete deeds and
a neighbour's unrelated one, none stating the abstraction) on its own scratch story: the deeds
are written as ``do`` turns through the turn commit's episodic writer, #143's
``consolidate_window`` consolidates the window (the ``consolidation`` purpose writes the
reflection), and the case's abstract question is asked through the retrieval pipeline twice —
with every memory kind, and with the reflection tier left out (``retrieve(..., kinds=…)``). When
the deployment names an embedding model, the story's vectors are written first
(``scenes.drain_embeddings``); otherwise retrieval is lexical.

Relevant: the theme's deeds and any reflection the window produced. docs/04 sets no floor, so
the rows are report-only; the comparison is the point:

- ``q03.recall_with``: a relevant memory is among the first ten with reflections;
- ``q03.recall_without``: the same with the reflection tier left out;
- ``q03.reflection_found``: the window's reflection itself is among the first ten.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from epistrel.core.enums import MemoryKind
from epistrel.core.memory.consolidation import Window
from epistrel.core.retrieval.lexical import lexical_backend
from epistrel.core.retrieval.pipeline import RetrievalQuery, retrieve
from epistrel.core.stories import StorySpec
from epistrel.core.tables.memories import memories
from epistrel.evals import scenes
from epistrel.evals.gate import Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.evals.suites.e03_consolidation import DIALS, Episode, cast_of
from epistrel.evals.suites.q01_recall import K, ranking
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.registry import CONSOLIDATION

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

WITH = "q03.recall_with"
WITHOUT = "q03.recall_without"
REFLECTION_FOUND = "q03.reflection_found"
WITHOUT_REFLECTIONS = (MemoryKind.EPISODIC, MemoryKind.SEMANTIC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AbstractInputs(_Model):
    actor: str
    other: str
    episodes: list[Episode] = Field(min_length=2, max_length=20)
    question: str = Field(min_length=1, max_length=300)


class AbstractLabels(_Model):
    reflection: str
    supports: list[int]


class AbstractCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: AbstractInputs
    expected: AbstractLabels


@register
class Q03Suite(Suite):
    name = "q03"
    title = "Q-03: abstract-query recall with and without reflections"
    set_path = "retrieval/abstract.jsonl"
    min_cases = 60
    ci_gate = False
    case_model = AbstractCase
    metrics = (
        Metric(WITH, Rule.informational, description="recalled with the reflection tier"),
        Metric(WITHOUT, Rule.informational, description="recalled without the reflection tier"),
        Metric(
            REFLECTION_FOUND,
            Rule.informational,
            description="the window's reflection is in the top ten",
        ),
    )
    reading_notes = (
        "Q-03 compares recall of an abstract question with and without the reflection tier; "
        "no floor applies (docs/04). Without an embedding model retrieval is lexical, so the "
        "arm without reflections finds only deeds that share a word with the question.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, AbstractCase)  # noqa: S101 - load() validated it
        created = await ctx.story(
            StorySpec(characters=cast_of(case.inputs.actor, case.inputs.other))
        )
        story = created.story_id
        await scenes.structured(ctx.engine, ctx.principal, story, DIALS)
        ids = {c.name: c.id for c in created.characters}
        written = await scenes.episodes(
            ctx.engine, story, [(ids[e.by], e.text) for e in case.inputs.episodes]
        )
        turns = [m.narrative_turn for m in written if m.narrative_turn is not None]
        await consolidate_window(
            ctx.engine.db, story, Window(min(turns), max(turns)), models=ctx.models
        )
        async with ctx.engine.db.connect() as conn:
            reflections = {
                str(i)
                for i in await conn.scalars(
                    select(memories.c.id).where(
                        memories.c.story_id == story,
                        memories.c.kind == MemoryKind.REFLECTION.value,
                    )
                )
            }
        if await scenes.embedding_configured(ctx.engine):
            await scenes.drain_embeddings(ctx.engine, story)
        deeds = {written[i].id for i in case.expected.supports}
        relevant = deeds | reflections
        embedder = ctx.engine.models.embeddings
        tops: dict[str, list[str]] = {}
        for arm, kinds in (("with", None), ("without", WITHOUT_REFLECTIONS)):
            async with ctx.engine.db.connect() as conn:
                found = await retrieve(
                    conn,
                    story,
                    RetrievalQuery(case.inputs.question),
                    embed=embedder.query_embedder(),
                    embed_batch=embedder.batch_embedder(),
                    lexical=lexical_backend(ctx.engine.settings.lexical_backend),
                    touch=False,
                    kinds=kinds,
                )
            tops[arm] = ranking(found)[:K]
        return Outcome(
            {
                WITH: bool(relevant & set(tops["with"])),
                WITHOUT: bool(relevant & set(tops["without"])),
                REFLECTION_FOUND: bool(reflections & set(tops["with"])) if reflections else False,
            },
            output=json.dumps(tops),
            detail={"reflections": len(reflections)},
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, AbstractCase)  # noqa: S101 - load() validated it
        reflection = {
            "text": case.expected.reflection,
            "modality": "observation",
            "attributed_to": None,
            "supports": case.expected.supports,
        }
        return {CONSOLIDATION.name: json.dumps({"facts": [], "reflections": [reflection]})}


__all__ = ["REFLECTION_FOUND", "WITH", "WITHOUT", "AbstractCase", "Q03Suite"]
