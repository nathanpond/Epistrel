"""S-FAMILY: a scenario filled from the catalog (docs/04 §7), its M5 part (#173).

The Sunday Dinner of ``tests/seeded_partitions_support``: Mother, Father, Brother and Sister
(open NPC roles, filled from the catalog), the player's Third Sibling played as Sam ("wry and
conflict-avoidant", 22), and an optional neighbour: the stranger to Sam (the doc's stranger
"introduced later"; here a role no relationship links to Sam, so his partition is the
stranger's). Renders are the stub's; a render's context is the captured ``CharacterRender`` call.

Not here: "only child" losing to the role's "has a sister" is S-FAMILY's M7 part (docs/04; moved
from M5 on the owner's decision of 2026-10-26, #576): it needs the background read as a closed
set (#580); the merge rule itself is U-27 M4's (``tests/test_u_27.py``). The Brother's revision by
observation is a direct ``observation`` transition, not a played turn: no turn can make it yet
(Sam is the player, so a render's claim about him is denied ``player_character``, and the
player's own ``do`` is routed without claims). The played-turn version is S-FAMILY's M7 part
(moved on the owner's decision of 2026-10-26, #523 option (c))."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import transitions
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from tests import genesis_support as g
from tests import partition_support as parts
from tests import seeded_partitions_support as sp
from tests.auth_support import auth_settings
from tests.event_support import scalar
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import (
    beat,
    claim,
    render,
    schema_calls,
    text_of,
    turn_report,
    utterances,
)

pytestmark = pytest.mark.integration

LINDA: dict[str, Any] = {
    "personality": "Linda Harrow is warm, blunt and fiercely honest.",
    "dispositions": {
        "threat_response": {"setting": "stands"},
        "dominance": {"setting": "balanced"},
        "honesty": {"setting": "very_honest", "nuance": "Cannot keep a straight face."},
        "warmth": {"setting": "neutral"},
        "caution": {"setting": "careful"},
    },
    "deflection_style": "She changes the subject to the roast.",
}
JUNE: dict[str, Any] = {
    "personality": "June Harrow is charming, guarded and quick with a cover story.",
    "dispositions": {
        "threat_response": {"setting": "stands"},
        "dominance": {"setting": "balanced"},
        "honesty": {"setting": "deceitful", "nuance": "Lies smoothly to keep the peace."},
        "warmth": {"setting": "neutral"},
        "caution": {"setting": "careful"},
    },
    "deflection_style": "She laughs it off and pours more wine.",
}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _context(client: Any, stub: Any, story: str, sam: str, who: str, name: str) -> str:
    """``who``'s render context of one turn (the stub renders a nod)."""
    stub.calls.clear()
    stub.script("generation", [render(beat("do", f"{name} nods."))])
    answer = await respond(client, story, body(sam, "Is dinner ready?", render_character_ids=[who]))
    assert answer.status_code == 200, answer.text
    (call,) = schema_calls(stub, "CharacterRender")
    return text_of(call)


async def _turn(client: Any, stub: Any, story: str, sam: str, who: str, *beats: Any) -> None:
    """A turn Sam plays in which ``who`` renders ``beats``."""
    stub.calls.clear()
    stub.script("generation", [render(*beats)])
    answer = await respond(client, story, body(sam, "Is dinner ready?", render_character_ids=[who]))
    assert answer.status_code == 200, answer.text


async def _said(engine: AsyncEngine, story: str, speaker: str) -> Any:
    """``speaker``'s latest utterance."""
    return [u for u in await utterances(engine, story) if str(u.speaker_character_id) == speaker][
        -1
    ]


async def _speech(engine: AsyncEngine, story: str) -> dict[str, Any]:
    """The latest turn's one verdict with a speech act."""
    (verdict,) = [
        v for v in (await turn_report(engine, story))["verify"]["verdicts"] if "speech" in v
    ]
    found: dict[str, Any] = verdict["speech"]
    return found


