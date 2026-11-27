"""I-12: repair primitives (docs/04 §4). Every I-12 part, M4's and M5's, lives in this file
(traceability counts a test ID per milestone in one file).

``test_i_12__neighborhood`` (#142, FR-VERIFY-6): a contradicted claim returns the contradicted
fact, and ``fact_neighborhood`` returns its dependency cluster over real canon. The cluster
holds the facts with the same subject and predicate, the facts linked by a declared
implication, the key's closed set, the facts on a shared subject in both directions (to depth
2), the projections and the linked memories. It leaves out superseded, discarded, hidden and
unconnected facts, and it is bounded.

``test_i_12__repair`` (#151, FR-REPAIR-1,2,3, FR-VERIFY-6): structured turns played end to end
with the stub adapter; the repair ladder corrects a contradicted say at rungs 1, 2 and 3 and
falls back at rung 4, the report exposes the contradicted fact and its neighborhood, and both
renders are recorded (the rejected one as audit, the correction as narrative state).

``test_i_12__classification`` (M5, #166, FR-VERIFY-5): the speech-act classification over the
verdict, the checked intent and the provenance, and its record on a repair.

``test_i_12__m12_*`` (M12, #1014, FR-REPAIR-5): ``repair.max_attempts`` per render (the
narrator's and the first primary's, three primaries, two flagged beats in one render, a call
budget with room for one regeneration), the drop rule (a fallback line beside its speaker's real
line, one fallback line per speaker, a lone failing beat, a narration) and the per-beat cause in
``respond`` and the history read. ``test_i_12__m12_name_only`` (M12, #1019, FR-REPAIR-2): a
narrowing that leaves only a name is not accepted at rung 1.

``test_i_12__m12_outcome_step_*`` (M12, #1033, #1034, FR-REPAIR-5): a staged turn's outcome
step. The intent's narrator line drops the outcome step's fallback (and its complement), and
the outcome step's repairs reach respond's and the history's ``degradation_details`` (and
the complement: the intent's entries, once).

M12 round 8 adds: a rejected regeneration's metric and a think's repair entry read at two
viewpoints (``test_i_12__m12_failed_regeneration``, #1051, #1053), the render counters with an
unregenerated render (``..._render_budget_limit``, #1052), a player-echo and a simultaneous
middle primary's copy (``..._copied_lines_player_echo``, ``..._simultaneous_middle``, #1048,
#1049) and an escalated character's copy (``..._copied_lines_escalated``, #944).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from epistrel.core.canon.facts import Visibility
from epistrel.core.enums import OperationKind, Provenance, RecordProvenance
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import memory_record
from epistrel.core.events import Operation
from epistrel.core.events.types import RenderRejected
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.memory import records
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import (
    Granularity,
    Implication,
    Instant,
    Interval,
    Proposition,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.resolution.time import story_time_at
from epistrel.core.tables.memories import memories
from epistrel.core.verify import speech_acts
from epistrel.core.verify.neighborhood import Edge, Neighborhood, Seed, fact_neighborhood
from epistrel.core.verify.rules import Rule, VerdictKind
from epistrel.core.views import audit_view
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.structured import staged
from epistrel.orchestrator.verify.verifier import story_verifier
from tests import clock_support as cs
from tests import commit_support
from tests import effect_support as es
from tests import multi_support as ms
from tests import partition_support as parts
from tests import provider_support as ps
from tests import reactive_support as rs
from tests import state_support as ss
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.canon_support import KNOWS, LOCATED_AT, PRINCIPAL, World, at, commit, supersede, world
from tests.event_support import rows
from tests.fixtures.bobby_education import DEGREE_AWARDED, GRADUATION, bobby_education
from tests.fixtures.key_room import key_room
from tests.memory_support import use_structured
from tests.projection_support import truncate
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ, create, override, story_body
from tests.structured_turn_support import (
    age_fact,
    beat,
    claim,
    render,
    structured_story,
    turn_report,
)

pytestmark = pytest.mark.integration

LOCATED_IN = CORE_BY_NAME["located_in"].id
PRESENT_AT = CORE_BY_NAME["present_at"].id
SIBLING_OF = CORE_BY_NAME["sibling_of"].id
FRIEND_OF = CORE_BY_NAME["friend_of"].id


def _rel(
    subject: str, predicate: str, obj: str, lo: int | None = None, hi: int | None = None
) -> Proposition:
    return Proposition(
        subject_id=subject, predicate_id=predicate, object_id=obj, valid_time=Interval.of(lo, hi)
    )


async def _memory(conn: AsyncConnection, op: Operation, story_id: str, **links: Any) -> str:
    """A position-less semantic memory linked by ``proposition_ids`` or ``source_event_ids``."""
    memory_id = new_ulid()
    row = {
        "id": memory_id,
        "story_id": story_id,
        "kind": "semantic",
        "text": "Bobby keeps to the kitchen.",
        "holder_character_id": None,
        "character_id": None,
        "visibility": {"scope": "objective"},
        "provenance": RecordProvenance.SYSTEM_CONSOLIDATED.value,
        "trust_tier": records.trust_tier_of(RecordProvenance.SYSTEM_CONSOLIDATED).value,
        "operation_id": op.operation_id,
        "source_event_ids": list(links.get("source_event_ids", ())),
        "proposition_ids": list(links.get("proposition_ids", ())),
        "narrative_turn": None,
        "turn_variant": None,
    }
    await insert_derived(conn, memories, [row])
    await index_records(conn, [memory_record(SimpleNamespace(**row))])
    return memory_id


async def _cluster(engine: AsyncEngine) -> dict[str, Any]:
    """Bobby in the kitchen (0-200, until he is at the school) with two memories about it, and
    what hangs on it. The kitchen window covers the head's story-time (``story_time_at``, the
    projections' "now"; #438), so the head's profile still places him there."""
    w: World = await world(engine)
    sid = w.story_id
    await use_structured(engine, sid)
    bobby, ann, cal = w.people["Bobby"], w.people["Ann"], w.people["Cal"]
    kitchen, garden, school = w.places["kitchen"], w.places["garden"], w.places["school"]
    async with engine.begin() as conn:
        house = await store.add_entity(conn, sid, "place", "house")
        dora = await store.add_entity(conn, sid, "person", "Dora")
        attic = await store.add_entity(conn, sid, "place", "attic")
    found: dict[str, Any] = {"world": w, "house": house}
    async with Operation(engine, story_id=sid, kind=OperationKind.TURN, principal=PRINCIPAL) as op:
        op.append("player_input", {"text": "where is Bobby?"}, provenance=Provenance.PLAYER, beat=1)
        op.append("message_rendered", {"text": "In the kitchen."}, beat=2)

        async def step(conn: AsyncConnection) -> None:
            beats = await records.beats_of_operation(conn, op.operation_id)
            event = beats[-1].event_id
            fact = await canon.commit_fact(
                conn,
                sid,
                at(bobby, kitchen, 0, 200),
                provenance=RecordProvenance.RENDER,
                operation=op,
                source_event_ids=[event],
            )
            found["seed"] = fact
            found["by_proposition"] = await _memory(
                conn, op, sid, proposition_ids=[fact.proposition_id]
            )
            found["by_source"] = await _memory(conn, op, sid, source_event_ids=[event])
            found["unlinked"] = await _memory(conn, op, sid)

        op.add_commit_step(step)
        await op.commit()

    found["kitchen_in_house"] = await commit(engine, sid, _rel(kitchen, LOCATED_IN, house))
    found["garden_in_house"] = await commit(engine, sid, _rel(garden, LOCATED_IN, house))
    found["present"] = await commit(engine, sid, _rel(bobby, PRESENT_AT, kitchen, 0, 100))
    found["sibling"] = await commit(engine, sid, _rel(ann, SIBLING_OF, bobby))
    found["knows_cal"] = await commit(engine, sid, _rel(bobby, KNOWS, cal, 0, 100))
    found["ann_in_garden"] = await commit(engine, sid, at(ann, garden, 0, 100))
    old = await commit(engine, sid, at(bobby, garden, 200, 300))
    found["moved"] = await supersede(engine, sid, old.id, at(bobby, school, 200, 300))
    found["superseded"] = old
    found["hidden"] = await commit(
        engine,
        sid,
        _rel(bobby, KNOWS, ann, 0, 100),
        visibility=Visibility(scope="characters", characters=(w.characters["Ann"],)),
    )
    found["dora"] = await commit(engine, sid, at(dora, attic, 0, 100))
    async with engine.begin() as conn:
        reg = await registry.load(conn, sid)
        found["closed"] = await store.declare_closed_set(
            conn, sid, store.ClosedSet(KNOWS, bobby, (cal,)), reg, asserted_by="author"
        )
    # A friendship a truncation discards, with the turn after it.
    async with engine.connect() as conn:
        head = await conn.scalar(
            text("SELECT next_turn_index FROM stories WHERE id = :s"),
            {"s": sid},
        )
    found["discarded"] = await commit(engine, sid, _rel(bobby, FRIEND_OF, ann, 0, 100))
    await commit(engine, sid, at(dora, attic, 100, 200))
    assert await truncate(engine, sid, int(head) - 1) is not None
    async with engine.connect() as conn:
        kept = await conn.scalar(
            text("SELECT next_turn_index FROM stories WHERE id = :s"), {"s": sid}
        )
        now = story_time_at(int(kept) - 1, await load_time_basis(conn, sid))
    assert 0 <= now < 200, now  # the head's "now" is inside the kitchen window
    return found


async def _neighborhood(
    engine: AsyncEngine, story_id: str, seed: Seed, **kw: Any
) -> tuple[Neighborhood, StructuredReadSet]:
    read_set = StructuredReadSet()
    async with engine.connect() as conn:
        reg = await registry.load(conn, story_id)
        rule = Implication(when=LOCATED_AT, then=PRESENT_AT)
        found = await fact_neighborhood(
            KeyedLookup(conn, read_set),
            story_id,
            seed,
            registry=replace(reg, story_rules=(*reg.story_rules, rule)),
            **kw,
        )
    return found, read_set


def _edges(found: Neighborhood) -> dict[str, tuple[Edge, int]]:
    return {i.id: (i.edge, i.depth) for i in found.items}


@pytest.mark.part("M4", "FR-VERIFY-6")
async def test_i_12__neighborhood(engine: AsyncEngine) -> None:
    """A contradicted claim (Bobby in the garden at 50-60, while canon has him in the kitchen of
    the same house) cites the kitchen fact, and its neighborhood is its dependency cluster:

    - the seed; Bobby's later place (the version that superseded the garden, not the garden);
      ``present_at`` through the declared implication; the closed set of whom Bobby knows;
    - the facts on a shared subject in both directions: the kitchen in the house (depth 1),
      Ann his sibling, whom Bobby knows (depth 1), and through them the garden in the house and
      Ann in the garden (depth 2);
    - the placement and relationship projections, each with its source fact;
    - the memory wrapping the seed's proposition, then the one from its source event.

    Complement: the superseded version, the friendship a truncation discarded, the unrelated
    memory and Dora in the attic (unconnected) are not in it. A fact hidden from Bobby is in
    the omniscient cluster and not in Bobby's. A cap of 3 keeps the seed, the same predicate
    and the implication, and says it truncated. A superseded seed has no neighborhood.
    Everything was read as context: retrieved rows, no query key."""
    f = await _cluster(engine)
    w: World = f["world"]
    sid = w.story_id
    bobby, garden = w.people["Bobby"], w.places["garden"]

    read_set = StructuredReadSet()
    async with engine.connect() as conn:
        v = await story_verifier(conn, read_set, sid, models=None, budget=TurnBudget())
        result = await v.verify(at(bobby, garden, 50, 60))
    assert result is not None
    assert (result.kind, result.rule) == (VerdictKind.CONTRADICTED, Rule.FUNCTIONAL_KEY.value)
    cited = [e.fact_id for e in result.verdict.evidence if e.fact_id is not None]
    assert f["seed"].id in cited
    assert result.needs_repair

    found, keyed = await _neighborhood(engine, sid, Seed.fact(f["seed"].id))
    edges = _edges(found)
    assert edges[f["seed"].id] == (Edge.SEED, 0)
    assert edges[f["moved"].id] == (Edge.PREDICATE, 1)
    assert edges[f["present"].id] == (Edge.IMPLICATION, 1)
    assert edges[f["closed"]] == (Edge.CLOSED_SET, 1)
    assert edges[f["kitchen_in_house"].id] == (Edge.SUBJECT, 1)
    assert edges[f["knows_cal"].id] == (Edge.SUBJECT, 1)
    assert edges[f["sibling"].id][1] == 1
    assert edges[f["garden_in_house"].id] == (Edge.OBJECT, 2)
    assert edges[f["ann_in_garden"].id] == (Edge.SUBJECT, 2)
    assert f["hidden"].id in edges
    projections = [i for i in found.items if i.kind == "projection"]
    assert {i.projection for i in projections} >= {"placement", "relationship"}
    assert all(i.source_fact_id in edges for i in projections if i.source_fact_id)
    assert found.ids("memory") == [f["by_proposition"], f["by_source"]]
    order = [PRIORITY_OF[i.edge] for i in found.items]
    assert order == sorted(order)
    assert not found.truncated

    # The complement: what does not hang on the seed, or is no longer canon, is left out.
    for absent in ("superseded", "discarded", "dora"):
        assert f[absent].id not in edges, absent
    assert f["unlinked"] not in edges
    assert keyed.query_keys == []
    assert keyed.retrieved

    as_bobby, _ = await _neighborhood(
        engine, sid, Seed.fact(f["seed"].id), viewpoint=w.viewpoint("Bobby")
    )
    assert f["hidden"].id not in _edges(as_bobby)
    assert f["knows_cal"].id in _edges(as_bobby)

    capped, _ = await _neighborhood(engine, sid, Seed.fact(f["seed"].id), max_facts=3)
    assert [i.id for i in capped.items] == [f["seed"].id, f["moved"].id, f["present"].id]
    assert capped.truncated

    gone, _ = await _neighborhood(engine, sid, Seed.fact(f["superseded"].id))
    assert gone.items == ()
    assert not gone.truncated

    around, _ = await _neighborhood(engine, sid, Seed.entity(bobby), depth=1)
    near = _edges(around)
    assert f["seed"].id in near
    assert f["sibling"].id in near
    assert f["ann_in_garden"].id not in near  # depth 2 only


PRIORITY_OF = {
    Edge.SEED: 0,
    Edge.PREDICATE: 1,
    Edge.IMPLICATION: 2,
    Edge.CLOSED_SET: 3,
    Edge.SUBJECT: 4,
    Edge.OBJECT: 4,
    Edge.PROJECTION: 5,
    Edge.MEMORY: 6,
}


# -- test_i_12__repair (#151: FR-REPAIR-1,2,3, FR-VERIFY-6) ---------------------------------------


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _regenerated(text_: str, *props: dict[str, Any]) -> str:
    """A scripted ``regeneration`` answer (#151's ``RegeneratedBeat``)."""
    return json.dumps({"text": text_, "propositions": list(props)})


async def _audit(engine: AsyncEngine, story_id: str, event_type: str) -> list[Any]:
    return [e for e in await audit_view(engine, story_id) if e.type == event_type]


async def _age_story(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], str]:
    """Wren (the player) and Ada, whose canon age is 64: the story, the ids, the fact's id."""
    story, ids = await structured_story(client, "Wren", "Ada")
    await age_fact(engine, story, ids["Ada"], 64)
    (fact,) = await rows(
        engine,
        "SELECT id FROM facts WHERE story_id = :s AND subject_id = :a",
        s=story,
        a=ids["Ada"],
    )
    return story, ids, str(fact.id)


