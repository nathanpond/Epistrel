"""I-13: the rewrite side-channel end to end (docs/04 §5; FR-REWRITE-1,2,6,7,8; #223, #224).

On the Bobby education fixture (``tests.rewrite_support``), a turn's records depend on Bobby's
enrollment at OSU through #220's typed edges. "Bobby actually went to UCLA" (``rewrite_parse``
scripted) parses into one proposition change, ``enrolled_at(Bobby, UCLA)`` superseding OSU, and
the preview's blast radius puts each record in the bucket its edge type and authorship give: the
reply naming OSU is a cascaded ``fix``, the reply assuming it without naming OSU and the
player's line are irreducible conflicts (``high``: delivered messages), the reflection that
assumes it is an irreducible conflict confined to a memory (``low``), the memory mentioning OSU
is incidental and the speech act is relabelled. The preview is stamped with the revision, which
opening never moves; a story forked before the session keeps its rows.

#224: under the default ``synchronous`` + ``preview_first`` every conflict is a required question
recommending the least change. Accepting them (the replies rewritten by ``rewrite_record``,
scripted) and applying with the base revision records one amendment: canon now holds UCLA, the
reply naming OSU names UCLA, the conflicting reply reads its rewrite, the revision moved by
exactly one, and the session is ``applied`` with its report. The questions and answers are
``rewrite_question``/``rewrite_answer`` audit events of the amendment's operation, with
``rewrite_closed``. The forked sibling is untouched.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import rewrite_support as rs
from tests.amendment_support import table_rows
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import ENROLLED_AT
from tests.respond_support import respond_client
from tests.story_support import WRITE

pytestmark = pytest.mark.integration


async def _snapshot(engine: AsyncEngine, story: str) -> dict[str, list[dict[str, Any]]]:
    return {
        table: await table_rows(engine, table, story)
        for table in ("events", "facts", "state_changes", "rewrite_sessions", "memories")
    }


def _bucket(preview: dict[str, Any], name: str) -> dict[tuple[str, str], dict[str, Any]]:
    entries = preview["relabels"] if name == "relabels" else preview["blast_radius"][name]
    return {(e["record"]["table"], e["record"]["id"]): e for e in entries}


@pytest.mark.part("M7", "FR-REWRITE-1,2,7,8")
async def test_i_13(pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        world = await rs.story(client, engine)
        ed, made, story = world.ed, world.records, world.story_id
        fork = await client.post(
            f"/stories/{story}/fork", json={"at_turn": 1, "name": "Before"}, headers=WRITE
        )
        assert fork.status_code in (200, 201), fork.text
        forked = fork.json()["story_id"]
        before = await _snapshot(engine, forked)
        revision = await rs.revision(engine, story)

        epistrel.models.stub.script("rewrite_parse", [rs.UCLA])
        opened = await rs.open_session(client, story)
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert (session["resolution_mode"], session["preview_mode"]) == (
            "synchronous",
            "preview_first",
        )
        assert session["timeline_revision"] == revision
        preview = session["preview"]
        assert preview["base_revision"] == revision
        (change,) = preview["changes"]
        assert (change["subject_id"], change["predicate_id"], change["object_id"]) == (
            ed.bobby,
            ENROLLED_AT,
            ed.ucla,
        )
        assert change["superseded_object_id"] == ed.osu
        cascaded, irreducible = _bucket(preview, "cascaded"), _bucket(preview, "irreducible")
        assert cascaded[made["names"]]["action"] == "fix"
        assert irreducible[made["assumes"]]["reason"] == "not_substitutable"
        assert irreducible[made["player"]]["reason"] == "player_authored"
        assert irreducible[made["assumes"]]["severity"] == "high"
        assert irreducible[made["player"]]["severity"] == "high"
        assert irreducible[made["reflection"]]["severity"] == "low"
        assert set(irreducible[made["player"]]["options"]) == {"rewrite", "drop", "keep"}
        assert _bucket(preview, "incidental")[made["memory"]]["action"] == "fix"
        assert set(_bucket(preview, "relabels")) == {made["utterance"]}
        stored = (
            await rows(
                engine,
                "SELECT plan_read_set FROM rewrite_sessions WHERE id = :i",
                i=session["id"],
            )
        )[0].plan_read_set
        assert {(d["table"], d["id"]) for d in stored["dependents"]} >= set(made.values())
        assert await rs.revision(engine, story) == revision

        # #224: synchronous asks about every conflict, recommending the least change.
        assert session["state"] == "awaiting_answers"
        conflicts = [q for q in session["questions"] if q["kind"] == "conflict"]
        assert len(conflicts) == 3
        assert all(q["required"] for q in conflicts)
        early = await rs.apply(client, story, session["id"], revision)
        assert (early.status_code, early.json()["error"]["code"]) == (
            409,
            "rewrite_questions_pending",
        )
        epistrel.models.stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
        answered = await rs.answer_all(client, story, session)
        assert answered.status_code == 200, answered.text
        assert answered.json()["state"] == "awaiting_approval"
        assert await rs.count(engine, "amendments", story) == 0  # preview_first: nothing yet
        missing = await rs.apply(client, story, session["id"], None)
        assert missing.json()["error"]["code"] == "base_revision_required"
        stale = await rs.apply(client, story, session["id"], revision - 1)
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "rewrite_stale")

        applied = await rs.apply(client, story, session["id"], revision)
        assert applied.status_code == 200, applied.text
        result = applied.json()
        assert result["timeline_revision"] == revision + 1
        assert result["session"]["state"] == "applied"
        assert result["session"]["result_amendment_id"] == result["amendment_id"]
        report = result["report"]
        assert {r["action"] for r in report["resolutions"]} == {"rewrite"}
        assert [r["auto"] for r in report["resolutions"]] == [False, False, False]
        assert {a["record"]["id"] for a in report["applied"]["cascaded"]} == {made["names"][1]}

        # The story now: canon holds UCLA; the replies read their repairs.
        current = await rows(
            engine,
            "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.subject_id = :b AND p.predicate_id = :e "
            "AND p.object_id = :u AND f.superseded_by IS NULL AND f.membership = 'active'",
            s=story,
            b=ed.bobby,
            e=ENROLLED_AT,
            u=ed.ucla,
        )
        assert current
        lines = {
            str(r.message_id): r.text
            for r in await rows(
                engine,
                "SELECT message_id, text FROM transcript_messages WHERE story_id = :s",
                s=story,
            )
        }
        assert lines[made["names"][1]] == "Bobby says UCLA was a fine school."
        assert lines[made["assumes"][1]] == rs.REWRITTEN
        (amendment,) = await rows(
            engine, "SELECT session_id FROM amendments WHERE story_id = :s", s=story
        )
        assert amendment.session_id == session["id"]

        # The Q&A are audit events of the amendment's operation.
        audit = await rows(
            engine,
            "SELECT e.type, e.category FROM events e JOIN amendments a "
            "ON a.operation_id = e.operation_id WHERE a.id = :a",
            a=result["amendment_id"],
        )
        kinds = [r.type for r in audit if r.category == "audit"]
        asked = answered.json()["questions"]
        assert kinds.count("rewrite_question") == len(asked)
        assert kinds.count("rewrite_answer") == len(asked)
        assert kinds.count("rewrite_closed") == 1

        # The forked sibling is untouched, and has no session.
        assert await _snapshot(engine, forked) == before
        listed = await client.get(
            f"/stories/{forked}/rewrites", params=rs.OMNISCIENT, headers=WRITE
        )
        assert listed.json()["items"] == []
