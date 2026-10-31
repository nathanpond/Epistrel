"""S-PHONE: eager commitment, a lie and its reveal (docs/04 §7; FR-TURN-4,5,6,7, FR-COMMIT-2,4;
#166's verification half, #169's hidden half).

Wren (the player), Jenni (the rendered character) and Bobby share the parlor, with per-character
partitions and testimony ``defaulted``. The story registers ``message_content`` (functional: a
message has one content). Played end to end through ``respond`` with the stub adapter.

1. Jenni's phone buzzes. The render's ``latent`` beat commits what the message says as a
   hidden fact (#169's ``commit_hidden``): canon scoped to Jenni (``characters: [Jenni]``), so
   the truth is fixed at turn 1, and her observation of it in her partition (``known``, citing
   the latent beat). ``/inspect/ledger`` lists it under ``hidden_facts`` for ``omniscient``
   only.
2. Bobby asks who it was. Jenni says "Oh, just a meme." to him, declared ``deceptive``: the claim
   is objectively false and contradicts what she believes, so it is a lie. It is stored as a
   speech act (an utterance: false / inconsistent / deceptive, ``lie``), never canon, and not
   repaired; a ``divergences`` row records the asserted proposition against her belief. Bobby,
   who trusts her and knows nothing to contradict it, believes it (FR-TURN-7: a deception the
   hearer does not detect is accepted on his normal trust rules).
3. The reveal: Jenni sincerely says what the message was. It matches the turn-1 truth, which no
   turn changed: the fact is widened (superseded with the same value, scoped to Jenni and the
   say's content perceivers, Bobby and Wren), and the lie's divergence is marked revealed by the
   reveal's beat (``revealed_by_event_id``).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import FRIEND_OF
from epistrel.core.resolution.context import load_index
from epistrel.core.verify.protected import load_protected_index
from epistrel.core.visibility import Visibility
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.event_support import rows
from tests.respond_support import body, played, respond, respond_client
from tests.story_support import READ
from tests.structured_turn_support import beat, claim, render, turn_report, utterances

pytestmark = pytest.mark.integration

PHONE = "the phone"
TRUTH = "the breakup text"
MEME = "the meme"
LIE_LINE = "Oh, just a meme."
REVEAL_LINE = "It was the breakup text."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _phone_story(
    client: Any,
    engine: AsyncEngine,
    *away: str,
    level: str = "full",
) -> tuple[str, dict[str, str], str]:
    """The room, the phone and its two possible contents, and ``message_content`` (``away``:
    more characters, in the kitchen; ``level``: the perception level)."""
    story, ids, _ = await ts.room(client, engine, "Wren", "Jenni", "Bobby", *away, away=away)
    await ts.enable_testimony(engine, story, "defaulted", level=level)
    async with engine.begin() as conn:
        for name, kind in ((PHONE, "item"), (TRUTH, "other"), (MEME, "other")):
            await store.add_entity(conn, story, kind, name)
        rules = registry.ScenarioRules.parse(
            {
                "predicates": [
                    {
                        "name": "message_content",
                        "subject_type": "item",
                        "object_type": "other",
                        "cardinality": "functional",
                        "temporal_kind": "permanent",
                        "docs": "What the message on a phone says.",
                    }
                ]
            }
        )
        found = await registry.register_author(conn, story, rules)
    content = found.named("message_content")
    assert content is not None
    # Bobby trusts Jenni (#162's trust seed).
    await ps._author(
        engine,
        story,
        Proposition(
            subject_id=ids["Bobby"],
            predicate_id=FRIEND_OF,
            object_id=ids["Jenni"],
            valid_time=Interval.of(None, None),
        ),
    )
    return story, ids, content.id


async def _turn(client: Any, story: str, ids: dict[str, str], line: str, *beats: Any) -> Any:
    response = await respond(client, story, body(ids["Wren"], line))
    assert response.status_code == 200, response.text
    return response.json()


async def _contents(engine: AsyncEngine, story: str, predicate: str) -> list[Any]:
    """The canon facts of ``message_content``, current or not."""
    return await rows(
        engine,
        "SELECT f.id, f.narrative_turn, f.visibility, f.tx_to, f.superseded_by, p.object_id "
        "FROM facts f JOIN propositions p ON p.id = f.proposition_id WHERE f.story_id = :s "
        "AND f.predicate_id = :p ORDER BY f.id",
        s=story,
        p=predicate,
    )


@pytest.mark.part("M5", "FR-TURN-4,5,6,7; FR-COMMIT-2,4")
async def test_s_phone(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, content = await _phone_story(client, engine)
        jenni, bobby = ids["Jenni"], ids["Bobby"]
        entity = {
            r.canonical_name: str(r.id)
            for r in await rows(
                engine, "SELECT id, canonical_name FROM entities WHERE story_id = :s", s=story
            )
        }

        # -- turn 1: the truth, scoped to Jenni
        stub.script(
            "generation",
            [
                render(
                    beat("do", "Her phone buzzes. She glances at it."),
                    beat(
                        "latent",
                        "The message is the breakup text.",
                        visibility={"scope": "characters", "characters": [jenni]},
                        propositions=[claim(PHONE, "message_content", TRUTH)],
                    ),
                )
            ],
        )
        await _turn(client, story, ids, "Is that your phone?")
        (truth,) = await _contents(engine, story, content)
        assert (truth.narrative_turn, truth.object_id) == (1, entity[TRUTH])
        assert truth.visibility == {"scope": "characters", "characters": [jenni]}
        # She read it: her own observation of what it says, made by the hidden commit.
        (hidden,) = (await turn_report(engine, story))["commit"]["hidden_commits"]
        assert (hidden["fact_id"], hidden["characters"]) == (truth.id, [jenni])
        assert len(hidden["observation_transition_ids"]) == 1
        (observed,) = [
            b
            for b in ts.first_order(await ts.beliefs_of(engine, story, jenni))
            if b.predicate_id == content
        ]
        assert (observed.object_id, observed.cause, observed.confidence_level) == (
            entity[TRUTH],
            "observation",
            "known",
        )
        assert observed.antecedent_ref["table"] == "events"
        # Nobody else knows: Bobby holds nothing about it; the ledger shows it to omniscient.
        assert not [
            b for b in await ts.beliefs_of(engine, story, bobby) if b.predicate_id == content
        ]
        ledger = await client.get(
            f"/stories/{story}/inspect/ledger", params={"viewpoint": "omniscient"}, headers=READ
        )
        assert ledger.status_code == 200, ledger.text
        assert [(f["fact_id"], f["characters"]) for f in ledger.json()["hidden_facts"]] == [
            (truth.id, [jenni])
        ]
        refused = await client.get(
            f"/stories/{story}/inspect/ledger",
            params={"viewpoint": f"character:{bobby}"},
            headers=READ,
        )
        assert refused.status_code == 403

        # -- turn 2: the lie
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        LIE_LINE,
                        intent="deceptive",
                        addressee=bobby,
                        propositions=[claim(PHONE, "message_content", MEME)],
                    )
                )
            ],
        )
        played = await _turn(client, story, ids, "Who was that?")
        assert LIE_LINE in played["message"]["text"]
        report = await turn_report(engine, story)
        assert "repairs" not in report
        (lie,) = [u for u in await utterances(engine, story) if u.speaker_character_id == jenni]
        assert (
            lie.objective_relation,
            lie.belief_relation,
            lie.intent,
            lie.classification,
        ) == ("false", "inconsistent", "deceptive", "lie")
        (divergence,) = await rows(engine, "SELECT * FROM divergences WHERE story_id = :s", s=story)
        assert (
            divergence.utterance_id,
            divergence.speaker_character_id,
            divergence.believed_belief_id,
            divergence.position_turn,
        ) == (lie.id, jenni, observed.id, 2)
        assert divergence.asserted_proposition_id in lie.proposition_ids
        assert report["verify"]["divergences_written"] == 1
        # Being said made nothing true: canon still holds the turn-1 content only.
        assert [(f.object_id, f.tx_to) for f in await _contents(engine, story, content)] == [
            (entity[TRUTH], None)
        ]
        # Bobby, deceived, believes the meme on his trust in her.
        heard = [
            b
            for b in ts.first_order(await ts.beliefs_of(engine, story, bobby))
            if b.predicate_id == content
        ]
        assert [(b.object_id, b.cause, b.source_character_id) for b in heard] == [
            (entity[MEME], "testimony", jenni)
        ]

        # -- turn 3: the reveal matches the turn-1 truth
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        REVEAL_LINE,
                        intent="sincere",
                        addressee=bobby,
                        propositions=[claim(PHONE, "message_content", TRUTH)],
                    )
                )
            ],
        )
        await _turn(client, story, ids, "Really?")
        reveal = [u for u in await utterances(engine, story) if u.speaker_character_id == jenni][-1]
        assert (reveal.objective_relation, reveal.belief_relation, reveal.classification) == (
            "true",
            "consistent",
            "sincere",
        )
        # The reveal widened the turn-1 truth (same value, one current version).
        (closed, widened) = await _contents(engine, story, content)
        assert (closed.id, closed.object_id, closed.superseded_by) == (
            truth.id,
            entity[TRUTH],
            widened.id,
        )
        assert closed.tx_to is not None
        assert (widened.object_id, widened.tx_to) == (entity[TRUTH], None)
        assert widened.visibility == {
            "scope": "characters",
            "characters": sorted([jenni, bobby, ids["Wren"]]),
        }
        (revealed,) = (await turn_report(engine, story))["commit"]["reveals"]
        assert (revealed["fact_id"], revealed["superseded_id"]) == (widened.id, truth.id)
        assert revealed["divergence_ids"] == [divergence.id]
        # The divergence persists, queryable, settled by the reveal's beat.
        (reveal_event,) = await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'beat_say' "
            "AND narrative_turn = 3 AND character_id = :c",
            s=story,
            c=jenni,
        )
        kept = await rows(
            engine, "SELECT id, revealed_by_event_id FROM divergences WHERE story_id = :s", s=story
        )
        assert [(d.id, d.revealed_by_event_id) for d in kept] == [(divergence.id, reveal_event.id)]


# -- a narrated reveal of a hidden fact (#566) ---------------------------------------------------

NARRATED = "In the parlor, all can read the message on the phone: the breakup text."


async def _protects_truth(engine: AsyncEngine, story: str, speaker: str) -> bool:
    """``speaker``'s protected index flags the truth's words."""
    async with engine.connect() as conn:
        index = await load_index(conn, story)
        protected = await load_protected_index(conn, story, speaker, index=index)
    return protected is not None and protected.match(REVEAL_LINE) != []


@pytest.mark.part("M5", "FR-TURN-4,5,6,7; FR-COMMIT-2,4")
async def test_s_phone__narrated_reveal(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """#566: a perceived narration stating a hidden fact (any predicate, not only #546's
    ``same_as``) widens it to the beat's content perceivers, as a say or do reveal does. At
    perception ``co_location`` the truth is scoped to Bobby; a narration in the parlor shows it,
    with Jenni and Wren there and Marcus in the kitchen. The fact widens to the parlor (Marcus
    not joining its scope), the turn reports the reveal, Jenni's protected index stops flagging
    it and her later sincere line saying it is not repaired. Complement: before the narration
    her index flags it, and Marcus, who perceived nothing, is still flagged after."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, content = await _phone_story(client, engine, "Marcus", level="co_location")
        jenni, bobby, wren, marcus = ids["Jenni"], ids["Bobby"], ids["Wren"], ids["Marcus"]
        entity = {
            r.canonical_name: str(r.id)
            for r in await rows(
                engine, "SELECT id, canonical_name FROM entities WHERE story_id = :s", s=story
            )
        }
        truth = await commit(
            engine,
            story,
            Proposition(
                subject_id=entity[PHONE],
                predicate_id=content,
                object_id=entity[TRUTH],
                valid_time=Interval.of(None, None),
            ),
            provenance="author",
            visibility=Visibility(scope="characters", characters=(bobby,)),
        )
        assert await _protects_truth(engine, story, jenni)
        assert await _protects_truth(engine, story, marcus)

        # -- the narration, in the parlor
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "narration",
                        NARRATED,
                        location="parlor",
                        propositions=[claim(PHONE, "message_content", TRUTH)],
                    )
                ),
                render(beat("think", "So now they know.")),
            ],
        )
        await played(
            client, story, body(wren, None, do="Wren waits.", render_character_ids=[bobby])
        )
        (closed, widened) = await _contents(engine, story, content)
        assert (closed.id, closed.superseded_by, widened.tx_to) == (truth.id, widened.id, None)
        assert widened.visibility == {
            "scope": "characters",
            "characters": sorted([jenni, bobby, wren]),
        }
        (revealed,) = (await turn_report(engine, story))["commit"]["reveals"]
        assert (revealed["fact_id"], revealed["superseded_id"]) == (widened.id, truth.id)
        assert not await _protects_truth(engine, story, jenni)
        assert await _protects_truth(engine, story, marcus)

        # -- Jenni says it: not protected content for her any more, so nothing is repaired
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        REVEAL_LINE,
                        intent="sincere",
                        addressee=bobby,
                        propositions=[claim(PHONE, "message_content", TRUTH)],
                    )
                )
            ],
        )
        said = await _turn(client, story, ids, "What did it say?")
        assert REVEAL_LINE in said["message"]["text"]
        assert "repairs" not in await turn_report(engine, story)
        (line,) = [u for u in await utterances(engine, story) if u.speaker_character_id == jenni]
        assert (line.objective_relation, line.classification) == ("true", "sincere")
