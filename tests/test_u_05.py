"""U-05: speech-act handling (docs/04 §3). The M4 parts (FR-TURN-5,6; #150) and the M5 parts
(FR-TURN-6,7: #162's hearer clause, ``test_u_05__hearer_intent``; the divergence record is a later
M5 story's) live in this file (traceability counts a test ID per milestone in one file).

A structured story's ``say`` beats are stored as utterances, never promoted to canon by being
said, with labels derived independently: the objective relation from the claim's verdict
against canon (#142), the communicative intent from the beat's declared ``intent``; the second
label is ``unknown`` in M4 (M5 derives it).
"""

from __future__ import annotations

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
from tests.story_support import override
from tests.structured_turn_support import (
    AGE,
    UNKNOWN,
    age_fact,
    beat,
    claim,
    labels_of,
    render,
    row_labels,
    structured_story,
    utterances,
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M4", "FR-TURN-5,6")
async def test_u_05(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Canon says Ada is 64. Ada says, sincerely, that she is 30: the utterance is stored with
    ``objective_relation = false``, ``intent = sincere`` and the second label ``unknown``, and no
    fact says 30 (FR-TURN-5). The same false claim said ``deceptive``, said ``joking`` and said
    with no declared intent keeps ``false`` while the intent follows the declaration
    (``unspecified`` when none): the two labels are independent. Complement: a true sincere
    claim (64) is ``true``, and a say asserting nothing is ``unresolved``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        # The repair ladder off (#151's ``repair.max_attempts`` 0): a false say stands, labelled;
        # tests/integration/test_i_12.py repairs one.
        await override(client, story, {"repair": {"max_attempts": 0}})
        wren, ada = ids["Wren"], ids["Ada"]
        await age_fact(engine, story, ada, 64)
        stub = epistrel.models.stub
        lie = claim("I", "age", "30")
        cases = [
            ("sincere", lie, ("false", UNKNOWN, "sincere")),
            ("deceptive", lie, ("false", UNKNOWN, "deceptive")),
            ("joking", lie, ("false", UNKNOWN, "joking")),
            (None, lie, ("false", UNKNOWN, "unspecified")),
            ("sincere", claim("I", "age", "64"), ("true", UNKNOWN, "sincere")),
            ("sincere", None, ("unresolved", UNKNOWN, "sincere")),
        ]
        for intent, said, expected in cases:
            fields = {} if intent is None else {"intent": intent}
            stub.script(
                "generation",
                [
                    render(
                        beat(
                            "say",
                            "I am somewhere.",
                            propositions=[] if said is None else [said],
                            **fields,
                        ),
                        beat("do", "She smiles."),
                    )
                ],
            )
            response = await respond(client, story, body(wren, "Where are you?"))
            assert response.status_code == 200, response.text
            (spoken,) = [b for b in response.json()["beats"] if b["kind"] == "beat_say"]
            assert labels_of(spoken) == expected, (intent, said)
        stored = [u for u in await utterances(engine, story) if u.speaker_character_id == ada]
        assert [row_labels(u) for u in stored] == [expected for _, _, expected in cases]
        # Being said made nothing true: the only age fact of Ada's is canon's 64.
        ages = await rows(
            engine,
            "SELECT p.literal FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND f.subject_id = :a AND f.predicate_id = :p",
            s=story,
            a=ada,
            p=AGE,
        )
        assert [r.literal["value"] for r in ages] == [64]


@pytest.mark.part("M5", "FR-TURN-7")
async def test_u_05__hearer_intent(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """FR-TURN-7's hearer clause (#162), under ``testimony = defaulted``: Jenni tells Bobby (who
    trusts her) that Olivia sent the text.

    - ``joking``: Bobby holds that Jenni said it (``speech_act_only``) and believes nothing;
    - ``deceptive``, undetected (nothing Bobby holds contradicts it): accepted on his normal
      trust rules, a ``testimony`` belief at ``believed``, exactly as ``sincere`` is;
    - ``deceptive``, detected (Bobby saw for himself that Olivia did not send it): rejected, his
      observation stands.
    """
    cases = (
        ("joking", False, "speech_act_only", None),
        ("sincere", False, "accept", "believed"),
        ("deceptive", False, "accept", "believed"),
        ("deceptive", True, "reject", None),
    )
    async with respond_client(stub_settings, engine) as (client, epistrel):
        for intent, knows, decision, level in cases:
            story, ids, things = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
            await ts.enable_testimony(engine, story, "defaulted")
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
            if knows:
                await ts.observed(engine, story, ids["Bobby"], ts.sent(things, Polarity.NEGATED))
            stub = epistrel.models.stub
            stub.reset()
            stub.script(
                "generation",
                [
                    render(
                        beat(
                            "say",
                            ts.CLAIM_LINE,
                            intent=intent,
                            addressee=ids["Bobby"],
                            propositions=[claim("Olivia", "sent", "the text")],
                        )
                    )
                ],
            )
            response = await respond(client, story, body(ids["Wren"], "What happened?"))
            assert response.status_code == 200, response.text
            assert not [c for c in stub.calls if c.purpose == "belief_adjudication"]
            found = await ts.beliefs_of(engine, story, ids["Bobby"])
            (heard,) = [b for b in ts.said(found) if b.predicate_id == things["sent"]]
            assert (heard.outcome, heard.source_character_id) == (decision, ids["Jenni"]), intent
            held = [b for b in ts.first_order(found) if b.predicate_id == things["sent"]]
            if level is None:
                expected = [("negated", "observation")] if knows else []
                assert [(b.polarity, b.cause) for b in held] == expected, intent
            else:
                assert [(b.polarity, b.cause, b.confidence_level) for b in held] == [
                    ("affirmed", "testimony", level)
                ], intent