@pytest.mark.part("M4", "FR-REPAIR-1,2,3; FR-VERIFY-6")
async def test_i_12__repair(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A contradicted claim returns the contradicted fact and its neighborhood (the repair's
    report); the corrected span is re-verified before it is accepted; the rejected render is
    recorded as a ``render_rejected`` audit event and the accepted correction as the beat's
    ``narrative_state`` event (with ``repaired_from``). Rung 1 drops the contradicting sentence;
    rung 2 swaps the object for canon's; rung 3 regenerates through ``regeneration`` under the
    constraint. Complement: a correction that still fails verification is not accepted and the
    next rung runs (a second regeneration, then the safe fallback); nothing a rejected text
    asserted is ever canonized."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- rung 2: a functional claim citing one fact takes canon's value
        story, ids, age_id = await _age_story(client, engine)
        wren = ids["Wren"]
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say", "I'm 70.", intent="sincere", propositions=[claim("I", "age", "70")]
                    ),
                    beat("do", "She sits down by the fire."),
                )
            ],
        )
        played = (await respond(client, story, body(wren, "Ada, how old are you?"))).json()
        assert played["message"]["text"] == 'Ada: "I\'m 64."\n\n*She sits down by the fire.*'
        said = played["beats"][1]
        assert said["labels"]["objective_relation"] == "true"
        report = await turn_report(engine, story)
        (entry,) = report["repairs"]
        assert (entry["verdict"], entry["rung"], entry["class"], entry["accepted"]) == (
            "contradicted",
            2,
            "cosmetic",
            True,
        )
        # FR-REPAIR-1: the contradicted fact and its neighborhood (seeded at that fact).
        assert entry["contradicted_facts"] == [age_id]
        (cluster,) = entry["neighborhood"]
        assert cluster["seed"] == {"fact_id": age_id}
        assert age_id in {i["id"] for i in cluster["items"]}
        assert entry["neighborhood_size"] == 1
        (rejected,) = await _audit(engine, story, "render_rejected")
        assert (rejected.payload.text, rejected.payload.rung) == ("I'm 70.", 2)
        assert rejected.payload.attempt == 0
        assert rejected.payload.verdicts[0]["kind"] == "contradicted"
        assert rejected.category.value == "audit"
        (verdict,) = await _audit(engine, story, "verify_verdict")
        assert verdict.payload.beat == said["beat"]
        (stored,) = await rows(
            engine,
            "SELECT payload, category FROM events WHERE story_id = :s AND type = 'beat_say'",
            s=story,
        )
        assert stored.category == "narrative_state"
        assert stored.payload["text"] == "I'm 64."
        assert stored.payload["repaired_from"] == {"rung": 2, "class": "cosmetic"}
        assert not [c for c in stub.calls if c.purpose == "regeneration"]

        # -- rung 1: the contradicting sentence is dropped when another is left
        story, ids, _ = await _age_story(client, engine)
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        "I'm 70. The fire is warm.",
                        intent="sincere",
                        propositions=[claim("I", "age", "70")],
                    )
                )
            ],
        )
        played = (await respond(client, story, body(ids["Wren"], "How old are you?"))).json()
        assert played["message"]["text"] == 'Ada: "The fire is warm."'
        ((entry,),) = [(await turn_report(engine, story))["repairs"]]
        assert (entry["rung"], entry["class"]) == (1, "cosmetic")

        # -- rung 3 under its constraint; complement: a failing correction is not accepted
        story, ids, age_id = await _age_story(client, engine)
        await override(client, story, {"repair": {"max_attempts": 2}})
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        "I am seventy years old.",
                        intent="sincere",
                        propositions=[claim("I", "age", "70")],
                    )
                )
            ],
        )
        stub.script(
            "regeneration",
            [
                _regenerated("I'm 71.", claim("I", "age", "71")),
                _regenerated("I'm sixty-four.", claim("I", "age", "64")),
            ],
        )
        played = (await respond(client, story, body(ids["Wren"], "How old are you?"))).json()
        assert played["message"]["text"] == 'Ada: "I\'m sixty-four."'
        regenerations = [c for c in stub.calls if c.purpose == "regeneration"]
        assert len(regenerations) == 2
        prompt = "\n".join(m.content for m in regenerations[0].messages)
        assert "I am seventy years old." in prompt  # edit in place: the rejected text
        assert "Do not assert that Ada age 70" in prompt  # the constraint
        assert "Ada age 64" in prompt  # the contradicted fact
        ((entry,),) = [(await turn_report(engine, story))["repairs"]]
        assert (entry["rung"], entry["attempts"], entry["accepted"]) == (3, 2, True)
        texts = [
            (e.payload.text, e.payload.attempt, e.payload.rung)
            for e in await _audit(engine, story, "render_rejected")
        ]
        assert texts == [("I am seventy years old.", 0, 3), ("I'm 71.", 1, 3)]
        assert await _ages(engine, story, ids["Ada"]) == [64]

        # -- complement: the last rung, when every regeneration still fails
        story, ids, _ = await _age_story(client, engine)
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        "I am seventy years old.",
                        intent="sincere",
                        propositions=[claim("I", "age", "70")],
                    )
                )
            ],
        )
        stub.script("regeneration", [_regenerated("I'm 71.", claim("I", "age", "71"))])
        played = (await respond(client, story, body(ids["Wren"], "How old are you?"))).json()
        assert played["message"]["text"] == 'Ada: "Ada changes the subject."'
        assert {"safe_fallback", "repair_deterministic"} <= set(played["degradation"])
        ((entry,),) = [(await turn_report(engine, story))["repairs"]]
        assert (entry["rung"], entry["class"], entry["accepted"]) == (4, "cascade", False)
        assert [e.payload.text for e in await _audit(engine, story, "render_rejected")] == [
            "I am seventy years old.",
            "I'm 71.",
        ]
        assert await _ages(engine, story, ids["Ada"]) == [64]


