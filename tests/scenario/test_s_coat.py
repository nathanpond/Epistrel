"""S-COAT, M4 part: intra-narrative consistency (docs/04 §7; FR-REPAIR-1,4; #151).

Bobby hung his coat on the hook (canon: ``located_at(coat, hook)``, the author's; the hook is in
the hallway and the armchair in the parlor, so the two places are told apart). A later
render has him playing with its hem with the coat somewhere else: the placement rule (one place
at a time, ``located_at`` is functional) makes the claim ``contradicted``, the repair exposes the
hook fact and its neighborhood, and the corrected beat is accepted only once it is re-verified.

- ``test_s_coat``: the coat "across the armchair" — rung 2 puts it back on the hook; the
  neighborhood (the hook fact and the hook's place in the hallway) makes it a
  ``substitution``.
- ``test_s_coat__self_correcting``: the render names no place in its prose, so neither
  deterministic rung applies; the scripted regeneration corrects itself (the coat on the hook)
  and is classified ``cosmetic``.

Played end to end through ``respond`` with the stub adapter.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.events.types import BeatDo, RenderRejected
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.views import audit_view
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import (
    LOCATED_AT,
    beat,
    claim,
    render,
    structured_story,
    turn_report,
)

pytestmark = pytest.mark.integration

LOCATED_IN = CORE_BY_NAME["located_in"].id


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _coat_story(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], str]:
    """Wren (the player) and Bobby; the coat on the hook (the author's canon): the story, its
    character ids and the hook fact's id."""
    story, ids = await structured_story(client, "Wren", "Bobby")
    async with engine.begin() as conn:
        coat = await store.add_entity(conn, story, "item", "coat")
        hook = await store.add_entity(conn, story, "place", "hook")
        armchair = await store.add_entity(conn, story, "place", "armchair")
        hallway = await store.add_entity(conn, story, "place", "hallway")
        parlor = await store.add_entity(conn, story, "place", "parlor")
    # Each place in the house's hierarchy, so two of them are told apart (not world knowledge).
    for inner, outer in ((hook, hallway), (armchair, parlor)):
        await commit(
            engine,
            story,
            Proposition(
                subject_id=inner,
                predicate_id=LOCATED_IN,
                object_id=outer,
                valid_time=Interval.of(None, None),
            ),
            kind=OperationKind.CONTROL,
            provenance="author",
        )
    hung = await commit(
        engine,
        story,
        Proposition(
            subject_id=coat,
            predicate_id=LOCATED_AT,
            object_id=hook,
            valid_time=Interval.of(None, None),
        ),
        kind=OperationKind.CONTROL,
        provenance="author",
    )
    return story, ids, hung.id


@pytest.mark.part("M4", "FR-REPAIR-1,4")
async def test_s_coat(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The coat across the armchair contradicts the hung coat (``functional_key``); the repair's
    neighborhood holds the hook fact and what hangs on it; rung 2 swaps the place back, the
    correction is re-verified (``supported``) and classified ``substitution`` (one fact replaced
    in a two-fact neighborhood). Complement: no regeneration was called."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, hung = await _coat_story(client, engine)
        epistrel.models.stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "The coat lies across the armchair; Bobby plays with its hem.",
                        propositions=[claim("coat", "located_at", "armchair")],
                    )
                )
            ],
        )
        response = await respond(client, story, body(ids["Wren"], "What are you doing?"))
        assert response.status_code == 200, response.text
        played = response.json()
        assert played["message"]["text"] == (
            "*The coat lies across the hook; Bobby plays with its hem.*"
        )
        report = await turn_report(engine, story)
        (verdict,) = [v for v in report["verify"]["verdicts"] if v["kind"] == "contradicted"]
        assert verdict["rule"] == "functional_key"
        (entry,) = report["repairs"]
        assert entry["contradicted_facts"] == [hung]
        (cluster,) = entry["neighborhood"]
        assert hung in {i["id"] for i in cluster["items"]}
        assert entry["neighborhood_size"] == 2
        assert (entry["rung"], entry["class"], entry["accepted"]) == (2, "substitution", True)
        (stored,) = [e for e in await audit_view(engine, story) if e.type == "beat_do"]
        payload = stored.payload
        assert isinstance(payload, BeatDo)
        assert payload.repaired_from is not None
        assert payload.repaired_from.repair_class == "substitution"
        assert [p.verdict for p in payload.propositions] == ["supported"]
        assert not [c for c in epistrel.models.stub.calls if c.purpose == "regeneration"]


@pytest.mark.part("M4", "FR-REPAIR-1,4")
async def test_s_coat__self_correcting(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The render plays with the hem and declares the coat on the armchair without saying so in
    its prose: rungs 1 and 2 do not apply, the regeneration (scripted) puts the coat back on the
    hook, its re-verification supports it and the repair is ``cosmetic``. The rejected render is
    in the audit view only."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, hung = await _coat_story(client, engine)
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "Bobby pulls the coat down and plays with its hem.",
                        propositions=[claim("coat", "located_at", "armchair")],
                    )
                )
            ],
        )
        corrected = "Bobby glances at his coat on the hook and plays with his sleeve."
        stub.script(
            "regeneration",
            [
                json.dumps(
                    {
                        "text": corrected,
                        "propositions": [claim("coat", "located_at", "hook")],
                    }
                )
            ],
        )
        played = (await respond(client, story, body(ids["Wren"], "Fidgeting?"))).json()
        assert played["message"]["text"] == f"*{corrected}*"
        (entry,) = (await turn_report(engine, story))["repairs"]
        assert (entry["rung"], entry["class"], entry["accepted"]) == (3, "cosmetic", True)
        assert entry["contradicted_facts"] == [hung]
        (regeneration,) = [c for c in stub.calls if c.purpose == "regeneration"]
        prompt = "\n".join(m.content for m in regeneration.messages)
        assert "coat located_at hook" in prompt
        rejected = [
            e.payload
            for e in await audit_view(engine, story)
            if isinstance(e.payload, RenderRejected)
        ]
        assert [r.text for r in rejected] == ["Bobby pulls the coat down and plays with its hem."]
