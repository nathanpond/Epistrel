"""The erasure ledger outside the database, its table copy, and the reconciler (FR-ADMIN-7;
#125). The milestone part (restore, block, reapply, verify) is ``test_s_erasure_boundary__restore``
in ``tests/erasure/test_s_erasure_boundary.py``; these are the details around it.

Each test gets a fresh database on a container of the module's own (``tests/ledger_support.py``),
since they delete ledger rows, block stories and compare the whole ledger with a file.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from epistrel import Engine
from epistrel.cli import main
from epistrel.config import ConfigError
from epistrel.core import clock
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.erasure.ledger import GENESIS_HASH, Entry, LedgerFile, canonical, parse
from epistrel.core.errors import ErasurePending
from epistrel.core.ids import new_ulid
from epistrel.engine import ERASURE_GATE_EXEMPT, ERASURE_GATED_ATTRIBUTE
from epistrel.observability import logging as telemetry_logging
from epistrel.orchestrator.jobs import queue as job_queue
from epistrel.tools import backup
from tests import erasure_support as e
from tests import ledger_support as ledger
from tests.respond_support import respond_client
from tests.story_support import WRITE

CTX = RequestContext(principal="p-ledger")


@pytest.fixture(scope="module")
def ledger_server() -> Iterator[ledger.Server]:
    with ledger.dedicated_server() as server:
        yield server


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Epistrel's JSON handler at INFO on a buffer; the root logger restored after."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    stream = io.StringIO()
    handler = telemetry_logging.configure_logging(level="INFO", stream=stream)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.handlers[:] = handlers
        root.setLevel(level)


def _lines(stream: io.StringIO, logger: str) -> list[dict[str, Any]]:
    found = [json.loads(raw) for raw in stream.getvalue().splitlines() if raw.strip()]
    return [line for line in found if line.get("logger") == logger]


async def _erased(url: str, ledger_dir: Path) -> tuple[ledger.Stories, dict[str, Any]]:
    """Two stories, the subject erased from hers, through the app with its lifespan."""
    async with ledger.started(ledger.settings(url, ledger_dir)) as (client, _):
        stories = await ledger.two_stories(client)
        done = await e.erase(client, "character", stories.subject, stories.named)
    return stories, done


async def _without_ledger_rows(url: str) -> None:
    """Delete every ``erasure_ledger`` row past its append-only trigger (the test's own
    superuser session), as a restore of an older backup would leave it."""
    await ledger.sql(
        url,
        "DO $$ BEGIN "
        "ALTER TABLE erasure_ledger DISABLE TRIGGER erasure_ledger_append_only; "
        "DELETE FROM erasure_ledger; "
        "ALTER TABLE erasure_ledger ENABLE TRIGGER erasure_ledger_append_only; END $$",
    )


# -- the file ---------------------------------------------------------------------------------


def test_the_file_is_hash_chained(tmp_path: Path) -> None:
    """Each line's ``ledger_hash`` is the SHA-256 of its canonical JSON without it, chained from
    64 zeros; the line carries the AC's fields. A changed, removed or reordered line, or a
    malformed one, is ``LedgerCorrupt`` with its line number."""
    file = LedgerFile(tmp_path)
    file.prepare()
    entries = [
        Entry(
            erasure_id=new_ulid(),
            at=clock.now(),
            subject_kind="character",
            subject_id=new_ulid(),
            scope="story",
            story_id=new_ulid(),
            principal="p-op",
            operation_id=new_ulid(),
            counts={"records": {"events": n}},
            scan_term_hashes=("ab" * 32,),
        )
        for n in range(3)
    ]
    written = file.append(entries[:2]) + file.append(entries[2:])
    assert file.read() == written
    assert written[0].ledger_hash_prev == GENESIS_HASH
    assert [w.ledger_hash_prev for w in written[1:]] == [w.ledger_hash for w in written[:-1]]
    raw = file.path.read_text().splitlines()
    first = json.loads(raw[0])
    assert set(first) == {
        "erasure_id",
        "subject",
        "scope",
        "story_id",
        "at",
        "principal",
        "operation_id",
        "counts",
        "scan_term_hashes",
        "ledger_hash_prev",
        "ledger_hash",
    }
    assert set(first["subject"]) == {"kind", "id"}
    body = {k: v for k, v in first.items() if k != "ledger_hash"}
    assert first["ledger_hash"] == hashlib.sha256(canonical(body)).hexdigest()

    changed = json.loads(raw[1])
    changed["counts"] = {"records": {"events": 99}}
    for broken, line in (
        ([raw[0], json.dumps(changed), raw[2]], 2),
        ([raw[0], raw[2]], 2),
        ([raw[1], raw[0], raw[2]], 1),
        ([raw[0], "{not json", raw[2]], 2),
    ):
        with pytest.raises(erasure_ledger.LedgerCorrupt) as caught:
            parse("\n".join(broken))
        assert caught.value.line == line


async def test_an_erasure_writes_the_file_and_the_table(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """One erasure is one line in ``ledger.jsonl`` and one ``erasure_ledger`` row with the same
    fields and hashes, applied; nothing in either names the subject."""
    url = ledger_server.fresh()
    _, done = await _erased(url, tmp_path)
    (line,) = LedgerFile(tmp_path).read()
    (row,) = await ledger.ledger_rows(url)
    assert line.erasure_id == done["erasure_id"] == row.id
    assert Entry.from_row(row) == line
    assert row.applied_at is not None
    assert row.applied_note is None
    assert ledger.SUBJECT not in (tmp_path / "ledger.jsonl").read_text()


async def test_an_unwritable_ledger_refuses_the_erasure(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """A ledger directory that cannot be written (here, a file stands where it should be):
    503 ``ledger_unavailable``, and nothing is erased or ledgered (the story still names her)."""
    url = ledger_server.fresh()
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    config = ledger.settings(url, blocked)
    db = make_engine(config)
    try:
        async with respond_client(config, db) as (client, _):
            stories = await ledger.two_stories(client)
            refused = await client.post(
                e.PATH, json=e.request("character", stories.subject, stories.named), headers=e.WRITE
            )
    finally:
        await db.dispose()
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "ledger_unavailable"
    assert await ledger.ledger_rows(url) == []
    assert ledger.SUBJECT in " ".join(await ledger.pins(url, stories.named))
    assert erasure_ledger.ledger_problem(LedgerFile(blocked)) is not None


def test_the_preflight_refuses_a_broken_ledger(tmp_path: Path, monkeypatch: Any) -> None:
    """``epistrel serve`` exits 78 with the reason when the ledger's chain is broken, before it
    reaches the database."""
    file = LedgerFile(tmp_path)
    file.prepare()
    file.path.write_text('{"erasure_id": "x"}\n')
    monkeypatch.setenv("EPISTREL_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/none")
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", "a" * 40)
    monkeypatch.setenv("EPISTREL_ERASURE_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("EPISTREL_MEDIA_ROOT", str(tmp_path / "media"))
    assert main(["serve"]) == 78


# -- reconciling ------------------------------------------------------------------------------


async def test_the_table_is_restored_from_the_file(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """With the table's rows deleted, a restart puts the file's entry back (same fields and
    hashes), blocks the story until it is reapplied (an erasure with nothing left to erase), and
    settles it ``reapplied``; the story is served again."""
    url = ledger_server.fresh()
    stories, done = await _erased(url, tmp_path)
    (line,) = LedgerFile(tmp_path).read()
    await _without_ledger_rows(url)
    assert await ledger.ledger_rows(url) == []

    async with ledger.started(ledger.settings(url, tmp_path)) as (client, lib):
        assert lib._reconcile_task is not None
        await lib._reconcile_task
        story = await client.get(f"/stories/{stories.named}", headers=WRITE)
        assert story.status_code == 200, story.text
        assert story.json()["status"] == "active"
    (row,) = await ledger.ledger_rows(url)
    assert Entry.from_row(row) == line
    assert (row.id, row.applied_note) == (done["erasure_id"], "reapplied")


async def test_a_row_the_file_lacks_is_appended(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """A new ledger volume: the database's rows are appended to the empty file, chained, and
    the rows take the file's hashes; nothing is blocked."""
    url = ledger_server.fresh()
    first = tmp_path / "first"
    stories, done = await _erased(url, first)
    fresh = tmp_path / "fresh"
    async with ledger.started(ledger.settings(url, fresh)) as (client, _):
        story = await client.get(f"/stories/{stories.named}", headers=WRITE)
        assert story.status_code == 200, story.text
    (line,) = LedgerFile(fresh).read()
    assert line.erasure_id == done["erasure_id"]
    assert line.ledger_hash_prev == GENESIS_HASH
    (row,) = await ledger.ledger_rows(url)
    assert (row.ledger_hash_prev, row.ledger_hash) == (line.ledger_hash_prev, line.ledger_hash)
    assert row.applied_note is None  # applied when it was made: nothing to reapply