async def _ages(engine: AsyncEngine, story_id: str, character_id: str) -> list[int]:
    found = await rows(
        engine,
        "SELECT p.literal FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND f.subject_id = :a AND p.literal IS NOT NULL",
        s=story_id,
        a=character_id,
    )
    return sorted(int(r.literal["value"]) for r in found)


_S, _X, _N = VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED, VerdictKind.NOVEL
CLASSIFICATIONS = (
    # (intent, canon, belief, contrary, hidden, licensed) → (classification, intent, write)
    (("sincere", _X, _S, False, None, False), ("sincere_error", "sincere", None)),
    (("sincere", _X, _X, False, None, False), ("hallucination_repaired", "sincere", None)),
    (("sincere", _X, _X, False, None, True), ("sincere_error", "sincere", "acquire")),
    (("sincere", _X, _X, True, None, True), ("sincere_error", "sincere", "correct")),
    (("sincere", _S, _X, True, None, True), ("sincere", "sincere", "correct")),
    (("unspecified", _S, _S, False, None, False), ("sincere", "unspecified", None)),
    (("deceptive", _X, _X, True, None, False), ("lie", "deceptive", None)),
    (("deceptive", _S, _X, True, None, False), ("lie", "deceptive", None)),
    (("deceptive", _N, _N, False, None, False), ("lie", "deceptive", None)),
    (("deceptive", _X, _X, False, None, False), ("hallucination_repaired", "deceptive", None)),
    (("deceptive", _N, _N, False, _X, False), ("hallucination_repaired", "deceptive", None)),
    (("deceptive", _S, _S, False, None, False), ("sincere", "sincere", None)),
    (("joking", _X, _X, False, None, False), ("joke", "joking", None)),
    (("sincere", _N, _N, False, None, False), ("sincere", "sincere", None)),
    (("sincere", _N, _N, False, _X, False), ("hallucination_repaired", "sincere", None)),
    (("sincere", _N, _N, False, _X, True), ("sincere_error", "sincere", "acquire")),
    (("sincere", _N, _N, False, _S, True), ("sincere", "sincere", "acquire")),
)


