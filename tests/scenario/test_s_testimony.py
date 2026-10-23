"""S-TESTIMONY: hearing is not believing (docs/04 §7; FR-BELIEF-1,2,3,9; #162).

Jenni (the rendered character) asserts "Olivia sent the text" to the parlor, played end to end
through ``respond`` with the stub adapter under per-character partitions, perception ``full``
and ``character_knowledge.testimony = model``:

- the speech act is recorded for all: one utterance row, and every content witness with a
  partition holds a ``said`` belief (``kind = speech_act``: Jenni said it) whatever it made of
  it;
- Bobby (``friend_of`` Jenni, so trusting) is salient (the salience step marked the say) and is
  adjudicated (the stub echoes the rules' default): he forms a first-order ``testimony`` belief
  at ``believed`` from Jenni;
- Marcus (a prior observation: Olivia did not send it) is salient by the partition-touch rule;
  his records contradict the claim, so no model is asked and he rejects: his prior belief
  stands and he holds only the ``said`` belief;
- Sarah (in the kitchen) perceives nothing and gets nothing;
- a background witness (Kid, a minor, not addressed) is never salient and forms no belief here
  (its exposure record is #164's);
- the player's character has no partition; the speaker does not testify to itself.

Every testimony transition cites the say beat's event and sits at its step 1. Complements:
under ``commitment.salience = rules`` no adjudication is ever called (Marcus still rejects by
the default, Bobby's beat is not salient and he is left to #164's exposure path), and with
``partitions`` off nothing here runs (no belief at all).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import FRIEND_OF
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, render, turn_report, utterances

pytestmark = pytest.mark.integration

NAMES = ("Wren", "Jenni", "Bobby", "Marcus", "Sarah", "Kid")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _assertion() -> str:
    return render(
        beat(
            "say",
            ts.CLAIM_LINE,
            intent="sincere",
            propositions=[claim("Olivia", "sent", "the text")],
        )
    )


async def _setup(
    client: Any, engine: AsyncEngine, **testify: Any
) -> tuple[str, dict[str, str], dict[str, str]]:
    story, ids, things = await ts.room(client, engine, *NAMES, away=("Sarah",))
    await ts.enable_testimony(engine, story, **testify)
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
    await ts.make_minor(engine, ids["Kid"])
    await ts.observed(engine, story, ids["Marcus"], ts.sent(things, Polarity.NEGATED))
    return story, ids, things


async def _play(
    client: Any,
    epistrel: Any,
    story: str,
    ids: dict[str, str],
    when: tuple[str, str] | None = None,
) -> None:
    stub = epistrel.models.stub
    stub.reset()
    if when is not None:
        stub.when(when[0], when[1], purpose="belief_adjudication")
    stub.script("generation", [_assertion()])
    stub.script("salience_detection", [ts.salient_say(2)])
    response = await respond(client, story, body(ids["Wren"], "What happened?"))
    assert response.status_code == 200, response.text


@pytest.mark.part("M5", "FR-BELIEF-1,2,3,9")
async def test_s_testimony(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """See the module docstring (the ``model`` play)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, things = await _setup(client, engine)
        # Marcus's model "accepts" everything: the guard keeps his contradicted claim rejected.
        epistrel.models.stub.reset()
        await _play(
            client,
            epistrel,
            story,
            ids,
            when=("Marcus (trust", ts.adjudicated("accept", 0.95)),
        )
        stub = epistrel.models.stub
        calls = [c for c in stub.calls if c.purpose == "belief_adjudication"]
        # Bobby's and Marcus's: the say also carries a claim Marcus's records leave open. The
        # background minor is never adjudicated.
        witnesses = sorted(
            n for c in calls for n in NAMES if f"{n} (trust in Jenni" in c.messages[0].content
        )
        assert witnesses == ["Bobby", "Marcus"]

        # The speech act is recorded once, for everyone.
        spoken = [
            u for u in await utterances(engine, story) if u.speaker_character_id == ids["Jenni"]
        ]
        assert len(spoken) == 1
        (say_event,) = await rows(
            engine,
            "SELECT id, beat FROM events WHERE story_id = :s AND type = 'beat_say' "
            "AND character_id = :c",
            s=story,
            c=ids["Jenni"],
        )

        def about_sending(found: list[Any]) -> list[Any]:
            return [b for b in found if b.predicate_id == things["sent"]]

        # Bobby: a testimony belief from Jenni at believed, and the said belief.
        bobby = await ts.beliefs_of(engine, story, ids["Bobby"])
        (held,) = about_sending(ts.first_order(bobby))
        assert (held.subject_id, held.object_id, held.polarity) == (
            things["Olivia"],
            things["the text"],
            "affirmed",
        )
        assert (held.cause, held.source_character_id, held.outcome) == (
            "testimony",
            ids["Jenni"],
            "accept",
        )
        assert held.confidence_level == "believed"
        (heard,) = about_sending(ts.said(bobby))
        assert (heard.source_character_id, heard.outcome, heard.confidence_level) == (
            ids["Jenni"],
            "accept",
            "known",
        )
        assert len(ts.said(bobby)) == len(spoken[0].proposition_ids)
        for b in bobby:
            assert b.antecedent_kind == "record"
            assert b.antecedent_ref == {"table": "events", "id": say_event.id}
            assert (b.position_beat, b.position_step) == (say_event.beat, 1)

        # Marcus: his observation stands; about the sending he holds only that Jenni said it.
        marcus = await ts.beliefs_of(engine, story, ids["Marcus"])
        (prior,) = about_sending(ts.first_order(marcus))
        assert (prior.polarity, prior.cause, prior.state) == ("negated", "observation", "held")
        assert prior.held_to_turn is None
        (claimed,) = about_sending(ts.said(marcus))
        assert (claimed.source_character_id, claimed.outcome) == (ids["Jenni"], "reject")
        assert claimed.polarity == "affirmed"

        # Complement: Sarah (absent), the background minor, the player and the speaker hold
        # nothing from the say.
        for name in ("Sarah", "Kid", "Wren", "Jenni"):
            assert await ts.beliefs_of(engine, story, ids[name]) == [], name

        report = (await turn_report(engine, story))["epistemic"]["testimony"]
        assert (report["witnesses"], report["adjudicated"], report["defaulted"]) == (3, 2, 0)
        assert report["deferred"] == 1
        assert report["outcomes_by_kind"]["reject"] == 1  # Marcus, about the sending


