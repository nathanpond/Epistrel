"""U-03 (M5): per-character belief partitions (docs/04 §3; #159).

``test_u_03__partitions`` covers U-03's self/social storage and query clauses (FR-EPI-1,3,5,6):
a belief is stored in its holder's partition, beside canon and never in it; a false belief is the
holder's truth, with its provenance; the partition is queried by holder (``partition``) and by
proposition (``believers_of``, the ``belief_state`` projection), and over REST
(``GET /stories/{id}/inspect/beliefs``, omniscient only). Every fact, memory and belief carries
the one visibility shape, and a belief is visible to its holder alone. Nested beliefs, the
attributed confidences and could-have-known gating are #167's and #164's parts of U-03.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from epistrel.core.enums import BeliefConfidenceLevel, BeliefKind
from epistrel.core.epistemic import partitions
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.visibility import Visibility, private_to
from tests import canon_support as c
from tests import partition_support as ps
from tests.auth_support import bearer
from tests.memory_support import use_structured
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

ACQUISITION = "01JT0000000000000000000001"
"""A stand-in for #160's acquisition id (``beliefs.provenance`` is nullable until #160)."""


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
        provenance=ACQUISITION,
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
    assert (false.provenance, false.confidence_level, false.confidence_scalar) == (
        ACQUISITION,
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
            "membership_tx) SELECT '01JT00000000000000000000X1', story_id, holder_character_id, "
            "proposition_id, polarity, confidence_level, valid_span, held_from_turn, tx_from, "
            "jsonb_build_object('scope', 'characters', 'characters', "
            "jsonb_build_array(CAST(:other AS text))), "
            "operation_id, membership_tx FROM beliefs WHERE id = :id",
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
    # Append-only: a belief is never changed in place (its level changes by a new row).
    with pytest.raises(DBAPIError, match="append-only"):
        await ps.execute(
            engine, "UPDATE beliefs SET confidence_level = 'known' WHERE id = :id", id=false.id
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
            ACQUISITION,
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
