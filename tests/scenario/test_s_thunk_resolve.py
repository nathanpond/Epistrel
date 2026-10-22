"""S-THUNK-RESOLVE — deferred resolution and its retroactive obligations (FR-COMMIT-10,11,14;
docs/04 §7; #154). One file per test ID and milestone: the M5 part (could-have-known, the
knowledge obligations) joins it as ``test_s_thunk_resolve__m5``.

Structured turns on the stub. Jenni (the player) is at home; Ada is rendered; Bobby is a neighbour
the world records (an entity, not a cast member). Turn 1: someone
knocks at the porch (a salient open reference: the minimal commit discharges its entry and "who
knocked" stays an open thunk with no obligation). Bobby is recorded at the school, across town,
at the knock's time, and the author rule ``knocked_at(x, place) ⇒ located_at(x, place)`` holds.

- Turn 2, one message later, resolves the knock to the mailman delivering a parcel: it passes
  quietly (reconciled, its obligation "the mailman was at the porch at t" committed with it,
  nothing rejected), and the render that settled it was steered toward insignificance.
- A variant of turn 2 (its regeneration) binds the old knock to Bobby: the check derives "Bobby
  at the porch at t", finds his recorded position, and the model-driven binding is rejected
  (``resolution_conflict``) and steered elsewhere through the repair ladder's
  ``avoid_binding`` constraint: the corrected beat binds it to the mailman. Bobby's knock is never
  committed; the rejected text is a ``render_rejected`` audit record; the ledger inspection
  lists the rejected binding.
- Complement: an unforced thunk may remain unresolved indefinitely with no ledger obligation:
  after more turns that never touch it, it is still open and nothing blocks a turn's close.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.orchestrator.models.registry import SalienceSketch
from epistrel.orchestrator.structured.prompt import RESOLUTION_STEERING
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.commit_support import salience_answer
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.story_support import READ, WRITE, override
from tests.structured_turn_support import beat, claim, render, renders_of, structured_story, text_of

LOCATED_AT = CORE_BY_NAME["located_at"].id
LOCATED_IN = CORE_BY_NAME["located_in"].id
KNOCK = "Someone knocks at the porch."
FOREVER = Interval.of(None, None)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _porch(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], dict[str, str]]:
    story, ids = await structured_story(client, "Jenni", "Ada")
    # Room for a narrator render, a character render and a repair's regeneration in one turn.
    await override(client, story, {"budgets": {"model_calls": 20}})
    async with engine.begin() as conn:
        places = {
            name: await store.add_entity(conn, story, "place", name)
            for name in ("porch", "Maple Street", "school", "Hilltop")
        }
        places["mailman"] = await store.add_entity(conn, story, "person", "mailman")
        # Bobby is a neighbour the world records but the cast does not model: in M4 a claim
        # about a modeled character's doings is #144's (never this test's) to refuse.
        ids["Bobby"] = await store.add_entity(conn, story, "person", "Bobby")
        await registry.auto_register(conn, story, "knocked_at")
        await registry.auto_register(conn, story, "rattled")
        await registry.register_author(
            conn,
            story,
            registry.ScenarioRules.parse({"rules": [{"when": "knocked_at", "then": "located_at"}]}),
        )
    for inner, outer in (("porch", "Maple Street"), ("school", "Hilltop")):
        await commit(
            engine,
            story,
            Proposition(
                subject_id=places[inner],
                predicate_id=LOCATED_IN,
                object_id=places[outer],
                valid_time=FOREVER,
            ),
            kind=OperationKind.CONTROL,
            provenance="author",
        )
    return story, ids, places


async def _thunk(engine: AsyncEngine, story: str) -> Any:
    (found,) = await rows(engine, "SELECT * FROM thunks WHERE story_id = :s", s=story)
    return found


async def _ledger(client: Any, story: str) -> dict[str, Any]:
    response = await client.get(
        f"/stories/{story}/inspect/ledger", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def _regenerated(text_: str, *props: dict[str, Any]) -> str:
    return json.dumps({"text": text_, "propositions": list(props)})


async def _knocks(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.subject_id, f.commit_state, f.membership, f.provenance, f.valid_from "
        "FROM facts f JOIN predicates q ON q.id = f.predicate_id "
        "WHERE f.story_id = :s AND q.name = 'knocked_at' ORDER BY f.id",
        s=story,
    )


@pytest.mark.part("M4", "FR-COMMIT-10,11,14")
async def test_s_thunk_resolve__m4(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, places = await _porch(client, engine)
        jenni, bobby = ids["Jenni"], ids["Bobby"]

        # Turn 1: the knock, salient, its "who" left open; the follow-up does not settle it.
        stub.script(
            "generation",
            [render(beat("do", KNOCK)), render(beat("narration", "The knocking stops."))],
        )
        stub.script(
            "salience_detection",
            [
                salience_answer(
                    (
                        2,
                        KNOCK,
                        SalienceSketch(subject="someone", relation="knocked_at", object="porch"),
                    )
                )
            ],
        )
        await played(client, story, body(jenni, "Ada, did you hear that?"))
        knock = await _thunk(engine, story)
        assert str(knock.state) == "open"
        t = int(knock.valid_time)
        ledger = await _ledger(client, story)
        assert [e["discharged_mode"] for e in ledger["ledger"]] == ["minimal"]
        # Bobby is recorded across town at the knock's time.
        await commit(
            engine,
            story,
            Proposition(
                subject_id=bobby,
                predicate_id=LOCATED_AT,
                object_id=places["school"],
                valid_time=Interval.of(t - 5, t + 5),  # not at turn 2's own time
            ),
            kind=OperationKind.CONTROL,
            provenance="author",
        )

        # Turn 2: the mailman delivered a parcel. It passes quietly. (Jenni's input addresses no
        # one, so the narrator renders first: the director may settle the world's references.)
        delivered = "On the step lies a parcel: the mailman had knocked."
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "narration",
                        delivered,
                        propositions=[claim("mailman", "knocked_at", "porch")],
                    )
                ),
                render(beat("say", "A parcel!", intent="sincere")),
            ],
        )
        calls_before = len(renders_of(stub))
        await played(client, story, body(jenni, None, do="Jenni opens the door."))
        prompt = text_of(renders_of(stub)[calls_before])
        assert RESOLUTION_STEERING in prompt  # the deferral bias (FR-COMMIT-11)
        report = (
            await rows(
                engine,
                "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
                "AND narrative_turn = 2",
                s=story,
            )
        )[0].turn_report["commit"]
        assert report["reconciliations"] == 1
        assert report["rejected_bindings"] == []
        (mailman,) = await _knocks(engine, story)
        assert (mailman.subject_id, str(mailman.provenance), mailman.valid_from) == (
            places["mailman"],
            "resolution",
            t,
        )
        assert str((await _thunk(engine, story)).state) == "resolved"

        # A variant of turn 2 binds the old knock to Bobby: rejected, and steered elsewhere.
        bound = "So it was Bobby at the door all along."
        stub.script(
            "generation",
            [
                render(
                    beat("narration", bound, propositions=[claim("Bobby", "knocked_at", "porch")])
                ),
                render(beat("say", "Oh!", intent="sincere")),
            ],
        )
        stub.script(
            "regeneration",
            [
                _regenerated(
                    "On the step lies a parcel: the mailman knocked.",
                    claim("mailman", "knocked_at", "porch"),
                )
            ],
        )
        response = await client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
        assert response.status_code == 200, response.text
        regenerated = response.json()
        assert "Bobby" not in regenerated["message"]["text"]
        variant = regenerated["variant"]
        report = (
            await rows(
                engine,
                "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
                "AND narrative_turn = 2 AND turn_variant = :v",
                s=story,
                v=variant,
            )
        )[0].turn_report
        (rejected,) = report["commit"]["rejected_bindings"]
        assert rejected["bound_entity"] == bobby
        assert rejected["thunk_id"] == knock.id
        assert "binding_rejected" in report["degradation"]
        assert report["commit"]["reverted_facts"] >= 1  # the first variant's mailman resolution
        ((repair,),) = [report["repairs"]]
        assert repair["rung"] == 3
        regeneration = next(c for c in stub.calls if c.purpose == "regeneration")
        steer = text_of(regeneration)
        assert "avoid_binding: Bobby" in steer
        assert RESOLUTION_STEERING in steer  # again on the re-steer
        audit = await rows(
            engine,
            "SELECT e.payload FROM events e JOIN operations o ON o.operation_id = e.operation_id "
            "WHERE e.story_id = :s AND e.type = 'render_rejected' AND o.turn_variant = :v",
            s=story,
            v=variant,
        )
        assert [p.payload["text"] for p in audit] == [bound]
        assert audit[0].payload["verdicts"][0]["cause"] == "resolution_conflict"
        knocks = await _knocks(engine, story)
        assert bobby not in {k.subject_id for k in knocks}  # never committed
        active = [k for k in knocks if k.membership == "active"]
        assert [(k.subject_id, str(k.commit_state)) for k in active] == [
            (places["mailman"], "committed")
        ]
        ledger = await _ledger(client, story)
        (inspected,) = ledger["thunks"]
        assert inspected["state"] == "resolved"
        assert inspected["resolution_level"] == "consequential"
        assert [r["bound_entity"] for r in inspected["rejected_bindings"]] == [bobby]

        # Complement: an unforced thunk stays unresolved indefinitely, with no obligation.
        rattle = "Something rattles the porch."
        stub.script(
            "generation",
            [
                render(beat("do", rattle)),
                render(beat("narration", "Silence.")),
            ],
        )
        stub.script(
            "salience_detection",
            [
                salience_answer(
                    (
                        2,
                        rattle,
                        SalienceSketch(subject="something", relation="rattled", object="porch"),
                    )
                )
            ],
        )
        await played(client, story, body(jenni, "Ada, what was that?"))
        later = [
            t_
            for t_ in await rows(engine, "SELECT * FROM thunks WHERE story_id = :s", s=story)
            if t_.narrative_turn == 3
        ]
        (unforced,) = later
        for n in range(3):
            stub.script(
                "generation", [render(beat("say", f"Nothing yet ({n}).", intent="sincere"))]
            )
            await played(client, story, body(jenni, "Ada, anything?"))
        ledger = await _ledger(client, story)
        still = next(x for x in ledger["thunks"] if x["id"] == unforced.id)
        assert still["state"] == "open"
        undischarged = [
            e for e in ledger["ledger"] if e["thunk_id"] == unforced.id and not e["discharged_mode"]
        ]
        assert undischarged == []