@pytest.mark.part("M5", "FR-BELIEF-3,9")
async def test_s_testimony__rules_and_inert(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """Under ``commitment.salience = rules`` adjudication is never called: Marcus rejects by the
    default (partition touch), Bobby's say is not salient and he forms nothing now. With
    ``partitions`` off nothing here runs: no belief at all."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, things = await _setup(client, engine, salience="rules")
        await _play(client, epistrel, story, ids)
        stub = epistrel.models.stub
        assert not [c for c in stub.calls if c.purpose == "belief_adjudication"]
        assert ts.first_order(await ts.beliefs_of(engine, story, ids["Bobby"])) == []
        said = ts.said(await ts.beliefs_of(engine, story, ids["Marcus"]))
        (claimed,) = [b for b in said if b.predicate_id == things["sent"]]
        assert claimed.outcome == "reject"
        report = (await turn_report(engine, story))["epistemic"]["testimony"]
        assert (report["adjudicated"], report["deferred"]) == (0, 2)

        story, ids, _ = await ts.room(client, engine, *NAMES)
        await ts.enable_testimony(engine, story, partitions=False)
        await _play(client, epistrel, story, ids)
        assert not [c for c in epistrel.models.stub.calls if c.purpose == "belief_adjudication"]
        found = await rows(engine, "SELECT id FROM beliefs WHERE story_id = :s", s=story)
        assert found == []
        assert "epistemic" not in await turn_report(engine, story)