@pytest.mark.part("M5", "FR-VERIFY-5")
async def test_i_12__classification(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The lie / false-belief / hallucination classification combines the verdict, the checked
    ``think`` intent and the provenance (#166, ``core.verify.speech_acts``): the decision table
    above (canon and belief verdicts, a contrary belief, canon beyond the speaker's view, a
    licensing antecedent), the intent checked (a declared lie the beliefs support is relabelled
    ``sincere``; ``unspecified`` stays), and an utterance's worst claim wins. End to end, a
    hallucination is repaired: ``hallucination_repaired`` lives on the ``render_rejected``
    audit record, and the accepted correction carries its re-verified classification."""
    for (intent, canon_kind, belief_kind, contrary, hidden, licensed), want in CLASSIFICATIONS:
        inputs = speech_acts.Inputs(intent, canon_kind, belief_kind, contrary, hidden)
        decision = speech_acts.classify(inputs, licensed=licensed)
        case = (intent, canon_kind.value, belief_kind.value, contrary, hidden, licensed)
        assert (decision.classification, decision.intent, decision.write) == want, case
        assert decision.intent_corrected is (intent == "deceptive" and want[1] == "sincere")
        if licensed:
            assert speech_acts.needs_licence(inputs), case
    assert not speech_acts.needs_licence(speech_acts.Inputs("joking", _X, _X))
    assert speech_acts.worst(["sincere", "joke", "lie", None]) == "lie"
    assert speech_acts.worst([None]) is None
    assert speech_acts.aggregate_relation(["consistent", "unknown"]) == "unknown"
    assert speech_acts.aggregate_relation(["unknown", "inconsistent"]) == "inconsistent"
    assert speech_acts.aggregate_relation([]) == "unknown"

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        edu = await bobby_education(client, engine)
        await ts.enable_testimony(engine, edu.story_id, "defaulted", level="co_location")
        osu = Proposition(
            subject_id=edu.bobby,
            predicate_id=DEGREE_AWARDED,
            object_id=edu.osu,
            qualifiers={"degree_id": "bobby_ba"},
            valid_time=Instant.of(GRADUATION, Granularity.MINUTE),
        )
        await parts.believe(engine, edu.story_id, edu.bobby, osu, kind=OperationKind.CONTROL)
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        "My degree is from Florida.",
                        intent="sincere",
                        propositions=[
                            claim(
                                "I",
                                "degree_awarded",
                                "Florida",
                                qualifiers={"degree_id": "bobby_ba"},
                            )
                        ],
                    )
                )
            ],
        )
        response = await respond(client, edu.story_id, body(edu.cal, "Where did you study?"))
        assert response.status_code == 200, response.text
        (repaired,) = (await turn_report(engine, edu.story_id))["repairs"]
        assert repaired["rung"] == 2
        async with engine.connect() as conn:
            audit = await audit_view(conn, edu.story_id)
        (rejected,) = [
            e.payload
            for e in audit
            if isinstance(e.payload, RenderRejected) and e.payload.attempt == 0
        ]
        (verdict,) = rejected.verdicts
        assert isinstance(verdict, dict)
        assert isinstance(verdict["speech"], dict)
        assert verdict["speech"]["classification"] == "hallucination_repaired"
        (said,) = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'beat_say'",
            s=edu.story_id,
        )
        assert said.payload["text"] == "My degree is from OSU."
        assert said.payload["labels"]["classification"] == "sincere"
        (spoken,) = await rows(
            engine,
            "SELECT classification, belief_relation FROM utterances WHERE story_id = :s "
            "AND speaker_character_id = :b",
            s=edu.story_id,
            b=edu.bobby,
        )
        assert (spoken.classification, spoken.belief_relation) == ("sincere", "consistent")


# -- M12: the repair budget per render, the drop rule and the per-beat cause (#1014) ----------

SEVENTY = "I am seventy years old."
"""A say whose age claim canon (64) contradicts, and that rungs 1 and 2 cannot fix."""
NARRATED_SEVENTY = "{name} is seventy years old."
SAY_REGENERATED = "I'd rather not say."
NARRATION_REGENERATED = "The fire crackles."
FALLBACK_SUFFIX = "changes the subject."
BUDGET_CAP = 9
"""A call budget with room for two primaries' planned calls, their salience and one
regeneration (#1014): at 8 neither primary's flagged beat regenerates."""


def _old(kind: str = "say", text_: str = SEVENTY) -> dict[str, Any]:
    fields: dict[str, Any] = {"intent": "sincere"} if kind == "say" else {}
    return beat(kind, text_, propositions=[claim("I", "age", "70")], **fields)


def _narrated_old(name: str) -> dict[str, Any]:
    return beat(
        "narration", NARRATED_SEVENTY.format(name=name), propositions=[claim(name, "age", "70")]
    )


async def _cast(
    client: Any, engine: AsyncEngine, *names: str, model_calls: int = 50, **settings: Any
) -> tuple[str, dict[str, str]]:
    """Wren (the player) and ``names``, each 64 in canon, rendered per character."""
    story, ids = await structured_story(client, "Wren", *names)
    for name in names:
        await age_fact(engine, story, ids[name], 64)
    await ms.per_character(engine, story, model_calls=model_calls, **settings)
    return story, ids


def _regenerations(outcome: str) -> float:
    return (
        REGISTRY.get_sample_value("epistrel_repair_regenerations_total", {"outcome": outcome})
        or 0.0
    )


def _renders(regenerated: bool) -> float:
    label = "true" if regenerated else "false"
    return REGISTRY.get_sample_value("epistrel_repair_renders_total", {"regenerated": label}) or 0.0


def _repairs_by_rung() -> dict[str, float]:
    """``epistrel_repairs_total`` by ``rung``, summed over its classes."""
    found: dict[str, float] = {}
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == "epistrel_repairs_total":
                rung = sample.labels["rung"]
                found[rung] = found.get(rung, 0.0) + sample.value
    return found


def _repairs_since(before: dict[str, float]) -> dict[str, float]:
    """The rungs ``epistrel_repairs_total`` counted since ``before``, each with its delta."""
    return {
        rung: value - before.get(rung, 0.0)
        for rung, value in _repairs_by_rung().items()
        if value != before.get(rung, 0.0)
    }


def _degraded(step: str) -> float:
    return REGISTRY.get_sample_value("epistrel_turn_degradation_total", {"step": step}) or 0.0


def _texts(played: dict[str, Any]) -> list[str]:
    return [b["text"] for b in played["beats"] if str(b["kind"]).startswith("beat_")]


def _repair_details(played: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in played["degradation_details"] if "beat" in d]


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_render_budget(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1014 (FR-REPAIR-5): ``repair.max_attempts`` counts per render. Under ``max_attempts:
    1`` the narrator's render and the first primary's render each regenerate their flagged
    beat (before #1014 they shared one attempt and the second fell back). Three primaries, one
    flagged beat each: three regenerations, all accepted. Complement: a render with two
    flagged beats regenerates only the first; the second is ``repair_deterministic``
    (``max_attempts``) and, its speaker having a line, is dropped."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- the narrator's render and the first primary's: one regeneration each
        story, ids = await _cast(client, engine, "Ada", "Bo")
        stub.reset()
        es.narrator(stub, es.render(_narrated_old("Ada")))
        ms.renders_for(stub, "Ada", ms.render(_old()))
        ms.renders_for(stub, "Bo", ms.render(beat("say", "Evening, all.", intent="sincere")))
        # The first match wins: Ada's regeneration prompt also holds the narration before it.
        stub.when(SEVENTY, _regenerated(SAY_REGENERATED), purpose="regeneration")
        stub.when("Ada is seventy", _regenerated(NARRATION_REGENERATED), purpose="regeneration")
        accepted = _regenerations("accepted")
        played = (
            await respond(
                client,
                story,
                body(
                    ids["Wren"],
                    None,
                    do="looks around",
                    render_character_ids=[ids["Ada"], ids["Bo"]],
                ),
            )
        ).json()
        texts = _texts(played)
        assert NARRATION_REGENERATED in texts, texts
        assert SAY_REGENERATED in texts, texts
        assert not [t for t in texts if t.endswith(FALLBACK_SUFFIX) or t == "A moment passes."]
        assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 2
        report = await turn_report(engine, story)
        assert report["repair"] == {
            "renders": 2,
            "renders_regenerated": 2,
            "regenerations": 2,
            "fallbacks": 0,
            "dropped": 0,
        }
        assert _regenerations("accepted") - accepted == 2

        # -- three primaries, one flagged beat each: three regenerations (regression)
        story, ids = await _cast(client, engine, "Ada", "Bo", "Cy")
        stub.reset()
        for name in ("Ada", "Bo", "Cy"):
            ms.renders_for(stub, name, ms.render(_old()))
        stub.script("regeneration", [_regenerated(SAY_REGENERATED)] * 3)
        accepted, regenerated = _regenerations("accepted"), _renders(True)
        played = (
            await respond(
                client,
                story,
                body(
                    ids["Wren"], "Well?", render_character_ids=[ids[n] for n in ("Ada", "Bo", "Cy")]
                ),
            )
        ).json()
        assert _texts(played).count(SAY_REGENERATED) == 3, _texts(played)
        assert _regenerations("accepted") - accepted == 3
        assert _renders(True) - regenerated == 3
        assert (await turn_report(engine, story))["repair"]["renders_regenerated"] == 3

        # -- complement: two flagged beats in one render; only the first regenerates
        story, ids = await _cast(client, engine, "Ada", "Bo")
        stub.reset()
        ms.renders_for(stub, "Ada", ms.render(_old(), _old(text_="Seventy years old, I am.")))
        ms.renders_for(stub, "Bo", ms.render(beat("say", "Evening, all.", intent="sincere")))
        stub.script("regeneration", [_regenerated(SAY_REGENERATED)])
        skipped = _regenerations("skipped_max_attempts")
        played = (
            await respond(
                client,
                story,
                body(ids["Wren"], "Well?", render_character_ids=[ids["Ada"], ids["Bo"]]),
            )
        ).json()
        assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 1
        assert _regenerations("skipped_max_attempts") - skipped >= 1
        assert "repair_deterministic" in played["degradation"]
        details = _repair_details(played)
        second = [d for d in details if d["step"] == "repair_deterministic"]
        assert [(d["cause"], d["rung"]) for d in second] == [("max_attempts", "dropped")], details
        assert not [t for t in _texts(played) if t.endswith(FALLBACK_SUFFIX)]


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_render_budget_limit(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1014: the per-render budget never spends past the turn's call budget. With a cap that
    leaves room for one regeneration, the first primary's flagged beat regenerates and the
    second's is ``repair_deterministic`` (``budget``); no call goes over the cap. #1052: two
    renders ran the ladder and one regenerated, so ``epistrel_repair_renders_total`` counts
    one ``regenerated="true"`` and one ``"false"``, and ``turn_report.repair`` has ``renders``
    above ``renders_regenerated`` and one regeneration for two repairs."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        cap = BUDGET_CAP
        story, ids = await _cast(client, engine, "Ada", "Bo", model_calls=cap)
        stub.reset()
        ms.renders_for(stub, "Ada", ms.render(_old()))
        ms.renders_for(stub, "Bo", ms.render(_old()))
        stub.script("regeneration", [_regenerated(SAY_REGENERATED)] * 2)
        skipped = _regenerations("skipped_budget")
        regenerated, unregenerated = _renders(True), _renders(False)
        answer = await respond(
            client, story, body(ids["Wren"], "Well?", render_character_ids=[ids["Ada"], ids["Bo"]])
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 1
        assert _regenerations("skipped_budget") - skipped == 1
        details = [d for d in _repair_details(played) if d["step"] == "repair_deterministic"]
        assert [d["cause"] for d in details] == ["budget"], played["degradation_details"]
        report = await turn_report(engine, story)
        assert report["budget"]["used"] <= cap, report["budget"]
        assert (_renders(True) - regenerated, _renders(False) - unregenerated) == (1, 1)
        assert len(report["repairs"]) == 2, report["repairs"]
        assert report["repair"] == {
            "renders": 2,
            "renders_regenerated": 1,
            "regenerations": 1,
            "fallbacks": 1,
            "dropped": 0,
        }


KNOCK_TEXT = "Someone knocks at the door."
"""The knock fixture's salient ``do`` (``tests/commit_support``): who knocked is open, so the
turn owes a follow-up render."""
RAIN = "Rain taps the window."


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_render_budget_follow_up(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1039 (#1014, FR-REPAIR-5): the last primary and the follow-up render are two renders,
    each with its own ``repair.max_attempts``. Sequential turn, ``max_attempts: 1``: Bo (the
    last primary) knocks, which owes a follow-up, and says a flagged line; the follow-up
    narrates a flagged beat. Both regenerate and neither falls back (before #1014 they shared
    one attempt, and the follow-up's beat was ``skipped_max_attempts``)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _cast(client, engine, "Ada", "Bo")
        async with engine.begin() as conn:
            await store.add_entity(conn, story, "item", "door")
            await store.add_entity(conn, story, "person", "mailman")
            await registry.auto_register(conn, story, "knocked_at")
        stub.reset()
        ms.renders_for(stub, "Ada", ms.render(beat("say", "Evening, all.", intent="sincere")))
        ms.renders_for(stub, "Bo", ms.render(beat("do", KNOCK_TEXT), _old()))
        stub.when(
            "## Settle now",
            es.render(
                beat(
                    "narration",
                    "The mailman knocked at the door.",
                    propositions=[claim("mailman", "knocked_at", "door")],
                ),
                _narrated_old("Bo"),
            ),
            purpose="generation",
        )
        stub.when(
            KNOCK_TEXT,
            commit_support.salience_answer((3, KNOCK_TEXT, commit_support.KNOCK)),
            purpose="salience_detection",
        )
        # The first match wins: the follow-up's regeneration prompt holds Bo's line before it.
        stub.when("Bo is seventy", _regenerated(RAIN), purpose="regeneration")
        stub.when(SEVENTY, _regenerated(SAY_REGENERATED), purpose="regeneration")
        accepted = _regenerations("accepted")
        skipped = _regenerations("skipped_max_attempts")
        answer = await respond(
            client, story, body(ids["Wren"], "Well?", render_character_ids=[ids["Ada"], ids["Bo"]])
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        report = await turn_report(engine, story)
        follow = report["commit"]["follow_up"]
        assert follow["ran"] is True, follow
        texts = _texts(played)
        assert SAY_REGENERATED in texts, texts
        assert RAIN in texts, texts
        assert [b["kind"] for b in played["beats"] if b["text"] == RAIN] == ["beat_narration"]
        assert next(b["beat"] for b in played["beats"] if b["text"] == RAIN) in follow["beats"]
        assert not [t for t in texts if t.endswith(FALLBACK_SUFFIX) or t == "A moment passes."]
        assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 2
        assert report["repair"] == {
            "renders": 2,
            "renders_regenerated": 2,
            "regenerations": 2,
            "fallbacks": 0,
            "dropped": 0,
        }
        assert _regenerations("accepted") - accepted == 2
        assert _regenerations("skipped_max_attempts") == skipped
        assert "repair_deterministic" not in played["degradation"]


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_drop_rule(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1014 (FR-REPAIR-5): a beat that fails repair is dropped when its speaker has a real
    line, and a speaker keeps at most one fallback line. (a) The #1014 shape: Ada's failing
    ``do``, failing ``say`` and passing ``say``: only the passing ``say`` is committed, with
    two ``repair_dropped`` (``speaker_has_line``). (b) Both of Ada's beats fail: one fallback
    line, one ``repair_dropped`` (``duplicate_fallback``). (c) Complement: one failing beat
    still gets the fallback line. (d) A failing narration next to an accepted one is
    dropped."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        still = _regenerated("I'm 71.", claim("I", "age", "71"))

        # -- (a) a failing do, a failing say and a passing say
        story, ids, _ = await _age_story(client, engine)
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    _old("do", "Ada says she is seventy years old."),
                    _old(),
                    beat("say", "The fire is warm.", intent="sincere"),
                )
            ],
        )
        stub.script("regeneration", [still])
        counted = _repairs_by_rung()
        played = (await respond(client, story, body(ids["Wren"], "How old are you?"))).json()
        assert _texts(played) == ["The fire is warm."], _texts(played)
        # #1038: a dropped beat counts as ``rung="dropped"`` only, not also at its numeric rung.
        assert _repairs_since(counted) == {"dropped": 2.0}
        assert FALLBACK_SUFFIX not in played["message"]["text"]
        dropped = [d for d in _repair_details(played) if d["step"] == "repair_dropped"]
        assert [(d["cause"], d["rung"], d["character_id"]) for d in dropped] == [
            ("speaker_has_line", "dropped", ids["Ada"])
        ] * 2
        assert "safe_fallback" not in played["degradation"]
        report = await turn_report(engine, story)
        assert report["repair"]["dropped"] == 2
        assert report["repair"]["fallbacks"] == 0
        assert {(r["rung"], r["dropped"]) for r in report["repairs"]} == {(4, True)}

        # -- (b) both of the speaker's beats fail: one fallback line is kept
        story, ids, _ = await _age_story(client, engine)
        stub.reset()
        stub.script(
            "generation", [render(_old("do", "Ada says she is seventy years old."), _old())]
        )
        stub.script("regeneration", [still])
        played = (await respond(client, story, body(ids["Wren"], "How old are you?"))).json()
        texts = _texts(played)
        assert [t for t in texts if t.endswith(FALLBACK_SUFFIX)] == ["Ada changes the subject."]
        steps = [(d["step"], d.get("cause")) for d in _repair_details(played)]
        assert ("repair_dropped", "duplicate_fallback") in steps, steps
        assert ("safe_fallback", "repair") in steps, steps

        # -- (c) complement: a speaker's only beat fails: the fallback line stays
        story, ids, _ = await _age_story(client, engine)
        stub.reset()
        stub.script("generation", [render(_old())])
        stub.script("regeneration", [still])
        counted = _repairs_by_rung()
        played = (await respond(client, story, body(ids["Wren"], "How old are you?"))).json()
        assert played["message"]["text"] == 'Ada: "Ada changes the subject."'
        # #1038's complement: a committed fallback counts at its own rung, never as dropped.
        assert _repairs_since(counted) == {"safe_fallback": 1.0}
        assert "repair_dropped" not in played["degradation"]

        # -- (d) a failing narration next to an accepted one is dropped
        story, ids, _ = await _age_story(client, engine)
        stub.reset()
        es.narrator(
            stub, es.render(_narrated_old("Ada"), beat("narration", "Rain taps the window."))
        )
        ms.renders_for(stub, "Ada", ms.render(beat("say", "Evening.", intent="sincere")))
        stub.when("Ada is seventy", still, purpose="regeneration")
        played = (await respond(client, story, body(ids["Wren"], None, do="looks around"))).json()
        texts = _texts(played)
        assert "Rain taps the window." in texts, texts
        assert "A moment passes." not in texts
        (entry,) = [d for d in _repair_details(played) if d["step"] == "repair_dropped"]
        assert (entry["cause"], entry["character_id"]) == ("speaker_has_line", None)


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_per_beat_cause(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1014 (FR-REPAIR-5): a fallback caused by an ``other_character_secret`` denial reports
    its beat, speaker, rung and violation codes in ``degradation_details``, in ``respond`` and
    in the history read of the turn (omniscient, and a character viewpoint that sees the beat);
    no entry carries beat text, and no log line carries it (invariant 3)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        stub.reset()
        told = "Bobby holds the brass key."
        ms.renders_for(
            stub,
            "Jenni",
            ms.render(
                beat(
                    "say",
                    told,
                    intent="sincere",
                    propositions=[claim("Bobby", "holds", "the brass key")],
                )
            ),
        )
        stub.script("regeneration", [_regenerated(told, claim("Bobby", "holds", "the brass key"))])
        with caplog.at_level(logging.DEBUG):
            answer = await respond(
                client,
                room.story_id,
                body(room.cal, "Who has it?", render_character_ids=[room.jenni]),
            )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        (entry,) = [d for d in _repair_details(played) if d["step"] == "safe_fallback"]
        said = next(b for b in played["beats"] if b["kind"] == "beat_say")
        assert entry == {
            "step": "safe_fallback",
            "cause": "repair",
            "beat": said["beat"],
            "character_id": room.jenni,
            "rung": "safe_fallback",
            "violations": ["unpermitted:other_character_secret"],
        }
        # Entries that are not a repair's carry none of the per-beat fields.
        assert all(
            set(d) == {"step", "cause"} for d in played["degradation_details"] if "beat" not in d
        )
        assert told not in json.dumps(played["degradation_details"])
        assert told not in caplog.text

        for viewpoint in ("omniscient", f"character:{room.cal}"):
            response = await client.get(
                f"/stories/{room.story_id}/history",
                params={"viewpoint": viewpoint},
                headers=READ,
            )
            assert response.status_code == 200, response.text
            (turn,) = [t for t in response.json()["turns"] if t["turn"] == played["turn"]]
            assert entry in turn["degradation_details"], (viewpoint, turn)


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_failed_regeneration(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1051: Ada says and thinks the contradicted age. The say's regeneration repeats the
    claim, so it is rejected and counts ``epistrel_repair_regenerations_total{outcome="failed"}``
    (never ``accepted``); the say falls back. #1053: the think, past ``max_attempts``, is
    dropped. Its repair entry is in the omniscient history read and absent from Wren's, who
    cannot see Ada's thoughts; the say's fallback entry is in both (FR-REPAIR-5(c),
    ``visible_details`` at the request's viewpoint)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _cast(client, engine, "Ada")
        stub.reset()
        ms.renders_for(stub, "Ada", ms.render(_old(), _old("think")))
        stub.script("regeneration", [_regenerated(SEVENTY, claim("I", "age", "70"))])
        failed, accepted = _regenerations("failed"), _regenerations("accepted")
        answer = await respond(
            client, story, body(ids["Wren"], "Well?", render_character_ids=[ids["Ada"]])
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 1
        assert (_regenerations("failed") - failed, _regenerations("accepted") - accepted) == (1, 0)
        assert SEVENTY not in _texts(played), _texts(played)
        said = next(b["beat"] for b in played["beats"] if b["kind"] == "beat_say")
        steps = [("repair_deterministic", "max_attempts"), ("safe_fallback", "repair")]
        report = await turn_report(engine, story)
        stored = [d for d in report["degradation_details"] if "beat" in d]
        assert [(d["beat"], d["kind"], d["rung"]) for d in stored] == [
            (said, "say", "safe_fallback"),
            (said, "say", "safe_fallback"),
            (said + 1, "think", "dropped"),
            (said + 1, "think", "dropped"),
        ], stored
        assert [(d["step"], d["cause"]) for d in stored] == steps * 2, stored
        everything = [{k: v for k, v in d.items() if k != "kind"} for d in stored]
        seen = [d for d in everything if d["beat"] == said]
        # respond reads at the request's viewpoint (Wren's): the think's entries are not there.
        assert _repair_details(played) == seen, played["degradation_details"]
        for viewpoint, expected in (
            ("omniscient", everything),
            (f"character:{ids['Wren']}", seen),
        ):
            response = await client.get(
                f"/stories/{story}/history", params={"viewpoint": viewpoint}, headers=READ
            )
            assert response.status_code == 200, response.text
            (turn,) = [t for t in response.json()["turns"] if t["turn"] == played["turn"]]
            found = [d for d in turn["degradation_details"] if "beat" in d]
            assert found == expected, (viewpoint, found)


# -- M12: copied lines across speakers (#1013) -----------------------------------------------

MALIK_LINE = (
    "Hey, we lost, that's the game. Nobody gets to sulk for long out here. "
    "Shake it off, we need every body tonight."
)
SAM_LINE = "Hey, we lost, that's the game. Tomorrow we train twice as hard as them."
"""Malik's first sentence (6 words), then an original one: #1013's near-copy."""
JARED_OWN = "I'm not ready to let this one go, honestly."
SAM_OWN = "We will get them next time, I promise you all."
NO_COPY = "Do not repeat what Malik just said; respond in your own words."
TRIBE = ("Malik", "Jared", "Sam")


def _says(text_: str) -> str:
    return ms.render(beat("say", text_, intent="sincere"))


def _lines(played: dict[str, Any], ids: dict[str, str]) -> dict[str, list[str]]:
    """Each named character's committed beat texts."""
    names = {v: k for k, v in ids.items()}
    found: dict[str, list[str]] = {}
    for b in played["beats"]:
        if str(b["kind"]).startswith("beat_") and b.get("actor_character_id") in names:
            found.setdefault(names[b["actor_character_id"]], []).append(b["text"])
    return found


def _copied(outcome: str) -> float:
    return REGISTRY.get_sample_value("epistrel_copied_lines_total", {"outcome": outcome}) or 0.0


def _regeneration_prompts(stub: Any) -> list[str]:
    return [
        "\n".join(m.content for m in c.messages) for c in stub.calls if c.purpose == "regeneration"
    ]


@pytest.mark.part("M12", "FR-MULTI-11; FR-REPAIR-5")
async def test_i_12__m12_copied_lines(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1013 (FR-MULTI-11): the #1013 evidence on the stub, three primaries in ``sequential``
    order. Jared's render returns Malik's line word for word and Sam's Malik's first sentence
    plus an original one. (a) Each copy is regenerated once, with the ``no_copy`` constraint
    naming Malik; with original regenerations the three commit distinct lines, and
    ``turn_report.multi.copied`` is 2. (b) A regeneration that copies again drops Jared's beat
    (``repair_dropped``, ``copied_line``, ``violations: ["copied_line"]`` in the omniscient
    history read), with no fallback line for him, Jared still in the turn's order; the turn
    commits, and the rejected render records the source beat and speaker (ids only)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- (a) original regenerations: three distinct lines
        story, ids = await _cast(client, engine, *TRIBE)
        stub.reset()
        ms.renders_for(stub, "Malik", _says(MALIK_LINE))
        ms.renders_for(stub, "Jared", _says(MALIK_LINE))
        ms.renders_for(stub, "Sam", _says(SAM_LINE))
        stub.script("regeneration", [_regenerated(JARED_OWN), _regenerated(SAM_OWN)])
        regenerated = _copied("regenerated")
        answer = await respond(
            client,
            story,
            body(ids["Wren"], "Well that sucked.", render_character_ids=[ids[n] for n in TRIBE]),
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert _lines(played, ids) == {
            "Malik": [MALIK_LINE],
            "Jared": [JARED_OWN],
            "Sam": [SAM_OWN],
        }, _lines(played, ids)
        prompts = _regeneration_prompts(stub)
        assert len(prompts) == 2
        assert all(NO_COPY in p for p in prompts)
        assert "repair_dropped" not in played["degradation"]
        report = await turn_report(engine, story)
        assert report["multi"]["copied"] == 2
        assert len(report["multi"]["regenerated_beats"]) == 2
        assert _copied("regenerated") - regenerated == 2

        # -- (b) the regeneration copies again: Jared's beat is dropped, never the fallback
        story, ids = await _cast(client, engine, *TRIBE)
        stub.reset()
        ms.renders_for(stub, "Malik", _says(MALIK_LINE))
        ms.renders_for(stub, "Jared", _says(MALIK_LINE))
        ms.renders_for(stub, "Sam", _says(SAM_LINE))
        stub.script("regeneration", [_regenerated(MALIK_LINE), _regenerated(SAM_OWN)])
        dropped = _copied("dropped")
        answer = await respond(
            client,
            story,
            body(ids["Wren"], "Well that sucked.", render_character_ids=[ids[n] for n in TRIBE]),
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert _lines(played, ids) == {"Malik": [MALIK_LINE], "Sam": [SAM_OWN]}, _lines(played, ids)
        assert FALLBACK_SUFFIX not in json.dumps(played["beats"])
        assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 2
        assert "repair_dropped" in played["degradation"]
        # Jared has no beat left for the player's viewpoint to see, so the per-beat entry reads
        # in the omniscient history (#1014's viewpoint filter).
        response = await client.get(
            f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
        )
        assert response.status_code == 200, response.text
        (turn,) = [t for t in response.json()["turns"] if t["turn"] == played["turn"]]
        (entry,) = [d for d in turn["degradation_details"] if d["step"] == "repair_dropped"]
        assert {k: entry[k] for k in ("cause", "character_id", "rung", "violations")} == {
            "cause": "copied_line",
            "character_id": ids["Jared"],
            "rung": "dropped",
            "violations": ["copied_line"],
        }
        assert _copied("dropped") - dropped == 1
        report = await turn_report(engine, story)
        assert ids["Jared"] in report["multi"]["order"]
        assert report["multi"]["copied"] == 2
        malik_beat = next(
            b["beat"] for b in played["beats"] if b.get("actor_character_id") == ids["Malik"]
        )
        sources = [
            v["copied_from"]
            for e in await _audit(engine, story, "render_rejected")
            for v in e.payload.verdicts
            if v.get("cause") == "copied_line"
        ]
        assert {"beat": malik_beat, "character_id": ids["Malik"]} in sources
        assert all(set(s) == {"beat", "character_id"} for s in sources)


@pytest.mark.part("M12", "FR-MULTI-11; FR-REPAIR-5")
async def test_i_12__m12_copied_lines_complements(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1013 complements: a 4-word reply said by two characters is no copy and both commit;
    beats sharing no sentence with another speaker's are never regenerated (no
    ``regeneration`` call at all); ``turn_report.multi.copied`` is 0."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _cast(client, engine, *TRIBE)
        stub.reset()
        short = "I hear you, man."
        ms.renders_for(stub, "Malik", _says(short))
        ms.renders_for(stub, "Jared", _says(short))
        ms.renders_for(stub, "Sam", _says(SAM_OWN))
        answer = await respond(
            client,
            story,
            body(ids["Wren"], "Well that sucked.", render_character_ids=[ids[n] for n in TRIBE]),
        )
        assert answer.status_code == 200, answer.text
        assert _lines(answer.json(), ids) == {
            "Malik": [short],
            "Jared": [short],
            "Sam": [SAM_OWN],
        }
        assert not [c for c in stub.calls if c.purpose == "regeneration"]
        assert (await turn_report(engine, story))["multi"]["copied"] == 0


PLAYER_LINE = "I can not believe we lost that one."
"""Wren's own sentence (8 words), which two speakers repeat in #1048's echo."""


@pytest.mark.part("M12", "FR-MULTI-11; FR-REPAIR-5")
async def test_i_12__m12_copied_lines_player_echo(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1048 (#1013's "a line repeating the player's input is out of scope"): Malik and Jared
    each repeat Wren's sentence. Neither copies the other, since the sentence is the player's
    (the played turn passes the player's ``say``/``do`` into the copy scope): both commit, no
    ``regeneration`` call, ``turn_report.multi.copied`` is 0."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _cast(client, engine, "Malik", "Jared")
        stub.reset()
        malik = f"{PLAYER_LINE} Me neither."
        jared = f"{PLAYER_LINE} We had them."
        ms.renders_for(stub, "Malik", _says(malik))
        ms.renders_for(stub, "Jared", _says(jared))
        copied = _copied("regenerated")
        answer = await respond(
            client,
            story,
            body(ids["Wren"], PLAYER_LINE, render_character_ids=[ids["Malik"], ids["Jared"]]),
        )
        assert answer.status_code == 200, answer.text
        assert _lines(answer.json(), ids) == {"Malik": [malik], "Jared": [jared]}
        assert not [c for c in stub.calls if c.purpose == "regeneration"]
        assert _copied("regenerated") == copied
        assert (await turn_report(engine, story))["multi"]["copied"] == 0


ECHOED = "A long slow nod toward the door."
"""A 6-word sentence: a copy under #1013 when another speaker's later beat repeats it."""


@pytest.mark.part("M12", "FR-MULTI-11; FR-REPAIR-5")
async def test_i_12__m12_copied_lines_single_primary(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1040 (#1013's scope, FR-MULTI-11): a single-primary turn is not checked for copied
    lines. Jenni is the only primary; the reaction she narrates for Ada repeats a 6-word
    sentence of Jenni's own ``do``. Both beats commit as rendered: no ``copied_line``
    violation, no regeneration, no ``epistrel_copied_lines_total`` count, and
    ``turn_report.multi.copied`` is 0."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine)
        stub.reset()
        ms.renders_for(
            stub,
            "Jenni",
            rs.render(beat("do", ECHOED), reactions=[rs.reaction(b["Ada"], ECHOED)]),
        )
        regenerated, dropped = _copied("regenerated"), _copied("dropped")
        answer = await respond(
            client,
            b.story_id,
            body(b["Cal"], "Well that sucked.", render_character_ids=[b["Jenni"]]),
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert _lines(played, b.ids) == {"Jenni": [ECHOED], "Ada": [ECHOED]}, played["beats"]
        assert not [c for c in stub.calls if c.purpose == "regeneration"]
        assert "repair_dropped" not in played["degradation"]
        assert not [d for d in played["degradation_details"] if "copied_line" in json.dumps(d)]
        assert (_copied("regenerated"), _copied("dropped")) == (regenerated, dropped)
        report = await turn_report(engine, b.story_id)
        assert not report.get("multi", {}).get("copied")
        assert "repairs" not in report
        copied = [
            v
            for e in await _audit(engine, b.story_id, "render_rejected")
            for v in e.payload.verdicts
            if v.get("cause") == "copied_line"
        ]
        assert copied == []


@pytest.mark.part("M12", "FR-MULTI-11; FR-REPAIR-5")
async def test_i_12__m12_copied_lines_lite(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1040 (#1013's scope, FR-MULTI-11): a Lite turn is not checked for copied lines. Its one
    reply has Malik and Jared say the same 6-word sentence; it commits as generated, with one
    ``generation`` call, no regeneration, no ``epistrel_copied_lines_total`` count and no
    ``multi`` or ``repairs`` in the turn report."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        created = await create(client, story_body("Wren", "Malik", "Jared"))
        story = created["story_id"]
        ids = {c["name"]: c["id"] for c in created["characters"]}
        stub.reset()
        reply = f'Malik: "{ECHOED}"\n\nJared: "{ECHOED}"'
        stub.script("generation", [reply])
        regenerated, dropped = _copied("regenerated"), _copied("dropped")
        answer = await respond(
            client,
            story,
            body(ids["Wren"], "Well that sucked.", render_character_ids=[ids["Malik"]]),
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert played["message"]["text"] == reply
        assert [c.purpose for c in stub.calls if c.purpose in ("generation", "regeneration")] == [
            "generation"
        ]
        assert "repair_dropped" not in played["degradation"]
        assert (_copied("regenerated"), _copied("dropped")) == (regenerated, dropped)
        report = await turn_report(engine, story)
        assert "multi" not in report
        assert "repairs" not in report


@pytest.mark.part("M12", "FR-MULTI-8; FR-REPAIR-5")
async def test_i_12__m12_copied_lines_simultaneous(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1013 (FR-MULTI-8): in ``simultaneous`` mode two primaries render the same line; the
    second in request order is flagged and regenerated, the first keeps its line."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _cast(client, engine, "Malik", "Jared")
        stub.reset()
        ms.renders_for(stub, "Malik", _says(MALIK_LINE))
        ms.renders_for(stub, "Jared", _says(MALIK_LINE))
        stub.script("regeneration", [_regenerated(JARED_OWN)])
        answer = await respond(
            client,
            story,
            body(
                ids["Wren"],
                "Well that sucked.",
                render_character_ids=[ids["Malik"], ids["Jared"]],
                interaction_mode="simultaneous",
            ),
        )
        assert answer.status_code == 200, answer.text
        assert _lines(answer.json(), ids) == {"Malik": [MALIK_LINE], "Jared": [JARED_OWN]}
        (prompt,) = _regeneration_prompts(stub)
        assert NO_COPY in prompt
        report = await turn_report(engine, story)
        assert report["multi"]["mode"] == "simultaneous"
        assert report["multi"]["copied"] == 1


@pytest.mark.part("M12", "FR-MULTI-8; FR-REPAIR-5")
async def test_i_12__m12_copied_lines_simultaneous_middle(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1049 (FR-MULTI-8): in ``simultaneous`` mode with three primaries, the middle one
    (Jared) copies the first. A non-last primary's copy is checked at its own repair, before
    the last primary renders: Jared's copy is regenerated, Malik and Sam keep their lines,
    ``turn_report.multi.copied`` is 1."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _cast(client, engine, *TRIBE)
        stub.reset()
        ms.renders_for(stub, "Malik", _says(MALIK_LINE))
        ms.renders_for(stub, "Jared", _says(MALIK_LINE))
        ms.renders_for(stub, "Sam", _says(SAM_OWN))
        stub.script("regeneration", [_regenerated(JARED_OWN)])
        answer = await respond(
            client,
            story,
            body(
                ids["Wren"],
                "Well that sucked.",
                render_character_ids=[ids[n] for n in TRIBE],
                interaction_mode="simultaneous",
            ),
        )
        assert answer.status_code == 200, answer.text
        assert _lines(answer.json(), ids) == {
            "Malik": [MALIK_LINE],
            "Jared": [JARED_OWN],
            "Sam": [SAM_OWN],
        }
        (prompt,) = _regeneration_prompts(stub)
        assert NO_COPY in prompt
        report = await turn_report(engine, story)
        assert report["multi"]["mode"] == "simultaneous"
        assert report["multi"]["copied"] == 1


VOTE_LINE = "Good idea. First vote's usually the most important one."
"""#944's line: the render target's, which an escalated character repeated word for word."""


@pytest.mark.part("M12", "FR-MULTI-11; FR-REPAIR-5")
async def test_i_12__m12_copied_lines_escalated(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#944 (FR-MULTI-11, FR-MULTI-10): with interjections off, only Bobby is a render target.
    Ada, present and default-``reactive``, reacts with speech longer than an interjection, so she
    escalates (respond's ``roles``: ``primary``, ``escalated``) and renders her own line,
    which repeats Bobby's word for word. The copy is regenerated (``no_copy`` naming Bobby),
    the turn commits two distinct lines, ``multi.copied`` is 1."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine)
        stub.reset()
        long = rs.reaction(
            b["Ada"], '"I told you this would happen, Bobby, every time."', kind="interjection"
        )
        ms.renders_for(
            stub,
            "Bobby",
            rs.render(beat("say", VOTE_LINE, intent="sincere"), reactions=[long]),
        )
        ms.renders_for(stub, "Ada", rs.render(beat("say", VOTE_LINE, intent="sincere")))
        stub.script("regeneration", [_regenerated(JARED_OWN)])
        answer = await respond(
            client,
            b.story_id,
            body(b["Cal"], "Let's vote first.", render_character_ids=[b["Bobby"]]),
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert rs.character_renders(stub) == ["Bobby", "Ada"]
        assert played["roles"][b["Ada"]] == {"role": "primary", "source": "escalated"}
        ids = {"Bobby": b["Bobby"], "Ada": b["Ada"]}
        lines = _lines(played, ids)
        assert lines["Bobby"] == [VOTE_LINE], lines
        assert JARED_OWN in lines["Ada"], lines
        assert VOTE_LINE not in lines["Ada"], lines  # her narrated reaction may stay
        (prompt,) = _regeneration_prompts(stub)
        assert "Do not repeat what Bobby just said" in prompt
        report = await turn_report(engine, b.story_id)
        assert report["multi"]["copied"] == 1


NAME_ONLY_SEVENTY = "Wren. I am 70 years old."
"""#1019: a say whose second sentence (canon: 64) narrowing drops, leaving only a name."""
WORDED_SEVENTY = "Okay, Wren. I am 70 years old."
ALIAS_SEVENTY = "Birdie. I am 70 years old."
"""#1085: the same say, opened with Wren's entity alias "Birdie" (not Wren's character name),
so only ``_name_words``' alias query makes the narrowed "Birdie." name-only."""


async def _alias(engine: AsyncEngine, story: str, character_id: str, alias: str) -> None:
    """Give ``character_id``'s entity the alias ``alias`` (an ``entity_aliases`` row)."""
    async with engine.begin() as conn:
        added = await conn.execute(
            text(
                "INSERT INTO entity_aliases (id, story_id, entity_id, alias, source) "
                "SELECT :id, story_id, id, :alias, 'canonized' FROM entities "
                "WHERE story_id = :s AND character_id = :c"
            ),
            {"id": new_ulid(), "alias": alias, "s": story, "c": character_id},
        )
        assert added.rowcount == 1


@pytest.mark.part("M12", "FR-REPAIR-2")
async def test_i_12__m12_name_only(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#1019 (FR-REPAIR-2): a narrowing that leaves only a name is not a line. Ada's "Wren. I
    am 70 years old." (canon: 64) narrowed to "Wren." is not accepted at rung 1 (before #1019
    it committed): the next rung runs (substitution: "Wren. I am 64 years old.") and "Wren." is
    recorded as a rejected render with the ``name_only`` constraint. Complement: "Okay, Wren. I
    am 70 years old." is accepted at rung 1 as "Okay, Wren."; neither regenerates. #1085: an
    entity alias counts as a name: "Birdie. I am 70 years old.", where Birdie is Wren's alias,
    narrowed to "Birdie." is rejected the same way (``_name_words`` reads the aliases)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for said, kept in (
            (NAME_ONLY_SEVENTY, "Wren. I am 64 years old."),
            (WORDED_SEVENTY, "Okay, Wren."),
            (ALIAS_SEVENTY, "Birdie. I am 64 years old."),
        ):
            story, ids = await _cast(client, engine, "Ada")
            if said == ALIAS_SEVENTY:
                await _alias(engine, story, ids["Wren"], "Birdie")
            stub.reset()
            ms.renders_for(stub, "Ada", ms.render(_old(text_=said)))
            stub.script("regeneration", [_regenerated(SAY_REGENERATED)])
            played = (
                await respond(
                    client, story, body(ids["Wren"], "Well?", render_character_ids=[ids["Ada"]])
                )
            ).json()
            texts = _texts(played)
            assert kept in texts, texts
            assert "Wren." not in texts and "Birdie." not in texts
            rejected = [e.payload for e in await _audit(engine, story, "render_rejected")]
            name_only = [
                r for r in rejected if {"kind": "narrowing", "reason": "name_only"} in r.constraints
            ]
            if said == NAME_ONLY_SEVENTY:
                assert [(r.rung, r.text) for r in name_only] == [(1, "Wren.")]
            elif said == ALIAS_SEVENTY:
                assert [(r.rung, r.text) for r in name_only] == [(1, "Birdie.")]
            else:
                assert name_only == []
            assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 0


# -- M12: a staged turn's outcome step (#1033, #1034) ----------------------------------------

FORGE = "The forge glows."
TAKES = "Mira takes the coins."
EDGE = "Mind the edge."
WENT = "The purchase went through."
MIRA_SEVENTY = "Mira is seventy years old."
"""Canon has Mira at 64; no sentence is left to narrow and no number to substitute, so a
failed regeneration ends the beat at the safe fallback."""


async def _shop(client: Any, engine: AsyncEngine, epistrel: Any) -> tuple[str, str, Any]:
    """Cal buys from Mira (64 in canon); Cal's gold is owned by a ``synchronous`` write-capable
    provider, so a turn that spends it is staged (#212) and its outcome narrated by the outcome
    step. Returns the story, Cal and the scripted provider."""
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name, confirmation="synchronous"))
    fake = ps.ScriptedProvider(name, default=300).attach(epistrel)
    story, ids = await structured_story(client, "Cal", "Mira")
    await age_fact(engine, story, ids["Mira"], 64)
    await override(client, story, {"budgets": {"model_calls": 40}})
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    return story, ids["Cal"], fake


async def _purchase(
    client: Any,
    stub: Any,
    fake: Any,
    story: str,
    cal: str,
    *,
    do: str,
    outcome: dict[str, Any],
    said: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One staged purchase the game confirms. The intent narrates "The forge glows." when the
    narrator is triggered (``do`` names no one); Mira takes the coins (the gold effect) and
    says ``said``; the outcome step narrates ``outcome``. Every regeneration fails."""
    stub.reset()
    fake.script_writes("gold", "confirmed")
    stub.when(staged.OUTCOME_STEP, es.render(outcome), purpose="generation")
    mira = es.render(
        beat("do", TAKES, effects=[es.effect("gold", scope_key=cal, delta=-50, reason="a sword")]),
        said or beat("say", EDGE, intent="sincere"),
    )
    ms.renders_for(stub, "Mira", mira)
    es.narrator(stub, es.render(beat("narration", FORGE)))
    stub.fault("regeneration", "timeout", n=2)
    answer = await respond(client, story, body(cal, None, do=do))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def _seventy() -> dict[str, Any]:
    return beat("narration", MIRA_SEVENTY, propositions=[claim("Mira", "age", "70")])


def _narrations(played: dict[str, Any]) -> list[str]:
    return [b["text"] for b in played["beats"] if b["kind"] == "beat_narration"]


async def _history_details(client: Any, story: str, turn: int) -> list[dict[str, Any]]:
    response = await client.get(
        f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert response.status_code == 200, response.text
    (found,) = [t for t in response.json()["turns"] if t["turn"] == turn]
    return [d for d in found.get("degradation_details", []) if "beat" in d]


async def _close_shop(engine: AsyncEngine, story: str) -> None:
    await ps.drop_outbox_jobs(engine, story)
    await ss.set_level(engine, story, None)


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_outcome_step_drop(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#1033, #1034 (FR-REPAIR-5): the narrator has a real line in a staged turn's intent ("The
    forge glows."). The outcome step's narration contradicts canon and its regeneration fails,
    so the beat ends at the safe fallback. The intent's line counts for the drop rule
    (``render_outcome_step`` passes ``before=``), so the fallback is dropped: no narration but
    the intent's commits. The drop is reported ``repair_dropped``/``speaker_has_line`` in
    ``turn_report.staged.outcome_step.repairs``, in respond's ``degradation_details`` and in
    the history read of the turn (#1034: before, only the intent's repairs reached them)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, fake = await _shop(client, engine, epistrel)
        counted, degraded = _repairs_by_rung(), _degraded("repair_dropped")
        played = await _purchase(
            client, stub, fake, story, cal, do="buys the sword", outcome=_seventy()
        )
        assert _narrations(played) == [FORGE]
        assert MIRA_SEVENTY not in played["message"]["text"]
        report = await turn_report(engine, story)
        (repair,) = report["staged"]["outcome_step"]["repairs"]
        assert (repair["verdict"], repair["dropped"]) == ("contradicted", True)
        dropped = {
            "step": "repair_dropped",
            "cause": "speaker_has_line",
            "beat": repair["beat"],
            "character_id": None,
            "rung": "dropped",
            "violations": ["contradicted"],
        }
        details = _repair_details(played)
        assert dropped in details, details
        assert "repair_dropped" in played["degradation"]
        assert dropped in await _history_details(client, story, played["turn"])
        # #1037: the outcome step's repair is in the turn's totals and in the counters.
        assert report["repair"] == {
            "renders": 1,
            "renders_regenerated": 1,
            "regenerations": 1,
            "fallbacks": 0,
            "dropped": 1,
        }
        assert _repairs_since(counted) == {"dropped": 1.0}
        assert _degraded("repair_dropped") - degraded == 1
        await _close_shop(engine, story)


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_outcome_step_lone_fallback(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#1033's complement, #1034 (FR-REPAIR-5): Cal's action names Mira, so the narrator has no
    line in the intent. The outcome step's failing narration has no earlier line of its speaker
    to drop it for, so its safe fallback commits, reported ``safe_fallback`` with its beat,
    speaker, rung and violation codes in respond and in the history read; nothing is
    ``repair_dropped``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, fake = await _shop(client, engine, epistrel)
        counted, degraded = _repairs_by_rung(), _degraded("safe_fallback")
        played = await _purchase(
            client, stub, fake, story, cal, do="buys the sword from Mira", outcome=_seventy()
        )
        (fallback,) = _narrations(played)
        assert fallback not in (FORGE, MIRA_SEVENTY)
        report = await turn_report(engine, story)
        (repair,) = report["staged"]["outcome_step"]["repairs"]
        assert (repair["verdict"], repair["dropped"]) == ("contradicted", False)
        committed = next(b for b in played["beats"] if b["kind"] == "beat_narration")
        assert committed["beat"] == repair["beat"]
        entry = {
            "step": "safe_fallback",
            "cause": "repair",
            "beat": repair["beat"],
            "character_id": None,
            "rung": "safe_fallback",
            "violations": ["contradicted"],
        }
        details = _repair_details(played)
        assert entry in details, details
        assert not [d for d in details if d["step"] == "repair_dropped"]
        assert entry in await _history_details(client, story, played["turn"])
        # #1037: a committed outcome-step fallback is counted at its own rung.
        assert (report["repair"]["fallbacks"], report["repair"]["dropped"]) == (1, 0)
        assert _repairs_since(counted) == {"safe_fallback": 1.0}
        assert _degraded("safe_fallback") - degraded == 1
        await _close_shop(engine, story)


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_outcome_step_no_repair(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#1034's complement (FR-REPAIR-5): the intent repairs a beat (Mira's "I am seventy years
    old." fails and is dropped beside her real ``do``) and the outcome step repairs nothing.
    respond's and the history's per-beat entries are the intent's, each once, none at an
    outcome-step beat."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, fake = await _shop(client, engine, epistrel)
        said = beat(
            "say",
            "I am seventy years old.",
            intent="sincere",
            propositions=[claim("I", "age", "70")],
        )
        counted, degraded = _repairs_by_rung(), _degraded("repair_dropped")
        played = await _purchase(
            client,
            stub,
            fake,
            story,
            cal,
            do="buys the sword",
            outcome=beat("narration", WENT),
            said=said,
        )
        assert WENT in _narrations(played)
        report = await turn_report(engine, story)
        assert report["staged"]["outcome_step"]["repairs"] == []
        (first,) = report["staged"]["outcome_step"]["beats"]
        details = _repair_details(played)
        assert [d["step"] for d in details if d["step"] == "repair_dropped"] == ["repair_dropped"]
        assert all(d["beat"] < first for d in details), details
        assert len({json.dumps(d, sort_keys=True) for d in details}) == len(details)
        assert await _history_details(client, story, played["turn"]) == details
        # #1037's complement: an outcome step that repairs nothing adds nothing to the
        # intent's totals or counters.
        assert report["repair"]["dropped"] == 1
        assert _repairs_since(counted) == {"dropped": 1.0}
        assert _degraded("repair_dropped") - degraded == 1
        await _close_shop(engine, story)


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_outcome_step_counts(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#1037 (FR-REPAIR-5): both stages of a staged turn drop a beat. The intent drops Mira's
    failing ``say`` beside her real ``do``, and the outcome step drops its failing narration
    beside the intent's. ``turn_report.repair`` totals the turn (two renders, two dropped),
    ``epistrel_repairs_total{rung="dropped"}`` counts both beats, and
    ``epistrel_turn_degradation_total{step="repair_dropped"}`` counts the step once for the
    turn (the completion doesn't count a step the intent already counted)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, fake = await _shop(client, engine, epistrel)
        said = beat(
            "say",
            "I am seventy years old.",
            intent="sincere",
            propositions=[claim("I", "age", "70")],
        )
        counted, degraded = _repairs_by_rung(), _degraded("repair_dropped")
        played = await _purchase(
            client, stub, fake, story, cal, do="buys the sword", outcome=_seventy(), said=said
        )
        assert _narrations(played) == [FORGE]
        dropped = [d for d in _repair_details(played) if d["step"] == "repair_dropped"]
        assert len(dropped) == 2, dropped
        report = await turn_report(engine, story)
        assert report["repair"] == {
            "renders": 2,
            "renders_regenerated": 2,
            "regenerations": 2,
            "fallbacks": 0,
            "dropped": 2,
        }
        assert _repairs_since(counted) == {"dropped": 2.0}
        assert _degraded("repair_dropped") - degraded == 1
        await _close_shop(engine, story)


CLOCK_CHANGE = {
    "kind": "change",
    "state": "world_clock",
    "delta": 30,
    "reason": "evening falls",
    "silent": True,
}
CLOCK_DROPPED = {"step": "clock_effect_dropped", "cause": "state"}


def _clock_drops(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in details if d["step"] == "clock_effect_dropped"]


async def _history_clock_drops(client: Any, story: str, turn: int) -> list[dict[str, Any]]:
    response = await client.get(
        f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert response.status_code == 200, response.text
    (found,) = [t for t in response.json()["turns"] if t["turn"] == turn]
    return _clock_drops(found.get("degradation_details", []))


async def _turn_clock_changes(engine: AsyncEngine, story: str) -> list[Any]:
    return list(
        await rows(
            engine,
            "SELECT c.id FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
            "WHERE c.story_id = :s AND d.name = 'world_clock' AND c.source = 'turn'",
            s=story,
        )
    )


async def _clock_purchase(
    client: Any,
    stub: Any,
    fake: Any,
    story: str,
    cal: str,
    *,
    intent_clock: bool,
    outcome_clock: bool,
) -> dict[str, Any]:
    """One staged purchase with the world clock on. Mira's render (the intent) and the outcome
    step's render each carry a ``world_clock`` change when asked to, on the first answer and
    on the corrected retry, so the change is dropped."""
    stub.reset()
    fake.script_writes("gold", "confirmed")
    outcome = beat("narration", WENT, effects=[CLOCK_CHANGE] if outcome_clock else [])
    stub.when(staged.OUTCOME_STEP, es.render(outcome), purpose="generation")
    spent = [es.effect("gold", scope_key=cal, delta=-50, reason="a sword")]
    ms.renders_for(
        stub,
        "Mira",
        es.render(beat("do", TAKES, effects=spent + ([CLOCK_CHANGE] if intent_clock else []))),
    )
    answer = await respond(client, story, body(cal, None, do="buys the sword from Mira"))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


@pytest.mark.part("M12", "FR-REPAIR-5")
@pytest.mark.parametrize(
    ("intent_clock", "outcome_clock"),
    [(False, True), (True, True), (True, False)],
    ids=["outcome_step", "both", "intent"],
)
async def test_i_12__m12_outcome_step_clock_drop(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    clock_delivered: None,
    drained_embedding_jobs: None,
    intent_clock: bool,
    outcome_clock: bool,
) -> None:
    """#1046 (#996, FR-CLOCK-12): a staged turn whose outcome-step render changes
    ``world_clock`` is 200, the clock does not move, and the drop is recorded
    (``clock_effect_dropped``) once in respond, in the stored report, in the history read and
    in ``epistrel_turn_degradation_total``; once too when the intent's render dropped one as
    well, and when only the intent's did."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, fake = await _shop(client, engine, epistrel)
        await cs.set_level(engine, story, "on")
        degraded = _degraded("clock_effect_dropped")
        played = await _clock_purchase(
            client,
            stub,
            fake,
            story,
            cal,
            intent_clock=intent_clock,
            outcome_clock=outcome_clock,
        )
        assert WENT in _narrations(played)
        report = await turn_report(engine, story)
        assert report["staged"]["outcome_step"]["deterministic"] is False
        assert played["degradation"].count("clock_effect_dropped") == 1
        assert _clock_drops(played["degradation_details"]) == [CLOCK_DROPPED]
        assert report["degradation"].count("clock_effect_dropped") == 1
        assert _clock_drops(report["degradation_details"]) == [CLOCK_DROPPED]
        assert await _history_clock_drops(client, story, played["turn"]) == [CLOCK_DROPPED]
        assert _degraded("clock_effect_dropped") - degraded == 1
        assert await _turn_clock_changes(engine, story) == []
        await cs.set_level(engine, story, None)
        await _close_shop(engine, story)


@pytest.mark.part("M12", "FR-REPAIR-5")
async def test_i_12__m12_outcome_step_no_clock_drop(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#1046's complement: a staged turn whose renders leave the clock alone reports no
    ``clock_effect_dropped`` anywhere and counts none."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, fake = await _shop(client, engine, epistrel)
        await cs.set_level(engine, story, "on")
        degraded = _degraded("clock_effect_dropped")
        played = await _clock_purchase(
            client, stub, fake, story, cal, intent_clock=False, outcome_clock=False
        )
        assert WENT in _narrations(played)
        report = await turn_report(engine, story)
        assert "clock_effect_dropped" not in played["degradation"]
        assert _clock_drops(played["degradation_details"]) == []
        assert "clock_effect_dropped" not in report["degradation"]
        assert _clock_drops(report["degradation_details"]) == []
        assert await _history_clock_drops(client, story, played["turn"]) == []
        assert _degraded("clock_effect_dropped") == degraded
        await cs.set_level(engine, story, None)
        await _close_shop(engine, story)
