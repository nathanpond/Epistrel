"""S-REWRITE-MODES: configurable resolution and preview (docs/04 §7; FR-REWRITE-3,4,5,6; #224,
#238 adds the undo of an applied rewrite as a whole).

The same rewrite with engineered conflicts (``tests.rewrite_support``: two delivered lines,
``high``, and a reflection, ``low``) under each mode:

- with no ``resolution_mode`` or ``preview`` the session is ``synchronous`` + ``preview_first``:
  it pauses on every conflict and commits nothing until answered and approved; while it waits, a
  turn played sees the pre-rewrite canon;
- ``best_effort`` settles every conflict by the least change and its report lists each
  resolution (``auto``), the line it rewrote with its before and after;
- ``threshold(high)`` pauses on the delivered lines only; the reflection is settled itself;
- ``preview_first`` applies nothing until approved; ``apply_directly`` applies in the call that
  leaves nothing pending; every apply is one amendment that moves the revision by one;
- detection identical across the modes on one story is U-09's (``test_u_09__detection``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import ENROLLED_AT
from tests.respond_support import respond_client
from tests.story_support import WRITE

pytestmark = pytest.mark.integration


async def _ucla_is_canon(engine: AsyncEngine, world: rs.Dependents) -> bool:
    found = await rows(
        engine,
        "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND p.subject_id = :b AND p.predicate_id = :e "
        "AND p.object_id = :u AND f.superseded_by IS NULL AND f.membership = 'active'",
        s=world.story_id,
        b=world.ed.bobby,
        e=ENROLLED_AT,
        u=world.ed.ucla,
    )
    return bool(found)


@pytest.mark.part("M7", "FR-REWRITE-3,4,5,6")
async def test_s_rewrite_modes(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        def script() -> None:
            stub.reset()
            stub.when("", rs.UCLA, purpose="rewrite_parse")
            stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
            stub.when("", "Mm.", purpose="generation")

        # The defaults: synchronous + preview_first.
        world = await rs.story(client, engine)
        script()
        session = (await rs.open_session(client, world.story_id)).json()
        assert (session["resolution_mode"], session["preview_mode"]) == (
            "synchronous",
            "preview_first",
        )
        assert session["state"] == "awaiting_answers"
        assert session["preview"]["plan_hash"]
        asked = [q for q in session["questions"] if q["kind"] == "conflict"]
        assert len(asked) == 3
        assert all(q["required"] for q in asked)
        assert not await _ucla_is_canon(engine, world)
        answered = (await rs.answer_all(client, world.story_id, session)).json()
        assert answered["state"] == "awaiting_approval"
        assert not await _ucla_is_canon(engine, world)
        revision = await rs.revision(engine, world.story_id)
        applied = await rs.apply(client, world.story_id, session["id"], revision)
        assert applied.status_code == 200, applied.text
        assert applied.json()["timeline_revision"] == revision + 1
        assert await _ucla_is_canon(engine, world)

        # best_effort: every conflict settled by the least change, each one reported.
        world = await rs.story(client, engine)
        script()
        session = (
            await rs.open_session(client, world.story_id, resolution_mode="best_effort")
        ).json()
        assert session["preview"]["plan_hash"]
        assert session["state"] == "awaiting_approval"
        revision = await rs.revision(engine, world.story_id)
        report = (await rs.apply(client, world.story_id, session["id"], revision)).json()["report"]
        assert len(report["resolutions"]) == 3
        assert all(r["auto"] and r["action"] == "rewrite" for r in report["resolutions"])
        assumes = world.records["assumes"][1]
        (resolved,) = [r for r in report["resolutions"] if r["records"][0]["id"] == assumes]
        assert resolved["records"][0]["before_excerpt"] == rs.LINES["assumes"]
        assert resolved["records"][0]["after_excerpt"] == rs.REWRITTEN

        # threshold(high): the delivered lines pause; the reflection is settled itself.
        world = await rs.story(client, engine)
        script()
        session = (
            await rs.open_session(client, world.story_id, resolution_mode={"threshold": "high"})
        ).json()
        assert session["preview"]["plan_hash"]
        conflicts = {q["severity"]: q for q in session["questions"] if q["kind"] == "conflict"}
        required = sorted(
            q["severity"] for q in session["questions"] if q["kind"] == "conflict" and q["required"]
        )
        assert required == ["high", "high"]
        assert conflicts["low"]["required"] is False
        assert conflicts["low"]["answered_by"] == "engine"
        assert session["state"] == "awaiting_answers"

        # apply_directly: the call that clears the last question applies.
        cleared = await client.post(
            f"/stories/{world.story_id}/rewrites/{session['id']}/cancel",
            params=rs.OMNISCIENT,
            headers=WRITE,
        )
        assert cleared.status_code == 200
        session = (
            await rs.open_session(
                client,
                world.story_id,
                resolution_mode={"threshold": "high"},
                preview="apply_directly",
            )
        ).json()
        assert session["state"] == "awaiting_answers"
        assert not await _ucla_is_canon(engine, world)
        done = await rs.answer_all(client, world.story_id, session)
        assert done.status_code == 200, done.text
        assert done.json()["state"] == "applied"
        assert done.json()["apply"]["amendment_id"]
        assert await _ucla_is_canon(engine, world)


async def _story_state(engine: AsyncEngine, story_id: str) -> dict[str, list[tuple[object, ...]]]:
    """The story's truth, normalised (no ids a rewrite mints, no transaction times): the open
    canon facts by their proposition, the transcript's and the active memories' texts and the
    utterances' labels."""
    from epistrel.core.canon import facts as canon

    async with engine.connect() as conn:
        held = await canon.canon(conn, story_id)
    facts = [
        (
            f.proposition.subject_id,
            f.proposition.predicate_id,
            f.proposition.object_id,
            str(f.proposition.literal),
            str(f.proposition.polarity),
        )
        for f in held
    ]
    events = await rows(
        engine,
        "SELECT message_id, text FROM transcript_messages WHERE story_id = :s ORDER BY message_id",
        s=story_id,
    )
    memories = await rows(
        engine,
        "SELECT id, text FROM memories WHERE story_id = :s AND membership = 'active' ORDER BY id",
        s=story_id,
    )
    utterances = await rows(
        engine,
        "SELECT id, objective_relation, classification FROM utterances WHERE story_id = :s "
        "ORDER BY id",
        s=story_id,
    )
    return {
        "facts": sorted(facts, key=str),
        "events": [tuple(r) for r in events],
        "memories": [tuple(r) for r in memories],
        "utterances": [tuple(r) for r in utterances],
    }


