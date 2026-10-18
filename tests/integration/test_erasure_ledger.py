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
import re
import typing
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from epistrel import Engine
from epistrel.cli import main
from epistrel.config import ConfigError
from epistrel.core import clock
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.erasure.erase import ErasureRequest
from epistrel.core.erasure.ledger import GENESIS_HASH, Entry, LedgerFile, canonical, parse
from epistrel.core.errors import ErasurePending
from epistrel.core.feedback_export import FeedbackExportRequest
from epistrel.core.feedback_reporting import FeedbackReviewList
from epistrel.core.ids import new_ulid
from epistrel.core.stories import StorySpec
from epistrel.engine import CATALOG_GATED_ATTRIBUTE, ERASURE_GATE_EXEMPT, ERASURE_GATED_ATTRIBUTE
from epistrel.observability import logging as telemetry_logging
from epistrel.orchestrator.jobs import queue as job_queue
from epistrel.testing import pauses
from epistrel.tools import backup
from tests import character_support as characters
from tests import erasure_support as e
from tests import genesis_support as g
from tests import ledger_support as ledger
from tests import media_support as media
from tests import scenario_support as scenarios
from tests.feedback_support import (
    OPERATOR,
    answered,
    as_principal,
    play,
    rate,
    set_deployment,
)
from tests.respond_support import respond_client
from tests.story_support import WRITE, story_body

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


