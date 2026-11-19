"""#754 (ADR-199): a commit's epistemic writes are batched (``core.pending``).

- **Equivalence:** the same lazy resolutions committed batched and per belief (``disabled()``)
  leave the same rows: events, beliefs, transitions, subject references, edges, the change index,
  exposures, propositions and both projections, the story's revision. Each variant runs in its own
  transaction over the same story, which is rolled back after the rows are read, so both start
  from one state; the ids each mints are renamed by mint order and timestamps masked.
- **FR-BELIEF-6 through the overlay:** a pass of three exposures on one functional key resolves
  in one batch with no flush in between, so each later frame read the earlier resolutions from
  the pending rows; the equivalence shows it read what the per-belief path read.
- **Complement:** another connection (another commit) never sees a batch's pending rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AsyncExitStack
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import pending
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.epistemic import exposure, partitions
from epistrel.core.events import Operation
from epistrel.core.ids import ULID_LENGTH, new_ulid
from tests import canon_support as c
from tests import exposure_support as xs
from tests import partition_support as parts
from tests.memory_support import use_structured
from tests.profile_support import store_overrides

pytestmark = pytest.mark.integration

PARTITIONED = {
    "character_knowledge": {"partitions": True, "lazy_resolution": True, "testimony": "defaulted"},
    "perception": {"level": "co_location"},
}

DUMPED = {
    "events": (
        "SELECT * FROM events WHERE story_id = :s"
        " AND type IN ('belief_committed', 'belief_transition', 'belief_resolved')"
    ),
    "beliefs": "SELECT * FROM beliefs WHERE story_id = :s",
    "belief_transitions": "SELECT * FROM belief_transitions WHERE story_id = :s",
    "subject_references": (
        "SELECT * FROM subject_references WHERE story_id = :s"
        " AND (record_table IN ('beliefs', 'belief_transitions', 'propositions')"
        " OR (record_table = 'events' AND record_id IN (SELECT id FROM events WHERE story_id = :s"
        " AND type IN ('belief_committed', 'belief_transition', 'belief_resolved'))))"
    ),
    "dependency_edges": "SELECT * FROM dependency_edges WHERE story_id = :s",
    "proposition_changes": "SELECT * FROM proposition_changes WHERE story_id = :s",
    "exposures": "SELECT * FROM exposures WHERE story_id = :s",
    "propositions": "SELECT * FROM propositions WHERE story_id = :s",
    "belief_state": "SELECT * FROM belief_state WHERE story_id = :s",
    "nested_belief_state": "SELECT * FROM nested_belief_state WHERE story_id = :s",
    "stories": "SELECT timeline_revision FROM stories WHERE id = :s",
}
"""Every table a resolution writes (or projects), read whole for the story."""


class _RolledBack(Exception):
    """Raised by a commit step after it read the rows: the transaction rolls back."""


async def _exposed(engine: AsyncEngine) -> tuple[str, str, Any]:
    """Ann, told by Bobby where Cal was three times (one functional key, so each resolution
    contradicts or confirms the one before): the story, Ann and the claim a need names."""
    w = await c.world(engine)
    await use_structured(engine, w.story_id)
    await store_overrides(engine, w.story_id, PARTITIONED)
    ann, bobby, cal = w.characters["Ann"], w.characters["Bobby"], w.people["Cal"]
    for place in ("kitchen", "garden", "school"):
        await xs.expose(
            engine, w.story_id, ann, c.at(cal, w.places[place], None, None), source=bobby
        )
    return w.story_id, ann, c.at(cal, w.places["garden"], None, None)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _normal(value: Any, renamed: Mapping[str, str]) -> Any:
    if isinstance(value, datetime):
        return "<ts>"
    if isinstance(value, str):
        return renamed.get(value, value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {k: _normal(v, renamed) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_normal(v, renamed) for v in value]
    if value is not None and type(value).__name__ == "Range":
        return str(value)
    return value


async def _dump(conn: AsyncConnection, story_id: str, mark: str) -> dict[str, list[str]]:
    """The story's rows, each id minted after ``mark`` renamed by mint order (the order is the
    same on both paths: ``new_ulid`` is monotonic), timestamps masked; each table a sorted list."""
    raw = {
        name: [dict(r._mapping) for r in await conn.execute(text(sql), {"s": story_id})]
        for name, sql in DUMPED.items()
    }
    minted = sorted(
        {
            s
            for rows in raw.values()
            for row in rows
            for s in _strings(list(row.values()))
            if len(s) == ULID_LENGTH and s > mark
        }
    )
    renamed = {s: f"<new {n}>" for n, s in enumerate(minted)}
    return {name: sorted(repr(_normal(row, renamed)) for row in rows) for name, rows in raw.items()}


async def _resolved(
    engine: AsyncEngine, story: str, witness: str, claim: Any, *, batched: bool
) -> tuple[dict[str, list[str]], pending.Pending | None, int]:
    """A turn whose commit resolves ``witness``'s need for ``claim``, batched or per belief; the
    rows it leaves (read in its transaction, then rolled back), its batch and how many it
    resolved."""
    settings = await parts.partitioned(engine, story)
    found: dict[str, Any] = {}
    mark = new_ulid()
    try:
        async with Operation(
            engine, story_id=story, kind=OperationKind.TURN, principal=c.PRINCIPAL
        ) as op:
            op.append("player_input", {"text": "where was Cal?"}, provenance=Provenance.PLAYER)
            op.append("message_rendered", {"text": "Let me think."})

            async def step(conn: AsyncConnection) -> None:
                async with AsyncExitStack() as stack:
                    batch = (
                        await stack.enter_async_context(pending.batching(conn)) if batched else None
                    )
                    budget = exposure.Pass(cap=10)
                    await exposure.resolve_needed(
                        conn, op, witness, [claim], budget=budget, settings=settings
                    )
                    if batch is not None:
                        found["flushes"] = (batch.flushes, batch.guard_flushes)
                        found["held"] = sorted(batch.beliefs)
                        # The complement: another commit reads none of the pending rows.
                        async with engine.connect() as other:
                            assert pending.current(other) is None
                            seen = await partitions.partition(other, story, witness)
                            found["other"] = {b.id for b in seen}
                    found["batch"] = batch
                    found["made"] = len(budget.made)
                found["rows"] = await _dump(conn, story, mark)
                raise _RolledBack

            op.add_commit_step(step)
            await op.commit()
    except _RolledBack:
        pass
    except Exception as exc:  # the operation may wrap the step's exception
        if not isinstance(exc.__cause__ or exc.__context__, _RolledBack):
            raise
    if found["batch"] is not None:
        assert found["flushes"] == (0, 0), found["flushes"]
        assert found["held"]
        assert not set(found["held"]) & found["other"]
    return found["rows"], found["batch"], found["made"]


async def test_a_batched_commit_writes_what_the_per_belief_one_writes(engine: AsyncEngine) -> None:
    """Three exposures on one key, resolved in one pass: batched (no flush until the scope ends,
    so each later frame read the earlier resolutions from the overlay) and per belief, from the
    same state, leave the same rows in every table a resolution writes.

    Complement asserted: the pass resolved all three (each variant), and while the batch held its
    rows another connection's partition read saw none of them."""
    story, ann, claim = await _exposed(engine)
    batched, batch, made = await _resolved(engine, story, ann, claim, batched=True)
    assert batch is not None
    assert made == 3
    with pending.disabled():
        single, none, made_single = await _resolved(engine, story, ann, claim, batched=True)
    assert none is None
    assert made_single == 3
    for table in DUMPED:
        assert batched[table] == single[table], table
    assert len(batched["beliefs"]) > 0
    assert len(batched["belief_transitions"]) > 0


