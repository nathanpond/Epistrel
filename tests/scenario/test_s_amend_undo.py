"""S-AMEND-UNDO (docs/04 §7; FR-REWRITE-10,11, FR-STORE-18; #238): undo is a checked
operation.

The OSU fixture (``tests/rewrite_support``). Amendment A ("Bobby actually went to UCLA", applied
best-effort) writes Bobby's enrollment at UCLA; amendment B adds a UCLA graduation, recorded as
relying on A's fact (its #220 edges); a later turn has Marcus form a memory from the graduation
(a ``derives`` edge onto B's fact). Undo A:

- **synchronous** (``preview_first``): the undo's blast radius holds B (a ``dependent``
  question, options ``cascade_undo`` (recommended) and ``cancel``) and Marcus's memory (through
  B: B is recommended for cascade-undo); nothing commits while the question is open (canon
  still UCLA, B active). Answered and applied: one ``undo`` amendment (``undoes = A``); A and B
  ``reverted`` with ``reverted_by`` the undo, never deleted (amendment rows only grow); canon
  is OSU again and no active part's fact contradicts it. Redo A: a ``redo`` session, its own
  checked plan; applied, a ``redo`` amendment (``redoes = A``) and canon says UCLA again, while
  A stays ``reverted``.
- **best_effort** (``apply_directly``): B is cascade-undone and Marcus's memory regenerated,
  both in the report.
- **cancel**: answering B's question with ``cancel`` cancels the whole undo; nothing reverted.
- The routes (``test_s_amend_undo__routes``): list and detail shapes (``relies_on``,
  ``dependents``), 404 ``amendment_not_found``, 403 ``omniscient_required``, 409
  ``amendment_already_reverted``, 422 ``use_redo``, 409 ``amendment_not_reverted``, 409
  ``amendment_already_redone``; a Lite story's undo opens a Lite session (#245)
  with no session while its list and detail answer 200.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import edges
from epistrel.core.amendments.store import (
    AmendmentKind,
    PropositionPartDraft,
    RecordPartDraft,
    record_amendment,
)
from epistrel.core.enums import (
    DependencyEdgeType,
    MemoryKind,
    OperationKind,
    Provenance,
    RecordProvenance,
)
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, insert_derived
from epistrel.core.memory import records
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.tables.memories import memories
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import respond_client
from tests.story_support import WRITE, create, story_body

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Amended:
    story_id: str
    ed: Any
    a: str
    b: str
    ucla: str
    graduation_fact: str
    memory: str


async def _amend(engine: AsyncEngine, story_id: str, draft: Any, relies_on: list[str]) -> str:
    """An amendment of one part, recorded in its own revision-bumping operation."""
    found: dict[str, str] = {}
    async with Operation(
        engine,
        story_id=story_id,
        kind=OperationKind.REWRITE_SESSION,
        principal="p-author",
        bumps_revision=True,
    ) as op:

        async def step(conn: AsyncConnection) -> None:
            made = await record_amendment(
                conn, op, kind=AmendmentKind.REWRITE, parts=[draft], relies_on=relies_on
            )
            found["id"] = made.amendment_id

        op.add_commit_step(step)
        await op.commit()
    return found["id"]


async def _remembers(engine: AsyncEngine, ed: Any, fact_id: str) -> str:
    """A turn in which Marcus forms a memory from the graduation (``derives`` onto it)."""
    memory = new_ulid()
    async with Operation(
        engine, story_id=ed.story_id, kind=OperationKind.TURN, principal="p-test"
    ) as op:
        op.append("player_input", {"text": "Marcus?"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "Marcus smiles."})

        async def step(conn: AsyncConnection) -> None:
            await insert_derived(
                conn,
                memories,
                [
                    {
                        "id": memory,
                        "story_id": ed.story_id,
                        "kind": MemoryKind.EPISODIC.value,
                        "text": "Marcus remembers Bobby's graduation day at UCLA.",
                        "holder_character_id": ed.marcus,
                        "character_id": ed.marcus,
                        "visibility": {"scope": "public"},
                        "provenance": RecordProvenance.RENDER.value,
                        "trust_tier": records.trust_tier_of(RecordProvenance.RENDER).value,
                        "operation_id": op.operation_id,
                        "source_event_ids": [],
                        "narrative_turn": op.narrative_turn,
                        "turn_variant": op.turn_variant,
                        "beat": 1,
                        "proposition_ids": [],
                        "participants": [ed.marcus],
                    }
                ],
            )
            specs = await edges.fact_edges(
                conn,
                ed.story_id,
                DependencyEdgeType.DERIVES,
                "memories",
                memory,
                [fact_id],
                position=Position(op.narrative_turn or 0, 0, 0, 1, 0),
            )
            await edges.record_edges(
                conn, op, [dataclasses.replace(e, acquisition_kind="testimony") for e in specs]
            )

        op.add_commit_step(step)
        await op.commit()
    return memory


async def _amended(client: Any, engine: AsyncEngine, stub: Any) -> Amended:
    stub.when("", rs.UCLA, purpose="rewrite_parse")
    stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
    deps = await rs.story(client, engine)
    story, ed = deps.story_id, deps.ed
    opened = await rs.open_session(
        client, story, resolution_mode="best_effort", preview="apply_directly"
    )
    assert opened.status_code == 201, opened.text
    a = opened.json()["apply"]["amendment_id"]
    (part,) = await rows(
        engine,
        "SELECT p.written_fact_id, f.proposition_id FROM amendment_parts p "
        "JOIN facts f ON f.id = p.written_fact_id WHERE p.amendment_id = :a",
        a=a,
    )
    (ucla,) = await rows(
        engine,
        "SELECT object_id FROM propositions WHERE id = :p",
        p=part.proposition_id,
    )
    degree = ed.degree.proposition
    b = await _amend(
        engine,
        story,
        PropositionPartDraft(
            Proposition(
                subject_id=degree.subject_id,
                predicate_id=degree.predicate_id,
                object_id=ucla.object_id,
                valid_time=Interval.of(0, None),
            )
        ),
        [str(part.written_fact_id)],
    )
    (graduation,) = await rows(
        engine, "SELECT written_fact_id FROM amendment_parts WHERE amendment_id = :b", b=b
    )
    memory = await _remembers(engine, ed, str(graduation.written_fact_id))
    return Amended(story, ed, a, b, str(ucla.object_id), str(graduation.written_fact_id), memory)


def _undo(client: Any, story: str, amendment: str, **body: Any) -> Any:
    return client.post(
        f"/stories/{story}/amendments/{amendment}/undo",
        params=rs.OMNISCIENT,
        json=body,
        headers=WRITE,
    )


def _redo(client: Any, story: str, amendment: str, **body: Any) -> Any:
    return client.post(
        f"/stories/{story}/amendments/{amendment}/redo",
        params=rs.OMNISCIENT,
        json=body,
        headers=WRITE,
    )


async def _status(engine: AsyncEngine, amendment: str) -> tuple[str, str | None]:
    (row,) = await rows(
        engine, "SELECT status, reverted_by FROM amendments WHERE id = :a", a=amendment
    )
    return str(row.status), None if row.reverted_by is None else str(row.reverted_by)


async def _enrolled(engine: AsyncEngine, ed: Any) -> set[str]:
    """The objects canon holds now for Bobby's enrollment."""
    from epistrel.core.canon import facts as canon

    async with engine.connect() as conn:
        held = ed.enrollment.proposition
        found = await canon.canon(
            conn, ed.story_id, subject=held.subject_id, predicate=held.predicate_id
        )
    return {str(f.proposition.object_id) for f in found}


