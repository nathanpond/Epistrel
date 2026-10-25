"""S-ENTITY-IDENTITY: identity as a held fact (docs/04 §7), its M5 part (FR-PROP-7; #171).

Structured turns on the stub under per-character partitions (perception ``full``, testimony
``defaulted``). Wren (the player) and Jenni are in the parlor; Bobby is on the porch. "The
stranger" is an entity of its own; canon holds that Jenni knows Bobby, and Jenni, who once saw
the stranger and did not know him, holds that she does not know the stranger.

- **Early turns.** A narration in the parlor shows "the stranger": it resolves to the stranger
  entity, for canon and for everyone, with no representative.
- **The narrated reveal** (on the porch, Jenni elsewhere): "the stranger is Bobby" is committed
  as ``same_as(stranger, Bobby)`` in canon, scoped ``characters: [Bobby]`` (the narration's
  perceiver), with Bobby's observation belief; Jenni holds nothing of it. The engine files an
  identity proposal for the pair (no merge). Holder-aware resolution (#139/#140's expansion
  through ``core.resolution.identity``): for canon and for Bobby "the stranger" resolves with
  Bobby as its representative, the verifier's record set expands through the same ``same_as``,
  retrieval's query entities and alias arm reach Bobby; for Jenni the stranger stays the
  stranger. Her protected-content index (#148) protects the pairing (a clause naming both),
  never either name alone. ``/inspect/beliefs`` lists Bobby's identity, not Jenni's.
- **Jenni's line** "I've never met that stranger." is consistent with her beliefs and false in
  canon (labelled ``sincere_error``, objective relation false), and never repaired.
- **Merges are M7's**: a canon ``same_as`` between two characters is 422
  ``identity_merge_not_yet``; ``merge_entities`` raises it; ``entities/merge`` has no row in the
  FR-API-6 route table and docs/02 §9 lists it, with ``entities/split``, as M7.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import modeling
from epistrel.core.errors import IdentityMergeNotYet
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution import identity
from epistrel.core.resolution.context import load_context, load_index, load_time_basis
from epistrel.core.resolution.entities import NARRATOR, resolve_mentions
from epistrel.core.retrieval.pipeline import resolve_query
from epistrel.core.verify.protected import load_protected_index
from epistrel.core.verify.rules import Holder, VerdictKind, check_with
from epistrel.core.visibility import Visibility
from epistrel.orchestrator.verify.extraction import load_env
from tests import partition_support as parts
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.story_support import READ, WRITE, override
from tests.structured_turn_support import beat, claim, render, structured_story, turn_report

pytestmark = pytest.mark.integration

SAME_AS = CORE_BY_NAME["same_as"].id
KNOWS = CORE_BY_NAME["knows"].id
ROOT = Path(__file__).resolve().parents[2]
EARLY = "A stranger in a hooded coat watches the parlor from the doorway, then is gone."
REVEAL = "On the porch, the stranger pushes back his hood: the stranger is Bobby."
NEVER = "I've never met that stranger."
WAIT = "Wren waits by the window."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _never(ids: dict[str, str]) -> Proposition:
    """Jenni does not know the stranger (her line's claim, as it normalizes for her)."""
    return Proposition(
        subject_id=ids["Jenni"],
        predicate_id=KNOWS,
        object_id=ids["the stranger"],
        polarity=Polarity.NEGATED,
        valid_time=Interval.of(None, None),
    )


async def _house(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str]]:
    story, ids = await structured_story(client, "Wren", "Jenni", "Bobby")
    await ts.enable_testimony(engine, story, "defaulted")
    await override(client, story, {"budgets": {"model_calls": 20}})
    for name in ("parlor", "porch"):
        ids[name] = await ps.place_entity(engine, story, name)
    async with engine.begin() as conn:
        ids["the stranger"] = await store.add_entity(conn, story, "person", "the stranger")
    for name, place in (("Wren", "parlor"), ("Jenni", "parlor"), ("Bobby", "porch")):
        await ps.put(engine, story, ids[name], ids[place])
    # Canon: Jenni knows Bobby. Her own view: she does not know the stranger.
    await commit(
        engine,
        story,
        Proposition(
            subject_id=ids["Jenni"],
            predicate_id=KNOWS,
            object_id=ids["Bobby"],
            valid_time=Interval.of(None, None),
        ),
        provenance="author",
    )
    await parts.believe(engine, story, ids["Jenni"], _never(ids))
    return story, ids


async def _identities(engine: AsyncEngine, story: str, holder: str) -> identity.Identities:
    async with engine.connect() as conn:
        lookup = KeyedLookup(conn, StructuredReadSet())
        return await identity.identities_for(
            lookup, story, holder, beliefs=holder != identity.CANON
        )


