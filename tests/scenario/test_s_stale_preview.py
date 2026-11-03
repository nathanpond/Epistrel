"""S-STALE-PREVIEW (docs/04 §7; FR-CONC-7, FR-REWRITE-4; #237): a rewrite holds no lock while
the author deliberates, and the final commit revalidates the plan.

The OSU fixture (``tests/rewrite_support``): "Bobby actually went to UCLA", previewed at
revision R under ``synchronous`` (three conflicts, answered).

1. A turn commits touching nothing in the blast radius (a line about the weather): the apply at
   R succeeds; the story moved, the plan did not.
2. A turn commits a fact on Bobby's enrollment (a ``proposition_changes`` row on the planned
   key): the apply is 409 ``rewrite_stale`` with reason ``proposition_changed``, a re-preview at
   the new revision, and the author's answers carried over (no question re-asked whose key
   survived). No amendment exists after the 409; an apply naming the new base applies.
3. ``synchronous`` with the change landing mid-Q&A: every answer is accepted (no check runs at
   answer time); each answer re-plans at the current revision (#223), so the plan approved
   after the change is computed against it and the final commit finds it fresh.
4. An apply naming another base than the session's preview: 409 ``rewrite_stale``
   ``base_mismatch`` with the current preview, nothing recomputed (``stale_count`` unchanged).
5. ``revalidate`` over a State the plan writes: a committed change on the same
   ``(state_id, scope_key)`` since the base is ``state_changed`` (no ``proposition_changes``
   row); a change on an unrelated key leaves the plan fresh.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.orchestrator.rewrite import revalidate
from tests import canon_support as c
from tests import rewrite_support as rs
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.memory_support import use_structured
from tests.respond_support import respond_client
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration


async def _weather(engine: AsyncEngine, story_id: str) -> None:
    """A turn that touches nothing the plan reads."""
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal="p-test"
    ) as op:
        op.append("player_input", {"text": "Nice day."}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "The sun is out over the quad."})
        await op.commit()


async def _enrollment(engine: AsyncEngine, ed: Any) -> None:
    """A turn that writes a fact on the planned key (Bobby, enrolled_at)."""
    proposition = ed.enrollment.proposition
    await c.commit(
        engine,
        ed.story_id,
        Proposition(
            subject_id=proposition.subject_id,
            predicate_id=proposition.predicate_id,
            object_id=ed.florida,
            valid_time=Interval.of(0, None),
        ),
    )


async def _answered(client: Any, story: str) -> dict[str, Any]:
    opened = await rs.open_session(client, story)
    assert opened.status_code == 201, opened.text
    answered = await rs.answer_all(client, story, opened.json())
    assert answered.status_code == 200, answered.text
    session: dict[str, Any] = answered.json()
    assert session["state"] == "awaiting_approval"
    return session


@pytest.mark.part("M7", "FR-CONC-7; FR-REWRITE-4")
async def test_s_stale_preview(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        deps = await rs.story(client, engine)
        story = deps.story_id
        stub = epistrel.models.stub
        stub.when("", rs.UCLA, purpose="rewrite_parse")
        stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")

        # 1. The story moves outside the blast radius: the apply at R succeeds.
        session = await _answered(client, story)
        base = session["base_revision"]
        await _weather(engine, story)
        assert await rs.revision(engine, story) == base + 1
        applied = await rs.apply(client, story, session["id"], base)
        assert applied.status_code == 200, applied.text
        assert applied.json()["session"]["stale_count"] == 0


@pytest.mark.part("M7", "FR-CONC-7; FR-REWRITE-4")
async def test_s_stale_preview__blast_radius(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        deps = await rs.story(client, engine)
        story, ed = deps.story_id, deps.ed
        stub = epistrel.models.stub
        stub.when("", rs.UCLA, purpose="rewrite_parse")
        stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")

        # 2. A turn touches the blast radius: 409 stale, answers carried, nothing written.
        session = await _answered(client, story)
        base = session["base_revision"]
        answered = {
            q["id"]: q["answer_option_id"] for q in session["questions"] if q["answered_at"]
        }
        await _enrollment(engine, ed)
        stale = await rs.apply(client, story, session["id"], base)
        assert stale.status_code == 409, stale.text
        error = stale.json()["error"]
        assert error["code"] == "rewrite_stale"
        details = error["details"]
        assert "proposition_changed" in {r["kind"] for r in details["reasons"]}
        assert details["base_revision"] == base + 1
        assert details["stale_count"] == 1
        assert details["preview"]["base_revision"] == base + 1
        carried = {a["question_id"]: a.get("option_id") for a in details["carried_answers"]}
        assert {k: v for k, v in carried.items() if k in answered} == {
            k: v for k, v in answered.items() if k in carried
        }
        assert set(carried) & set(answered)
        assert await rs.count(engine, "amendments", story) == 0
        again = await client.get(
            f"/stories/{story}/rewrites/{session['id']}", params=rs.OMNISCIENT, headers=READ
        )
        now = again.json()
        assert now["base_revision"] == base + 1
        assert now["last_error"]["code"] == "rewrite_stale"
        if now["state"] == "awaiting_answers":
            answered_again = await rs.answer_all(client, story, now)
            assert answered_again.status_code == 200, answered_again.text

        # 4. Another base than the session's preview: base_mismatch, nothing recomputed.
        mismatch = await rs.apply(client, story, session["id"], base)
        assert mismatch.status_code == 409, mismatch.text
        assert mismatch.json()["error"]["details"]["reason"] == "base_mismatch"
        assert mismatch.json()["error"]["details"]["stale_count"] == 1

        applied = await rs.apply(client, story, session["id"], base + 1)
        assert applied.status_code == 200, applied.text
        assert await rs.count(engine, "amendments", story) == 1


@pytest.mark.part("M7", "FR-CONC-7; FR-REWRITE-4")
async def test_s_stale_preview__mid_qa(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """3. The change lands mid-Q&A: every answer is accepted; the answer after it re-plans, so
    the final commit applies."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        deps = await rs.story(client, engine)
        story, ed = deps.story_id, deps.ed
        stub = epistrel.models.stub
        stub.when("", rs.UCLA, purpose="rewrite_parse")
        stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
        opened = (await rs.open_session(client, story)).json()
        base = opened["base_revision"]
        first = next(q for q in opened["questions"] if q["required"])
        one = await client.post(
            f"/stories/{story}/rewrites/{opened['id']}/answers",
            params=rs.OMNISCIENT,
            json={"answers": [{"question_id": first["id"], "accept_recommended": True}]},
            headers=WRITE,
        )
        assert one.status_code == 200, one.text
        await _enrollment(engine, ed)
        rest_answered = await rs.answer_all(client, story, one.json())
        assert rest_answered.status_code == 200, rest_answered.text
        session = rest_answered.json()
        assert session["state"] == "awaiting_approval"
        # The answer after the change re-planned at the current revision (#223's answer
        # semantics): no stale check ran at answer time, and the plan the author approves is
        # the one computed after the change, so the final commit finds it fresh.
        assert session["base_revision"] == base + 1
        applied = await rs.apply(client, story, session["id"], session["base_revision"])
        assert applied.status_code == 200, applied.text
        assert applied.json()["session"]["stale_count"] == 0


