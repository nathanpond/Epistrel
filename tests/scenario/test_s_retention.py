"""S-RETENTION, M2 part: retention defaults (docs/04 §7; FR-STORE-30; #71).

Discarded and dormant continuations are story history and persist until the story is deleted.
Audit records (``audit`` events and ``audit_log`` rows) expire after the story's
``audit_retention_days`` (the deployment's 90 by default), deleted by the ``retention_sweep``
under the events trigger's ``epistrel.allow_audit_expiry`` exemption. Aged audit records are
written with a past ``tx_time``/``at``: the log is append-only, so a test cannot age them.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.orchestrator.jobs.handlers import retention
from tests.event_support import new_story, play_turn, rows, scalar, sqlstate
from tests.projection_support import truncate

pytestmark = pytest.mark.integration

REFUSED = "EPI01"


async def _aged_event(engine: AsyncEngine, story: str, operation_id: str, days: int) -> str:
    """An ``audit`` event of ``operation_id`` recorded ``days`` days ago."""
    event_id = new_ulid()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO events (id, story_id, seq, operation_id, category, principal, type, "
                "payload, valid_time, tx_time, provenance, authority) "
                "SELECT :id, :s, coalesce(max(seq), 0) + 1, :o, 'audit', 'p-test', "
                "'guardrail_output_rejected', CAST(:payload AS jsonb), "
                "now() - make_interval(days => :d), now() - make_interval(days => :d), "
                "'engine', 'authoritative' FROM events WHERE story_id = :s"
            ),
            {
                "id": event_id,
                "s": story,
                "o": operation_id,
                "d": days,
                "payload": '{"text": "aged", "reason": "test"}',
            },
        )
    return event_id


async def _aged_audit_log(engine: AsyncEngine, story: str | None, days: int) -> str:
    row_id = new_ulid()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_log (id, at, story_id, principal, kind) "
                "VALUES (:id, now() - make_interval(days => :d), :s, 'p-test', 'story_deleted')"
            ),
            {"id": row_id, "d": days, "s": story},
        )
    return row_id


async def _story_state(engine: AsyncEngine, *stories: str) -> dict[str, Any]:
    """Every non-audit event and every operation (with its membership) of ``stories``."""
    found = {
        "events": await rows(
            engine,
            "SELECT id, seq, type, payload::text FROM events "
            "WHERE story_id = ANY(:s) AND category <> 'audit' ORDER BY story_id, seq",
            s=list(stories),
        ),
        "operations": await rows(
            engine,
            "SELECT operation_id, kind::text, narrative_turn, turn_variant, membership::text, "
            "status::text FROM operations WHERE story_id = ANY(:s) ORDER BY operation_id",
            s=list(stories),
        ),
    }
    return {k: [tuple(r) for r in v] for k, v in found.items()}


async def _ids(engine: AsyncEngine, table: str, ids: list[str]) -> set[str]:
    found = await rows(engine, f"SELECT id FROM {table} WHERE id = ANY(:i)", i=ids)  # noqa: S608
    return {r.id for r in found}


@pytest.mark.part("M2", "FR-STORE-30")
async def test_s_retention(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A story with a dormant continuation (a rewound tail played over) and a discarded tail,
    and audit records of several ages. The sweep deletes the audit records past retention: 91
    days under the default 90, 31 days in a story whose own retention is 30, and the matching
    ``audit_log`` rows. Every non-audit event and every operation, discarded and dormant ones
    included, is unchanged. Complements: an 89-day-old audit record **remains** (and a 31-day
    one under the default), a fresh one remains, and the same sweep without the trigger's
    exemption is refused and deletes nothing."""
    story = await new_story(engine)
    played = [await play_turn(engine, story, f"line {n}") for n in (1, 2, 3, 4)]
    fresh = await play_turn(engine, story, "line 5", rejected="a fresh rejected draft")
    assert await truncate(engine, story, 2) is not None  # 3..5 discarded
    await play_turn(engine, story, "new 3")  # the old 3..5 become a dormant continuation
    await play_turn(engine, story, "new 4")
    assert await truncate(engine, story, 3) is not None  # the new 4 is a discarded tail
    memberships = {
        r.membership
        for r in await rows(
            engine,
            "SELECT membership::text AS membership FROM operations WHERE story_id = :s",
            s=story,
        )
    }
    assert {"active", "dormant", "discarded"} <= memberships

    short = await new_story(engine)
    short_turn = await play_turn(engine, short, "hello")
    await _execute(
        engine,
        "UPDATE stories SET settings = settings || '{\"audit_retention_days\": 30}'::jsonb "
        "WHERE id = :s",
        s=short,
    )

    anchor = played[0].operation_id
    expire = [
        await _aged_event(engine, story, anchor, 91),
        await _aged_event(engine, short, short_turn.operation_id, 31),
    ]
    keep = [
        await _aged_event(engine, story, anchor, 89),
        await _aged_event(engine, story, anchor, 31),
        await _aged_event(engine, short, short_turn.operation_id, 29),
    ]
    fresh_audit = {
        r.id
        for r in await rows(
            engine,
            "SELECT id FROM events WHERE operation_id = :o AND category = 'audit'",
            o=fresh.operation_id,
        )
    }
    assert len(fresh_audit) == 1
    log_expire = [
        await _aged_audit_log(engine, story, 91),
        await _aged_audit_log(engine, short, 31),
    ]
    log_keep = [await _aged_audit_log(engine, story, 89), await _aged_audit_log(engine, None, 89)]
    before = await _story_state(engine, story, short)

    # Complement: without the exemption (the trigger as it stands for any other delete), the
    # sweep is refused and nothing goes.
    async def no_exemption(conn: Any) -> None:
        return None

    monkeypatch.setattr(retention, "_allow_audit_expiry", no_exemption)
    with pytest.raises(DBAPIError) as refused:
        await retention.sweep_retention(engine)
    assert sqlstate(refused.value) == REFUSED
    assert await _ids(engine, "events", expire) == set(expire)
    monkeypatch.undo()

    swept = await retention.sweep_retention(engine)
    assert swept["audit_events"] >= 2
    assert swept["audit_log"] >= 2
    assert await _ids(engine, "events", expire) == set()
    assert await _ids(engine, "events", keep) == set(keep)
    assert await _ids(engine, "events", list(fresh_audit)) == fresh_audit
    assert await _ids(engine, "audit_log", log_expire) == set()
    assert await _ids(engine, "audit_log", log_keep) == set(log_keep)
    assert await _story_state(engine, story, short) == before
    discarded = await scalar(
        engine,
        "SELECT count(*) FROM operations WHERE story_id = :s AND membership <> 'active'",
        s=story,
    )
    assert discarded >= 4

    # Run again: nothing more is past retention.
    again = await retention.sweep_retention(engine)
    assert (again["audit_events"], again["audit_log"]) == (0, 0)

    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('epistrel.allow_audit_expiry', 'on', true)"))
        await conn.execute(text("DELETE FROM audit_log WHERE id = ANY(:i)"), {"i": log_keep})


async def _execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)