async def _resolved(engine: AsyncEngine, story: str, speaker: str, holder: str) -> Any:
    """How ``speaker`` resolves "the stranger" (with ``holder``'s identities)."""
    held = await _identities(engine, story, holder)
    async with engine.connect() as conn:
        index = await load_index(conn, story)
        context = await load_context(
            conn, story, position=(3, 0), speaker=speaker, index=index, identities=held
        )
        basis = await load_time_basis(conn, story)
    (mention,) = resolve_mentions("Then the stranger left.", speaker, context)
    query = resolve_query(
        "What did the stranger do?",
        speaker=speaker,
        position=(3, 0),
        context=context,
        basis=basis,
    )
    return mention, query


async def _beliefs_body(client: Any, story: str, character: str) -> dict[str, Any]:
    response = await client.get(
        f"/stories/{story}/inspect/beliefs",
        params={"character": character, "viewpoint": "omniscient"},
        headers=READ,
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


@pytest.mark.part("M5", "FR-PROP-7")
async def test_s_entity_identity__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _house(client, engine)
        jenni, bobby, wren, stranger = ids["Jenni"], ids["Bobby"], ids["Wren"], ids["the stranger"]

        # -- early turns: the stranger, as itself for everyone
        # The player's input addresses nobody, so the narrator renders first (#150).
        stub.script("generation", [render(beat("narration", EARLY)), render(beat("think", "Odd."))])
        await played(client, story, body(wren, None, do=WAIT, render_character_ids=[jenni]))
        assert (await turn_report(engine, story))["render"]["narrator"] is True
        for speaker, holder in ((NARRATOR, identity.CANON), (jenni, jenni), (bobby, bobby)):
            mention, _ = await _resolved(engine, story, speaker, holder)
            assert (mention.entity_id, mention.representative_id) == (stranger, None)

        # -- the narrated reveal, on the porch (Jenni and Wren in the parlor)
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "narration",
                        REVEAL,
                        location="porch",
                        propositions=[claim("the stranger", "same_as", "Bobby")],
                    )
                ),
                render(beat("think", "Time to go in.")),
            ],
        )
        await played(client, story, body(wren, None, do=WAIT, render_character_ids=[bobby]))
        (fact,) = await rows(
            engine,
            "SELECT f.id, f.visibility, p.subject_id, p.object_id FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id WHERE f.story_id = :s "
            "AND f.predicate_id = :p AND f.tx_to IS NULL AND f.membership = 'active'",
            s=story,
            p=SAME_AS,
        )
        assert {fact.subject_id, fact.object_id} == {stranger, bobby}
        assert fact.visibility == {"scope": "characters", "characters": [bobby]}
        report = await turn_report(engine, story)
        assert [h["fact_id"] for h in report["commit"]["hidden_commits"]] == [fact.id]
        # Bobby observed it; Jenni holds nothing of it.
        held = [
            b
            for b in ts.first_order(await ts.beliefs_of(engine, story, bobby))
            if b.predicate_id == SAME_AS
        ]
        assert [(b.cause, b.confidence_level) for b in held] == [("observation", "known")]
        assert not [
            b
            for b in ts.first_order(await ts.beliefs_of(engine, story, jenni))
            if b.predicate_id == SAME_AS
        ]
        # An identity proposal, never a merge.
        async with engine.connect() as conn:
            (proposal,) = await registry.identity_proposals(conn, story)
        assert proposal["entities"] == sorted((stranger, bobby))
        assert (proposal["status"], proposal["reason"]) == ("pending", "canon_same_as")
        assert proposal["evidence"]["fact_ids"] == [fact.id]
        (entity,) = await rows(engine, "SELECT merged_into FROM entities WHERE id = :e", e=stranger)
        assert entity.merged_into is None

        # Holder-aware resolution: Bobby for canon and for Bobby, the stranger for Jenni.
        for speaker, holder, rep in (
            (NARRATOR, identity.CANON, bobby),
            (bobby, bobby, bobby),
            (jenni, jenni, None),
        ):
            mention, query = await _resolved(engine, story, speaker, holder)
            assert (mention.entity_id, mention.representative_id) == (stranger, rep), holder
            reached = bobby in query.entities
            assert reached is (rep is not None), holder
        # The verifier's expansion is the holder's: in canon, Jenni not knowing the stranger
        # contradicts her knowing Bobby; over her own partition it is what she believes.
        async with engine.connect() as conn:
            lookup = KeyedLookup(conn, StructuredReadSet())
            assert (await check_with(lookup, story, _never(ids))).kind is VerdictKind.CONTRADICTED
            assert (
                await check_with(lookup, story, _never(ids), holder=Holder.belief(jenni))
            ).kind is VerdictKind.SUPPORTED
            # #148: the pairing is protected for Jenni, never either name alone; Bobby may say it.
            index = await load_index(conn, story)
            protected = await load_protected_index(conn, story, jenni, index=index)
            assert protected is not None
            (hit,) = [h for h in protected.match("He said the stranger is Bobby.")]
            assert hit[2].proposition is not None
            assert hit[2].proposition.predicate_id == SAME_AS
            assert protected.match(NEVER) == []
            assert protected.match("Bobby waved. Later the stranger left.") == []
            for_bobby = await load_protected_index(conn, story, bobby, index=index)
            assert for_bobby is None or for_bobby.match("The stranger is Bobby.") == []
        # #163's modeling: the stranger is Bobby now, so an interaction never models him anew.
        async with engine.connect() as conn:
            canon_held = await identity.identities_for(
                KeyedLookup(conn, StructuredReadSet()), story, identity.CANON
            )
            linked = identity.linked_characters(
                canon_held, [e for p in canon_held.pairs() for e in p]
            )
            known = await modeling.load_entities(conn, story, linked=linked)
        assert known[stranger].character_id == bobby
        waved = modeling.BeatView("do", 2, "Jenni waves to the stranger.", jenni)
        assert modeling.candidates([waved], known) == []
        # /inspect/beliefs: identity beliefs per holder.
        shown = await _beliefs_body(client, story, bobby)
        assert {tuple(i["entities"]) for i in shown["identities"]} == {
            tuple(sorted((stranger, bobby)))
        }
        assert {i["source"] for i in shown["identities"]} == {"canon", "belief"}
        assert (await _beliefs_body(client, story, jenni))["identities"] == []

        # -- Jenni's line: belief-consistent, false in canon, never repaired
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        NEVER,
                        intent="sincere",
                        propositions=[claim("I", "knows", "the stranger", negated=True)],
                    )
                )
            ],
        )
        said = await played(
            client, story, body(wren, "Jenni, who was that?", render_character_ids=[jenni])
        )
        assert NEVER in said["message"]["text"]
        report = await turn_report(engine, story)
        assert "repairs" not in report
        (speech,) = [v["speech"] for v in report["verify"]["verdicts"] if "speech" in v]
        assert (speech["classification"], speech["belief_relation"]) == (
            "sincere_error",
            "consistent",
        )
        (line,) = await rows(
            engine,
            "SELECT classification, belief_relation, objective_relation FROM utterances "
            "WHERE story_id = :s AND speaker_character_id = :j AND classification IS NOT NULL",
            s=story,
            j=jenni,
        )
        assert (line.classification, line.belief_relation, line.objective_relation) == (
            "sincere_error",
            "consistent",
            "false",
        )

        # -- merges and splits are M7's
        refused = await client.post(
            f"/admin/stories/{story}/facts",
            json={
                "proposition": {
                    "subject_id": jenni,
                    "predicate_id": SAME_AS,
                    "object_id": bobby,
                    "valid_time": {"kind": "interval", "start": None, "end": None},
                }
            },
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "identity_merge_not_yet"
        async with engine.connect() as conn:
            with pytest.raises(IdentityMergeNotYet):
                await store.merge_entities(conn, story, stranger, bobby)
    table = json.loads((ROOT / "tests/fixtures/api/fr_api_6_routes.json").read_text())
    paths = {
        r["path"]
        for group in ("routes", "extras", "outside")
        for r in table.get(group, [])
        if isinstance(r, dict) and "path" in r
    }
    assert not any("entities/merge" in p or "entities/split" in p for p in paths)
    section = (ROOT / "docs/02-technology-architecture.md").read_text().split("## 9.")[1]
    section = section.split("\n## ")[0]
    assert "entities/merge" in section
    assert "entities/split" in section
    assert "M7" in section


# -- #171 AC4: identity widening follows #169's reveal rule and #161's routing (#546) ------------

PAIRING = "He said the stranger is Bobby."
REASSERT = {
    "say": beat(
        "say",
        "I am the stranger, Jenni.",
        intent="sincere",
        propositions=[claim("I", "same_as", "the stranger")],
    ),
    "do": beat(
        "do",
        "Bobby pulls on the hooded coat: he is the stranger.",
        propositions=[claim("I", "same_as", "the stranger")],
    ),
    "narration": beat(
        "narration",
        "In the parlor, all can see it: the stranger is Bobby.",
        location="parlor",
        propositions=[claim("the stranger", "same_as", "Bobby")],
    ),
}


async def _same_as(engine: AsyncEngine, story: str) -> Any:
    (fact,) = await rows(
        engine,
        "SELECT f.id, f.visibility FROM facts f WHERE f.story_id = :s AND f.predicate_id = :p "
        "AND f.tx_to IS NULL AND f.membership = 'active'",
        s=story,
        p=SAME_AS,
    )
    return fact


async def _protects_pairing(engine: AsyncEngine, story: str, speaker: str) -> bool:
    async with engine.connect() as conn:
        index = await load_index(conn, story)
        protected = await load_protected_index(conn, story, speaker, index=index)
    return protected is not None and protected.match(PAIRING) != []


@pytest.mark.part("M5", "FR-PROP-7")
@pytest.mark.parametrize("kind", ["say", "do", "narration"])
async def test_s_entity_identity__m5_widening(
    kind: str,
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """#171 AC4 (#546): after the porch reveal (``same_as(stranger, Bobby)`` scoped to Bobby),
    Bobby comes into the parlor and the identity is re-asserted where Jenni perceives it: by
    Bobby's sincere ``say``, his ``do``, or a narration at the parlor. Each widens the canon
    fact like #169's reveal (Jenni joins its scope, the turn reports the reveal), so Jenni now
    resolves "the stranger" to Bobby and her protected index no longer flags the pairing.
    Complement: before the re-assertion it does, and she resolves the stranger as itself."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _house(client, engine)
        jenni, bobby, wren, stranger = ids["Jenni"], ids["Bobby"], ids["Wren"], ids["the stranger"]
        stub.script("generation", [render(beat("narration", EARLY)), render(beat("think", "Odd."))])
        await played(client, story, body(wren, None, do=WAIT, render_character_ids=[jenni]))
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "narration",
                        REVEAL,
                        location="porch",
                        propositions=[claim("the stranger", "same_as", "Bobby")],
                    )
                ),
                render(beat("think", "Time to go in.")),
            ],
        )
        await played(client, story, body(wren, None, do=WAIT, render_character_ids=[bobby]))
        hidden = await _same_as(engine, story)
        assert hidden.visibility == {"scope": "characters", "characters": [bobby]}
        assert await _protects_pairing(engine, story, jenni)
        mention, _ = await _resolved(engine, story, jenni, jenni)
        assert mention.representative_id is None

        await ps.put(engine, story, bobby, ids["parlor"])
        stub.reset()
        if kind == "narration":
            stub.script(
                "generation", [render(REASSERT[kind]), render(beat("think", "So it is out."))]
            )
            request = body(wren, None, do=WAIT, render_character_ids=[bobby])
        else:
            stub.script("generation", [render(REASSERT[kind])])
            request = body(wren, "Bobby, is that you?", render_character_ids=[bobby])
        await played(client, story, request)
        report = await turn_report(engine, story)
        widened = await _same_as(engine, story)
        assert widened.visibility["scope"] == "characters"
        assert jenni in widened.visibility["characters"], kind
        assert bobby in widened.visibility["characters"], kind
        assert [(r["superseded_id"], r["fact_id"]) for r in report["commit"]["reveals"]] == [
            (hidden.id, widened.id)
        ], kind
        assert not await _protects_pairing(engine, story, jenni), kind
        mention, _ = await _resolved(engine, story, jenni, jenni)
        assert (mention.entity_id, mention.representative_id) == (stranger, bobby), kind


@pytest.mark.part("M5", "FR-PROP-7")
async def test_s_entity_identity__m5_held_by_belief(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """#546: one helper (``identity.speaker_identities``) feeds the protected index and the
    turn-time mention resolver (the detection environment's ``load_context(identities=)``). A
    hidden ``same_as(stranger, Bobby)`` scoped to Bobby protects the pairing for Jenni and she
    resolves the stranger as itself; once Jenni holds the identity as her own belief, her
    protected index no longer flags the pairing and her mentions resolve to Bobby. Complement:
    the canon fact is still hidden from her (its scope unchanged)."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await _house(client, engine)
    jenni, bobby, stranger = ids["Jenni"], ids["Bobby"], ids["the stranger"]
    same = Proposition(
        subject_id=stranger,
        predicate_id=SAME_AS,
        object_id=bobby,
        valid_time=Interval.of(None, None),
    )
    await commit(
        engine,
        story,
        same,
        provenance="director",
        visibility=Visibility(scope="characters", characters=(bobby,)),
    )

    async def env_mention() -> Any:
        async with engine.connect() as conn:
            env = await load_env(
                conn, story, StructuredReadSet(), position=(3, 0), speakers=[jenni]
            )
        (mention,) = resolve_mentions("Then the stranger left.", jenni, env.context_for(jenni))
        return mention

    assert await _protects_pairing(engine, story, jenni)
    assert (await env_mention()).representative_id is None
    await parts.believe(engine, story, jenni, same)
    assert not await _protects_pairing(engine, story, jenni)
    mention = await env_mention()
    assert (mention.entity_id, mention.representative_id) == (stranger, bobby)
    fact = await _same_as(engine, story)
    assert fact.visibility == {"scope": "characters", "characters": [bobby]}
