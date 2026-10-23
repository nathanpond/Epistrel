"""U-05: speech-act handling (docs/04 §3). The M4 parts (FR-TURN-5,6; #150) and the M5 parts
(FR-TURN-6,7: #162's hearer clause, ``test_u_05__hearer_intent``; #166's speaker clause,
``test_u_05__m5``: the speaker's belief relation derived independently of both, and the
divergence a lie records) live in this file (traceability counts a test ID per milestone in one
file).

A structured story's ``say`` beats are stored as utterances, never promoted to canon by being
said, with labels derived independently: the objective relation from the claim's verdict
against canon (#142), the communicative intent from the beat's declared ``intent``; the second
label is ``unknown`` in M4 (M5 derives it).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.propositions.model import (
    Interval,
    LiteralKind,
    Polarity,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.vocabulary import FRIEND_OF
from tests import partition_support as parts
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
    turn_report,
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


def _age(character_id: str, years: int) -> Proposition:
    return Proposition(
        subject_id=character_id,
        predicate_id=AGE,
        literal=TypedLiteral(kind=LiteralKind.INT, value=years),
        valid_time=Interval.of(None, None),
    )


@pytest.mark.part("M5", "FR-TURN-6,7")
async def test_u_05__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """The speaker's belief relation, derived independently of the objective relation and the
    intent (#166): canon says Ada is 64, and Ada's partition holds what she believes of her age
    (an author seed). Each case is ``(believed, said, intent) → (objective, belief, intent,
    classification, divergence)``:

    - believes 30, says 30 sincerely: false / consistent / sincere, a ``sincere_error``, no
      divergence (the complement);
    - believes 64, says 30 to deceive: false / inconsistent / deceptive, a ``lie``, a divergence
      against her 64 belief;
    - believes 30, says 64 to deceive: true / inconsistent / deceptive, a lie that happens to be
      true, a divergence against her 30 belief;
    - believes 64, says 64 "to deceive": consistent, so no lie: the intent is checked and
      relabelled ``sincere`` (``intent_corrected``), no divergence.
    """
    cases = (
        (30, "30", "sincere", ("false", "consistent", "sincere", "sincere_error", False)),
        (64, "30", "deceptive", ("false", "inconsistent", "deceptive", "lie", True)),
        (30, "64", "deceptive", ("true", "inconsistent", "deceptive", "lie", True)),
        (64, "64", "deceptive", ("true", "consistent", "sincere", "sincere", False)),
    )
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for believed, said, intent, expected in cases:
            story, ids = await structured_story(client, "Wren", "Ada")
            await ts.enable_testimony(engine, story, "defaulted", level="co_location")
            ada = ids["Ada"]
            await age_fact(engine, story, ada, 64)
            held = await parts.believe(
                engine, story, ada, _age(ada, believed), kind=OperationKind.CONTROL
            )
            stub.reset()
            stub.script(
                "generation",
                [
                    render(
                        beat(
                            "say",
                            f"I am {said}.",
                            intent=intent,
                            propositions=[claim("I", "age", said)],
                        )
                    )
                ],
            )
            response = await respond(client, story, body(ids["Wren"], "How old are you?"))
            assert response.status_code == 200, response.text
            (stored,) = [
                u for u in await utterances(engine, story) if u.speaker_character_id == ada
            ]
            objective, relation, label, classification, diverges = expected
            case = (believed, said, intent)
            assert (
                stored.objective_relation,
                stored.belief_relation,
                stored.intent,
                stored.classification,
            ) == (objective, relation, label, classification), case
            found = await rows(
                engine,
                "SELECT * FROM divergences WHERE story_id = :s ORDER BY id",
                s=story,
            )
            if diverges:
                (divergence,) = found
                assert (
                    divergence.utterance_id,
                    divergence.speaker_character_id,
                    divergence.believed_belief_id,
                    divergence.believed_proposition_id,
                ) == (stored.id, ada, held.id, held.proposition_id), case
                assert divergence.asserted_proposition_id in stored.proposition_ids, case
                assert divergence.revealed_by_event_id is None
                report = await turn_report(engine, story)
                assert report["verify"]["divergences_written"] == 1, case
            else:
                assert found == [], case
            if classification == "sincere" and intent == "deceptive":
                (verdict,) = [
                    v["speech"]
                    for v in (await turn_report(engine, story))["verify"]["verdicts"]
                    if "speech" in v
                ]
                assert verdict["intent_corrected"] is True
