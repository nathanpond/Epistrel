"""U-03 (M5): per-character belief partitions (docs/04 §3; #159).

``test_u_03__partitions`` covers U-03's self/social storage and query clauses (FR-EPI-1,3,5,6):
a belief is stored in its holder's partition, beside canon and never in it; a false belief is the
holder's truth, with its provenance; the partition is queried by holder (``partition``) and by
proposition (``believers_of``, the ``belief_state`` projection), and over REST
(``GET /stories/{id}/inspect/beliefs``, omniscient only). Every fact, memory and belief carries
the one visibility shape, and a belief is visible to its holder alone. Could-have-known gating
is #165's part of U-03.

``test_u_03__nested`` covers U-03's nested clause over the knock world (``tests.nested_support``):
Jenni overheard Bobby joke that Marcus knocked. Her record that Bobby thinks so is a ``nested``
row in **her** partition with Bobby as attributed holder, an immutable ``attributed`` inner row,
the inference provenance citing the overheard say, her own and the attributed confidence and the
three times; what Bobby *said* is a separate ``speech_act`` row. Neither is a first-order belief
(``partition``, ``belief_state``); the nested one is projected into ``nested_belief_state``,
which a fold reproduces. Nesting is depth 2 by default: a depth-3 write is 422
``nesting_too_deep`` for the API writer and truncated (``nesting_truncated``) for an internal
one, and with depth 3 configured the chain is written to depth 3. A contradicting attribution
(Bobby later tells her "I figured it was the delivery guy") closes the first with its history
kept. The belief relation reads Jenni's rows only; the objective relation reads Bobby's.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from epistrel.core.enums import BeliefConfidenceLevel, BeliefKind
from epistrel.core.epistemic import nested, partitions, testimony, transitions
from epistrel.core.errors import InvalidRequest, ModuleOff
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.resolution.time import story_time_at
from epistrel.core.verify.rules import VerdictKind
from epistrel.core.visibility import Visibility, private_to
from tests import canon_support as c
from tests import nested_support as ns
from tests import partition_support as ps
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows
from tests.memory_support import use_structured
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


@pytest.mark.part("M5", "FR-EPI-1,3,5,6")
async def test_u_03__partitions(engine: AsyncEngine, settings: Settings) -> None:
    """Canon says Bobby is in the kitchen. Ann believes he is in the garden (a false belief,
    self-world: what she holds of the world), Bobby believes he is in the kitchen (the canon
    fact's own proposition, FR-PROP-2) and that Ann knows Cal (social-world: about other
    people). Each belief is in its holder's partition only, with its provenance, visible to its
    holder alone; canon holds no belief and the partition no fact (FR-EPI-1); the false belief is
    Ann's truth and canon is unchanged by it (FR-EPI-3). Queries by holder and by proposition
    answer the projection (FR-EPI-6), over REST too (omniscient only). Complements: Ann's
    partition before her belief, and Bobby's at a position before his, are empty; a belief
    visible to another character, or written over in place, is refused (FR-EPI-5)."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby, cal = (w.characters[n] for n in ("Ann", "Bobby", "Cal"))
    people, places = w.people, w.places
    fact = await c.commit(engine, story, c.at(people["Bobby"], places["kitchen"], 0, None))

    false = await ps.believe(
        engine,
        story,
        ann,
        c.at(people["Bobby"], places["garden"], 0, None),
        scalar=0.8,
    )
    true = await ps.believe(engine, story, bobby, c.at(people["Bobby"], places["kitchen"], 0, None))
    knows = Proposition(
        subject_id=people["Ann"],
        predicate_id=c.KNOWS,
        object_id=people["Cal"],
        valid_time=Interval.of(None, None),
    )
    social = await ps.believe(engine, story, bobby, knows, level=BeliefConfidenceLevel.SUSPECTED)

    # Storage: the holder's own record, private by construction, with its provenance and times.
    assert (false.holder_character_id, false.kind) == (ann, BeliefKind.FIRST_ORDER)
    assert false.visibility == private_to(ann) == Visibility(scope="characters", characters=(ann,))
    acquisition = await ps.rows_of(
        engine, "SELECT id, kind FROM belief_transitions WHERE belief_id = :b", b=false.id
    )
    assert [(r.id, r.kind) for r in acquisition] == [(false.provenance, "author")]
    assert (false.provenance, false.confidence_level, false.confidence_scalar) == (
        acquisition[0].id,
        BeliefConfidenceLevel.BELIEVED,
        0.8,
    )
    assert (false.valid_from, false.valid_to, false.held_to) == (0, None, None)
    assert false.held_from == Position(false.narrative_turn or 0, false.turn_variant or 0)
    assert false.polarity == Polarity.AFFIRMED.value
    # One proposition shape, two holders (FR-PROP-2): Bobby's true belief and the canon fact
    # wrap the same proposition, viewpoint-free; the holder is on the record.
    assert true.proposition.normalized_key() == fact.proposition.normalized_key()
    assert false.proposition.normalized_key() != fact.proposition.normalized_key()

    async with engine.connect() as conn:
        # Query by holder: each partition holds its own beliefs only.
        assert [b.id for b in await partitions.partition(conn, story, ann)] == [false.id]
        assert {b.id for b in await partitions.partition(conn, story, bobby)} == {
            true.id,
            social.id,
        }
        assert await partitions.partition(conn, story, cal) == []
        about_ann = await partitions.partition(conn, story, bobby, subjects=[people["Ann"]])
        assert [b.id for b in about_ann] == [social.id]
        # Complement: before Ann came to believe it, her partition was empty.
        assert (
            await partitions.partition(
                conn, story, ann, through=Position.end_of(false.held_from.turn - 1)
            )
            == []
        )
        # Query by proposition: the projection names who holds what, at which level.
        (garden,) = await partitions.believers_of(conn, story, false.proposition_id)
        assert (garden.holder_character_id, garden.belief_id, garden.current_level) == (
            ann,
            false.id,
            BeliefConfidenceLevel.BELIEVED,
        )
        assert garden.current_scalar == 0.8
        assert garden.since == false.held_from
        (kitchen,) = await partitions.believers_of(conn, story, true.proposition_id)
        assert kitchen.holder_character_id == bobby
        (suspected,) = await partitions.believers_of(conn, story, social.proposition_id)
        assert suspected.current_level is BeliefConfidenceLevel.SUSPECTED
        # FR-EPI-1 / FR-EPI-3: canon is untouched by the false belief and holds no belief.
        everything = await canon.canon(conn, story)
        assert [f.id for f in everything] == [fact.id]
        assert everything[0].proposition.object_id == places["kitchen"]
        for viewpoint in (w.viewpoint("Ann"), "omniscient"):
            seen = await canon.canon(conn, story, viewpoint=viewpoint)
            assert all(f.proposition.object_id != places["garden"] for f in seen)

    # FR-EPI-5: one visibility shape everywhere; a belief readable by another is refused.
    held = await ps.belief_rows(engine, story)
    assert {r.visibility["scope"] for r in held} == {"characters"}
    with pytest.raises(DBAPIError, match="holder_only"):
        await ps.execute(
            engine,
            "INSERT INTO beliefs (id, story_id, holder_character_id, proposition_id, polarity, "
            "confidence_level, valid_span, held_from_turn, tx_from, visibility, operation_id, "
            "membership_tx, provenance, lineage_id) SELECT '01JT00000000000000000000X1', "
            "story_id, holder_character_id, "
            "proposition_id, polarity, confidence_level, valid_span, held_from_turn, tx_from, "
            "jsonb_build_object('scope', 'characters', 'characters', "
            "jsonb_build_array(CAST(:other AS text))), "
            "operation_id, membership_tx, provenance, lineage_id FROM beliefs WHERE id = :id",
            other=bobby,
            id=false.id,
        )
    with pytest.raises(DBAPIError, match="visibility_characters"):
        await ps.execute(
            engine,
            "INSERT INTO memories (id, story_id, kind, text, provenance, trust_tier, "
            "operation_id, membership_tx, visibility) SELECT '01JT00000000000000000000X2', "
            "story_id, 'episodic', 'x', 'render', 'in_world', operation_id, membership_tx, "
            '\'{"scope": "characters", "characters": []}\' FROM beliefs WHERE id = :id',
            id=false.id,
        )
    # Append-only: what a belief holds is never changed in place (#160's transitions change only
    # its confidence, accessibility and hold state).
    with pytest.raises(DBAPIError, match="append-only"):
        await ps.execute(
            engine, "UPDATE beliefs SET polarity = 'negated' WHERE id = :id", id=false.id
        )
    with pytest.raises(DBAPIError, match="append-only"):
        await ps.execute(engine, "DELETE FROM beliefs WHERE id = :id", id=false.id)

    # The projection over REST: omniscient only, with the belief and the proposition it holds.
    path = f"/stories/{story}/inspect/beliefs"
    async with respond_client(settings, engine) as (client, _):
        page = await client.get(
            path, params={"character": ann, "viewpoint": "omniscient"}, headers=bearer()
        )
        assert page.status_code == 200, page.text
        body = page.json()
        (item,) = body["beliefs"]
        assert (item["belief_id"], item["current_level"], item["provenance"]) == (
            false.id,
            "believed",
            false.provenance,
        )
        assert item["proposition"]["object_id"] == places["garden"]
        assert (item["kind"], item["held_to"], body["next_after"]) == ("first_order", None, None)
        bob = await client.get(
            path,
            params={"character": bobby, "viewpoint": "omniscient", "limit": 1},
            headers=bearer(),
        )
        assert len(bob.json()["beliefs"]) == 1
        assert bob.json()["next_after"] is not None
        rest = await client.get(
            path,
            params={
                "character": bobby,
                "viewpoint": "omniscient",
                "after": bob.json()["next_after"],
            },
            headers=bearer(),
        )
        assert {i["belief_id"] for i in (*bob.json()["beliefs"], *rest.json()["beliefs"])} == {
            true.id,
            social.id,
        }
        refused = await client.get(
            path, params={"character": ann, "viewpoint": w.viewpoint("Ann")}, headers=bearer()
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "omniscient_required"
        stranger = await client.get(
            path,
            params={"character": "01JT00000000000000000000ZZ", "viewpoint": "omniscient"},
            headers=bearer(),
        )
        assert stranger.status_code == 422


Level = BeliefConfidenceLevel
Kind = transitions.Kind


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _headers() -> dict[str, str]:
    return {**bearer(), "X-Epistrel-Principal": "p-author"}


def _at(op: Operation) -> Position:
    return testimony.witness_position(op.narrative_turn or 0, op.turn_variant or 0, 2)


@pytest.mark.part("M5", "FR-BELIEF-15; FR-EPI-2")
async def test_u_03__nested(
    stub_settings: Settings,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    ns.deliver_nested(monkeypatch)
    async with respond_client(stub_settings, engine) as (client, _):
        w = await ns.knock_world(client, engine, "Cal", "Bobby", "Jenni", "Ada")
        story = w.story_id
        cal, bobby, jenni, ada = (w.ids[n] for n in ("Cal", "Bobby", "Jenni", "Ada"))
        await ns.enable_nested(engine, story)
        settings = await ns.nested_settings(engine, story)
        marcus, driver = w.knocked(w.marcus), w.knocked(w.driver)

        # -- the joke: a said row and a depth-2 nested inference citing the overheard say
        async def joke(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
            cited = transitions.Antecedent.record("events", event_id)
            said = await nested.write_speech_act(
                conn,
                op,
                holder=jenni,
                speaker=bobby,
                proposition=marcus,
                antecedent=cited,
                position=_at(op),
                perceived_intent="joking",
                settings=settings,
            )
            made = await nested.write_nested(
                conn,
                op,
                holder=jenni,
                attributed_holder=bobby,
                proposition=marcus,
                cause=nested.Cause(Kind.INFERENCE, cited),
                position=_at(op),
                confidence=transitions.Confidence(Level.SUSPECTED),
                attributed_confidence=transitions.Confidence(Level.BELIEVED),
                settings=settings,
            )
            return said, made, event_id, _at(op)

        said, made, overheard, heard_at = await ns.in_operation(
            engine, story, joke, say=(bobby, "Probably Marcus again.", "joking")
        )
        assert made.depth == 2
        assert not made.truncated
        outer = made.belief
        (inner,) = made.inner
        assert (outer.kind, outer.attributed_holder_id, outer.inner_belief_id, outer.depth) == (
            BeliefKind.NESTED,
            bobby,
            inner.id,
            2,
        )
        assert (inner.kind, inner.attributed_holder_id, inner.inner_belief_id, inner.depth) == (
            BeliefKind.ATTRIBUTED,
            bobby,
            None,
            2,
        )
        assert outer.holder_character_id == inner.holder_character_id == jenni
        assert outer.proposition_id == inner.proposition_id
        assert outer.visibility.document() == {"scope": "characters", "characters": [jenni]}
        # Her confidence, the attributed confidence and the three times.
        assert outer.confidence_level is Level.SUSPECTED
        assert outer.attributed_confidence_level is Level.BELIEVED
        assert outer.held_from == heard_at
        assert outer.held_to is None
        async with engine.connect() as conn:
            basis = await load_time_basis(conn, story)
        assert outer.attributed_held_from == story_time_at(heard_at.turn, basis)
        assert outer.attributed_held_to is None
        assert (outer.valid_from, outer.valid_to) == (None, None)
        # The inference provenance cites the overheard say.
        (row,) = [r for r in await ns.nested_rows(engine, story, jenni) if r.id == outer.id]
        assert (row.cause, row.antecedent_ref) == (
            "inference",
            {"table": "events", "id": overheard},
        )
        # What Bobby said is a speech-act row of its own, never a belief about his belief.
        heard = said.belief
        assert (heard.kind, heard.speaker_character_id, heard.perceived_intent, heard.depth) == (
            BeliefKind.SPEECH_ACT,
            bobby,
            "joking",
            2,
        )
        assert heard.attributed_holder_id is None
        assert heard.id not in (outer.id, inner.id)
        async with engine.connect() as conn:
            (held,) = await partitions.nested_of(conn, story, jenni)
            assert held.belief.id == outer.id
            assert held.holders == (bobby,)
            assert held.said_by is None
            assert [a.belief.id for a in await partitions.speech_acts(conn, story, jenni)] == [
                heard.id
            ]
            # Not first-order: no partition read, no belief_state row.
            assert await partitions.partition(conn, story, jenni) == []
            assert await partitions.believers_of(conn, story, outer.proposition_id) == []
            folded = await partitions.fold_nested_state(conn, story)
        projected = await rows(
            engine, "SELECT * FROM nested_belief_state WHERE story_id = :s", s=story
        )
        assert [(r.belief_id, r.attributed_holder_id, r.depth) for r in projected] == [
            (outer.id, bobby, 2)
        ]
        assert [r["belief_id"] for r in folded] == [outer.id]
        assert projected[0].current_level == "suspected"
        assert projected[0].attributed_level == "believed"

        # -- depth 3 at the default depth 2: refused for the API, truncated for the engine
        path = f"/admin/stories/{story}/beliefs"
        deep = {
            "holder": jenni,
            "attributed_to": [bobby, ada],
            "proposition": marcus.model_dump(mode="json"),
        }
        refused = await client.post(path, json=deep, headers=_headers())
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "nesting_too_deep"
        assert refused.json()["error"]["details"] == {
            "depth": 3,
            "limit": 2,
            "field": "attributed_to",
        }

        async def truncated(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
            return await nested.write_nested(
                conn,
                op,
                holder=jenni,
                attributed_holder=bobby,
                deeper=[ada],
                proposition=marcus,
                cause=nested.Cause(
                    Kind.INFERENCE, transitions.Antecedent.record("events", event_id)
                ),
                position=_at(op),
                settings=settings,
            )

        cut = await ns.in_operation(
            engine, story, truncated, say=(bobby, "Ada thinks it was Marcus.", "sincere")
        )
        assert cut.truncated
        assert cut.depth == 2
        assert cut.belief.id == outer.id  # the same content, strengthened in place
        assert nested.NESTING_TRUNCATED == "nesting_truncated"

        # -- the player and the holder itself
        async def to_self(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
            return await nested.write_nested(
                conn,
                op,
                holder=jenni,
                attributed_holder=jenni,
                proposition=marcus,
                cause=nested.Cause(
                    Kind.INFERENCE, transitions.Antecedent.record("events", event_id)
                ),
                position=_at(op),
                settings=settings,
            )

        with pytest.raises(InvalidRequest) as self_refused:
            await ns.in_operation(engine, story, to_self, say=(ada, "Hm.", "sincere"))
        assert self_refused.value.details["reason"] == "attributed_self"

        # -- configured depth 3: the chain is written to depth 3
        await ns.set_depth(engine, story, 3)
        written = await client.post(path, json=deep, headers=_headers())
        assert written.status_code == 201, written.text
        body = written.json()["belief"]
        assert (body["kind"], body["attributed_holder_id"], body["depth"]) == ("nested", bobby, 3)
        async with engine.connect() as conn:
            deeper = [
                n for n in await partitions.nested_of(conn, story, jenni) if n.belief.depth == 3
            ]
        (three,) = deeper
        assert three.holders == (bobby, ada)
        assert [(b.kind, b.attributed_holder_id, b.depth) for b in three.chain] == [
            (BeliefKind.ATTRIBUTED, bobby, 3),
            (BeliefKind.ATTRIBUTED, ada, 2),
        ]
        await ns.set_depth(engine, story, 2)
        async with engine.connect() as conn:
            # Lowering the dial keeps every row readable.
            assert len(await partitions.nested_of(conn, story, jenni)) == 2

        # -- Bobby actually believes the driver did it (he was told so)
        settings = await ns.nested_settings(engine, story)

        async def told(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
            return await transitions.transition(
                conn,
                op,
                holder=bobby,
                kind=Kind.TESTIMONY,
                proposition=driver,
                antecedent=transitions.Antecedent.record("events", event_id),
                position=_at(op),
                source=cal,
                outcome=testimony.ACCEPT,
                confidence=transitions.Confidence(Level.BELIEVED),
                settings=settings,
            )

        await ns.in_operation(
            engine, story, told, say=(cal, "It was the delivery driver.", "sincere")
        )

        # -- the belief relation reads Jenni's rows; the objective relation Bobby's
        async with engine.connect() as conn:
            before = await nested.attribution_belief_relation(
                conn, story, jenni, bobby, marcus, None
            )
            other = await nested.attribution_belief_relation(
                conn, story, jenni, bobby, driver, None
            )
            nothing = await nested.attribution_belief_relation(
                conn, story, jenni, ada, driver, None
            )
            said_rel = await nested.attribution_belief_relation(
                conn, story, jenni, bobby, marcus, None, mode="said"
            )
            really = await nested.attribution_objective_relation(conn, story, bobby, marcus, None)
            truly = await nested.attribution_objective_relation(conn, story, bobby, driver, None)
            player = await nested.attribution_objective_relation(conn, story, cal, driver, None)
        assert before.verdict is VerdictKind.SUPPORTED
        assert before.belief_ids == (outer.id,)
        assert other.verdict is VerdictKind.CONTRADICTED
        assert other.contrary == (outer.id,)
        assert nothing.verdict is VerdictKind.NOVEL  # her own first-order beliefs are no evidence
        assert said_rel.verdict is VerdictKind.SUPPORTED
        assert (really, truly, player) == ("false", "true", "unresolved")

        # -- "I figured it was the delivery guy": revised by testimony, history kept
        async def figured(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
            return await nested.write_nested(
                conn,
                op,
                holder=jenni,
                attributed_holder=bobby,
                proposition=driver,
                cause=nested.Cause(
                    Kind.TESTIMONY, transitions.Antecedent.record("events", event_id), source=bobby
                ),
                position=_at(op),
                confidence=transitions.Confidence(Level.BELIEVED),
                settings=settings,
            )

        revised = await ns.in_operation(
            engine, story, figured, say=(bobby, "I figured it was the delivery guy.", "sincere")
        )
        assert revised.result.closed == (outer.id,)
        assert revised.belief.inner_belief_id not in (None, inner.id)
        async with engine.connect() as conn:
            old = await partitions.read_belief(conn, story, outer.id)
            history = await transitions.history(conn, story, revised.belief.id)
            now = await nested.attribution_belief_relation(conn, story, jenni, bobby, marcus, None)
            then = await nested.attribution_belief_relation(
                conn, story, jenni, bobby, marcus, heard_at
            )
            kept = await partitions.read_belief(conn, story, inner.id)
        assert old is not None
        assert old.state.value == "corrected"
        assert old.corrected_by == revised.belief.id
        assert old.held_to == revised.belief.held_from
        assert [e.belief.id for e in history] == [outer.id, revised.belief.id]
        assert now.verdict is VerdictKind.CONTRADICTED
        assert then.verdict is VerdictKind.SUPPORTED
        assert kept is not None  # an inner row is immutable: the old content stays
        projected = await rows(
            engine,
            "SELECT belief_id FROM nested_belief_state WHERE story_id = :s AND depth = 2",
            s=story,
        )
        assert [r.belief_id for r in projected] == [revised.belief.id]
        async with engine.connect() as conn:
            assert sorted(
                r["belief_id"] for r in await partitions.fold_nested_state(conn, story)
            ) == sorted(
                r.belief_id
                for r in await rows(
                    engine, "SELECT belief_id FROM nested_belief_state WHERE story_id = :s", s=story
                )
            )

        # -- the module off: nothing nested is written
        off = await ns.nested_settings(engine, story, nested=False)

        async def gated(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
            return await nested.write_nested(
                conn,
                op,
                holder=jenni,
                attributed_holder=ada,
                proposition=driver,
                cause=nested.Cause(
                    Kind.INFERENCE, transitions.Antecedent.record("events", event_id)
                ),
                position=_at(op),
                settings=off,
            )

        with pytest.raises(ModuleOff):
            await ns.in_operation(engine, story, gated, say=(ada, "Hm.", "sincere"))

        # -- Rule 1 (#167): consolidation over a window with said rows decays first-order and
        # nested beliefs only (a said row is no belief to forget, and never raised).
        async def consolidate(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
            del event_id
            return await transitions.forget_covered(
                conn, op, holders=[jenni], from_turn=0, to_turn=heard_at.turn, settings=settings
            )

        assert await ns.in_operation(engine, story, consolidate, say=(ada, "Hm.", "sincere")) >= 0
        async with engine.connect() as conn:
            (still,) = await partitions.speech_acts(conn, story, jenni)
        assert still.belief.state.value == "held"