async def test_a_ledger_that_disagrees_refuses_to_serve(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """A file whose chain holds but whose entry differs from the table's row (its principal
    changed and the chain rebuilt): the start is refused, naming the entry. A broken chain is
    refused too."""
    url = ledger_server.fresh()
    _, done = await _erased(url, tmp_path)
    (line,) = LedgerFile(tmp_path).read()
    forged = Entry(**{**_fields(line), "principal": "p-someone-else"}).chained(GENESIS_HASH)
    (tmp_path / "ledger.jsonl").write_bytes(canonical(forged.line()) + b"\n")
    with pytest.raises(ConfigError) as caught:
        async with ledger.started(ledger.settings(url, tmp_path)):
            pass
    assert done["erasure_id"] in str(caught.value)

    (tmp_path / "ledger.jsonl").write_text('{"broken": true}\n')
    with pytest.raises(ConfigError) as broken:
        async with ledger.started(ledger.settings(url, tmp_path)):
            pass
    assert "EPISTREL_ERASURE_LEDGER_DIR" in str(broken.value)


def _fields(entry: Entry) -> dict[str, Any]:
    return {
        name: getattr(entry, name)
        for name in Entry.__dataclass_fields__
        if name not in ("ledger_hash_prev", "ledger_hash")
    }


async def test_the_table_is_append_only(ledger_server: ledger.Server, tmp_path: Path) -> None:
    """``erasure_ledger`` refuses DELETE, TRUNCATE and an UPDATE of anything but how the row was
    settled and its hashes."""
    url = ledger_server.fresh()
    _, done = await _erased(url, tmp_path)
    for statement in (
        "DELETE FROM erasure_ledger",
        "TRUNCATE erasure_ledger",
        "UPDATE erasure_ledger SET principal = 'p-other'",
        "UPDATE erasure_ledger SET counts = '{}'::jsonb",
    ):
        with pytest.raises(Exception, match="append-only"):
            await ledger.sql(url, statement)
    await ledger.sql(url, "UPDATE erasure_ledger SET applied_note = 'reapplied'")
    (row,) = await ledger.ledger_rows(url)
    assert (row.id, row.applied_note) == (done["erasure_id"], "reapplied")


async def test_an_empty_ledger_reconciles_to_nothing(
    ledger_server: ledger.Server, tmp_path: Path, captured: io.StringIO
) -> None:
    """Complement: with no erasure anywhere, a reconcile run blocks nothing, writes nothing
    (no ledger file line, no row, no run state) and logs one summary line of counts only, per
    run."""
    url = ledger_server.fresh()
    async with ledger.started(ledger.settings(url, tmp_path)) as (client, lib):
        assert lib._reconcile_task is not None
        await lib._reconcile_task  # the start-up run
        stories = await ledger.two_stories(client)
        run = await client.post("/admin/erasure/reconcile", headers=e.WRITE)
        status = await client.get("/admin/erasure/status", headers=e.WRITE)
        listed = await client.get("/stories", headers=WRITE)
    assert run.status_code == 202, run.text
    assert {k: v for k, v in run.json().items() if k != "run_id"} == {
        "ran": True,
        "settled": 0,
        "failed": 0,
        "pending_count": 0,
    }
    assert status.json() == {
        "pending": [],
        "pending_count": 0,
        "applied_count": 0,
        "in_progress": False,
        "last_run_at": None,
        "last_error": None,
        "attempts": {},
    }
    assert {s["status"] for s in listed.json()["items"]} == {"active"}
    assert {s["id"] for s in listed.json()["items"]} >= {stories.named, stories.other}
    assert (tmp_path / "ledger.jsonl").read_text() == ""
    assert await ledger.ledger_rows(url) == []
    (state,) = await ledger.sql(url, "SELECT erasure_reconcile_state FROM deployment_state")
    assert state.erasure_reconcile_state == {}
    lines = _lines(captured, erasure_ledger.__name__)
    # One summary per run (the start-up run and the requested one), and nothing else.
    assert [line["event"] for line in lines] == ["erasure ledger reconciled"] * 2
    for summary in lines:
        assert (summary["inserted"], summary["blocked"], summary["settled"]) == (0, 0, 0)
        assert (summary["erasure_ids"], summary["story_ids"]) == ([], [])
    assert ledger.SUBJECT not in captured.getvalue()


# -- the block ---------------------------------------------------------------------------------


async def _pending_story(url: str, ledger_dir: Path, **fields: Any) -> ledger.Stories:
    """Two stories (``fields`` for hers), and a ledger line erasing the subject from hers that
    the database lacks (as after a restore), the reconciler not started."""
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        async with respond_client(config, db) as (client, _):
            stories = await ledger.two_stories(client, **fields)
    finally:
        await db.dispose()
    LedgerFile(ledger_dir).append(
        [
            Entry(
                erasure_id=new_ulid(),
                at=clock.now(),
                subject_kind="character",
                subject_id=stories.subject,
                scope="story",
                story_id=stories.named,
                principal="p-op",
                operation_id=new_ulid(),
                counts={},
                scan_term_hashes=(),
            )
        ]
    )
    return stories


async def test_a_blocked_story_refuses_everything_but_its_deletion(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """An Engine that never ran the start-up compares the ledger on its first story call: the
    story becomes ``blocked_by_erasure``. The library refuses it like the API (``ErasurePending``
    with the pending count), a background job of it stays queued and unclaimed, a read of one
    of its operations is refused too (#333: its stored result may hold what the erasure
    removes) while the other story's operation answers, the other story is served, and deleting
    the story is allowed and settles its entry ``story_deleted``."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    stories = await _pending_story(url, ledger_dir)
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        lib = Engine(config)
        lib.bind(db)
        with pytest.raises(ErasurePending) as caught:
            await lib.get_story(CTX, stories.named)
        assert caught.value.details["pending_count"] == 1
        with pytest.raises(ErasurePending):
            await lib.history(CTX, stories.named, viewpoint="omniscient")
        # A story named by ``entity_type``/``entity_id`` is refused too (the relation and owner
        # methods); the same call for the other story answers.
        with pytest.raises(ErasurePending):
            await lib.list_relations(CTX, "story", stories.named)
        assert (await lib.list_relations(CTX, "story", stories.other)).entity_id == stories.other
        assert (await lib.get_story(CTX, stories.other)).id == stories.other
        (held,) = await ledger.sql(
            url,
            "SELECT operation_id FROM operations WHERE story_id = :s LIMIT 1",
            s=stories.named,
        )
        with pytest.raises(ErasurePending):
            await lib.get_operation(CTX, held.operation_id)
        (status,) = await ledger.sql(
            url, "SELECT status, status_before_block FROM stories WHERE id = :s", s=stories.named
        )
        assert (status.status, status.status_before_block) == ("blocked_by_erasure", "active")

        async with db.begin() as conn:
            job = await job_queue.enqueue(
                conn, "erasure_reconcile_retry", {}, story_id=stories.named
            )
        assert await job_queue.claim(db, "w-ledger", lease_s=30) is None
        (queued,) = await ledger.sql(url, "SELECT status FROM jobs WHERE id = :j", j=job)
        assert queued.status == "queued"

        async with respond_client(config, db) as (client, _):
            for story_id, status in ((stories.named, 503), (stories.other, 200)):
                operations = await ledger.sql(
                    url, "SELECT operation_id FROM operations WHERE story_id = :s", s=story_id
                )
                assert operations
                for operation in operations:
                    read = await client.get(f"/operations/{operation.operation_id}", headers=WRITE)
                    assert read.status_code == status, read.text
                    if status == 503:
                        assert read.json()["error"]["code"] == "erasure_pending"
                        assert ledger.SUBJECT not in read.text
            for path in (f"/stories/{stories.named}", f"/stories/{stories.named}/relations"):
                refused = await client.get(path, headers=WRITE)
                assert refused.status_code == 503, (path, refused.text)
                assert refused.json()["error"]["code"] == "erasure_pending", path
            served = await client.get(f"/stories/{stories.other}/relations", headers=WRITE)
            assert served.status_code == 200, served.text
            deleted = await client.delete(f"/stories/{stories.named}", headers=WRITE)
            assert deleted.status_code == 204, deleted.text
    finally:
        await db.dispose()
    (row,) = await ledger.ledger_rows(url)
    assert row.applied_note == "story_deleted"


async def test_a_failed_reapply_keeps_the_story_blocked(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """Without ``EPISTREL_CONFIG_KEY`` the reapply cannot run: the entry stays pending, the
    story blocked, and the status reports the attempt and the error class; once the key is back
    a reconcile run applies it and serves the story."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    stories = await _pending_story(url, ledger_dir)
    async with ledger.started(ledger.settings(url, ledger_dir, config_key=None)) as (client, lib):
        assert lib._reconcile_task is not None
        await lib._reconcile_task
        status = (await client.get("/admin/erasure/status", headers=e.WRITE)).json()
        refused = await client.get(f"/stories/{stories.named}", headers=WRITE)
    assert refused.status_code == 503
    assert status["pending_count"] == 1
    assert status["last_error"] == "ErasureKeyMissing"
    assert list(status["attempts"].values()) == [1]
    async with ledger.started(ledger.settings(url, ledger_dir)) as (client, lib):
        assert lib._reconcile_task is not None
        await lib._reconcile_task
        story = await client.get(f"/stories/{stories.named}", headers=WRITE)
        status = (await client.get("/admin/erasure/status", headers=e.WRITE)).json()
    assert story.status_code == 200, story.text
    assert (status["pending_count"], status["attempts"], status["last_error"]) == (0, {}, None)


async def test_a_blocked_story_is_listed_without_its_name(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#348: a blank story named after the subject, blocked after a restore. ``GET /stories``
    (also filtered by ``blocked_by_erasure``) and the library's ``list_stories`` list it with
    its status but no name and no settings, the first call to an Engine marking the block, while
    the other story keeps its name. #349: the reapply of the erasure (the story has no scenario
    snapshot) succeeds, the story is served with its name redacted, and the snapshot stays SQL
    NULL."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    tale = f"{ledger.SUBJECT}'s tale"
    stories = await _pending_story(url, ledger_dir, name=tale)
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        lib = Engine(config)
        lib.bind(db)
        listed = {s.id: s for s in await lib.list_stories(CTX)}
        held = listed[stories.named]
        assert (held.status.value, held.name, held.settings) == ("blocked_by_erasure", None, {})
        assert listed[stories.other].name == "A story"
        async with respond_client(config, db) as (client, _):
            for params in ({}, {"status": "blocked_by_erasure"}):
                response = await client.get("/stories", params=params, headers=WRITE)
                assert response.status_code == 200, response.text
                assert ledger.SUBJECT not in response.text
                names = {s["id"]: s["name"] for s in response.json()["items"]}
                assert names[stories.named] is None
    finally:
        await db.dispose()
    async with ledger.started(config) as (client, lib):
        assert lib._reconcile_task is not None
        await lib._reconcile_task
        status = (await client.get("/admin/erasure/status", headers=e.WRITE)).json()
        story = await client.get(f"/stories/{stories.named}", headers=WRITE)
        response = await client.get("/stories", headers=WRITE)
    assert (status["pending_count"], status["applied_count"], status["last_error"]) == (0, 1, None)
    assert story.status_code == 200, story.text
    assert story.json()["name"] == "[erased]'s tale"
    names = {s["id"]: s["name"] for s in response.json()["items"]}
    assert names[stories.named] == "[erased]'s tale"
    (row,) = await ledger.sql(
        url,
        "SELECT scenario_snapshot IS NULL AS sql_null FROM stories WHERE id = :s",
        s=stories.named,
    )
    assert row.sql_null


def test_every_story_method_is_gated() -> None:
    """Every public Engine coroutine method that can name a story (a ``story_id``, or an
    ``entity_type`` and ``entity_id``) refuses a blocked story, except the deletion: the check
    is in the Engine, so REST and the library agree."""

    def names_a_story(member: Any) -> bool:
        parameters = inspect.signature(member).parameters
        return "story_id" in parameters or {"entity_type", "entity_id"} <= parameters.keys()

    missing = [
        name
        for name, member in vars(Engine).items()
        if not name.startswith("_")
        and inspect.iscoroutinefunction(member)
        and names_a_story(member)
        and name not in ERASURE_GATE_EXEMPT
        and not getattr(member, ERASURE_GATED_ATTRIBUTE, False)
    ]
    assert missing == []
    for name in ("list_relations", "add_relation", "remove_relation", "transfer_owner"):
        assert getattr(getattr(Engine, name), ERASURE_GATED_ATTRIBUTE, False), name
    assert not getattr(Engine.delete_story, ERASURE_GATED_ATTRIBUTE, False)


# -- backup verify ------------------------------------------------------------------------------


def test_copy_rows_are_read_as_postgres_writes_them() -> None:
    """``COPY`` text: tab-separated, ``\\N`` null, backslash escapes."""
    script = (
        "SET x = 1;\n"
        "COPY public.events (id, type, payload, redacted_at) FROM stdin;\n"
        '1\terasure_marker\t{"a": "b\\\\tc"}\t\\N\n'
        "2\tmessage_rendered\tline\\none\t2026-10-14 10:00:00.5+00\n"
        "\\.\n"
    )
    assert backup.copy_blocks(script) == {
        "events": [
            {"id": "1", "type": "erasure_marker", "payload": '{"a": "b\\tc"}', "redacted_at": None},
            {
                "id": "2",
                "type": "message_rendered",
                "payload": "line\none",
                "redacted_at": "2026-10-14 10:00:00.5+00",
            },
        ]
    }


async def test_backup_verify_exit_codes(ledger_server: ledger.Server, tmp_path: Path) -> None:
    """``epistrel backup verify``: no ``pg_restore`` exits 4, no ledger file 5, a broken chain 2,
    a dump holding a row the file lacks 2, an unreadable dump 3; ``--json`` prints the report."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    await _erased(url, ledger_dir)
    dump = ledger_server.dump(url, tmp_path / "now.dump")
    wrapper = str(ledger_server.pg_restore_wrapper(tmp_path))

    missing = backup.verify(dump, ledger_dir=ledger_dir, pg_restore="no-such-pg-restore")
    assert missing.exit_code == backup.EXIT_NO_BINARY
    absent = backup.verify(dump, ledger_dir=tmp_path / "none", pg_restore=wrapper)
    assert absent.exit_code == backup.EXIT_NO_LEDGER
    garbage = tmp_path / "garbage.dump"
    garbage.write_bytes(b"not a dump")
    assert (
        backup.verify(garbage, ledger_dir=ledger_dir, pg_restore=wrapper).exit_code
        == backup.EXIT_UNREADABLE
    )
    other = tmp_path / "other"
    LedgerFile(other).prepare()
    lacking = backup.verify(dump, ledger_dir=other, pg_restore=wrapper)
    assert lacking.exit_code == backup.EXIT_BROKEN
    assert "the file lacks" in lacking.problems[0]
    broken = tmp_path / "broken"
    LedgerFile(broken).prepare()
    (broken / "ledger.jsonl").write_text("{}\n")
    assert (
        backup.verify(dump, ledger_dir=broken, pg_restore=wrapper).exit_code == backup.EXIT_BROKEN
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(
            [
                "backup",
                "verify",
                str(dump),
                "--ledger",
                str(ledger_dir),
                "--json",
                "--pg-restore",
                wrapper,
            ]
        )
    assert code == backup.EXIT_OK
    report = json.loads(out.getvalue())
    assert report["exit_code"] == 0
    assert len(report["expected_gaps"]["erasure_markers"]) >= 1
    assert report["ledger_rows_in_dump"] == 1


async def test_reconcile_reads_go_through_the_engine(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """``GET /admin/erasure/status`` after a reapply reports it applied, with the run's time."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    await _pending_story(url, ledger_dir)
    async with ledger.started(ledger.settings(url, ledger_dir)) as (client, lib):
        assert lib._reconcile_task is not None
        await lib._reconcile_task
        status = (await client.get("/admin/erasure/status", headers=e.WRITE)).json()
    assert (status["pending_count"], status["applied_count"]) == (0, 1)
    assert status["last_run_at"] is not None
    rows = await ledger.sql(url, "SELECT count(*) AS n FROM erasure_ledger")
    assert rows[0].n == 1
