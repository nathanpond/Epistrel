"""S-CARDOORS: salience drives commitment (docs/04 §7; FR-COMMIT-3,4,7; #169).

Structured turns on the stub under per-character partitions. Wren (the player), Jenni (the
rendered character) and Bobby share the parlor; the street outside has car doors, and Mr. Hale.

- **A single door** (texture). Turn 1: a car door slams once. Salience calls it texture, so it
  commits nothing: no thunk, no ledger entry, no hidden fact, no observation.
- **The all-night pattern** (salient). Turn 2: the doors have slammed all night, salient. Jenni
  noticed the pattern, not who slams them: her render's ``latent`` beats commit the signal she
  perceived (the doors slamming near the house, scoped to her, with her observation of it in
  her partition) and the truth behind it (``hidden: {observed: signal}``: Mr. Hale slams them,
  in canon but scoped to the engine, away from everyone, Jenni included). The salient
  reference ("someone slams the car doors") is settled by the hidden truth, so it owes no ledger
  entry and the turn closes with no minimal commit. Nobody else holds either; the omniscient
  ledger lists both under ``hidden_facts``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import registry, store
from epistrel.orchestrator.models.registry import SalienceSketch
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.commit_support import salience_answer
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.story_support import READ
from tests.structured_turn_support import beat, claim, render, turn_report

pytestmark = pytest.mark.integration

ONCE = "A car door slams outside. Jenni glances up."
ALL_NIGHT = "Car doors have slammed outside all night. Jenni counts them."
WHO = SalienceSketch(subject="someone", relation="slammed", object="the car doors")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _ledger(client: Any, story: str) -> dict[str, Any]:
    response = await client.get(
        f"/stories/{story}/inspect/ledger", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def _counts(engine: AsyncEngine, story: str) -> dict[str, int]:
    found = {}
    for table in ("thunks", "promissory_ledger"):
        (row,) = await rows(
            engine,
            f"SELECT count(*) AS n FROM {table} WHERE story_id = :s",  # noqa: S608
            s=story,
        )
        found[table] = int(row.n)
    return found


async def _doors(engine: AsyncEngine, story: str, holder: str, ids: dict[str, str]) -> list[Any]:
    """``holder``'s current first-order beliefs about who slams the doors, or the slamming."""
    return [
        b
        for b in ts.first_order(await ts.beliefs_of(engine, story, holder))
        if b.predicate_id in (ids["slammed"], ids["slammed_near"])
    ]


@pytest.mark.part("M5", "FR-COMMIT-3,4,7")
async def test_s_cardoors(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, _ = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
        await ts.enable_testimony(engine, story, "defaulted")
        async with engine.begin() as conn:
            for name, kind in (
                ("the car doors", "item"),
                ("the house", "place"),
                ("Mr. Hale", "person"),
            ):
                ids[name] = await store.add_entity(conn, story, kind, name)
            for relation in ("slammed", "slammed_near"):
                ids[relation] = (await registry.auto_register(conn, story, relation)).id
        jenni = ids["Jenni"]

        # -- turn 1: one door, texture: nothing is committed
        stub.script("generation", [render(beat("do", ONCE))])
        stub.script("salience_detection", [salience_answer(texture=[(2, ONCE)])])
        await played(client, story, body(ids["Wren"], "What was that?"))
        report = await turn_report(engine, story)
        assert report["commit"]["hidden_commits"] == []
        assert report["commit"]["minimal_commits"] == 0
        assert await _counts(engine, story) == {"thunks": 0, "promissory_ledger": 0}
        assert await _doors(engine, story, jenni, ids) == []
        assert (await _ledger(client, story))["hidden_facts"] == []

        # -- turn 2: the all-night pattern, salient: the signal observed, the truth scoped away
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat("do", ALL_NIGHT),
                    beat(
                        "latent",
                        "Jenni has noticed the doors slam near the house all night.",
                        visibility={"scope": "characters", "characters": [jenni]},
                        propositions=[claim("the car doors", "slammed_near", "the house")],
                    ),
                    beat(
                        "latent",
                        "It is Mr. Hale who slams them.",
                        hidden={"noticers": [jenni], "observed": "signal"},
                        propositions=[claim("Mr. Hale", "slammed", "the car doors")],
                    ),
                )
            ],
        )
        stub.script("salience_detection", [salience_answer((2, ALL_NIGHT, WHO))])
        await played(client, story, body(ids["Wren"], "Again?"))
        report = await turn_report(engine, story)
        signal, truth = report["commit"]["hidden_commits"]
        assert (signal["scope"], signal["characters"]) == ("characters", [jenni])
        assert len(signal["observation_transition_ids"]) == 1
        assert (truth["scope"], truth["characters"], truth["observation_transition_ids"]) == (
            "engine",
            [],
            [],
        )
        # Her observation is of the signal; nobody holds the truth, not even Jenni.
        held = await _doors(engine, story, jenni, ids)
        assert [(b.predicate_id, b.cause, b.confidence_level) for b in held] == [
            (ids["slammed_near"], "observation", "known")
        ]
        assert await _doors(engine, story, ids["Bobby"], ids) == []
        facts = await rows(
            engine,
            "SELECT f.id, f.visibility, p.subject_id FROM facts f JOIN propositions p "
            "ON p.id = f.proposition_id WHERE f.story_id = :s AND f.predicate_id = :p",
            s=story,
            p=ids["slammed"],
        )
        assert [(f.subject_id, f.visibility) for f in facts] == [
            (ids["Mr. Hale"], {"scope": "engine"})
        ]
        # The salient reference is settled by the hidden truth: no entry, no minimal commit.
        assert (report["commit"]["ledger_created"], report["commit"]["minimal_commits"]) == (0, 0)
        assert [r["outcome"] for r in report["commit"]["salience"]["recorded"]] == ["settled"]
        ledger = await _ledger(client, story)
        assert {f["fact_id"] for f in ledger["hidden_facts"]} == {
            signal["fact_id"],
            truth["fact_id"],
        }
