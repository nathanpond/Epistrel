"""C-25: creative inputs reach the right contexts (docs/04 §5; NFR-QUAL-13,14,15, FR-SCN-2), its
M4 and M9 parts (#158). NFR-QUAL-14 gates from M9 (ADR-164), so ``test_c_25__m4`` also carries
C-25's M9 part; it runs in CI either way.

Every C-25 (M4) and C-25 (M9) part lives in this file (traceability counts a test ID per
milestone in one file).

``test_c_25__m4``: with the stub capturing the prompts, a structured story created from the
Sunday Dinner scenario (#149's structured genesis) plays one turn whose input addresses no
character, so the narrator (the director) renders first and the cast member second (#150's
deterministic trigger). The character's render carries that character's definition as its author
notes: its personality, every disposition, its interaction style (speech, vocabulary,
mannerisms) and its sample dialogue (#115's paths). The narrator's render carries the scenario's
plot hooks, objectives, director guidance and planned reveals inside the `## Author notes (not
facts)` block. Voice fidelity and plot adherence get no floor and no gate (NFR-QUAL-13): the
inputs are delivered structurally, and E-13 measures the outputs as signals.

Complement: in M4's shared-knowledge mode, the planned reveal, the hooks, the objectives and the
guidance appear only inside the narrator's labelled block, never in the character's prompt. Full
exclusion by visibility is M5's part (FR-EPI-5).

``test_c_25__m5`` (FR-EPI-5; #170): with per-character partitions, a planned reveal committed as
canon only Marcus may read (``commit_fact`` with ``characters: [Marcus]`` visibility: that Jenni
forged the letter for Wren) is in no other character's context. Asked about Jenni and the
letter, Bobby's and Jenni's renders do not carry the fact's line, and Marcus's does (the
complement: the fact is retrievable, and visibility alone keeps it from the others).

``test_c_25__m5_planned_reveal`` (FR-EPI-5; #173): a planned reveal created at genesis under
partitions stays ``engine``-scoped, in no partition, no memory and no character's context.
``test_c_25__m5_planned_reveal_belief`` (#574): the same when the truth says ``kind: belief``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon.facts import Visibility
from epistrel.core.enums import OperationKind
from epistrel.core.genesis.render import DIRECTOR_HEADING, DISPOSITION_PHRASES
from epistrel.core.propositions.model import Interval, Proposition
from tests import genesis_support as g
from tests import perception_support as ps
from tests import scenario_support as scenarios
from tests import seeded_partitions_support as sp
from tests import structured_genesis_support as sg
from tests.auth_support import auth_settings
from tests.canon_support import auto_predicate, commit
from tests.character_support import DISPOSITIONS
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, render, schema_calls, structured_story, text_of

pytestmark = pytest.mark.integration

PERSONALITY = "June hums hymns when she is worried and never says why"
SPEECH = "long sentences that circle back to the farm"
VOCABULARY = "old county words, never a curse"
MANNERISM = "smooths her apron before she answers"
SAMPLE_LINE = "We don't sell what my father planted, love."
DEFINITION = {
    "personality": PERSONALITY,
    "dispositions": DISPOSITIONS,
    "interaction_style": {
        "speech_style": SPEECH,
        "vocabulary": VOCABULARY,
        "mannerisms": [MANNERISM],
        "sample_dialogue": [SAMPLE_LINE],
    },
}
HOOK = "A stranger asks for the deed."
OBJECTIVE = "Find out who sold the orchard."
REVEAL = "The brother forged the second sale."
DRIVERS = (HOOK, OBJECTIVE, scenarios.GUIDANCE_SECRET, REVEAL)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M4", "NFR-QUAL-13,15; FR-SCN-2")
@pytest.mark.part("M9", "NFR-QUAL-14")
async def test_c_25__m4(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The character's render holds its personality, dispositions, interaction style and
    sample dialogue; the director's holds the hooks, objectives, guidance and planned reveals,
    labelled apart from the facts. Complement: none of the drivers is in the character's
    prompt (M4's shared mode keeps them in the narrator's labelled block only)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(
            client, "June Harrow", age=60, labels=["warm"], definition=DEFINITION
        )
        scenario_id, _ = await g.published_scenario(client, sg.structured_dinner(locked, default))
        made = await g.create_from(client, sg.structured_request(scenario_id, mother))
        story = made["story_id"]
        player = next(c["id"] for c in made["characters"] if c["role_id"] == "player")
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(beat("narration", "The kitchen door swings open.")),
                render(beat("do", "June looks up.")),
            ],
        )
        response = await respond(client, story, body(player, None, do="waits by the window"))
        assert response.status_code == 200, response.text

    (narrator,) = schema_calls(stub, "NarratorRender")
    (character,) = schema_calls(stub, "CharacterRender")
    director = text_of(narrator)
    own = text_of(character)

    # The character's context: its definition as author notes (#115's paths).
    for phrase in (PERSONALITY, SPEECH, VOCABULARY, MANNERISM, SAMPLE_LINE):
        assert phrase in own, phrase
    for axis, value in DISPOSITIONS.items():
        assert DISPOSITION_PHRASES[axis][value["setting"]] in own, axis
    assert "Keeps the secrets she was trusted with." in own  # a disposition's nuance

    # The director's context: the drivers, inside the labelled author-notes block.
    assert DIRECTOR_HEADING in director
    block = director[director.index(DIRECTOR_HEADING) :]
    for driver in DRIVERS:
        assert driver in block, driver

    # Complement: the drivers reach no character's prompt in M4's shared mode.
    assert DIRECTOR_HEADING not in own
    for driver in DRIVERS:
        assert driver not in own, driver


@pytest.mark.part("M5", "FR-EPI-5")
async def test_c_25__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Jenni", "Marcus", "Bobby")
        await ps.perceive(engine, story, "full")
        parlor = await ps.place_entity(engine, story, "parlor")
        for name in ids:
            await ps.put(engine, story, ids[name], parlor)
        forged = await auto_predicate(engine, story, "forged the letter for")
        await commit(
            engine,
            story,
            Proposition(
                subject_id=ids["Jenni"],
                predicate_id=forged,
                object_id=ids["Wren"],
                valid_time=Interval.of(None, None),
            ),
            kind=OperationKind.CONTROL,
            provenance="author",
            visibility=Visibility(scope="characters", characters=(ids["Marcus"],)),
        )
        stub = epistrel.models.stub

        async def context_of(name: str) -> str:
            stub.calls.clear()
            stub.script("generation", [render(beat("do", f"{name} shrugs."))])
            answer = await respond(
                client,
                story,
                body(
                    ids["Wren"],
                    "Jenni, Marcus, Bobby: who wrote the letter for Wren?",
                    render_character_ids=[ids[name]],
                ),
            )
            assert answer.status_code == 200, answer.text
            (call,) = schema_calls(stub, "CharacterRender")
            return text_of(call)

        # The canon line names the auto-registered relation.
        assert "Jenni forged_the_letter_for Wren" in await context_of("Marcus")
        for name in ("Bobby", "Jenni"):
            assert "forged_the_letter_for" not in await context_of(name), name


DEED = "Tom keeps the deed."
"""The planned reveal's hidden truth: the Brother has the deed (secret, his and his mother's)."""


@pytest.mark.part("M5", "FR-EPI-5")
async def test_c_25__m5_planned_reveal(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """A planned reveal created at genesis under per-character partitions stays with the
    director (#173: the owner's decision): S-FAMILY's Sunday Dinner with a reveal whose truth
    (the Brother keeps the deed) names a holder and an audience. Its truth is one canon fact,
    ``engine``-scoped (omniscient only), provenance ``seed``; no partition holds it (no belief)
    and no memory carries it; no character's render context has its line. Complement: the
    Mother's own secret seed, limited the same way by its audience, is in her partition and her
    context, so the reveal's absence is its scope, not the read."""
    document = sp.family()
    document["world"]["entities"].append({"id": "deed", "name": "The deed", "kind": "item"})
    document["drivers"] = {
        "planned_reveals": [
            {
                "truth": {
                    "text": DEED,
                    "holder": "brother",
                    "audience": ["self", "mother"],
                    "disclosure": "secret",
                },
                "allowed_roles": ["mother"],
                "conditions": "Only after the will is read.",
            }
        ]
    }
    async with respond_client(stub_settings, engine) as (client, epistrel):
        profile = await sp.partitioned_profile(client)
        scenario_id, _ = await g.published_scenario(client, document)
        cast = await sp.cast(client)
        made = await g.create_from(client, sp.request(scenario_id, cast, profile))
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        ents = await sp.entity_ids(engine, story)
        deed, letters = ents["The deed"], ents["The love letters"]

        (fact,) = await sp.rows_of(
            engine,
            "SELECT f.visibility, f.provenance, p.subject_id FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.object_id = :o",
            s=story,
            o=deed,
        )
        assert (fact.visibility, fact.provenance) == ({"scope": "engine"}, "seed")
        assert str(fact.subject_id) == by_role["brother"]
        held = await sp.beliefs(engine, story)
        assert [r for r in held if str(r.object_id) == deed] == []
        memories = await sp.rows_of(
            engine, "SELECT id FROM memories WHERE story_id = :s AND text = :t", s=story, t=DEED
        )
        assert memories == []
        # The complement: the Mother's own secret is in her partition.
        assert [str(r.holder) for r in held if str(r.object_id) == letters] == [by_role["mother"]]

        stub = epistrel.models.stub
        sam = by_role["player"]

        async def context_of(role: str, name: str) -> str:
            stub.calls.clear()
            stub.script("generation", [render(beat("do", f"{name} nods."))])
            answer = await respond(
                client,
                story,
                body(sam, "Is dinner ready?", render_character_ids=[by_role[role]]),
            )
            assert answer.status_code == 200, answer.text
            (call,) = schema_calls(stub, "CharacterRender")
            return text_of(call)

        for role in sp.NPC_ROLES:
            seen = await context_of(role, sp.NAMES[role])
            assert "holds The deed" not in seen, role
            assert DEED not in seen, role
            if role == "mother":
                assert "holds The love letters" in seen


@pytest.mark.part("M5", "FR-EPI-5")
async def test_c_25__m5_planned_reveal_belief(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """#574: a planned reveal's truth that says ``kind: belief`` (holder the Brother, audience
    him and the Mother) is still one ``engine``-scoped canon fact at genesis, never a belief in
    the audience's partitions: the truth's kind does not bypass the reveal's scope."""
    document = sp.family()
    document["world"]["entities"].append({"id": "deed", "name": "The deed", "kind": "item"})
    document["drivers"] = {
        "planned_reveals": [
            {
                "truth": {
                    "text": DEED,
                    "kind": "belief",
                    "holder": "brother",
                    "audience": ["self", "mother"],
                    "disclosure": "secret",
                },
                "allowed_roles": ["mother"],
                "conditions": "Only after the will is read.",
            }
        ]
    }
    async with respond_client(stub_settings, engine) as (client, _):
        profile = await sp.partitioned_profile(client)
        scenario_id, _ = await g.published_scenario(client, document)
        cast = await sp.cast(client)
        made = await g.create_from(client, sp.request(scenario_id, cast, profile))
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        ents = await sp.entity_ids(engine, story)
        deed, letters = ents["The deed"], ents["The love letters"]

        (fact,) = await sp.rows_of(
            engine,
            "SELECT f.visibility, f.provenance, p.subject_id FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.object_id = :o",
            s=story,
            o=deed,
        )
        assert (fact.visibility, fact.provenance) == ({"scope": "engine"}, "seed")
        assert str(fact.subject_id) == by_role["brother"]
        held = await sp.beliefs(engine, story)
        assert [r for r in held if str(r.object_id) == deed] == []
        memories = await sp.rows_of(
            engine, "SELECT id FROM memories WHERE story_id = :s AND text = :t", s=story, t=DEED
        )
        assert memories == []
        # The complement: the Mother's own secret is in her partition.
        assert [str(r.holder) for r in held if str(r.object_id) == letters] == [by_role["mother"]]
