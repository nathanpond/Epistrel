"""S-SPEECH-DIMENSIONS: truth, belief and intent are independent (docs/04 §7; FR-TURN-6,7,
FR-VERIFY-5,10, FR-DIRECT-8; #166's labelling, #225's retcon).

Canon: the Bobby education fixture (Bobby went to OSU), with #220's witnesses; Bobby believes OSU
and that he did not go to UCLA. Played through ``respond`` with the stub adapter:

- (a) Bobby sincerely says "I went to OSU." → ``true`` / ``consistent`` / ``sincere``;
- (c) Bobby, believing OSU, says "I went to UCLA." to deceive → ``false`` / ``inconsistent`` /
  ``deceptive``: a lie;
- (g) a joking "I went to Hogwarts." → ``joking`` intent, a joke;
- (d) a retcon then makes UCLA correct without changing Bobby's belief → the lie is ``true`` /
  ``inconsistent`` / ``deceptive`` (an attempted lie that happened to be true), its text and intent
  unchanged, the author notified (``scene_meaning_may_change``); the sincere OSU line is now
  ``false`` (a sincere error);
- (e) the variant where Bobby's belief follows the retcon (he had observed where he went) → the
  lie's belief relation is ``consistent``, its intent still ``deceptive``;
- an explicit intent change ("Bobby was joking when he said that") is the only path that writes
  ``intent``: the lie becomes a joke; complement, any other part that names ``intent`` is refused
  (``IntentNotRetconnable``) and writes nothing.

(b), (f) and (h) are #166's labelling (``tests/integration/test_i_12.py``, U-05) on the same
rules; they need no M7 code and are not replayed here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.amendments.store import RecordPartDraft
from epistrel.core.epistemic import transitions
from epistrel.core.errors import EpistrelError
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from tests import amendment_support as am
from tests import multi_support as ms
from tests import partition_support as parts
from tests import rewrite_support as rs
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import ENROLLED_AT, Education, bobby_education
from tests.respond_support import body, played, respond_client
from tests.structured_turn_support import beat, claim

pytestmark = pytest.mark.integration

UCLA_REWRITE = rs.UCLA


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _world(client: Any, engine: AsyncEngine, *, observed: bool) -> Education:
    # The closed set "Bobby's only enrollment is OSU" settles a claim of another school.
    w = await bobby_education(client, engine, witnesses=True)
    await ts.enable_testimony(engine, w.story_id, "defaulted", lazy=False)
    await parts.believe(
        engine,
        w.story_id,
        w.bobby,
        w.enrollment.proposition,
        cause=transitions.Kind.OBSERVATION if observed else transitions.Kind.AUTHOR,
    )
    await parts.believe(
        engine,
        w.story_id,
        w.bobby,
        Proposition(
            subject_id=w.bobby,
            predicate_id=ENROLLED_AT,
            object_id=w.ucla,
            polarity=Polarity.NEGATED,
            valid_time=Interval.of(None, None),
        ),
    )
    return w


async def _say(client: Any, stub: Any, w: Education, text: str, intent: str, school: str) -> str:
    """Bobby says ``text`` with ``intent``, declaring ``enrolled_at(Bobby, school)``; answers the
    utterance's id."""
    stub.reset()
    ms.renders_for(
        stub,
        "Bobby",
        ms.render(
            beat(
                "say",
                text,
                intent=intent,
                propositions=[claim("Bobby", "enrolled_at", school)],
            )
        ),
    )
    stub.script("salience_detection", [ts.salient_say(2, text)])
    said = await played(
        client, w.story_id, body(w.cal, "Bobby, tell me.", render_character_ids=[w.bobby])
    )
    (found,) = await rows(
        engine_of(client),
        "SELECT u.id FROM utterances u JOIN events e ON e.id = u.event_id "
        "WHERE e.story_id = :s AND e.narrative_turn = :t AND u.speaker_character_id = :b",
        s=w.story_id,
        t=said["turn"],
        b=w.bobby,
    )
    return str(found.id)


_ENGINES: dict[int, AsyncEngine] = {}


def engine_of(client: Any) -> AsyncEngine:
    return _ENGINES[id(client)]


