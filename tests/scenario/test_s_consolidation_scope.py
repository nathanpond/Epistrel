"""S-CONSOLIDATION-SCOPE, M4 part (docs/04 §7; #143): consolidation never widens an input's
epistemic type or scope (FR-MEM-9). The M5 clause (Jenni's and Bobby's private memories of one
event under M5's knowledge partitions) is #M5's; here the scope rule is shown on memories
scoped to characters written by the test, since M4 writes none."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import ScopeMismatch
from epistrel.core.memory import consolidation as core
from epistrel.core.memory.consolidation import Scope, Window
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import canon_support as c
from tests import consolidation_support as cs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.memory_support import event_ids, turn


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


@pytest.mark.part("M4", "FR-MEM-9")
async def test_s_consolidation_scope__type(
    engine: AsyncEngine, models: ModelAccessLayer, drained_embedding_jobs: None
) -> None:
    """Bobby publicly claims "The bridge is out." (turn 1, false in canon); canon says Cal is in
    the kitchen (turn 2); the narrator says the wind rattles the shutters (turn 3). The stub is
    scripted to state all three objectively, and a reflection over Bobby's claim and the fact.
    The engine keeps "Bobby said the bridge is out." as Bobby's ``attributed_claim`` (public),
    never an objective "the bridge is out"; the canon fact stays ``objective`` (objective
    visibility); the narration becomes an ``observation``; the reflection resting on Bobby's
    claim is his claim too. Each output carries ``system_consolidated``, the model, a prompt
    hash, no model version (the stub reports none), level 1, the replay key and every input's
    source events. Complement: only the statement every support of which is objective canon is
    objective. Scope: two characters' private memories cannot be combined (``ScopeMismatch``
    when planned, and a job over them makes no call); ``[Ann]`` with ``[Ann, Bobby]`` is
    compatible and its intersection is ``[Ann]``."""
    w = await cs.story(engine)
    story, bobby, ann = w.story_id, w.characters["Bobby"], w.characters["Ann"]
    claim_op, _ = await turn(engine, story, actor=bobby, say="The bridge is out.", reply=None)
    fact = await c.commit(
        engine,
        story,
        Proposition(
            subject_id=w.people["Cal"],
            predicate_id=CORE_BY_NAME["located_at"].id,
            object_id=w.places["kitchen"],
            valid_time=Interval.of(0, None),
        ),
    )
    narration_op, _ = await turn(engine, story, say=None, reply="The wind rattles the shutters.")
    models.stub.script(
        "consolidation",
        [
            cs.answer(
                facts=[
                    ("The bridge is out.", "objective", [0]),
                    ("Cal is in the kitchen.", "objective", [1]),
                    ("The wind rattles the shutters.", "objective", [2]),
                    ("Nothing supports this.", "objective", [7]),
                ],
                reflections=[
                    ("The way out is blocked.", "objective", [0, 1]),
                    # #462: a guess beyond the records is no input's type, and is dropped.
                    ("The wind suggests that a storm may be coming.", "observation", [2]),
                    ("Cal seems to be avoiding the bridge.", "observation", [0, 1]),
                ],
            )
        ],
    )
    run = await consolidate_window(engine, story, Window(1, 3), models=models)
    assert str(run.outcome) == "committed", run
    assert run.result["unsupported"] == 1
    assert run.result["downgraded"] == 3
    assert run.result["speculative"] == 2
    made = {r.text: r for r in await cs.outputs(engine, story)}
    assert set(made) == {
        "Bobby said the bridge is out.",
        "Cal is in the kitchen.",
        "The wind rattles the shutters.",
        "Bobby said the way out is blocked.",
    }
    claim = made["Bobby said the bridge is out."]
    assert (claim.kind, claim.modality, claim.character_id) == (
        "semantic",
        "attributed_claim",
        bobby,
    )
    assert claim.visibility == {"scope": "public"}
    assert not any(
        "bridge is out" in r.text.casefold() and r.modality != "attributed_claim"
        for r in made.values()
    )
    canon_row = made["Cal is in the kitchen."]
    assert (canon_row.modality, canon_row.visibility) == ("objective", {"scope": "objective"})
    seen = made["The wind rattles the shutters."]
    assert (seen.modality, seen.character_id) == ("observation", None)
    reflection = made["Bobby said the way out is blocked."]
    assert (reflection.kind, reflection.modality) == ("reflection", "attributed_claim")
    assert reflection.visibility == {"scope": "public"}
    # Complement: objective only where every support is objective canon.
    assert [r.text for r in made.values() if r.modality == "objective"] == [
        "Cal is in the kitchen."
    ]
    # #462's hedge rule, both ways: the issue's live misses are guesses; a literal deed or
    # saying that shares a word with a hedge is kept, and so is a hedge the record itself makes.
    episodes = ["sweeps the porch", "avoids the dog", "I might go to the market."]
    for guess in (
        "The avoidance pattern suggests Bobby may have been deliberately distancing himself.",
        "There appears to be a division of labor between Raj and Linda.",
        "Jenni's action of sweeping the porch seems unrelated to Marcus's cellar investigation.",
        "Ann probably wanted to leave.",
        "This suggests a quarrel.",
        "Tom may be ill.",
    ):
        assert core.speculative(guess, episodes), guess
    for plain in (
        "Bobby appears at the door.",
        "Ann suggests a walk to the river.",
        "Raj pushes the cart with all his might.",
        "Ada is working hard at school.",
        "Linda said the harvest may come early.",
        "Bobby said he might go to the market.",
    ):
        assert not core.speculative(plain, episodes), plain

    sources = [
        (await event_ids(engine, claim_op.operation_id))["player_input"],
        *fact.source_event_ids,
        (await event_ids(engine, narration_op.operation_id))["message_rendered"],
    ]
    (window,) = await cs.windows(engine, story)
    for row in made.values():
        assert row.provenance == "system_consolidated"
        assert row.trust_tier == "engine"
        assert (row.model, row.model_version) == ("stub", None)
        assert len(row.prompt_hash or "") == 64
        assert (row.level, row.inputs_hash) == (1, window.source_window["inputs_hash"])
        assert list(row.source_event_ids) == sources
        assert (row.operation_id, row.narrative_turn) == (window.operation_id, 3)
    assert (window.source_window["from_turn"], window.source_window["to_turn"]) == (1, 3)
    assert window.source_window["scope"] == "shared"
    assert {k.split(":")[0] for k in window.source_window["input_versions"]} == {
        "memories",
        "operations",
    }
    # The episodic inputs are covered by the outputs.
    covered = await rows(
        engine,
        "SELECT covered_by FROM memories WHERE story_id = :s AND kind = 'episodic'",
        s=story,
    )
    assert all(set(r.covered_by) == {m.id for m in made.values()} for r in covered)

    # Scope: Ann's and Bobby's private memories have no common holder.
    before = cs.calls(models)
    await cs.private_memory(engine, story, ann, [ann], "Ann hid the key under the mat.")
    await cs.private_memory(engine, story, bobby, [bobby], "Bobby saw the key under the mat.")
    pair = Scope(characters=(ann, bobby))
    async with engine.connect() as conn:
        with pytest.raises(ScopeMismatch) as refused:
            await core.plan(conn, story, Window(4, 5), pair)
    assert refused.value.code == "scope_mismatch"
    refusal = await consolidate_window(engine, story, Window(4, 5), pair, models=models)
    assert refusal.result["noop"] == "scope_mismatch"
    assert cs.calls(models) == before
    # Compatible: [Ann] and [Ann, Bobby] intersect in [Ann].
    await cs.private_memory(engine, story, ann, [ann, bobby], "Ann and Bobby found the key.")
    async with engine.connect() as conn:
        both = await core.plan(conn, story, Window(6, 6), Scope(characters=(ann,)))
        assert both is not None
        assert both.visibility.characters == tuple(sorted((ann, bobby)))
        ann_only = await core.plan(conn, story, Window(4, 6), Scope(characters=(ann,)))
        assert ann_only is not None
        assert ann_only.visibility.characters == (ann,)
        assert len(ann_only.inputs) == 2