@pytest.mark.part("M7", "FR-REWRITE-10")
async def test_s_rewrite_modes__undo(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#238: the rewrite applied under each mode is undone as a whole through the checked undo,
    and the story's truth equals its pre-rewrite state (facts, texts, labels)."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for mode in ("best_effort", {"threshold": "high"}, "synchronous"):
            stub.reset()
            stub.when("", rs.UCLA, purpose="rewrite_parse")
            stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
            world = await rs.story(client, engine)
            before = await _story_state(engine, world.story_id)
            session = (await rs.open_session(client, world.story_id, resolution_mode=mode)).json()
            if session["state"] == "awaiting_answers":
                session = (await rs.answer_all(client, world.story_id, session)).json()
            applied = await rs.apply(
                client, world.story_id, session["id"], session["base_revision"]
            )
            assert applied.status_code == 200, applied.text
            assert await _ucla_is_canon(engine, world)
            assert await _story_state(engine, world.story_id) != before
            undone = await client.post(
                f"/stories/{world.story_id}/amendments/{applied.json()['amendment_id']}/undo",
                params=rs.OMNISCIENT,
                json={"resolution_mode": "best_effort", "preview": "apply_directly"},
                headers=WRITE,
            )
            assert undone.status_code == 201, undone.text
            assert undone.json()["state"] == "applied", undone.text
            assert await _story_state(engine, world.story_id) == before, mode