async def _pending_story(
    url: str,
    ledger_dir: Path,
    *,
    prepare: Callable[[httpx.AsyncClient, ledger.Stories], Awaitable[None]] | None = None,
    catalog: Callable[[], str] | None = None,
    **fields: Any,
) -> ledger.Stories:
    """Two stories (``fields`` for hers, then ``prepare`` run on them), and a ledger line
    erasing the subject from hers that the database lacks (as after a restore), the reconciler
    not started. With ``catalog`` (called after ``prepare``: a catalog character's id), the
    line is a catalog-scope erasure of that catalog character instead, which blocks no story."""
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        async with respond_client(config, db) as (client, _):
            stories = await ledger.two_stories(client, **fields)
            if prepare is not None:
                await prepare(client, stories)
    finally:
        await db.dispose()
    LedgerFile(ledger_dir).append(
        [
            Entry(
                erasure_id=new_ulid(),
                at=clock.now(),
                subject_kind="character" if catalog is None else "catalog_character",
                subject_id=stories.subject if catalog is None else catalog(),
                scope="story" if catalog is None else "catalog",
                story_id=stories.named if catalog is None else None,
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


def _owned_stories(lookup: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in lookup["owned"]["stories"]["items"]}


async def test_a_blocked_story_is_looked_up_without_its_name(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#359: while the reapply is held, ``GET /principals/{id}`` lists the owner's blocked
    story with its status and a null name, as ``GET /stories`` does (#348), and the other story
    keeps its name; once the reapply settles it, the lookup names it, redacted."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    stories = await _pending_story(url, ledger_dir, name=f"{ledger.SUBJECT}'s tale")
    config = ledger.settings(url, ledger_dir)
    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, lib):
            await hold.arrived()
            found = await client.get("/principals/p-author", headers=WRITE)
            assert found.status_code == 200, found.text
            assert ledger.SUBJECT not in found.text
            owned = _owned_stories(found.json())
            held = owned[stories.named]
            assert (held["status"], held["name"]) == ("blocked_by_erasure", None)
            assert owned[stories.other]["name"] == "A story"
            hold.release()
            assert lib._reconcile_task is not None
            await lib._reconcile_task
            settled = await client.get("/principals/p-author", headers=WRITE)
    assert settled.status_code == 200, settled.text
    assert _owned_stories(settled.json())[stories.named]["name"] == "[erased]'s tale"


async def test_a_blocked_story_hides_its_policies(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#363: a story policy naming the subject. While the reapply is held, ``GET
    /admin/policies`` (also ``?scope=story``) leaves it out, ``GET /admin/policies/{id}`` is 503
    ``erasure_pending``, and a fresh library Engine agrees: its first ``policies_get`` (#371) or
    ``policies_list`` (#380) marks the block, each on a database nothing has marked yet; the
    deployment's policies are still served. Once the reapply settles the story, the policy is
    served, redacted."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    policy = {
        "name": f"Keep {ledger.SUBJECT} offstage",
        "scope": "story",
        "rule": {"type": "wordlist", "words": [f"{ledger.SUBJECT} cursed"]},
        "message": f"{ledger.SUBJECT} is not cursed.",
    }

    async def with_policy(client: httpx.AsyncClient, stories: ledger.Stories) -> None:
        done = await client.patch(
            f"/stories/{stories.named}/settings", json={"policies": [policy]}, headers=WRITE
        )
        assert done.status_code == 200, done.text

    stories = await _pending_story(url, ledger_dir, prepare=with_policy)
    (row,) = await ledger.sql(url, "SELECT id FROM policies WHERE story_id = :s", s=stories.named)
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        # #371, #380: nothing has marked the database yet, so the refusal comes from the
        # method's own ledger comparison as a fresh Engine's first call.
        await _unblock_all(url)
        first = Engine(config)
        first.bind(db)
        with pytest.raises(ErasurePending):
            await first.policies_get(CTX, row.id)
        await _unblock_all(url)
        lib = Engine(config)
        lib.bind(db)
        listed = await lib.policies_list(CTX)
        assert row.id not in {p.id for p in listed}
        assert ledger.SUBJECT not in repr(listed)
        with pytest.raises(ErasurePending):
            await lib.policies_get(CTX, row.id)
    finally:
        await db.dispose()
    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, lib):
            await hold.arrived()
            for params in ({}, {"scope": "story"}, {"include_retired": "true"}):
                found = await client.get("/admin/policies", params=params, headers=e.WRITE)
                assert found.status_code == 200, found.text
                assert ledger.SUBJECT not in found.text, params
                assert row.id not in {p["id"] for p in found.json()["items"]}
            refused = await client.get(f"/admin/policies/{row.id}", headers=e.WRITE)
            assert refused.status_code == 503, refused.text
            assert refused.json()["error"]["code"] == "erasure_pending"
            assert ledger.SUBJECT not in refused.text
            hold.release()
            assert lib._reconcile_task is not None
            await lib._reconcile_task
            served = await client.get(f"/admin/policies/{row.id}", headers=e.WRITE)
            listed_after = await client.get("/admin/policies", headers=e.WRITE)
    assert served.status_code == 200, served.text
    assert served.json()["name"] == "Keep [erased] offstage"
    assert row.id in {p["id"] for p in listed_after.json()["items"]}
    assert ledger.SUBJECT not in listed_after.text


def _fresh(config: Any, db: Any) -> Engine:
    """A library Engine that has compared nothing with the ledger yet."""
    lib = Engine(config)
    lib.bind(db)
    return lib


async def _unblock_all(url: str) -> None:
    """Undo every block, as a database nothing has compared with the ledger yet holds it."""
    await ledger.sql(
        url,
        "UPDATE stories SET status = status_before_block, status_before_block = NULL "
        "WHERE status = 'blocked_by_erasure'",
    )


async def test_library_reads_mark_before_they_serve(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#360: a fresh library Engine, never reconciled, marks the ledger on its first lookup,
    review or export as it does on ``list_stories``: the principal lookup lists the restored
    story without its name, and the memory review list and the export leave out its rating of a
    message whose input names the subject."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"

    async def rated(client: httpx.AsyncClient, stories: ledger.Stories) -> None:
        (player,) = await ledger.sql(
            url, "SELECT id FROM characters WHERE story_id = :s AND is_player", s=stories.named
        )
        turn = await play(client, stories.named, player.id, f"Where is {ledger.SUBJECT}?")
        message_id = turn["message"]["message_id"]
        payload = {"rating": 2, "tags": ["forgot_something"]}
        answered(await rate(client, stories.named, message_id, payload), 201)
        await set_deployment(client, feedback_export=True)

    stories = await _pending_story(url, ledger_dir, prepare=rated, name=f"{ledger.SUBJECT}'s tale")
    config = ledger.settings(url, ledger_dir)
    for read in ("lookup", "review", "export"):
        await ledger.sql(
            url,
            "UPDATE stories SET status = status_before_block, status_before_block = NULL "
            "WHERE status = 'blocked_by_erasure'",
        )
        db = make_engine(config)
        try:
            lib = Engine(config)
            lib.bind(db)
            if read == "lookup":
                found = await lib.lookup_principal(CTX, "p-author")
                assert ledger.SUBJECT not in found.model_dump_json()
                (item,) = [s for s in found.owned.stories.items if s.id == stories.named]
                assert (item.status, item.name) == ("blocked_by_erasure", None)
            elif read == "review":
                page = await lib.feedback_review(CTX, FeedbackReviewList.MEMORY)
                assert [i.story_id for i in page.items if i.story_id == stories.named] == []
                assert ledger.SUBJECT not in page.model_dump_json()
            else:
                written = await lib.export_feedback(
                    RequestContext(principal="p-op"), FeedbackExportRequest.model_validate({})
                )
                assert written.case_count == 0
        finally:
            await db.dispose()


def test_every_story_method_is_gated() -> None:
    """Every public Engine coroutine method that can name a story (a ``story_id``, or an
    ``entity_type`` and ``entity_id``) refuses a blocked story, except the deletion: the check
    is in the Engine, so REST and the library agree. #398: a method that names a story inside
    a request model (a parameter whose type has a ``story_id``, today ``Engine.erase``) is not
    seen by the decorator, so its own body must await ``self._erasure_gate``."""

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

    def kinds(hint: Any) -> Iterator[Any]:
        """``hint`` and every type inside it (unions, ``Optional``, generics; #406)."""
        yield hint
        for argument in typing.get_args(hint):
            yield from kinds(argument)

    def names_a_story_in_a_model(member: Any) -> bool:
        hints = typing.get_type_hints(member)
        return any(
            isinstance(kind, type) and hasattr(kind, "story_id")
            for parameter, hint in hints.items()
            if parameter != "return"
            for kind in kinds(hint)
        )

    async def optional(ctx: RequestContext, request: ErasureRequest | None) -> None: ...
    async def legacy(ctx: RequestContext, request: typing.Optional[ErasureRequest]) -> None: ...  # noqa: UP045
    async def batch(ctx: RequestContext, requests: list[ErasureRequest]) -> None: ...
    async def plain(ctx: RequestContext, story_name: str) -> None: ...

    for probe in (optional, legacy, batch):
        assert names_a_story_in_a_model(probe), probe.__name__
    assert not names_a_story_in_a_model(plain)

    modelled = [
        name
        for name, member in vars(Engine).items()
        if not name.startswith("_")
        and inspect.iscoroutinefunction(member)
        and name not in ERASURE_GATE_EXEMPT
        and not getattr(member, ERASURE_GATED_ATTRIBUTE, False)
        and names_a_story_in_a_model(member)
    ]
    assert "erase" in modelled  # the check sees the shape it is for
    gate_call = re.compile(r"^\s*await self\._erasure_gate\(", re.MULTILINE)
    for name in modelled:
        assert gate_call.search(inspect.getsource(getattr(Engine, name))), name


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


async def test_the_restore_window_serves_no_replay_or_export_of_a_blocked_story(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#369, #370: during the restore window, replaying the ``Idempotency-Key`` that created a
    blocked story (``POST /stories``, and the library's ``create_story`` as a fresh Engine's
    first call) is 503 ``erasure_pending``, like every read of the story; an undelivered
    feedback export with a case of it is 503 too and stays undelivered (in REST, and as a fresh
    library Engine's first call: #379). Once the reapply
    settles, the export's content holds no subject and the replay answers the reference."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    held: dict[str, str] = {}

    async def prepare(client: httpx.AsyncClient, stories: ledger.Stories) -> None:
        (player,) = await ledger.sql(
            url, "SELECT id FROM characters WHERE story_id = :s AND is_player", s=stories.named
        )
        turn = await play(client, stories.named, player.id, f"Where is {ledger.SUBJECT}?")
        rated = await rate(
            client,
            stories.named,
            turn["message"]["message_id"],
            {"rating": 2, "tags": ["forgot_something"]},
        )
        answered(rated, 201)
        await set_deployment(client, feedback_export=True)
        made = await client.post("/admin/feedback/export", json={}, headers=as_principal(OPERATOR))
        assert made.status_code == 201, made.text
        held["export"] = made.json()["export_id"]

    stories = await _pending_story(url, ledger_dir, prepare=prepare)
    keys = await ledger.sql(
        url, "SELECT key, result::text AS r FROM idempotency_keys WHERE scope <> 'story'"
    )
    (key,) = [k.key for k in keys if ledger.SUBJECT in (k.r or "") and stories.named in k.r]
    body = story_body(ledger.PLAYER_NAME, ledger.SUBJECT, ledger.BYSTANDER)
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        first = Engine(config)
        first.bind(db)
        with pytest.raises(ErasurePending):
            await first.create_story(
                RequestContext(principal="p-ledger", idempotency_key=key),
                StorySpec.model_validate(body),
            )
        # #379: the export's content as a fresh Engine's first call, on a database nothing has
        # marked: its own ledger comparison blocks the story, and the export stays undelivered.
        await _unblock_all(url)
        content_first = Engine(config)
        content_first.bind(db)
        with pytest.raises(ErasurePending):
            await content_first.get_feedback_export_content(CTX, held["export"])
    finally:
        await db.dispose()
    export_sql = "SELECT downloaded_at FROM feedback_export_log WHERE id = :i"
    (untouched,) = await ledger.sql(url, export_sql, i=held["export"])
    assert untouched.downloaded_at is None
    content_path = f"/admin/feedback/exports/{held['export']}/content"
    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, lib):
            await hold.arrived()
            replay = await client.post(
                "/stories", json=body, headers={**WRITE, "Idempotency-Key": key}
            )
            assert replay.status_code == 503, replay.text
            assert replay.json()["error"]["code"] == "erasure_pending"
            assert ledger.SUBJECT not in replay.text
            refused = await client.get(content_path, headers=e.WRITE)
            assert refused.status_code == 503, refused.text
            assert refused.json()["error"]["code"] == "erasure_pending"
            assert ledger.SUBJECT not in refused.text
            (log_row,) = await ledger.sql(url, export_sql, i=held["export"])
            assert log_row.downloaded_at is None
            hold.release()
            assert lib._reconcile_task is not None
            await lib._reconcile_task
            delivered = await client.get(content_path, headers=e.WRITE)
            replayed = await client.post(
                "/stories", json=body, headers={**WRITE, "Idempotency-Key": key}
            )
    assert delivered.status_code == 200, delivered.text
    assert delivered.content.strip()
    assert ledger.SUBJECT not in delivered.text
    assert replayed.status_code == 200, replayed.text
    assert ledger.SUBJECT not in replayed.text


async def test_the_restore_window_refuses_an_erasure_in_a_blocked_story(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#383, #387: during the restore window, ``POST /admin/erasure`` naming a blocked story is
    503 ``erasure_pending``: a dry run (its candidate snippets would quote the restored text
    the pending erasure removes) as much as a real erasure of another subject of the story
    (which would write to it before its reapply), in REST and as a fresh library Engine's first
    call. The story's revision is unchanged. Once the reapply settles, the erasure runs."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    stories = await _pending_story(url, ledger_dir)
    config = ledger.settings(url, ledger_dir)
    revision_sql = "SELECT timeline_revision FROM stories WHERE id = :s"
    (before,) = await ledger.sql(url, revision_sql, s=stories.named)
    db = make_engine(config)
    try:
        for subject, dry_run in ((stories.subject, True), (stories.bystander, False)):
            await _unblock_all(url)
            asked = e.request("character", subject, stories.named, dry_run=dry_run)
            with pytest.raises(ErasurePending):
                await _fresh(config, db).erase(CTX, ErasureRequest.model_validate(asked))
    finally:
        await db.dispose()
    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, lib):
            await hold.arrived()
            for subject, dry_run in ((stories.subject, True), (stories.bystander, False)):
                refused = await client.post(
                    e.PATH,
                    json=e.request("character", subject, stories.named, dry_run=dry_run),
                    headers={**e.WRITE, "Idempotency-Key": new_ulid()},
                )
                assert refused.status_code == 503, (dry_run, refused.text)
                assert refused.json()["error"]["code"] == "erasure_pending"
                assert ledger.SUBJECT not in refused.text
            (during,) = await ledger.sql(url, revision_sql, s=stories.named)
            assert during.timeline_revision == before.timeline_revision
            hold.release()
            assert lib._reconcile_task is not None
            await lib._reconcile_task
            done = await e.erase(client, "character", stories.bystander, stories.named)
    assert done["residue"] == 0


async def test_a_pending_catalog_erasure_holds_undelivered_exports(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#378: a pending catalog-scope erasure blocks no story, but an undelivered feedback
    export may still name its subject: during the restore window its content is 503
    ``erasure_pending`` and it stays undelivered, so the reapply redacts it; afterwards it is
    delivered without the subject."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    held: dict[str, str] = {}

    async def prepare(client: httpx.AsyncClient, stories: ledger.Stories) -> None:
        held["catalog"] = await g.catalog_character_id(client, ledger.SUBJECT)
        (player,) = await ledger.sql(
            url, "SELECT id FROM characters WHERE story_id = :s AND is_player", s=stories.named
        )
        turn = await play(client, stories.named, player.id, f"Where is {ledger.SUBJECT}?")
        rated = await rate(
            client,
            stories.named,
            turn["message"]["message_id"],
            {"rating": 2, "tags": ["forgot_something"]},
        )
        answered(rated, 201)
        await set_deployment(client, feedback_export=True)
        made = await client.post("/admin/feedback/export", json={}, headers=as_principal(OPERATOR))
        assert made.status_code == 201, made.text
        held["export"] = made.json()["export_id"]

    await _pending_story(url, ledger_dir, prepare=prepare, catalog=lambda: held["catalog"])
    config = ledger.settings(url, ledger_dir)
    export_sql = "SELECT downloaded_at FROM feedback_export_log WHERE id = :i"
    content_path = f"/admin/feedback/exports/{held['export']}/content"
    db = make_engine(config)
    try:
        with pytest.raises(ErasurePending) as caught:
            await _fresh(config, db).get_feedback_export_content(CTX, held["export"])
        assert caught.value.details["story_id"] is None
        assert caught.value.details["pending_count"] == 1
    finally:
        await db.dispose()
    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, lib):
            await hold.arrived()
            refused = await client.get(content_path, headers=e.WRITE)
            assert refused.status_code == 503, refused.text
            assert refused.json()["error"]["code"] == "erasure_pending"
            assert ledger.SUBJECT not in refused.text
            (log_row,) = await ledger.sql(url, export_sql, i=held["export"])
            assert log_row.downloaded_at is None
            hold.release()
            assert lib._reconcile_task is not None
            await lib._reconcile_task
            delivered = await client.get(content_path, headers=e.WRITE)
    assert delivered.status_code == 200, delivered.text
    assert "[erased]" in delivered.text
    assert ledger.SUBJECT not in delivered.text


def test_the_export_content_route_states_both_pending_refusals() -> None:
    """#399: the content route's description (committed ``docs/openapi.json``, held equal to the
    generated schema by ``epistrel.tools.openapi --check``) names the story-scope 503 and the
    catalog-scope one #378 added."""
    schema = json.loads((Path(__file__).parents[2] / "docs" / "openapi.json").read_text())
    route = schema["paths"]["/admin/feedback/exports/{export_id}/content"]["get"]
    assert "`blocked_by_erasure`" in route["description"]
    assert "while a catalog-scope erasure is pending" in route["description"]


# -- the catalog's restore window (#346, ADR-157) ------------------------------------------------


@dataclass
class _Catalog:
    """A catalog naming the subject, and one entry of each kind that does not (complements)."""

    subject: str
    friend: str
    bystander: str
    portrait: str
    plain_media: str
    scenario: str
    plain_scenario: str
    key: str
    body: dict[str, Any]


async def _subject_catalog(client: httpx.AsyncClient) -> _Catalog:
    """Catalog character ``SUBJECT`` (her portrait captioned with her name, a scenario role
    filled with her) and Odile, whose public card names her; Tobiah, a scenario and an image
    that do not. Her draft is created under a known ``Idempotency-Key``."""
    portrait = await media.uploaded(client, caption=f"{ledger.SUBJECT} at the lamp room door.")
    plain_media = await media.uploaded(client, caption="The quay at low tide.")
    body = characters.complete(ledger.SUBJECT, images={"primary": portrait["id"], "gallery": []})
    key = f"k-subject-{new_ulid()}"
    created = await client.post(
        "/catalog/characters", json=body, headers={**scenarios.WRITE, "Idempotency-Key": key}
    )
    assert created.status_code == 201, created.text
    subject = created.json()["id"]
    published = await client.post(
        f"/catalog/characters/{subject}/publish",
        json={"draft_revision": 1},
        headers=scenarios.WRITE,
    )
    assert published.status_code == 201, published.text
    friend = await scenarios.published_character(
        client,
        "Odile Brack",
        public_card={"name": "Odile Brack", "public_description": f"Sails with {ledger.SUBJECT}."},
    )
    bystander = await scenarios.published_character(client, ledger.BYSTANDER)
    scenario, _ = await g.published_scenario(
        client,
        g.scenario_body(
            g.role("keeper", fill="default", character={"catalog_id": subject}),
            g.role("player", kind="player", label="The eldest"),
            name="The Lamp Room",
        ),
    )
    plain_scenario, _ = await g.published_scenario(client, g.scenario_body(name="Low Tide"))
    return _Catalog(
        subject,
        friend,
        bystander,
        portrait["id"],
        plain_media["id"],
        scenario,
        plain_scenario,
        key,
        body,
    )


async def _pending_catalog_erasure(
    url: str, ledger_dir: Path, prepare: Callable[[httpx.AsyncClient], Awaitable[tuple[str, str]]]
) -> None:
    """``prepare``'s catalog, then a catalog-scope ledger line erasing its ``(kind, id)`` that
    the database lacks (as after a restore), the reconciler not started."""
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        async with respond_client(config, db) as (client, _):
            kind, subject_id = await prepare(client)
    finally:
        await db.dispose()
    LedgerFile(ledger_dir).append(
        [
            Entry(
                erasure_id=new_ulid(),
                at=clock.now(),
                subject_kind=kind,
                subject_id=subject_id,
                scope="catalog",
                story_id=None,
                principal="p-op",
                operation_id=new_ulid(),
                counts={},
                scan_term_hashes=(),
            )
        ]
    )


def _pending(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "erasure_pending"
    assert error["details"]["story_id"] is None
    assert response.headers["Retry-After"] == "30"
    assert ledger.SUBJECT not in response.text


async def test_a_pending_catalog_erasure_holds_what_it_reaches(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#346 (the owner's decision, ADR-157): during the restore window of a catalog-scope
    erasure of a catalog character, every read that touches her is 503 ``erasure_pending``:
    her entry, her companion, the character whose card names her, the scenario filled with
    her, her portrait's metadata and bytes, the stored result of her draft's creation (``GET
    /operations/{id}`` and its replay); a write or delete of her entry, a story from a scenario
    cast with her, and a catalog-scope erasure dry run are refused too. Lists and search leave
    out what she reaches; the principal lookup lists her entry without its name. What does not
    name her is served (complements). A fresh library Engine refuses alike as its first call.
    Once the reapply settles, everything is served, and nothing names her."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    held: dict[str, _Catalog] = {}

    async def prepare(client: httpx.AsyncClient) -> tuple[str, str]:
        held["c"] = await _subject_catalog(client)
        return "catalog_character", held["c"].subject

    await _pending_catalog_erasure(url, ledger_dir, prepare)
    c = held["c"]
    (made,) = await ledger.sql(
        url,
        "SELECT operation_id FROM idempotency_keys WHERE scope = 'deployment' AND key = :k",
        k=c.key,
    )
    (plain,) = await ledger.sql(
        url,
        "SELECT k.operation_id FROM idempotency_keys k WHERE k.scope = 'deployment' "
        "AND k.result::text LIKE :n",
        n=f"%{ledger.BYSTANDER}%",
    )
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        with pytest.raises(ErasurePending) as caught:
            await _fresh(config, db).get_character(CTX, c.subject)
        assert caught.value.details["story_id"] is None
        assert caught.value.details["pending_count"] == 1
        page = await _fresh(config, db).search_catalog(CTX, "character")
        assert {i.id for i in page.items} == {c.bystander}
        with pytest.raises(ErasurePending):
            await _fresh(config, db).get_media(CTX, c.portrait)
        with pytest.raises(ErasurePending):
            await _fresh(config, db).get_operation(CTX, made.operation_id)
    finally:
        await db.dispose()

    refused = (
        f"/catalog/characters/{c.subject}",
        f"/catalog/characters/{c.subject}/companion",
        f"/catalog/characters/{c.subject}/versions",
        f"/catalog/characters/{c.subject}/relations",
        f"/catalog/characters/{c.friend}",
        f"/catalog/scenarios/{c.scenario}",
        f"/media/{c.portrait}",
        f"/media/{c.portrait}/content",
        f"/operations/{made.operation_id}",
    )
    served = (
        f"/catalog/characters/{c.bystander}",
        f"/catalog/scenarios/{c.plain_scenario}",
        f"/media/{c.plain_media}",
        f"/operations/{plain.operation_id}",
    )
    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, lib):
            await hold.arrived()
            for path in refused:
                _pending(await client.get(path, headers=e.WRITE))
            for path in served:
                found = await client.get(path, headers=e.WRITE)
                assert found.status_code == 200, (path, found.text)
            for path, params in (
                ("/catalog/characters", {}),
                ("/catalog/characters", {"q": "Quenby"}),
                ("/catalog/scenarios", {}),
                ("/media", {}),
            ):
                listed = await client.get(path, params=params, headers=e.WRITE)
                assert listed.status_code == 200, listed.text
                assert ledger.SUBJECT not in listed.text, path
                ids = {item["id"] for item in listed.json()["items"]}
                assert not ids & {c.subject, c.friend, c.scenario, c.portrait}, path
            listed = await client.get("/catalog/characters", headers=e.WRITE)
            assert {i["id"] for i in listed.json()["items"]} == {c.bystander}
            looked = await client.get(f"/principals/{scenarios.PRINCIPAL}", headers=e.WRITE)
            assert looked.status_code == 200, looked.text
            assert ledger.SUBJECT not in looked.text
            names = {i["id"]: i["name"] for i in looked.json()["owned"]["characters"]["items"]}
            assert names == {c.subject: None, c.friend: None, c.bystander: ledger.BYSTANDER}
            _pending(
                await client.post(
                    "/catalog/characters",
                    json=c.body,
                    headers={**scenarios.WRITE, "Idempotency-Key": c.key},
                )
            )
            _pending(
                await client.patch(
                    f"/catalog/characters/{c.subject}",
                    json={"draft_revision": 2, "public_card": {"public_description": "Gone."}},
                    headers=scenarios.WRITE,
                )
            )
            _pending(await client.delete(f"/catalog/characters/{c.subject}", headers=e.WRITE))
            _pending(
                await client.post(
                    "/stories",
                    json=g.from_scenario(
                        c.plain_scenario,
                        slots={"mother": g.catalog_slot(c.subject)},
                        ignore_warnings=True,
                    ),
                    headers=g.headers(new_ulid()),
                )
            )
            _pending(
                await client.post(
                    e.PATH,
                    json=e.request("catalog_character", c.bystander, dry_run=True),
                    headers={**e.WRITE, "Idempotency-Key": new_ulid()},
                )
            )

            hold.release()
            assert lib._reconcile_task is not None
            await lib._reconcile_task
            for path in (f"/catalog/characters/{c.subject}", f"/media/{c.portrait}"):
                gone = await client.get(path, headers=e.WRITE)
                assert gone.status_code == 404, (path, gone.text)
            for path in (
                f"/catalog/characters/{c.friend}",
                f"/catalog/scenarios/{c.scenario}",
                f"/operations/{made.operation_id}",
                *served,
            ):
                found = await client.get(path, headers=e.WRITE)
                assert found.status_code == 200, (path, found.text)
                assert ledger.SUBJECT not in found.text, path
            listed = await client.get("/catalog/characters", headers=e.WRITE)
            assert {i["id"] for i in listed.json()["items"]} == {c.friend, c.bystander}
            assert ledger.SUBJECT not in listed.text
    (row,) = await ledger.ledger_rows(url)
    assert row.applied_note == "reapplied"


async def test_a_pending_catalog_erasure_of_a_persona_holds_it(
    ledger_server: ledger.Server, tmp_path: Path
) -> None:
    """#346: a pending catalog-scope erasure of a persona refuses its read and leaves it out of
    ``GET /personas`` (the library's ``list_personas`` too, as a fresh Engine's first call),
    while another persona is served; after the reapply it is gone."""
    url = ledger_server.fresh()
    ledger_dir = tmp_path / "ledger"
    held: dict[str, str] = {}

    async def prepare(client: httpx.AsyncClient) -> tuple[str, str]:
        for name, label in ((ledger.SUBJECT, "subject"), (ledger.BYSTANDER, "other")):
            made = await client.post(
                "/personas",
                json={"name": name, "gender": "woman", "age": 30, "description": "A sailor."},
                headers=WRITE,
            )
            assert made.status_code == 201, made.text
            held[label] = made.json()["id"]
        return "persona", held["subject"]

    await _pending_catalog_erasure(url, ledger_dir, prepare)
    config = ledger.settings(url, ledger_dir)
    db = make_engine(config)
    try:
        page = await _fresh(config, db).list_personas(CTX)
        assert [p.id for p in page.items] == [held["other"]]
        with pytest.raises(ErasurePending):
            await _fresh(config, db).get_persona(CTX, held["subject"])
    finally:
        await db.dispose()
    with pauses.held(pauses.ERASURE_RECONCILE_BEFORE_REAPPLY) as hold:
        async with ledger.started(config) as (client, lib):
            await hold.arrived()
            _pending(await client.get(f"/personas/{held['subject']}", headers=WRITE))
            other = await client.get(f"/personas/{held['other']}", headers=WRITE)
            assert other.status_code == 200, other.text
            listed = await client.get("/personas", headers=WRITE)
            assert [p["id"] for p in listed.json()["items"]] == [held["other"]]
            assert ledger.SUBJECT not in listed.text
            hold.release()
            assert lib._reconcile_task is not None
            await lib._reconcile_task
            gone = await client.get(f"/personas/{held['subject']}", headers=WRITE)
            assert gone.status_code == 404, gone.text


def test_every_catalog_method_is_gated() -> None:
    """#346: every public Engine coroutine method that can name a catalog entry, persona, media
    asset or upload session (and no story) refuses one a pending catalog-scope erasure
    reaches, deletes included; a story's methods are the story gate's."""
    names = {"character_id", "scenario_id", "persona_id", "media_id", "upload_id"}

    def names_an_entry(member: Any) -> bool:
        parameters = inspect.signature(member).parameters
        if "story_id" in parameters:
            return False
        return bool(names & parameters.keys()) or (
            "entity_type" in parameters and bool({"entity_id", "entry_id"} & parameters.keys())
        )

    members = {
        name: member
        for name, member in vars(Engine).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }
    missing = [
        name
        for name, member in members.items()
        if names_an_entry(member) and not getattr(member, CATALOG_GATED_ATTRIBUTE, False)
    ]
    assert missing == []
    for name in (
        "get_character",
        "get_companion",
        "get_scenario",
        "get_persona",
        "get_media",
        "get_media_content",
        "get_upload",
        "delete_character",
        "suggest_ratings",
        "list_relations",
    ):
        assert getattr(members[name], CATALOG_GATED_ATTRIBUTE, False), name
    assert not getattr(members["promote_character"], CATALOG_GATED_ATTRIBUTE, False)


def test_the_catalog_routes_state_the_pending_refusal() -> None:
    """#346: the committed ``docs/openapi.json`` lists 503 ``erasure_pending`` on the routes
    naming a catalog entry, persona or media asset, and ``POST /stories``; the lists that leave
    reached entries out may answer ``ledger_unavailable``."""
    schema = json.loads((Path(__file__).parents[2] / "docs" / "openapi.json").read_text())
    for path, method in (
        ("/catalog/characters/{character_id}", "get"),
        ("/catalog/characters/{character_id}", "delete"),
        ("/catalog/scenarios/{scenario_id}", "get"),
        ("/personas/{persona_id}", "get"),
        ("/media/{media_id}", "get"),
        ("/media/{media_id}/content", "get"),
        ("/stories", "post"),
        ("/admin/erasure", "post"),
    ):
        description = schema["paths"][path][method]["responses"]["503"]["description"]
        assert "`erasure_pending`" in description, (path, method)
    for path in ("/catalog/characters", "/catalog/scenarios", "/personas", "/media"):
        description = schema["paths"][path]["get"]["responses"]["503"]["description"]
        assert "`ledger_unavailable`" in description, path