@pytest.mark.part("M5", "FR-SCN-2,3,6,7,8,9; FR-CAT-2,6; FR-EPI-2; FR-BELIEF-15")
async def test_s_family(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """Genesis creates the family from the catalog and seeds each head as the author named.

    - **Personality from the catalog.** The same Mother role filled by Linda (very honest) and
      by June (deceitful) puts each one's personality, honesty disposition and deflection style
      into her render context; nothing of the other's.
    - **The affair** is in the Mother's partition only and her context marks it ``(secret)``;
      how she keeps it follows her own honesty disposition (in her definition block), never
      the role. Her secret makes a later denial a lie: when she sincerely denies keeping the
      letters, the turn labels the line ``lie`` (intent inferred ``deceptive``), unrepaired;
      when the Brother sincerely denies the sale, his open false belief makes it a
      ``sincere_error``, not a lie.
    - **Relationships** seed canon and beliefs: the Brother knows Sam is his sibling.
    - **The Father's secret** (the house is being sold) is in his partition, not the
      Brother's or the Sister's; the Brother's seeded false belief survives unchanged, and the
      Sister's nested belief that he knows is kept.
    - **The persona.** The family's partitions hold Sam's name, exact age and both traits, each
      a belief citing the persona's seed act; the neighbour's holds none and his context sees
      only "Sam (early twenties)". When Sam loses his temper in front of the Brother, the
      Brother's belief that Sam is conflict-avoidant is revised by observation (written
      directly; see the module docstring).
    - **The opening** is turn 1, with exposures for the roles present.

    Complements: the Father's and the Brother's contexts never say who has the letters; with
    the neighbour left out the story has five characters."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        profile = await sp.partitioned_profile(client, "Sunday partitions")
        scenario_id, _ = await g.published_scenario(client, sp.family())
        linda = await sp.cast(client, {"mother": LINDA})
        made = await g.create_from(client, sp.request(scenario_id, linda, profile))
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        sam = by_role["player"]
        ents = await sp.entity_ids(engine, story)
        rows = await sp.beliefs(engine, story)

        # -- the Mother's context: her personality, her secret, her own disposition
        mother = await _context(client, stub, story, sam, by_role["mother"], "Linda")
        assert LINDA["personality"] in mother
        assert LINDA["deflection_style"] in mother
        assert "scrupulously honest. Cannot keep a straight face." in mother
        assert "love letters (secret)" in mother
        assert JUNE["personality"] not in mother
        for other, name in (("father", "Frank"), ("brother", "Tom")):
            seen = await _context(client, stub, story, sam, by_role[other], name)
            assert "holds The love letters" not in seen, other  # the item is named, not who has it
        (denied,) = sp.held(rows, by_role["brother"], by_role["father"], ents["The sale contract"])

        # -- the affair denied: a lie (her belief is kept secret); the Brother's denial of the
        #    sale is a sincere error (his false belief is open)
        await _turn(
            client,
            stub,
            story,
            sam,
            by_role["mother"],
            beat(
                "say",
                "Love letters? I have never kept any love letters.",
                intent="sincere",
                propositions=[claim("Linda", "holds", "The love letters", negated=True)],
            ),
        )
        lie = await _said(engine, story, by_role["mother"])
        assert (lie.objective_relation, lie.belief_relation, lie.classification) == (
            "false",
            "inconsistent",
            "lie",
        )
        speech = await _speech(engine, story)
        assert (speech["declared_intent"], speech["intent_inferred"]) == ("sincere", True)
        assert "repairs" not in await turn_report(engine, story)  # a lie stands, unrepaired
        await _turn(
            client,
            stub,
            story,
            sam,
            by_role["brother"],
            beat(
                "say",
                "Dad would never sell the farm.",
                intent="sincere",
                propositions=[claim("Frank", "holds", "The sale contract", negated=True)],
            ),
        )
        error = await _said(engine, story, by_role["brother"])
        assert (error.objective_relation, error.belief_relation, error.classification) == (
            "false",
            "consistent",
            "sincere_error",
        )
        assert (await _speech(engine, story))["intent_inferred"] is False
        assert "repairs" not in await turn_report(engine, story)

        # -- the same role, another character: her own personality and disposition
        june = await sp.cast(client, {"mother": JUNE})
        second = await g.create_from(
            client, sp.request(scenario_id, june, profile, neighbour=False)
        )
        assert len(second["characters"]) == 5
        others = {c["role_id"]: c["id"] for c in second["characters"]}
        june_context = await _context(
            client, stub, second["story_id"], others["player"], others["mother"], "June"
        )
        assert JUNE["personality"] in june_context
        assert JUNE["deflection_style"] in june_context
        assert "Lies smoothly to keep the peace." in june_context
        assert LINDA["personality"] not in june_context
        assert "love letters (secret)" in june_context

        # -- relationships, the Father's secret, the false and the nested beliefs
        assert sp.held(rows, by_role["brother"], by_role["brother"], sam)
        sale = [r for r in rows if str(r.object_id) == ents["The sale contract"]]
        affirmed = {(str(r.holder), r.kind) for r in sale if r.polarity == "affirmed"}
        assert (by_role["father"], "first_order") in affirmed
        assert (by_role["brother"], "first_order") not in affirmed
        assert (by_role["sister"], "first_order") not in affirmed
        assert (by_role["sister"], "nested") in affirmed
        assert (denied.polarity, denied.confidence_level) == ("negated", "believed")

        # -- the persona: the family knows Sam, the neighbour sees an age band
        def about_sam(role: str) -> list[tuple[str, str]]:
            return sorted(
                (r.predicate, str((r.literal or {}).get("value")))
                for r in rows
                if str(r.holder) == by_role[role] and str(r.subject_id) == sam and r.literal
            )

        expected = sorted([("age", "22"), ("name", "Sam"), *(("trait", t) for t in sp.TRAITS)])
        for role in sp.NPC_ROLES:
            assert about_sam(role) == expected, role
        assert about_sam("neighbour") == []
        neighbour = await _context(client, stub, story, sam, by_role["neighbour"], "Mr Pike")
        assert "Sam (early twenties)" in neighbour
        assert "22" not in neighbour
        assert "conflict-avoidant" not in neighbour

        trait_id = await scalar(
            engine, "SELECT id FROM predicates WHERE story_id = :s AND name = 'trait'", s=story
        )
        await parts.transit(
            engine,
            story,
            by_role["brother"],
            Proposition(
                subject_id=sam,
                predicate_id=str(trait_id),
                literal=TypedLiteral(kind=LiteralKind.STR, value="conflict-avoidant"),
                polarity="negated",
                valid_time=Interval(start=None, end=None),
            ),
            cause=transitions.Kind.OBSERVATION,
            confidence=transitions.Confidence(level=BeliefConfidenceLevel.BELIEVED),
        )
        temper = (
            "SELECT b.polarity, b.held_to_turn, t.kind, e.type AS act FROM beliefs b "
            "JOIN propositions p ON p.id = b.proposition_id "
            "JOIN belief_transitions t ON t.id = b.provenance "
            "LEFT JOIN events e ON e.id::text = t.antecedent_ref->>'id' "
            "WHERE b.story_id = :s AND b.holder_character_id = :h "
            "AND p.literal->>'value' = 'conflict-avoidant' ORDER BY b.held_from_step"
        )
        revised = await sp.rows_of(engine, temper, s=story, h=by_role["brother"])
        assert sorted((r.polarity, r.held_to_turn is None, r.kind) for r in revised) == [
            ("affirmed", False, "author"),
            ("negated", True, "observation"),
        ]

        # -- the opening: turn 1, exposures for the roles present
        opening = await sp.rows_of(
            engine,
            "SELECT e.payload->>'finish_reason' AS finish, e.payload->>'text' AS text "
            "FROM events e JOIN operations o ON o.operation_id = e.operation_id "
            "WHERE e.story_id = :s AND o.narrative_turn = 1 AND e.type = 'message_rendered'",
            s=story,
        )
        assert [(r.finish, r.text) for r in opening] == [("opening", sp.OPENING)]
        witnesses = await sp.rows_of(
            engine,
            "SELECT DISTINCT witness_character_id FROM exposures WHERE story_id = :s "
            "AND position_turn = 1",
            s=story,
        )
        assert {str(w.witness_character_id) for w in witnesses} == {
            by_role[r] for r in ("mother", "father", "brother")
        }
