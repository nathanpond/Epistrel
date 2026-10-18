"""Q-03: abstract-query recall (docs/04 §9; FR-MEM-3), M4's stub-run part (#143). Reflections
make abstract questions retrievable: recall with the reflection tier against recall without it,
on a small labeled fixture run through the production path (``consolidate_window``, then
``retrieve``) with the stub scripted per window and no embedding model (the lexical arm). #157's
suite extends the fixture (``evals/sets/retrieval/abstract.jsonl`` over the 5,000-turn
fixture) and the live run."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import MemoryKind
from epistrel.core.memory.consolidation import Window
from epistrel.core.retrieval.pipeline import RetrievalQuery, retrieve
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import consolidation_support as cs
from tests.auth_support import auth_settings

pytestmark = pytest.mark.integration

THEMES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "Bobby",
        (
            "hides behind the shed when the neighbour's dog barks",
            "crosses the street to avoid the terrier",
            "refuses to walk past the kennel",
        ),
        "Bobby is afraid of dogs.",
        "Who is afraid?",
    ),
    (
        "Ann",
        (
            "slips an extra coin into the beggar's cup",
            "carries the widow's shopping home",
            "mends the orphans' coats for nothing",
        ),
        "Ann is generous to people in need.",
        "Who is generous?",
    ),
    (
        "Cal",
        (
            "checks the lock on the door three times",
            "counts the knives before bed",
            "asks twice whether the stove is off",
        ),
        "Cal is anxious about safety at home.",
        "Is anyone anxious?",
    ),
)
"""Per theme: the actor, three concrete deeds (none says the abstract word), the reflection a
consolidation of them forms, and the abstract question a label marks it relevant for."""


def _saying(deed: str) -> cs.Line:
    return lambda _: deed


@pytest.mark.part("M4", "FR-MEM-3")
async def test_q_03(engine: AsyncEngine, pg_url: str, drained_embedding_jobs: None) -> None:
    """Each theme's three deeds are consolidated into a reflection; each abstract question's
    relevant set is the theme's deeds and its reflection. Recall@k (a relevant memory among the
    ranked ``memories``) with the reflection tier is 1.0; without it (``kinds`` episodic only)
    it is 0.0: the deeds never say the abstract word. Complement: a concrete question about a
    deed is recalled either way, so the arm without reflections can retrieve at all."""
    models = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    models.bind(engine)
    w = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=1_000))
    story = w.story_id
    relevant: dict[str, set[str]] = {}
    turn = 0
    for actor, deeds, reflection, question in THEMES:
        made = []
        for deed in deeds:
            (t,) = await cs.bulk_turns(
                engine,
                story,
                1,
                actor=w.characters[actor],
                name=actor,
                line=_saying(deed),
                does=True,
            )
            made.append(t.memory_id)
        turn += len(deeds)
        models.stub.script(
            "consolidation", [cs.answer(reflections=[(reflection, "observation", [0, 1, 2])])]
        )
        run = await consolidate_window(engine, story, Window(turn - 2, turn), models=models)
        assert str(run.outcome) == "committed"
        (out,) = [r for r in await cs.outputs(engine, story) if r.text == reflection]
        relevant[question] = {*made, out.id}

    async def recall(kinds: tuple[MemoryKind, ...] | None, questions: dict[str, set[str]]) -> float:
        hits = 0
        for question, wanted in questions.items():
            async with engine.connect() as conn:
                found = await retrieve(
                    conn, story, RetrievalQuery(question), touch=False, kinds=kinds
                )
            hits += bool({m.id for m in found.memories} & wanted)
        return hits / len(questions)

    with_reflections = await recall(None, relevant)
    without = await recall((MemoryKind.EPISODIC,), relevant)
    assert (with_reflections, without) == (1.0, 0.0)
    assert with_reflections > without
    concrete = {"Who walked past the kennel?": relevant["Who is afraid?"]}
    assert await recall((MemoryKind.EPISODIC,), concrete) == 1.0