# -- P-04's first multi-character turn ---------------------------------------------------------

RESOLUTION_STATEMENTS_MAX = 20
"""#754's acceptance figure: one lazy resolution's cost in commit statements, measured as the
first turn's commit at the default cap less the same turn's at cap 0, over the resolutions
(about 100 per belief; 16 measured batched, ADR-199)."""

FIRST_TURN_COMMIT_MAX = 600
"""NFR-PERF-3's statement side for the first turn with 20 resolutions (#620's figure, #754's
AC 2; the ≤ 1 s side is the weekly P-04 run's)."""


async def _first_turn(
    url: str, spec: Any, ref: Any, claims: Any, monkeypatch: pytest.MonkeyPatch, *, cap: int
) -> tuple[int, int, dict[str, list[str]]]:
    """The first two-primary turn of the story copy at ``url`` with ``cap`` lazy resolutions:
    its commit's statements, its resolutions and its epistemic rows (``_dump``)."""
    from sqlalchemy import event

    from epistrel.core.db import make_engine
    from tests.auth_support import auth_settings
    from tests.integration.test_turn_read_counts import _in_commit
    from tests.respond_support import body, respond, respond_client

    settings = auth_settings(
        url,
        model_adapter="stub",
        stub_embedding_dimension=spec.dimension,
        stub_render_claims=claims,
    )
    made = [0]
    counted = exposure._counted

    async def counting(conn: Any, resolving: Any) -> Any:
        made[0] += 1
        return await counted(conn, resolving)

    statements = [0]

    def count(*_: Any) -> None:
        if _in_commit():
            statements[0] += 1

    db = make_engine(settings)
    mark = new_ulid()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(exposure, "_counted", counting)
            patch.setattr(exposure, "max_lazy_resolutions", lambda _settings: cap)
            async with respond_client(settings, db) as (client, _):
                event.listen(db.sync_engine, "before_cursor_execute", count)
                try:
                    answer = await respond(
                        client,
                        ref.story_id,
                        body(
                            ref.player_id, "What now?", render_character_ids=list(ref.primary_ids)
                        ),
                    )
                finally:
                    event.remove(db.sync_engine, "before_cursor_execute", count)
        assert answer.status_code == 200, answer.text
        async with db.connect() as conn:
            rows = await _dump(conn, ref.story_id, mark)
    finally:
        await db.dispose()
    return statements[0], made[0], rows


