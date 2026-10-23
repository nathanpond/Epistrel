"""S-SAME-TURN-BELIEF: cause and effect inside one turn (docs/04 §7; FR-EPI-13,14, FR-TURN-12,
FR-STORE-31, FR-VERIFY-11; #168 the render part, #174 the beat part, ``__beats``).

A structured story with Cal (the player) and Jenni, played end to end through ``respond`` with
the stub adapter; the story keeps per-character partitions, so Jenni has a belief set. Canon is
the author's: the river bridge's ``condition`` is ``collapsed`` (an author predicate,
``multi``). Jenni renders as the single primary and declares her reasoning as ordered ``steps``
(``orchestrator.structured.steps``):

- in order: *observe* the bridge collapsed → *believe* it is impassable (citing step 1) → *act*
  (she says so and heads for the ferry, a ``say`` naming step 3) is accepted: an
  ``observation`` transition citing the canon fact's event at ``(turn, beat, 1)``, the belief's
  transition citing ``render_step`` 1 at ``(turn, beat, 2)``, an observation memory at step 1,
  and the ``say`` event and utterance at step 3; the say is ``consistent`` with her record set
  (the verified step), so nothing else is licensed or repaired;
- a belief step citing a later step, or citing nothing, is rejected as self-attestation: no
  belief is written for it (the observation still is), and the say of the first act citing it
  is a hallucination, repaired;
- in a world where the bridge is only locked ``intact``, a render that invents the collapse
  itself (its own ``say`` declares it) is refused by #144's novelty permission
  (``locked_canon``), so the step observing it has nothing verified to observe and the belief
  citing it falls with it.

``__turn_boundaries``: within a turn a position is ``(turn, beat, step)``, yet truncation and
fork stay at turn boundaries (``N`` or ``N⁺k``, FR-STORE-31).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Proposition
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.event_support import rows, scalar
from tests.respond_support import body, new_story, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import (
    beat,
    claim,
    renders_of,
    structured_story,
    text_of,
    turn_report,
)

pytestmark = pytest.mark.integration

ALWAYS = Interval.of(None, None)
SEEN = claim("the river bridge", "condition", "collapsed")
CONCLUDED = claim("the river bridge", "condition", "impassable")
FERRY = "Jenni turns and heads for the ferry."
IMPASSABLE = "The river bridge is impassable; I'm heading for the ferry."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@dataclass(frozen=True)
class World:
    story_id: str
    cal: str
    jenni: str
    bridge: str
    collapsed: str
    impassable: str
    fact_event: str | None


async def _world(client: Any, engine: AsyncEngine, *, collapsed: bool = True) -> World:
    """The story (see the module docstring); ``collapsed`` false: the bridge is ``intact``,
    author-locked, and canon says nothing of a collapse."""
    story, ids = await structured_story(client, "Cal", "Jenni")
    await ts.enable_testimony(engine, story, "defaulted", level="co_location")
    async with engine.begin() as conn:
        bridge = await store.add_entity(conn, story, "place", "the river bridge")
        states = {
            name: await store.add_entity(conn, story, "other", name)
            for name in ("collapsed", "impassable", "intact")
        }
        rules = registry.ScenarioRules.parse(
            {
                "predicates": [
                    {
                        "name": "condition",
                        "subject_type": "place",
                        "object_type": "other",
                        "cardinality": "multi",
                        "temporal_kind": "permanent",
                        "docs": "A structure's condition.",
                    }
                ]
            }
        )
        reg = await registry.register_author(conn, story, rules)
    condition = reg.named("condition")
    assert condition is not None
    fact = await commit(
        engine,
        story,
        Proposition(
            subject_id=bridge,
            predicate_id=condition.id,
            object_id=states["collapsed" if collapsed else "intact"],
            valid_time=ALWAYS,
        ),
        kind=OperationKind.CONTROL,
        provenance="author",
        author_locked=not collapsed,
    )
    return World(
        story,
        ids["Cal"],
        ids["Jenni"],
        bridge,
        states["collapsed"],
        states["impassable"],
        fact.source_event_ids[0] if fact.source_event_ids else None,
    )


def _steps(*steps: dict[str, Any]) -> list[dict[str, Any]]:
    return list(steps)


async def _play(
    client: Any, world: World, beats: list[dict[str, Any]], steps: list[dict[str, Any]], stub: Any
) -> dict[str, Any]:
    stub.script("generation", [json.dumps({"beats": beats, "steps": steps})])
    response = await respond(client, world.story_id, body(world.cal, "Can we cross?"))
    assert response.status_code == 200, response.text
    played: dict[str, Any] = response.json()
    return played


async def _transitions(engine: AsyncEngine, world: World) -> list[Any]:
    return await rows(
        engine,
        "SELECT t.kind, t.antecedent_kind, t.antecedent_ref, t.position_beat, t.position_step, "
        "p.object_id FROM belief_transitions t JOIN beliefs b ON b.id = t.belief_id "
        "JOIN propositions p ON p.id = b.proposition_id "
        "WHERE t.story_id = :s AND t.holder_character_id = :h AND p.subject_id = :bridge "
        "ORDER BY t.position_step",
        s=world.story_id,
        h=world.jenni,
        bridge=world.bridge,
    )


async def _beliefs_about(engine: AsyncEngine, world: World, obj: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT b.id FROM beliefs b JOIN propositions p ON p.id = b.proposition_id "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND p.subject_id = :bridge "
        "AND p.object_id = :o",
        s=world.story_id,
        h=world.jenni,
        bridge=world.bridge,
        o=obj,
    )


@pytest.mark.part("M5", "FR-EPI-13,14; FR-TURN-12; FR-STORE-31; FR-VERIFY-11")
async def test_s_same_turn_belief__render(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """The render part (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- in order: observe → believe → act
        world = await _world(client, engine)
        await _play(
            client,
            world,
            [beat("say", IMPASSABLE, intent="sincere", step=3, propositions=[CONCLUDED])],
            _steps(
                {"index": 1, "kind": "observe", "proposition": SEEN},
                {"index": 2, "kind": "believe", "cites": [1], "proposition": CONCLUDED},
                {"index": 3, "kind": "act", "cites": [2]},
            ),
            stub,
        )
        report = await turn_report(engine, world.story_id)
        assert report["render"]["steps"] == {"declared": 3, "verified": 3, "rejected": []}
        # The prompt documents the step contract (and the think's cites) to the model, after
        # the canon (none retrieved here) and before the beat contract.
        (prompt,) = [text_of(c) for c in renders_of(stub)]
        assert prompt.index("## How to change your mind") < prompt.index("## Your part")
        assert "`cites`" in prompt
        seen, concluded = await _transitions(engine, world)
        (do_event,) = await rows(
            engine,
            "SELECT beat, step FROM events WHERE story_id = :s AND type = 'beat_say'",
            s=world.story_id,
        )
        # The say of what she concluded is checked against her record set, the verified
        # step included: consistent, nothing licensed or written beside the step's own.
        (said,) = [v for v in report["verify"]["verdicts"] if "speech" in v]
        assert (said["speech"]["belief_relation"], said["speech"]["classification"]) == (
            "consistent",
            "sincere",
        )
        assert "repairs" not in report
        (utterance,) = await rows(
            engine,
            "SELECT step FROM utterances WHERE story_id = :s AND speaker_character_id = :h",
            s=world.story_id,
            h=world.jenni,
        )
        assert utterance.step == 3
        assert (seen.kind, seen.antecedent_kind, seen.object_id) == (
            "observation",
            "record",
            world.collapsed,
        )
        assert seen.antecedent_ref == {"table": "events", "id": world.fact_event}
        assert (seen.position_beat, seen.position_step) == (do_event.beat, 1)
        assert (concluded.kind, concluded.antecedent_kind, concluded.object_id) == (
            "observation",
            "render_step",
            world.impassable,
        )
        assert concluded.antecedent_ref["step"] == 1
        assert concluded.antecedent_ref["beat"] == do_event.beat
        assert concluded.position_step == 2
        assert do_event.step == 3
        (memory,) = await rows(
            engine,
            "SELECT holder_character_id, beat, step, text FROM memories "
            "WHERE story_id = :s AND holder_character_id = :h AND text LIKE '%noticed%'",
            s=world.story_id,
            h=world.jenni,
        )
        assert (memory.beat, memory.step) == (do_event.beat, 1)
        assert report["epistemic"]["steps"] == {"transitions": 2, "memories": 1, "refused": 0}

        # -- a belief citing a later step, or nothing: self-attestation, nothing written for it
        for cites, cause in (([2], "later_cite"), ([], "no_cite")):
            world = await _world(client, engine)
            stub.reset()
            await _play(
                client,
                world,
                [beat("say", IMPASSABLE, intent="sincere", step=3, propositions=[CONCLUDED])],
                _steps(
                    {"index": 1, "kind": "believe", "cites": cites, "proposition": CONCLUDED},
                    {"index": 2, "kind": "observe", "proposition": SEEN},
                    {"index": 3, "kind": "act", "cites": [1]},
                ),
                stub,
            )
            report = await turn_report(engine, world.story_id)
            steps = report["render"]["steps"]
            assert steps["verified"] == 2, cause
            # The first act citing the rejected step asserts it: a hallucination, repaired.
            assert [r["tag"] for r in report["repairs"]] == ["hallucination"], cause
            assert steps["rejected"] == [{"index": 1, "reason": "self_attestation", "cause": cause}]
            assert await _beliefs_about(engine, world, world.impassable) == [], cause
            (observed,) = await _transitions(engine, world)
            assert (observed.object_id, observed.position_step) == (world.collapsed, 2)

        # -- a render inventing the collapse is refused by its permission
        world = await _world(client, engine, collapsed=False)
        stub.reset()
        await _play(
            client,
            world,
            [
                beat(
                    "say",
                    "The river bridge just collapsed!",
                    intent="sincere",
                    step=1,
                    propositions=[SEEN],
                ),
                beat("do", FERRY, step=4),
            ],
            _steps(
                {"index": 1, "kind": "act"},
                {"index": 2, "kind": "observe", "proposition": SEEN},
                {"index": 3, "kind": "believe", "cites": [2], "proposition": CONCLUDED},
                {"index": 4, "kind": "act", "cites": [3]},
            ),
            stub,
        )
        report = await turn_report(engine, world.story_id)
        (denied,) = report["verify"]["verdicts"]
        assert denied["permission"] == "denied:locked_canon"
        assert [r["tag"] for r in report["repairs"]] == ["unpermitted"]
        assert report["render"]["steps"]["rejected"] == [
            {"index": 2, "reason": "unverified_object", "cause": None},
            {"index": 3, "reason": "self_attestation", "cause": "unverified_cite"},
        ]
        assert await _transitions(engine, world) == []
        collapsed_facts = await rows(
            engine,
            "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.subject_id = :b AND p.object_id = :o",
            s=world.story_id,
            b=world.bridge,
            o=world.collapsed,
        )
        assert collapsed_facts == []

        # -- shared knowledge (no partitions): no step contract in the prompt
        story, ids = await structured_story(client, "Cal", "Jenni")
        stub.reset()
        played = await respond(client, story, body(ids["Cal"], "Can we cross?"))
        assert played.status_code == 200, played.text
        (prompt,) = [text_of(c) for c in renders_of(stub)]
        assert "## How to change your mind" not in prompt
        steps = (await turn_report(engine, story))["render"]["steps"]
        assert steps == {"declared": 0, "verified": 0, "rejected": []}


async def _revision(engine: AsyncEngine, story_id: str) -> Any:
    return await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story_id)


