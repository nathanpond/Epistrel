"""Every hot ``memories`` statement stays inside its story's partition (#866; ADR-187).

With ``memories`` LIST-partitioned by story (#784), a statement that does not name its story
plans over, and locks, every partition: past about a thousand stories on default settings it
fails with ``out of shared memory`` (ADR-188). The hot paths name it. "Hot" means the
statements issued during a turn, a retrieval, a retirement run, the reconcile and a re-embed
job, and this capture is the registry of them:

- ``test_hot_statements_stay_in_the_story_s_partition``: on a database of four stories, every
  statement that touches ``memories`` during a structured turn (REST), an ``Engine.retrieve``, a
  retirement run, the reconcile's story-less fan-out and a story's reconcile, and a re-embed
  job is captured (a ``before_cursor_execute`` listener), ``EXPLAIN``ed with its own
  parameters, and its plan names no other story's partition. A statement on ADR-187's
  exemption list may (``EXEMPT``); none of them is hot, so the capture meets none.
- ``test_connections_never_use_a_generic_plan``: the engine's connections start with
  ``plan_cache_mode = force_custom_plan``. A generic plan of a hot statement reaches every
  partition (``EXPLAIN (GENERIC_PLAN)``), and psycopg prepares a statement it has run five
  times, so without the setting a prepared statement could be planned generically. A hot
  statement run ten times on a pooled connection was planned custom every time.
- ``test_the_exemptions_are_adr_187_s``: every unpruned statement of the exempt paths matches
  an ``EXEMPT`` pattern, and every pattern is met.
- ``test_story_named_paths_stay_in_the_story_s_partition``: the paths #784's fix made name
  their story (prune, erasure, fork, an amendment's record part, the rewrite's text read and
  ``core.mutation``'s re-reads), under the same check.

These run on a database of their own (``embedding_support.embedding_db``): the first embedding
pins the deployment's dimension.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
import yaml
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.routes.admin import ADMIN_PREFIX
from epistrel.core import clock, embeddings, input_versions
from epistrel.core import fork as forking
from epistrel.core.amendments.store import RecordPartDraft
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.ids import new_ulid
from epistrel.core.input_versions import input_key
from epistrel.core.memory import story_partitions
from epistrel.core.tables.memories import memories
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import reembed
from epistrel.orchestrator.jobs.handlers.reembed import EMBED, REEMBED
from epistrel.orchestrator.jobs.handlers.retirement import RECONCILE
from epistrel.orchestrator.jobs.recurring import NoPayload
from epistrel.orchestrator.jobs.types import ClaimedJob
from epistrel.orchestrator.rewrite import apply as rewrite_apply
from epistrel.testing import postgres
from tests import amendment_support as am
from tests import erasure_support as e
from tests import hot_set_support as h
from tests.auth_support import auth_settings
from tests.conftest import REPO_ROOT, alembic_config
from tests.embedding_support import Scratch, embedding_db, reset, run_jobs
from tests.respond_support import body, respond
from tests.story_support import WRITE
from tests.structured_turn_support import beat, render, structured_story
from tests.summary_support import LEASE_S, worker

pytestmark = pytest.mark.integration

TOUCHES = re.compile(r"\bmemories\b", re.IGNORECASE)
EXEMPT: dict[str, re.Pattern[str]] = {
    # ADR-187's exemption list (#866): statements that read or write every story's memories on
    # purpose. None is hot.
    "embeddings.change_model (deployment-wide UPDATE)": re.compile(
        r"^UPDATE memories SET embedding=%\(embedding\)s, embedding_model_version=\S+ "
        r"WHERE memories\.embedding IS NOT NULL"
        r"( AND \(memories\.membership != \S+ OR memories\.retrieval_tier != \S+\))?$"
    ),
    "embeddings.change_model (story ordering)": re.compile(
        r"FROM stories LEFT OUTER JOIN memories ON memories\.story_id = stories\.id", re.DOTALL
    ),
    "embeddings.retype": re.compile(
        r"^(UPDATE memories SET embedding = NULL, embedding_model_version = NULL WHERE "
        r"embedding IS NOT NULL|ALTER TABLE memories|CREATE INDEX \S+ ON memories)"
    ),
    "reembed.status (page subqueries)": re.compile(r"WITH latest AS .*FROM memories m", re.DOTALL),
    "embeddings input version (legacy memories:<id> key)": re.compile(
        r"md5\(memories\.text\) AS digest\s+FROM memories\s+"
        r"WHERE memories\.id (= \S+|IN \(.+\))\s*$"
    ),
}
"""Each exemption by a pattern of its statement (``test_the_exemptions_are_adr_187_s`` checks the
patterns against the statements themselves). The operator lookups of one memory by id
(``amendments/store.py``, ``orchestrator/rewrite/apply.py``, ``core/mutation.py``) and the
reconcile's fan-out name their story since #784's fix, so none is exempt
(``test_story_named_paths_stay_in_the_story_s_partition``,
``test_hot_statements_stay_in_the_story_s_partition``)."""

OWN_PARTITION = re.compile(
    r"^(CREATE TABLE \"?memories_p_\w+\"? \(LIKE memories INCLUDING ALL\)"
    r"|ALTER TABLE memories ATTACH PARTITION \"?memories_p_\w+"
    r"|CREATE TRIGGER memories_no_truncate BEFORE TRUNCATE ON \"?memories_p_\w+"
    r"|ALTER TABLE ONLY memories ALTER COLUMN story_id SET STATISTICS -?\d+$)"
)
"""A new story's partition being made and settled (``story_partitions.create_partition`` and
``settle``, on a fork): DDL on that partition alone, or a no-op on the parent with ``ONLY``.
It reads no other partition (``audit`` also checks that it names none)."""


@dataclass
class Captured:
    statements: list[tuple[str, Any]] = field(default_factory=list)

    def touching(self) -> list[tuple[str, Any]]:
        return [(sql, params) for sql, params in self.statements if TOUCHES.search(sql)]


@contextmanager
def capture(db: AsyncEngine) -> Iterator[Captured]:
    """Every statement ``db`` runs while the block runs (an ``executemany``'s first row)."""
    seen = Captured()

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        del conn, cursor, context
        if many and isinstance(parameters, Sequence) and parameters:
            parameters = parameters[0]
        seen.statements.append((statement, parameters))

    event.listen(db.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", record)


async def explain(db: AsyncEngine, sql: str, params: Any, *, generic: bool = False) -> str:
    """The plan of ``sql`` with its parameters bound (``EXPLAIN``, nothing run)."""
    async with db.connect() as conn:
        raw = await conn.get_raw_connection()
        cursor = raw.driver_connection.cursor()  # type: ignore[union-attr]
        if "pg_temp.epistrel_fork" in sql:
            # A fork's statements read its id map and functions, made in its transaction.
            for setup in forking._SETUP:
                await cursor.execute(setup)
        if generic:
            await cursor.execute(
                "EXPLAIN (GENERIC_PLAN) " + re.sub(r"%\((\w+)\)s", "$1", sql, count=1)
            )
        else:
            await cursor.execute("EXPLAIN " + sql, params)
        found = await cursor.fetchall()
        await conn.rollback()
    return "\n".join(str(r[0]) for r in found)


def exemption(sql: str) -> str | None:
    return next((name for name, pattern in EXEMPT.items() if pattern.search(sql)), None)


def others_in(plan: str, others: Sequence[str]) -> list[str]:
    """The partitions of ``others`` the plan names (a partition's index names start with its
    table name and an underscore, so a whole-word match is the table)."""
    return [p for p in others if re.search(rf"\b{re.escape(p)}\b", plan)]


async def audit(
    db: AsyncEngine, seen: Captured, story_id: str | None, stories: Sequence[str], path: str
) -> list[str]:
    """Each captured ``memories`` statement whose plan leaves ``story_id``'s partition (and is
    not exempt), described. With ``story_id`` ``None`` (a fan-out over the stories, one story a
    statement), each plan may name one partition of ``stories`` at most."""
    others = [story_partitions.partition_name(s) for s in stories if s != story_id]
    bad: list[str] = []
    for sql, params in seen.touching():
        verb = sql.lstrip().split(None, 1)[0].upper()
        if OWN_PARTITION.search(sql) and not others_in(sql, others):
            continue
        if verb not in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE"}:
            bad.append(f"[{path}] not explainable: {sql[:300]}")
            continue
        plan = await explain(db, sql, params)
        leaked = others_in(plan, others)
        if story_id is None and len(leaked) <= 1:
            continue
        if leaked and exemption(sql) is None:
            bad.append(f"[{path}] {len(leaked)} other partition(s): {sql[:400]}\n{plan[:600]}")
    return bad


# -- the database -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def scratch(pg_url: str) -> AsyncIterator[Scratch]:
    async with embedding_db(pg_url) as s:
        yield s


@pytest.fixture
async def s(scratch: Scratch) -> Scratch:
    await reset(scratch.db)
    scratch.lib.models.embeddings.stub.reset()
    scratch.lib.models.stub.reset()
    return scratch


async def embedding_ids(db: AsyncEngine, story_id: str) -> list[str]:
    async with db.connect() as conn:
        found = await conn.scalars(
            text("SELECT id FROM memories WHERE story_id = :s ORDER BY id"), {"s": story_id}
        )
        return [str(i) for i in found]


@dataclass
class Played:
    story_id: str
    player: str
    other: str


async def _played(s: Scratch, lines: Sequence[str]) -> Played:
    """A structured story with a turn per line, its memories embedded."""
    story, ids = await structured_story(s.client, "Wren", "Ada")
    wren = ids["Wren"]
    for line in lines:
        await _turn(s, story, wren, line)
    await run_jobs(s.db, EMBED, story)
    return Played(story, wren, ids["Ada"])


async def _turn(s: Scratch, story: str, actor: str, line: str) -> None:
    s.lib.models.stub.script("generation", [render(beat("say", f"Ada hears: {line}"))])
    response = await respond(s.client, story, body(actor, line))
    assert response.status_code == 200, response.text


Path = Callable[[], Awaitable[Any]]

_CLAIM_STORYLESS = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:test:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = :kind AND story_id IS NULL "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def _run_storyless(db: AsyncEngine, kind: str) -> list[Any]:
    """Run the due story-less ``kind`` job (a recurring run) through the worker's
    ``execute``, as ``embedding_support.run_jobs`` runs a story's."""
    now = clock.now()
    async with db.begin() as conn:
        row = (
            await conn.execute(
                _CLAIM_STORYLESS,
                {"kind": kind, "now": now, "lease": now + timedelta(seconds=LEASE_S)},
            )
        ).one()
    job = ClaimedJob(
        id=row.id,
        kind=row.kind,
        story_id=None,
        payload=dict(row.payload or {}),
        attempt=int(row.attempt),
        failures=int(row.failures),
        due_at=row.due_at,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        coalesce_key=row.coalesce_key,
    )
    return [await worker(db).execute(job)]


async def test_hot_statements_stay_in_the_story_s_partition(s: Scratch) -> None:
    """A turn, a retrieval, a retirement run with its reconcile, and a re-embed job: every
    statement touching ``memories`` plans inside its story's partition, with four stories in
    the database.

    Complement asserted: the capture saw the paths' statements (each path touched ``memories``
    at least once, and the turn and the retrieval read vectors), so a pass is not that of an
    empty capture; and a statement naming no story would be refused (an unpruned read of the
    target story's own rows plans over every partition)."""
    db = s.db
    first = await _played(s, ["The lamp is lit.", "Where is the key?"])
    second = await _played(s, ["Rain on the roof.", "The lamp is lit."])
    third = await _played(s, ["The lamp is lit."])
    w = await h.story(db, memories_per_character=5, importance_floor=0.5)
    retiring, bobby = w.story_id, w.characters["Bobby"]
    await h.held(db, retiring, bobby, 3, importance=0.9)
    await run_jobs(db, EMBED, retiring)
    stories = [first.story_id, second.story_id, third.story_id, retiring]
    failures: list[str] = []
    counts: dict[str, int] = {}

    outcomes: dict[str, Any] = {}

    async def check(path: str, story: str, run: Path) -> Captured:
        with capture(db) as seen:
            outcomes[path] = await run()
        counts[path] = len(seen.touching())
        failures.extend(await audit(db, seen, story or None, stories, path))
        return seen

    turn = await check(
        "turn", first.story_id, lambda: _turn(s, first.story_id, first.player, "Is the lamp lit?")
    )
    ctx = RequestContext(principal="p-test")
    retrieval = await check(
        "retrieval",
        first.story_id,
        lambda: s.lib.retrieve(ctx, first.story_id, q="the lamp", viewpoint="omniscient"),
    )

    await h.held(db, retiring, bobby, 6, importance=0.1, label="dim")
    assert await h.queued(db, h.RETIREMENT, retiring)
    await check("retirement", retiring, lambda: h.run(db, h.RETIREMENT, retiring))
    # The daily reconcile: its story-less run fans out (``stories_with_memories``, then
    # ``needs_reconcile`` in each story's partition) and queues each story's reconcile.
    async with db.begin() as conn:
        await queue.enqueue(conn, RECONCILE, NoPayload())
    await check("reconcile fan-out", "", lambda: _run_storyless(db, RECONCILE))
    await check("reconcile", retiring, lambda: h.run(db, RECONCILE, retiring))

    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE memories SET embedding_model_version = 'old@0' WHERE story_id = :s"),
            {"s": first.story_id},
        )
        await reembed.schedule(conn, REEMBED, first.story_id)
    await check("reembed", first.story_id, lambda: run_jobs(db, REEMBED, first.story_id))

    assert not failures, "\n\n".join(failures)
    assert all(counts[p] > 0 for p in counts), counts
    for path in ("retirement", "reconcile fan-out", "reconcile", "reembed"):
        assert [str(o) for o in outcomes[path]] == ["committed"], (path, outcomes[path])
    for story in stories:
        assert await h.queued(db, RECONCILE, story) or story == retiring, story
    tiers = [r.retrieval_tier for r in (await h.memory_rows(db, retiring)).values()]
    assert tiers.count("retired") >= 4, tiers
    assert any("<=>" in sql for sql, _ in turn.touching()), "the turn's vector arm did not run"
    assert any("<=>" in sql for sql, _ in retrieval.touching()), "no vector arm in the retrieval"
    unpruned = await explain(
        db,
        "SELECT id FROM memories WHERE id = %(i)s",
        {"i": "nothing"},
    )
    assert len(others_in(unpruned, [story_partitions.partition_name(x) for x in stories])) == 4


async def test_connections_never_use_a_generic_plan(s: Scratch) -> None:
    """A hot statement (the vector-less embed read, ``pending_memories``' shape) run ten times on
    one pooled connection, prepared by psycopg from the sixth run, was planned with its
    parameters every time: ``pg_prepared_statements`` counts no generic plan. The connection's
    ``plan_cache_mode`` is ``force_custom_plan`` from its start.

    Complement asserted: the statement's generic plan (``EXPLAIN (GENERIC_PLAN)``) names every
    story's partition, so a generic plan is what the setting keeps away."""
    stories = [(await _played(s, ["The lamp is lit."])).story_id for _ in range(3)]
    sql = "SELECT id FROM memories WHERE story_id = %(s)s AND embedding IS NULL"
    async with s.db.connect() as conn:
        mode = await conn.scalar(text("SHOW plan_cache_mode"))
        raw = await conn.get_raw_connection()
        cursor = raw.driver_connection.cursor()  # type: ignore[union-attr]
        for _ in range(10):
            await cursor.execute(sql, {"s": stories[0]}, prepare=True)
            await cursor.fetchall()
        await cursor.execute(
            "SELECT generic_plans, custom_plans FROM pg_prepared_statements "
            "WHERE statement LIKE '%%FROM memories WHERE story_id%%'"
        )
        plans = await cursor.fetchall()
        await conn.rollback()
    assert plans, "the statement was not prepared"
    assert all(int(g) == 0 and int(c) >= 1 for g, c in plans), plans
    assert mode == "force_custom_plan"
    generic = await explain(s.db, sql, None, generic=True)
    partitions = [story_partitions.partition_name(x) for x in stories]
    assert others_in(generic, partitions) == partitions, generic


async def test_the_exemptions_are_adr_187_s(s: Scratch) -> None:
    """The exempt statements, run for real (a dimension change, rolled back; the re-embed
    status page; a legacy ``memories:<id>`` input read): each of their
    ``memories`` statements that plans over other partitions, or is DDL, matches an
    ``EXEMPT`` pattern, and every pattern matched one.

    Complement asserted: the same input read under the new key, ``memories:<story_id>:<id>``,
    plans inside the story's partition and answers the same versions."""
    stories = [(await _played(s, ["The lamp is lit.", "Rain."])).story_id for _ in range(3)]
    db = s.db
    ids = list(await embedding_ids(db, stories[0]))
    legacy = [input_key(memories.name, i) for i in ids]
    keyed = [embeddings.memory_input(stories[0], i) for i in ids]
    with capture(db) as seen:
        async with db.connect() as conn:
            async with conn.begin() as tx:
                revision = await conn.scalar(text("SELECT config_revision FROM deployment_state"))
                await embeddings.change_model(
                    conn,
                    version="other@2",
                    dimension=16,
                    config_revision=int(revision) + 1,
                    actor="p-test",
                )
                for story in stories:
                    await reembed.schedule(conn, REEMBED, story)
                assert (await reembed.status(conn)).stories
                await tx.rollback()
            old = await input_versions.read_versions(conn, legacy)
            await input_versions.read_version(conn, legacy[0])
            await conn.rollback()
    with capture(db) as new_seen:
        async with db.connect() as conn:
            new = await input_versions.read_versions(conn, keyed)
            assert await input_versions.read_version(conn, keyed[0]) == old[legacy[0]]
    assert list(new.values()) == list(old.values())
    assert all(v is not None for v in old.values())

    every = [story_partitions.partition_name(x) for x in stories]
    hit: set[str] = set()
    unexempt: list[str] = []
    for sql, params in seen.touching():
        verb = sql.lstrip().split(None, 1)[0].upper()
        wide = (
            verb not in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE"}
            or len(others_in(await explain(db, sql, params), every)) > 1
        )
        if not wide:
            continue
        name = exemption(sql)
        if name is None:
            unexempt.append(sql[:300])
        else:
            hit.add(name)
    assert unexempt == [], "\n".join(unexempt)
    assert hit == set(EXEMPT), set(EXEMPT) - hit
    assert await audit(db, new_seen, stories[0], stories, "keyed input") == []


async def test_story_named_paths_stay_in_the_story_s_partition(s: Scratch) -> None:
    """The statements #784's fix (471cf06, ADR-188) made name their story, which #866's plan
    listed as fixes to make (prune's ``operation_id IN`` and chunk delete, the erasure's memory
    deletes and updates, a fork's ``amendment_parts`` subquery) or as exempt operator lookups
    (an amendment's record part, ``rewrite.apply._text_of``, ``core.mutation``'s re-reads after
    a quarantine and a sanitize): each one, run for real on a database of three stories, plans
    inside its story's partition. A fork also writes its new story's partition, which is not
    one of the three.

    Complement asserted: each path touched ``memories`` (a pass is not that of an empty
    capture), and each operation did what it was asked."""
    db = s.db
    erased, amended, pruned = [
        await _played(s, ["The lamp is lit.", "Where is the key?"]) for _ in range(3)
    ]
    stories = [erased.story_id, amended.story_id, pruned.story_id]
    failures: list[str] = []
    counts: dict[str, int] = {}
    shapes: dict[str, list[str]] = {}

    async def check(path: str, story: str, run: Path) -> Any:
        with capture(db) as seen:
            found = await run()
        counts[path] = len(seen.touching())
        shapes[path] = [sql for sql, _ in seen.touching()]
        failures.extend(await audit(db, seen, story, stories, path))
        return found

    target, *_ = await embedding_ids(db, amended.story_id)
    await check(
        "amendment record part",
        amended.story_id,
        lambda: am.amend(
            db,
            amended.story_id,
            [RecordPartDraft("memories", target, "replace", {"text": "Corrected."})],
        ),
    )
    forked = await check(
        "fork",
        amended.story_id,
        lambda: s.client.post(
            f"/stories/{amended.story_id}/fork",
            json={"at_turn": 2, "name": "A fork"},
            headers=WRITE,
        ),
    )
    assert forked.status_code in (200, 201), forked.text

    first, *_ = await embedding_ids(db, erased.story_id)
    admin = f"{ADMIN_PREFIX}/stories/{erased.story_id}/memories/{first}"
    quarantined = await check(
        "quarantine",
        erased.story_id,
        lambda: s.client.post(f"{admin}/quarantine", json={"reason": "A test."}, headers=WRITE),
    )
    assert quarantined.status_code == 200, quarantined.text
    released = await s.client.delete(f"{admin}/quarantine", headers=WRITE)
    assert released.status_code == 200, released.text
    sanitized = await check(
        "sanitize",
        erased.story_id,
        lambda: s.client.post(
            f"{admin}/sanitize", json={"span": "lamp", "surrogate": "light"}, headers=WRITE
        ),
    )
    assert sanitized.status_code == 200, sanitized.text

    async def text_of() -> str | None:
        async with db.connect() as conn:
            return await rewrite_apply._text_of(conn, erased.story_id, "memories", first)

    assert await check("rewrite text", erased.story_id, text_of) is not None

    truncated = await s.client.post(
        f"/stories/{pruned.story_id}/truncate", json={"to_turn": 1}, headers=WRITE
    )
    assert truncated.status_code == 200, truncated.text
    before = len(await embedding_ids(db, pruned.story_id))
    prune_response = await check(
        "prune",
        pruned.story_id,
        lambda: s.client.post(
            f"/stories/{pruned.story_id}/prune", json={"ignore_warnings": True}, headers=WRITE
        ),
    )
    assert prune_response.status_code == 200, prune_response.text
    assert len(await embedding_ids(db, pruned.story_id)) < before

    await check(
        "erasure",
        erased.story_id,
        lambda: e.erase(s.client, "character", erased.other, erased.story_id, confirm_all=True),
    )

    assert not failures, "\n\n".join(failures)
    assert all(n > 0 for n in counts.values()), counts
    # The statements the plan named were among those checked.
    expected = {
        "prune": [r"memories\.operation_id IN", r"^DELETE FROM memories WHERE memories\.id IN"],
        "fork": [r"amendment_parts.*FROM \"memories\" r WHERE r\.story_id = "],
        "erasure": [r"^(DELETE FROM|UPDATE) memories "],
        "amendment record part": [r"^UPDATE memories SET"],
        "quarantine": [r"^SELECT .*FROM memories\s+WHERE memories\.story_id = "],
        "sanitize": [r"^SELECT .*FROM memories\s+WHERE memories\.story_id = "],
    }
    missing = {
        (path, pattern)
        for path, patterns in expected.items()
        for pattern in patterns
        if not any(re.search(pattern, sql, re.DOTALL) for sql in shapes[path])
    }
    assert not missing, (missing, {p: [q[:200] for q in shapes[p]] for p, _ in missing})


# -- the shipped lock table (#784 option (a), ADR-191) ---------------------------------------------

SMALL_SERVER: dict[str, object] = {
    "max_connections": 5,
    "superuser_reserved_connections": 0,
    "autovacuum_max_workers": 1,
    "max_worker_processes": 0,
    "max_parallel_workers": 0,
    "max_wal_senders": 0,
}
"""A server whose lock table is ADR-188's rule scaled down: about ``max_locks_per_transaction``
times 7 slots (5 connections, the autovacuum launcher and its one worker), so a few hundred
partitions stand in for thousands of stories."""
SMALL_CONNECTIONS = 5
SMALL_STORIES = postgres.MAX_LOCKS_PER_TRANSACTION * SMALL_CONNECTIONS // postgres.LOCKS_PER_STORY
"""The stories the shipped value covers on ``SMALL_SERVER`` by ADR-188's rule (320)."""


def test_the_test_server_runs_the_shipped_lock_table(pg_url: str) -> None:
    """The test database runs compose's ``max_locks_per_transaction`` (ADR-191), so the tests
    exercise the shipped configuration. A server named by ``EPISTREL_TEST_DATABASE_URL`` needs
    the setting too."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    command = compose["services"]["db"]["command"]
    shipped = command[command.index("-c") + 1].split("=", 1)
    assert shipped[0] == "max_locks_per_transaction"
    admin = make_sync_engine(pg_url)
    try:
        with admin.connect() as conn:
            running = conn.scalar(text("SHOW max_locks_per_transaction"))
    finally:
        admin.dispose()
    assert running == shipped[1]


async def _restart_with(server: Any, max_locks: int) -> str:
    """``server`` restarted with ``max_locks_per_transaction = max_locks`` (``ALTER SYSTEM``: it
    takes effect only at server start); its URL once it answers (Docker may map a new port)."""
    url = server.get_connection_url()
    admin = make_sync_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"ALTER SYSTEM SET max_locks_per_transaction = {int(max_locks)}"))
    finally:
        admin.dispose()
    await asyncio.to_thread(server.get_wrapped_container().restart)
    await asyncio.to_thread(server._connect)  # testcontainers' own readiness wait (psql)
    url = server.get_connection_url()
    admin = make_sync_engine(url)
    try:
        with admin.connect() as conn:
            assert conn.scalar(text("SHOW max_locks_per_transaction")) == str(max_locks)
    finally:
        admin.dispose()
    return str(url)


async def _retype(url: str) -> str | None:
    """The first embedding pin's retype (FR-MEM-8; ``embeddings.retype``), which locks every
    partition in one transaction, rolled back after it ran. Its error, or None."""
    db = make_engine(auth_settings(url), pool_size=1)
    try:
        async with db.connect() as conn:
            try:
                async with conn.begin() as tx:
                    await embeddings.retype(conn, 8)
                    await tx.rollback()
            except DBAPIError as exc:
                return str(exc.orig)
        return None
    finally:
        await db.dispose()


async def test_a_deployment_wide_retype_needs_the_shipped_lock_table() -> None:
    """ADR-188's rule at small scale (ADR-191): on a ``SMALL_SERVER`` migrated to head, with
    the ``SMALL_STORIES`` story partitions the shipped value covers there, a retype succeeds
    with the shipped ``max_locks_per_transaction`` and fails with ``out of shared memory`` on
    Postgres's default 64. One server is restarted between the two, so the migration (which
    needs more than 64 itself on so few connections) and the partitions are made once. The
    value is set by ``ALTER SYSTEM``, since a ``-c`` flag would override it."""
    assert SMALL_STORIES == 320
    flags = [arg for name, value in SMALL_SERVER.items() for arg in ("-c", f"{name}={value}")]
    server = postgres.container(command=["postgres", *flags])  # the lock table by ALTER SYSTEM
    await asyncio.to_thread(server.start)
    try:
        url = await _restart_with(server, postgres.MAX_LOCKS_PER_TRANSACTION)
        await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
        db = make_engine(auth_settings(url), pool_size=1)
        try:
            for _ in range(SMALL_STORIES):
                async with db.begin() as conn:
                    await story_partitions.create_partition(conn, new_ulid())
        finally:
            await db.dispose()
        assert await _retype(url) is None
        failed = await _retype(await _restart_with(server, 64))
        assert failed is not None
        assert "out of shared memory" in failed
    finally:
        await asyncio.to_thread(server.stop)