@pytest.mark.part("M7", "FR-CONC-7; FR-REWRITE-4")
async def test_s_stale_preview__states(engine: AsyncEngine, states_delivered: None) -> None:
    """5. ``state_changed``: a State the plan writes, changed since the base, with no
    ``proposition_changes`` row; complement: an unrelated key leaves the plan fresh."""
    w = await c.world(engine)
    story, bobby, ann = w.story_id, w.characters["Bobby"], w.characters["Ann"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    (purse,) = await ss.define_all(engine, story, ss.gold(name="purse"))
    base = await rs.revision(engine, story)
    async with engine.connect() as conn:
        read_set = {
            "entity_ids": [bobby],
            **await revalidate.snapshot(
                conn,
                story,
                {"entity_ids": [bobby]},
                [{"kind": "state", "state_id": str(purse.id), "scope_key": bobby}],
            ),
        }
    assert read_set["state_keys"] == [[str(purse.id), bobby]]

    async def replan(_: Any) -> None:
        return None

    async def outcome() -> revalidate.Outcome:
        async with engine.connect() as conn:
            return await revalidate.revalidate(
                conn,
                story,
                base_revision=base,
                current_revision=await rs.revision(engine, story),
                read_set=read_set,
                replan=replan,
            )

    await ss.change(engine, story, ss.delta("purse", 3, scope_key=ann))
    assert isinstance(await outcome(), revalidate.Fresh)
    await ss.change(engine, story, ss.delta("purse", 5, scope_key=bobby))
    found = await outcome()
    assert isinstance(found, revalidate.Stale)
    assert [r["kind"] for r in found.reasons] == ["state_changed"]
    assert found.reasons[0]["refs"][0]["table"] == "state_changes"


@pytest.mark.part("M7", "FR-CONC-7; FR-REWRITE-4")
async def test_s_stale_preview__ownership(engine: AsyncEngine, states_delivered: None) -> None:
    """#697: ``ownership_changed``: the State the plan writes becomes game-owned between the
    preview and the apply (no revision moves); complement: unchanged ownership is fresh."""
    from sqlalchemy import text

    w = await c.world(engine)
    story, bobby = w.story_id, w.characters["Bobby"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    (purse,) = await ss.define_all(engine, story, ss.gold(name="purse"))
    base = await rs.revision(engine, story)
    async with engine.connect() as conn:
        read_set = {
            "entity_ids": [bobby],
            **await revalidate.snapshot(
                conn,
                story,
                {"entity_ids": [bobby]},
                [{"kind": "state", "state_id": str(purse.id), "scope_key": bobby}],
            ),
        }
    assert read_set["ownership"] == {f"state:{purse.id}": ["story", None, None]}

    async def replan(_: Any) -> None:
        return None

    async def outcome() -> revalidate.Outcome:
        async with engine.connect() as conn:
            return await revalidate.revalidate(
                conn,
                story,
                base_revision=base,
                current_revision=await rs.revision(engine, story),
                read_set=read_set,
                replan=replan,
            )

    assert isinstance(await outcome(), revalidate.Fresh)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE state_definitions SET owner = 'external', provider_id = 'a-bank', "
                "external_value_name = 'purse' WHERE id = :i"
            ),
            {"i": str(purse.id)},
        )
    found = await outcome()
    assert isinstance(found, revalidate.Stale)
    assert [r["kind"] for r in found.reasons] == ["ownership_changed"]
    assert found.reasons[0]["refs"] == [{"table": "state_definitions", "id": str(purse.id)}]
