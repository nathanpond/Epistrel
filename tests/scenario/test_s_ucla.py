"""S-UCLA: a retcon cascades by dependency type (docs/04 §7; FR-DIRECT-3,4,5; #225).

Canon: Bobby went to OSU (the Bobby education fixture). A turn's records depend on it: a reply
mentioning his OSU sweatshirt in passing (``mentions`` OSU), a decision the enrollment caused
("Bobby decides to visit his old coach": ``causes``), and the player's own line "Go Buckeyes,
Bobby!" (``presupposes`` it without naming OSU). "Bobby went to UCLA", ``best_effort``:

- the sweatshirt mention is auto-fixed (incidental: "his UCLA sweatshirt");
- the decision is regenerated (``rewrite_record``) and flagged ``regenerated`` in the report;
- the player's line is an irreducible conflict with ``rewrite|drop|keep`` and a recommendation
  (``high``: a delivered message), settled by the least change and reported;
- the change is bitemporal: canon as recorded before the amendment shows OSU, canon now shows
  UCLA over the whole valid span;
- it is reversible: #221's ``revert_amendment`` restores every record (the transcript, the
  facts read now) and the amendment row stays, ``reverted``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import edges
from epistrel.core.canon import facts as canon
from epistrel.core.enums import DependencyEdgeType, DependencyTarget, OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from tests import amendment_support as am
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import ENROLLED_AT, Education, bobby_education
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

SWEATSHIRT = "Bobby pulls on his OSU sweatshirt."
DECISION = "Bobby decides to visit his old coach."
BUCKEYES = "Go Buckeyes, Bobby!"
VISIT = "Bobby decides to visit his old coach in Los Angeles."


async def _dependents(
    engine: AsyncEngine, ed: Education, *, premises: tuple[str, ...] = ()
) -> dict[str, str]:
    made: dict[str, str] = {}
    async with Operation(
        engine, story_id=ed.story_id, kind=OperationKind.TURN, principal="p-test"
    ) as op:
        op.append("player_input", {"text": BUCKEYES}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": SWEATSHIRT})
        op.append("message_rendered", {"text": DECISION})

        async def step(conn: AsyncConnection) -> None:
            found = {
                str(dict(r.payload).get("text")): str(r.id)
                for r in await conn.execute(
                    text("SELECT id, payload FROM events WHERE operation_id = :o"),
                    {"o": op.operation_id},
                )
            }
            at = Position(op.narrative_turn or 0, 0, 0, 1, 0)
            specs = [
                edges.EdgeSpec(
                    DependencyEdgeType.MENTIONS,
                    "events",
                    found[SWEATSHIRT],
                    DependencyTarget.ENTITY,
                    ed.osu,
                    position=at,
                ),
                *await edges.fact_edges(
                    conn,
                    ed.story_id,
                    DependencyEdgeType.CAUSES,
                    "events",
                    found[DECISION],
                    [ed.enrollment.id, *premises],
                    position=at,
                ),
                *await edges.fact_edges(
                    conn,
                    ed.story_id,
                    DependencyEdgeType.PRESUPPOSES,
                    "events",
                    found[BUCKEYES],
                    [ed.enrollment.id],
                    position=at,
                ),
            ]
            await edges.record_edges(conn, op, specs)
            made.update(
                {"sweatshirt": found[SWEATSHIRT], "decision": found[DECISION]}
                | {"buckeyes": found[BUCKEYES]}
            )

        op.add_commit_step(step)
        await op.commit()
    return made


async def _transcript(engine: AsyncEngine, story: str) -> dict[str, Any]:
    return {
        str(r.message_id): r.text
        for r in await rows(
            engine, "SELECT message_id, text FROM transcript_messages WHERE story_id = :s", s=story
        )
    }


async def _enrollments(engine: AsyncEngine, story: str, bobby: str, **at: Any) -> set[str]:
    async with engine.connect() as conn:
        found = await canon.canon(conn, story, subject=bobby, predicate=ENROLLED_AT, **at)
    return {str(f.proposition.object_id) for f in found if f.valid_to is None}


@pytest.mark.part("M7", "FR-DIRECT-3,4,5")
async def test_s_ucla(pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
        assert ed.ucla is not None
        story = ed.story_id
        made = await _dependents(engine, ed)
        transcript_before = await _transcript(engine, story)
        async with engine.connect() as conn:
            before_amendment = (await conn.execute(text("SELECT now()"))).scalar_one()
        assert await _enrollments(engine, story, ed.bobby) == {ed.osu}

        stub = epistrel.models.stub
        stub.when("", rs.UCLA, purpose="rewrite_parse")
        stub.when("", f'{{"text": "{VISIT}"}}', purpose="rewrite_record")
        session = (await rs.open_session(client, story, resolution_mode="best_effort")).json()
        (conflict,) = session["preview"]["blast_radius"]["irreducible"]
        assert conflict["record"]["id"] == made["buckeyes"]
        assert (conflict["reason"], conflict["severity"]) == ("player_authored", "high")
        assert set(conflict["options"]) == {"rewrite", "drop", "keep"}
        (question,) = [q for q in session["questions"] if q["kind"] == "conflict"]
        assert question["recommended_option_id"]
        assert question["recommendation_reason"]
        revision = await rs.revision(engine, story)
        applied = await rs.apply(client, story, session["id"], revision)
        assert applied.status_code == 200, applied.text
        result = applied.json()
        report = result["report"]

        lines = await _transcript(engine, story)
        assert lines[made["sweatshirt"]] == "Bobby pulls on his UCLA sweatshirt."
        assert {a["record"]["id"] for a in report["applied"]["incidental"]} == {made["sweatshirt"]}
        regenerated = [e for e in report["applied"]["cascaded"] if e.get("regenerated")]
        assert [e["record"]["id"] for e in regenerated] == [made["decision"]]
        assert lines[made["decision"]] == VISIT
        (resolution,) = report["resolutions"]
        assert (resolution["auto"], resolution["severity"]) == (True, "high")
        assert resolution["records"][0]["id"] == made["buckeyes"]

        # Bitemporal: as recorded before the amendment, OSU; now, UCLA.
        assert ed.ucla in await _enrollments(engine, story, ed.bobby)
        assert await _enrollments(engine, story, ed.bobby, record_time=before_amendment) == {ed.osu}

        # Reversible: the revert restores every record; the amendment stays, reverted.
        await am.amend(engine, story, [], kind="undo", revert=result["amendment_id"])
        assert await _transcript(engine, story) == transcript_before
        assert ed.ucla not in await _enrollments(engine, story, ed.bobby)
        (amendment,) = await rows(
            engine, "SELECT status FROM amendments WHERE id = :a", a=result["amendment_id"]
        )
        assert amendment.status == "reverted"


@pytest.mark.part("M7", "FR-DIRECT-3")
async def test_s_ucla__supported_decision(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#707 (#225's ``causes`` → ``re_evaluate``): a decision with another premise, Bobby's
    OSU degree, which #140's check still supports under the hypothetical canon (enrolled at
    UCLA), is kept: its line is not regenerated (no ``rewrite_record`` call for it), it is not
    flagged ``regenerated``, and the report says ``decision_kept``. The single-premise decision
    of ``test_s_ucla`` is the complement (regenerated)."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
        story = ed.story_id
        made = await _dependents(engine, ed, premises=(ed.degree.id,))
        stub = epistrel.models.stub
        stub.when("", rs.UCLA, purpose="rewrite_parse")
        stub.when("", f'{{"text": "{VISIT}"}}', purpose="rewrite_record")
        session = (await rs.open_session(client, story, resolution_mode="best_effort")).json()
        planned = {
            e["record"]["id"]: e["action"] for e in session["preview"]["blast_radius"]["cascaded"]
        }
        assert planned[made["decision"]] == "keep"
        revision = await rs.revision(engine, story)
        applied = await rs.apply(client, story, session["id"], revision)
        assert applied.status_code == 200, applied.text
        report = applied.json()["report"]
        assert not [e for e in report["applied"]["cascaded"] if e.get("regenerated")]
        kept = [n for n in report["notifications"] if n["kind"] == "decision_kept"]
        assert [n["record"]["id"] for n in kept] == [made["decision"]]
        assert (await _transcript(engine, story))[made["decision"]] == DECISION
