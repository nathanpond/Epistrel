"""S-PHONE: eager commitment, a lie and its reveal (docs/04 §7; FR-TURN-4,5,6,7, FR-COMMIT-2;
#166's verification half; #169 owns FR-COMMIT-4's hidden half).

Wren (the player), Jenni (the rendered character) and Bobby share the parlor, with per-character
partitions and testimony ``defaulted``. The story registers ``message_content`` (functional: a
message has one content). Played end to end through ``respond`` with the stub adapter.

1. Jenni's phone buzzes. The render's ``latent`` beat commits what the message says as canon
   scoped to Jenni (``characters: [Jenni]``, #133's scoped commit: #169 replaces this with
   ``commit_hidden``), so the truth is fixed at turn 1; Jenni reads it (her observation).
2. Bobby asks who it was. Jenni says "Oh, just a meme." to him, declared ``deceptive``: the claim
   is objectively false and contradicts what she believes, so it is a lie. It is stored as a
   speech act (an utterance: false / inconsistent / deceptive, ``lie``), never canon, and not
   repaired; a ``divergences`` row records the asserted proposition against her belief. Bobby,
   who trusts her and knows nothing to contradict it, believes it (FR-TURN-7: a deception the
   hearer does not detect is accepted on his normal trust rules).
3. The reveal: Jenni sincerely says what the message was. It matches the turn-1 truth, which no
   turn changed.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import FRIEND_OF
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
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


async def _phone_story(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], str]:
    """The room, the phone and its two possible contents, and ``message_content``."""
    story, ids, _ = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
    await ts.enable_testimony(engine, story, "defaulted")
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


@pytest.mark.part("M5", "FR-TURN-4,5,6,7; FR-COMMIT-2")
async def test_s_phone(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
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
        # She read it: her own observation of what it says.
        read = Proposition(
            subject_id=entity[PHONE],
            predicate_id=content,
            object_id=entity[TRUTH],
            valid_time=Interval.of(None, None),
        )
        observed = await ts.observed(engine, story, jenni, read)

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
        ) == (lie.id, jenni, observed.belief.id, 2)
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
        assert [(f.id, f.object_id, f.tx_to) for f in await _contents(engine, story, content)] == [
            (truth.id, entity[TRUTH], None)
        ]
        # The divergence persists, queryable.
        kept = await rows(engine, "SELECT id FROM divergences WHERE story_id = :s", s=story)
        assert [d.id for d in kept] == [divergence.id]