async def test_a_lazy_resolution_costs_at_most_twenty_statements(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-04's first two-primary turn (``ScaleSpec.scaled(1_000)``, the default knowledge layer),
    on three copies of one story: at cap 0, at the default cap of 20 (batched), and at 20 per
    belief (``pending.disabled()``, no statement budget, so the same 20 resolve).

    - AC 1: the turn at 20 less the turn at 0, over its resolutions, is at most
      ``RESOLUTION_STATEMENTS_MAX`` statements (about 100 per belief);
    - AC 2: the turn at 20 commits in at most ``FIRST_TURN_COMMIT_MAX`` statements;
    - AC 3: the batched and per-belief turns leave the same epistemic rows.

    Complement asserted: the turn at 20 resolved exactly 20 and the turn at 0 none, so the
    difference is 20 resolutions' cost; the per-belief turn spends more statements than the
    batched one."""
    from functools import partial

    from perf.lite_smoke import multi
    from sqlalchemy import text as sql

    from epistrel.core.context import RequestContext
    from epistrel.core.db import make_sync_engine
    from epistrel.testing.fixtures import knowledge, scale
    from tests.perf.scale_support import scale_database

    spec = scale.ScaleSpec.scaled(1_000)
    admin = make_sync_engine(pg_url, isolation_level="AUTOCOMMIT")
    copies: list[str] = []
    try:
        async with scale_database(pg_url, spec) as run:
            await knowledge.apply(run.db, spec, run.fixture.story, knowledge.KnowledgeSpec())
            ref = await multi.prepare(run.lib, RequestContext(principal="p"), run.fixture.story, 0)
            claims = multi.render_claims(scale.person_names(spec.characters)[1])
            await run.lib.drainer.stop()
            await run.db.dispose()
            source = run.url.rsplit("/", 1)[1]
            for _ in range(3):
                name = f"{source}_{new_ulid().lower()[-8:]}"
                with admin.connect() as conn:
                    conn.execute(sql(f'CREATE DATABASE "{name}" TEMPLATE "{source}"'))
                copies.append(name)
            base = pg_url.rsplit("/", 1)[0]
            none, none_made, _ = await _first_turn(
                f"{base}/{copies[0]}", spec, ref, claims, monkeypatch, cap=0
            )
            batched, made, rows = await _first_turn(
                f"{base}/{copies[1]}", spec, ref, claims, monkeypatch, cap=20
            )
            with pending.disabled(), monkeypatch.context() as patch:
                patch.setattr(exposure, "Pass", partial(exposure.Pass, statements=10**9))
                single, single_made, single_rows = await _first_turn(
                    f"{base}/{copies[2]}", spec, ref, claims, monkeypatch, cap=20
                )
    finally:
        with admin.connect() as conn:
            for name in copies:
                conn.execute(sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
    print(f"#754 first turn: cap 0 {none}, cap 20 {batched} ({made}), per belief {single}")
    assert (none_made, made, single_made) == (0, 20, 20)
    per_resolution = (batched - none) / made
    assert per_resolution <= RESOLUTION_STATEMENTS_MAX, (none, batched, made)
    assert batched <= FIRST_TURN_COMMIT_MAX, batched
    assert single > batched, (single, batched)
    for table in DUMPED:
        assert rows[table] == single_rows[table], table