async def _labels(engine: AsyncEngine, utterance: str) -> tuple[str, str, str, str | None]:
    (row,) = await rows(
        engine,
        "SELECT objective_relation, belief_relation, intent, classification FROM utterances "
        "WHERE id = :u",
        u=utterance,
    )
    return (row.objective_relation, row.belief_relation, row.intent, row.classification)


async def _retcon(client: Any, stub: Any, engine: AsyncEngine, w: Education) -> dict[str, Any]:
    stub.reset()
    stub.when("", UCLA_REWRITE, purpose="rewrite_parse")
    stub.when("", '{"text": "Bobby nods."}', purpose="rewrite_record")
    session = (await rs.open_session(client, w.story_id, resolution_mode="best_effort")).json()
    applied = await rs.apply(
        client, w.story_id, session["id"], await rs.revision(engine, w.story_id)
    )
    assert applied.status_code == 200, applied.text
    report: dict[str, Any] = applied.json()["report"]
    return report


@pytest.mark.part("M7", "FR-TURN-6,7; FR-VERIFY-5,10; FR-DIRECT-8")
async def test_s_speech_dimensions(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    try:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            _ENGINES[id(client)] = engine
            stub = epistrel.models.stub
            w = await _world(client, engine, observed=False)
            sincere = await _say(client, stub, w, "I went to OSU.", "sincere", "OSU")
            lie = await _say(client, stub, w, "I went to UCLA.", "deceptive", "UCLA")
            joke = await _say(client, stub, w, "I went to Hogwarts.", "joking", "UCLA")
            assert await _labels(engine, sincere) == ("true", "consistent", "sincere", "sincere")
            assert await _labels(engine, lie) == ("false", "inconsistent", "deceptive", "lie")
            assert (await _labels(engine, joke))[2:] == ("joking", "joke")

            # (d) the retcon: the lie is now true; nothing else about it moves.
            report = await _retcon(client, stub, engine, w)
            assert await _labels(engine, lie) == ("true", "inconsistent", "deceptive", "lie")
            assert (await _labels(engine, sincere))[0] == "false"
            assert (await _labels(engine, joke))[2:] == ("joking", "joke")
            noticed = {
                n["utterance_id"]
                for n in report["notifications"]
                if n["kind"] == "scene_meaning_may_change"
            }
            assert lie in noticed

            # Complement: no part but the explicit intent change writes intent.
            with pytest.raises(EpistrelError) as refused:
                await am.amend(
                    engine,
                    w.story_id,
                    [RecordPartDraft("utterances", lie, "relabel", {"intent": "joking"})],
                )
            assert refused.value.code == "intent_not_retconnable"
            assert (await _labels(engine, lie))[2] == "deceptive"

            # The explicit intent change: "Bobby was joking when he said that."
            (turn,) = await rows(
                engine,
                "SELECT e.narrative_turn FROM utterances u JOIN events e ON e.id = u.event_id "
                "WHERE u.id = :u",
                u=lie,
            )
            stub.reset()
            stub.when(
                "",
                json.dumps(
                    {
                        "changes": [
                            {
                                "kind": "intent",
                                "subject": "Bobby",
                                "ref": {"turn": turn.narrative_turn},
                                "intent": "joking",
                            }
                        ]
                    }
                ),
                purpose="rewrite_parse",
            )
            session = (
                await rs.open_session(client, w.story_id, resolution_mode="best_effort")
            ).json()
            applied = await rs.apply(
                client, w.story_id, session["id"], await rs.revision(engine, w.story_id)
            )
            assert applied.status_code == 200, applied.text
            assert (await _labels(engine, lie))[2:] == ("joking", "joke")

        # (e) the variant: Bobby's own (observed) belief follows the retcon.
        async with respond_client(stub_settings, engine) as (client, epistrel):
            _ENGINES[id(client)] = engine
            stub = epistrel.models.stub
            w = await _world(client, engine, observed=True)
            lie = await _say(client, stub, w, "I went to UCLA.", "deceptive", "UCLA")
            assert await _labels(engine, lie) == ("false", "inconsistent", "deceptive", "lie")
            await _retcon(client, stub, engine, w)
            assert await _labels(engine, lie) == ("true", "consistent", "deceptive", "lie")
    finally:
        await ts.forget(engine)
