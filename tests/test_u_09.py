"""U-09: rewrite mode disposition (docs/04 §4; FR-REWRITE-3,4,5,6; #223, #224).

- Over a stubbed blast radius with one ``high`` and one ``low`` conflict (``dispose``, pure):
  ``synchronous`` asks about both; ``best_effort`` settles both itself; ``threshold(high)`` asks
  about the high one and settles the low one; ``threshold(low)`` asks about both. Every question
  recommends the least change, and a settled one is pre-answered with it.
- ``preview_first`` writes nothing until the apply; ``apply_directly`` applies in the call that
  leaves nothing pending (the create, under ``best_effort``).
- Detection is identical across the six ``resolution_mode`` by ``preview`` combinations: one
  ``plan_hash``, the same changes and the same blast radius.
- An induced failure after the apply's parts (the fault point ``rewrite.apply.after_part``)
  reverts wholesale: 503 ``rewrite_apply_failed`` (retryable), no amendment, part or edge from
  the apply, the revision unchanged, and the session back in ``awaiting_approval`` with
  ``last_error``; the retry applies.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.orchestrator.rewrite.dispose import dispose, least_change
from epistrel.testing.faults import rewrite_apply_failing
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.story_support import WRITE

HIGH = {
    "conflict_id": "c-high",
    "record": {"table": "events", "id": "e1"},
    "severity": "high",
    "options": ["rewrite", "drop", "keep"],
    "reason": "not_substitutable",
}
LOW = {
    "conflict_id": "c-low",
    "record": {"table": "memories", "id": "m1"},
    "severity": "low",
    "options": ["rewrite", "drop", "keep"],
    "reason": "not_substitutable",
}
PART = {
    "conflict_id": "c-part",
    "record": {"table": "amendment_parts", "id": "p1"},
    "severity": "low",
    "options": ["rewrite", "drop", "keep", "cascade_undo"],
    "reason": "dependent_amendment",
}
MODES: list[Any] = ["synchronous", "best_effort", {"threshold": "high"}]
PREVIEWS = ["preview_first", "apply_directly"]


@pytest.mark.part("M7", "FR-REWRITE-3,4,5,6")
def test_u_09() -> None:
    def asked(mode: str, threshold: str | None = None) -> list[str]:
        found = dispose([HIGH, LOW], mode, threshold)
        assert len(found.questions) == 2
        return sorted(q.severity or "" for q in found.asked)

    assert asked("synchronous") == ["high", "low"]
    assert asked("best_effort") == []
    assert asked("threshold", "high") == ["high"]
    assert asked("threshold", "low") == ["high", "low"]
    settled = dispose([HIGH, LOW], "threshold", "high").auto
    assert [q.required for q in settled] == [False]
    for question in dispose([HIGH, LOW, PART], "synchronous", None).questions:
        chosen = next(o for o in question.options if o["id"] == question.recommended)
        assert chosen["effect"]["action"] != "keep"  # keep is never chosen automatically
    assert [least_change(c) for c in (HIGH, LOW, PART)] == ["rewrite", "rewrite", "cascade_undo"]
    assert least_change({**HIGH, "record": {"table": "facts", "id": "f"}}) == "drop"


@pytest.mark.integration
@pytest.mark.part("M7", "FR-REWRITE-5")
async def test_u_09__detection(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        world = await rs.story(client, engine)
        story = world.story_id
        epistrel.models.stub.when("", rs.UCLA, purpose="rewrite_parse")
        seen = []
        for mode in MODES:
            for preview in PREVIEWS:
                settles = mode == "best_effort" and preview == "apply_directly"
                if settles:
                    # It would apply in this very call: let the apply fail (nothing is
                    # written) to read the plan it was computed from.
                    with rewrite_apply_failing():
                        opened = await rs.open_session(
                            client, story, resolution_mode=mode, preview=preview
                        )
                    assert opened.status_code == 503, opened.text
                    listed = await client.get(
                        f"/stories/{story}/rewrites", params=rs.OMNISCIENT, headers=WRITE
                    )
                    newest = listed.json()["items"][0]["id"]
                    found = await client.get(
                        f"/stories/{story}/rewrites/{newest}", params=rs.OMNISCIENT, headers=WRITE
                    )
                    session = found.json()
                else:
                    opened = await rs.open_session(
                        client, story, resolution_mode=mode, preview=preview
                    )
                    assert opened.status_code == 201, opened.text
                    session = opened.json()
                assert (session["resolution_mode"], session["preview_mode"]) == (mode, preview)
                seen.append(session["preview"])
                cancelled = await client.post(
                    f"/stories/{story}/rewrites/{session['id']}/cancel",
                    params=rs.OMNISCIENT,
                    headers=WRITE,
                )
                assert cancelled.status_code == 200, cancelled.text
        assert len({p["plan_hash"] for p in seen}) == 1
        assert all(p["blast_radius"] == seen[0]["blast_radius"] for p in seen)
        assert all(p["changes"] == seen[0]["changes"] for p in seen)
        assert await rs.count(engine, "amendments", story) == 0


@pytest.mark.integration
@pytest.mark.part("M7", "FR-REWRITE-3,4,6")
async def test_u_09__apply(pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        world = await rs.story(client, engine)
        story = world.story_id
        stub = epistrel.models.stub
        stub.when("", rs.UCLA, purpose="rewrite_parse")
        stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
        revision = await rs.revision(engine, story)

        # best_effort + preview_first: settled, awaiting approval, nothing written.
        opened = await rs.open_session(client, story, resolution_mode="best_effort")
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert session["state"] == "awaiting_approval"
        auto = [q for q in session["questions"] if q["kind"] == "conflict"]
        assert auto
        assert all(not q["required"] and q["answered_by"] == "engine" for q in auto)
        assert await rs.count(engine, "amendments", story) == 0
        assert await rs.revision(engine, story) == revision

        # The induced failure reverts wholesale.
        edges_before = await rs.count(engine, "dependency_edges", story)
        with rewrite_apply_failing():
            failed = await rs.apply(client, story, session["id"], revision)
        assert failed.status_code == 503, failed.text
        error = failed.json()["error"]
        assert (error["code"], error["details"]["retryable"]) == ("rewrite_apply_failed", True)
        assert await rs.count(engine, "amendments", story) == 0
        assert await rs.count(engine, "amendment_parts", story) == 0
        assert await rs.count(engine, "dependency_edges", story) == edges_before
        assert await rs.revision(engine, story) == revision
        again = await client.get(
            f"/stories/{story}/rewrites/{session['id']}", params=rs.OMNISCIENT, headers=WRITE
        )
        assert again.json()["state"] == "awaiting_approval"
        assert again.json()["last_error"]["code"] == "InducedApplyFailure"

        # The retry applies; best_effort reports every resolution it chose.
        applied = await rs.apply(client, story, session["id"], revision)
        assert applied.status_code == 200, applied.text
        report = applied.json()["report"]
        assert len(report["resolutions"]) == 3
        assert all(r["auto"] for r in report["resolutions"])
        assert await rs.revision(engine, story) == revision + 1

    # apply_directly applies in the create call when nothing is pending.
    async with respond_client(settings, engine) as (client, epistrel):
        world = await rs.story(client, engine)
        epistrel.models.stub.when("", rs.UCLA, purpose="rewrite_parse")
        epistrel.models.stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
        direct = await rs.open_session(
            client, world.story_id, resolution_mode="best_effort", preview="apply_directly"
        )
        assert direct.status_code == 201, direct.text
        body = direct.json()
        assert body["state"] == "applied"
        assert body["apply"]["amendment_id"] == body["result_amendment_id"]


@pytest.mark.integration
@pytest.mark.part("M7", "FR-REWRITE-6")
async def test_u_09__pipeline_changed(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A session opened on a structured story whose effective pipeline is then flipped to
    ``lite`` (a direct configuration write) is refused at apply: 409 ``rewrite_stale
    {reason: pipeline_changed}``, and the session is ``abandoned``. Complement: no amendment,
    and the revision unchanged."""
    import json as _json

    from tests.event_support import execute_in_tx

    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        world = await rs.story(client, engine)
        story = world.story_id
        epistrel.models.stub.when("", rs.UCLA, purpose="rewrite_parse")
        session = (await rs.open_session(client, story, resolution_mode="best_effort")).json()
        assert session["state"] == "awaiting_approval"
        revision = await rs.revision(engine, story)
        await execute_in_tx(
            engine,
            (
                "INSERT INTO story_profile_overrides (story_id, settings, revision) "
                "VALUES (:s, CAST(:o AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE "
                "SET settings = story_profile_overrides.settings || excluded.settings, "
                "revision = story_profile_overrides.revision + 1",
                {"s": story, "o": _json.dumps({"pipeline": "lite"})},
            ),
        )
        refused = await rs.apply(client, story, session["id"], revision)
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["details"]["reason"]) == ("rewrite_stale", "pipeline_changed")
        found = await client.get(
            f"/stories/{story}/rewrites/{session['id']}", params=rs.OMNISCIENT, headers=WRITE
        )
        assert found.json()["state"] == "abandoned"
        assert await rs.count(engine, "amendments", story) == 0
        assert await rs.revision(engine, story) == revision