@pytest.mark.part("M5", "FR-STORE-31")
async def test_s_same_turn_belief__turn_boundaries(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Checkpoints stay at turn boundaries (FR-STORE-31): a truncation or fork naming a beat or
    a step is 422 ``turn_boundary_required`` before anything runs, the story untouched; at
    ``N⁺k`` (``slot``; 0 is ``N`` itself) it is accepted."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client, "Wren", "Ada")
        for _ in range(2):
            played = await respond(client, story, body(ids["Wren"]))
            assert played.status_code == 200, played.text
        before = await _revision(engine, story)
        for path, shape in (
            ("truncate", {"to_turn": 1, "beat": 2}),
            ("truncate", {"to_turn": 1, "step": 1}),
            ("truncate", {"to_turn": 1, "beat": 2, "step": 1}),
            ("fork", {"at_turn": 1, "beat": 2}),
            ("fork", {"at_turn": 1, "step": 1}),
        ):
            refused = await client.post(f"/stories/{story}/{path}", json=shape, headers=WRITE)
            assert refused.status_code == 422, (path, shape, refused.text)
            error = refused.json()["error"]
            assert error["code"] == "turn_boundary_required", (path, shape)
            assert error["details"]["boundary"] == ["turn", "slot"]
        assert await _revision(engine, story) == before
        stories = await scalar(engine, "SELECT count(*) FROM stories")
        forked = await client.post(
            f"/stories/{story}/fork", json={"at_turn": 1, "slot": 0}, headers=WRITE
        )
        assert forked.status_code == 201, forked.text
        assert await scalar(engine, "SELECT count(*) FROM stories") == int(stories) + 1
        truncated = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": 1, "slot": 0}, headers=WRITE
        )
        assert truncated.status_code == 200, truncated.text
        assert truncated.json()["to_turn"] == 1
        assert await _revision(engine, story) == int(before) + 1