def _planned(preview: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {
        (entry["record"]["table"], entry["record"]["id"]): bucket
        for bucket, entries in preview["blast_radius"].items()
        for entry in entries
    }


@pytest.mark.part("M7", "FR-REWRITE-10,11; FR-STORE-18")
async def test_s_amend_undo(pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        made = await _amended(client, engine, epistrel.models.stub)
        story, ed = made.story_id, made.ed
        assert made.ucla in await _enrolled(engine, ed)
        (b_part,) = await rows(
            engine, "SELECT id FROM amendment_parts WHERE amendment_id = :b", b=made.b
        )
        amendments_before = await rs.count(engine, "amendments", story)

        # synchronous, preview_first: B and Marcus's memory in the radius, B asked about.
        opened = await _undo(client, story, made.a)
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert (session["kind"], session["target_amendment_id"]) == ("undo", made.a)
        assert session["state"] == "awaiting_answers"
        planned = _planned(session["preview"])
        assert planned[("amendment_parts", str(b_part.id))] == "irreducible"
        assert ("memories", made.memory) in planned
        (asked,) = [q for q in session["questions"] if q["kind"] == "dependent"]
        actions = {o["id"]: o["effect"]["action"] for o in asked["options"]}
        assert sorted(actions.values()) == ["cancel", "cascade_undo"]
        assert actions[asked["recommended_option_id"]] == "cascade_undo"
        # Nothing commits while the question is open.
        assert made.ucla in await _enrolled(engine, ed)
        assert (await _status(engine, made.b))[0] == "active"
        keep = await client.post(
            f"/stories/{story}/rewrites/{session['id']}/answers",
            params=rs.OMNISCIENT,
            json={"answers": [{"question_id": asked["id"], "option_id": "o9"}]},
            headers=WRITE,
        )
        assert keep.status_code == 422, keep.text
        assert keep.json()["error"]["code"] == "invalid_answer"

        answered = await rs.answer_all(client, story, session)
        assert answered.status_code == 200, answered.text
        ready = answered.json()
        applied = await rs.apply(client, story, ready["id"], ready["base_revision"])
        assert applied.status_code == 200, applied.text
        undo_id = applied.json()["amendment_id"]
        (head,) = await rows(engine, "SELECT kind, undoes FROM amendments WHERE id = :a", a=undo_id)
        assert (head.kind, head.undoes) == ("undo", made.a)
        assert await _status(engine, made.a) == ("reverted", undo_id)
        assert await _status(engine, made.b) == ("reverted", undo_id)
        assert await rs.count(engine, "amendments", story) == amendments_before + 1
        assert made.ucla not in await _enrolled(engine, ed)
        assert ed.osu in await _enrolled(engine, ed)
        # No active part's fact contradicts canon: every active part's written fact is open.
        stale = await rows(
            engine,
            "SELECT p.id FROM amendment_parts p JOIN amendments a ON a.id = p.amendment_id "
            "JOIN facts f ON f.id = p.written_fact_id WHERE a.story_id = :s "
            "AND p.status = 'active' AND a.kind <> 'undo' AND f.superseded_by IS NOT NULL",
            s=story,
        )
        assert stale == []

        # Redo A: another checked operation; A stays reverted.
        redone = await _redo(
            client, story, made.a, resolution_mode="best_effort", preview="apply_directly"
        )
        assert redone.status_code == 201, redone.text
        body = redone.json()
        assert (body["kind"], body["target_amendment_id"]) == ("redo", made.a)
        assert body["state"] == "applied", body
        redo_id = body["apply"]["amendment_id"]
        (head,) = await rows(engine, "SELECT kind, redoes FROM amendments WHERE id = :a", a=redo_id)
        assert (head.kind, head.redoes) == ("redo", made.a)
        assert (await _status(engine, made.a))[0] == "reverted"
        assert made.ucla in await _enrolled(engine, ed)
        again = await _redo(client, story, made.a)
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "amendment_already_redone"


@pytest.mark.part("M7", "FR-REWRITE-10,11; FR-STORE-18")
async def test_s_amend_undo__best_effort(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """``best_effort``: B cascade-undone, Marcus's memory regenerated, both in the report."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        made = await _amended(client, engine, epistrel.models.stub)
        story = made.story_id
        undone = await _undo(
            client, story, made.a, resolution_mode="best_effort", preview="apply_directly"
        )
        assert undone.status_code == 201, undone.text
        body = undone.json()
        assert body["state"] == "applied", body
        report = body["apply"]["report"]
        assert "cascade_undo" in {r["action"] for r in report["resolutions"]}
        regenerated = {
            a["record"]["id"] for a in report["applied"]["cascaded"] if a.get("regenerated")
        }
        assert made.memory in regenerated
        assert (await _status(engine, made.b))[0] == "reverted"


@pytest.mark.part("M7", "FR-REWRITE-10,11; FR-STORE-18")
async def test_s_amend_undo__cancel(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """``cancel`` on B's question cancels the whole undo; nothing is reverted."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        made = await _amended(client, engine, epistrel.models.stub)
        story = made.story_id
        session = (await _undo(client, story, made.a)).json()
        (asked,) = [q for q in session["questions"] if q["kind"] == "dependent"]
        cancel = next(o["id"] for o in asked["options"] if o["effect"]["action"] == "cancel")
        cancelled = await client.post(
            f"/stories/{story}/rewrites/{session['id']}/answers",
            params=rs.OMNISCIENT,
            json={"answers": [{"question_id": asked["id"], "option_id": cancel}]},
            headers=WRITE,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "cancelled"
        assert (await _status(engine, made.a))[0] == "active"
        assert (await _status(engine, made.b))[0] == "active"


@pytest.mark.part("M7", "FR-REWRITE-10,11; FR-STORE-18")
async def test_s_amend_undo__routes(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        made = await _amended(client, engine, epistrel.models.stub)
        story, ed = made.story_id, made.ed
        listed = await client.get(
            f"/stories/{story}/amendments", params=rs.OMNISCIENT, headers=WRITE
        )
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert [i["id"] for i in items] == [made.b, made.a]
        assert set(items[0]) >= {
            "id",
            "kind",
            "status",
            "author",
            "tx_time",
            "parts_count",
            "reverted_by",
            "undoes",
            "redoes",
            "session_id",
        }
        detail = await client.get(
            f"/stories/{story}/amendments/{made.b}", params=rs.OMNISCIENT, headers=WRITE
        )
        assert detail.status_code == 200, detail.text
        found = detail.json()
        assert len(found["parts"]) == 1
        assert found["relies_on"]
        assert {"table": "memories", "id": made.memory} in [
            d["record"] for d in found["dependents"]
        ]
        a_detail = (
            await client.get(
                f"/stories/{story}/amendments/{made.a}", params=rs.OMNISCIENT, headers=WRITE
            )
        ).json()
        assert (
            found["parts"][0]["id"] in {d["record"]["id"] for d in a_detail["dependents"]}
            or a_detail["dependents"]
        )

        missing = await client.get(
            f"/stories/{story}/amendments/{new_ulid()}", params=rs.OMNISCIENT, headers=WRITE
        )
        assert (missing.status_code, missing.json()["error"]["code"]) == (
            404,
            "amendment_not_found",
        )
        character = await client.get(
            f"/stories/{story}/amendments", params={"viewpoint": ed.bobby}, headers=WRITE
        )
        assert (character.status_code, character.json()["error"]["code"]) == (
            403,
            "omniscient_required",
        )
        active = await _redo(client, story, made.b)
        assert (active.status_code, active.json()["error"]["code"]) == (
            409,
            "amendment_not_reverted",
        )
        undone = await _undo(
            client, story, made.a, resolution_mode="best_effort", preview="apply_directly"
        )
        undo_id = undone.json()["apply"]["amendment_id"]
        twice = await _undo(client, story, made.a)
        assert (twice.status_code, twice.json()["error"]["code"]) == (
            409,
            "amendment_already_reverted",
        )
        assert twice.json()["error"]["details"]["reverted_by"] == undo_id
        of_undo = await _undo(client, story, undo_id)
        assert (of_undo.status_code, of_undo.json()["error"]["code"]) == (422, "use_redo")
        filtered = await client.get(
            f"/stories/{story}/amendments",
            params={**rs.OMNISCIENT, "status": "reverted"},
            headers=WRITE,
        )
        assert {i["id"] for i in filtered.json()["items"]} == {made.a, made.b}

        # A Lite story: list and detail answer; undo is refused until #245.
        lite = (await create(client, story_body("Wren", "Ada")))["story_id"]
        async with Operation(
            engine, story_id=lite, kind=OperationKind.TURN, principal="p-test"
        ) as op:
            op.append("player_input", {"text": "Hello."}, provenance=Provenance.PLAYER)
            op.append("message_rendered", {"text": "Ada waves."})
            await op.commit()
        (reply,) = await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'message_rendered'",
            s=lite,
        )
        lite_amendment = await _amend(
            engine,
            lite,
            RecordPartDraft("events", str(reply.id), "replace", {"text": "Ada nods."}),
            [],
        )
        assert (
            await client.get(f"/stories/{lite}/amendments", params=rs.OMNISCIENT, headers=WRITE)
        ).status_code == 200
        assert (
            await client.get(
                f"/stories/{lite}/amendments/{lite_amendment}", params=rs.OMNISCIENT, headers=WRITE
            )
        ).status_code == 200
        # #245: a Lite story's amendment is undone through a Lite session like any other.
        opened = await _undo(client, lite, lite_amendment)
        assert opened.status_code == 201, opened.text
        assert opened.json()["kind"] == "undo"
        assert await rs.count(engine, "rewrite_sessions", lite) == 1
