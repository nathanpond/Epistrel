"""I-10, the story lifecycle: create with an inline cast, archive and unarchive, delete."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel import Engine
from epistrel.config import Settings
from epistrel.core import stories as stories_module
from epistrel.core.concurrency import Decide
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.epistemic.roles import DEFAULT_ROLE
from epistrel.core.errors import StoryArchived, StoryBusy
from epistrel.core.events import Operation
from epistrel.core.stories import STORY_TABLES, StorySpec
from tests.conftest import REPO_ROOT
from tests.event_support import STORY_SETUP_KIND, play_turn, rows
from tests.story_support import READ, WRITE, count, create, rest, story_body

AUTHOR = RequestContext(principal="p-author")


def _unkeyed(body: dict[str, Any]) -> dict[str, Any]:
    """A mutation's body without the echoed ``Idempotency-Key`` (a fresh ULID per call)."""
    assert len(body.pop("idempotency_key")) == 26
    return body


async def _operations(engine: AsyncEngine, story_id: str) -> int:
    return await count(engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story_id)


async def _rows_left(engine: AsyncEngine, story_id: str) -> dict[str, int]:
    left: dict[str, int] = {}
    for table in STORY_TABLES:
        sql = f"SELECT count(*) FROM {table.name} WHERE story_id = :s"  # noqa: S608
        left[table.name] = await count(engine, sql, s=story_id)
    left["stories"] = await count(engine, "SELECT count(*) FROM stories WHERE id = :s", s=story_id)
    return left


@pytest.mark.part("M2", "FR-ADMIN-1")
async def test_i_10(engine: AsyncEngine, settings: Settings) -> None:
    """Create a story with two characters, archive it (reads still work, a turn is refused
    with 409 ``story_archived`` and no operation row), unarchive it, then delete it: its
    characters and every row of the story are gone. Complement: after unarchiving, a turn
    commits."""
    async with rest(settings, engine) as client:
        body = story_body("Ada", "Brann", settings={"turn_concurrency": "queue"})
        response = await client.post("/stories", json=body, headers=WRITE)
        assert response.status_code == 201, response.text
        created = response.json()
        story_id = created["story_id"]
        assert response.headers["location"] == f"/stories/{story_id}"
        assert [c["name"] for c in created["characters"]] == ["Ada", "Brann"]
        assert created["timeline_revision"] == 1
        assert set(created["content_rating"]) == {"id", "version_id", "name"}
        ada, brann = (c["id"] for c in created["characters"])

        # One setup operation; its story_created event names the cast but never an age.
        setup = await rows(
            engine,
            "SELECT o.kind, e.type, e.payload, o.principal FROM operations o "
            "JOIN events e ON e.operation_id = o.operation_id WHERE o.story_id = :s",
            s=story_id,
        )
        assert [(g.kind, g.type, g.principal) for g in setup] == [
            (STORY_SETUP_KIND.value, "story_created", "p-author")
        ]
        payload = setup[0].payload
        assert payload["characters"] == [
            {"id": ada, "name": "Ada", "is_player": True},
            {"id": brann, "name": "Brann", "is_player": False},
        ]
        assert "true_age" not in json.dumps(payload)
        assert payload["rating_version_id"] == created["content_rating"]["version_id"]
        cast = await rows(
            engine,
            "SELECT c.name, c.true_age, c.ordinal, c.age_recorded_at = e.tx_time AS at_setup "
            "FROM characters c JOIN events e ON e.story_id = c.story_id "
            "WHERE c.story_id = :s ORDER BY c.ordinal",
            s=story_id,
        )
        assert [tuple(c) for c in cast] == [("Ada", 20, 0, True), ("Brann", 21, 1, True)]

        story = (await client.get(f"/stories/{story_id}", headers=READ)).json()
        assert story["status"] == "active"
        assert (story["timeline_revision"], story["next_turn_index"]) == (1, 1)
        assert story["characters"] == [
            {"id": ada, "name": "Ada", "is_player": True},
            {
                "id": brann,
                "name": "Brann",
                "is_player": False,
                # #176: every non-player character's participation (the default).
                "participation": {"role": DEFAULT_ROLE, "fixed": False},
            },
        ]
        assert story["settings"]["turn_concurrency"] == "queue"
        assert story["settings"]["default_output_viewpoint"] == f"character:{ada}"
        omniscient = (
            await client.get(f"/stories/{story_id}?viewpoint=omniscient", headers=READ)
        ).json()
        assert [c["true_age"] for c in omniscient["characters"]] == [20, 21]
        listed = (await client.get("/stories?status=active", headers=READ)).json()["items"]
        assert story_id in {s["id"] for s in listed}

        # Archive: a control operation with no revision bump; idempotent.
        archived = await client.post(f"/stories/{story_id}/archive", headers=WRITE)
        assert archived.status_code == 200, archived.text
        assert _unkeyed(archived.json()) == {"status": "archived", "timeline_revision": 1}
        ops = await _operations(engine, story_id)
        again = await client.post(f"/stories/{story_id}/archive", headers=WRITE)
        assert _unkeyed(again.json()) == {"status": "archived", "timeline_revision": 1}
        assert await _operations(engine, story_id) == ops
        assert (await client.get(f"/stories/{story_id}", headers=READ)).json()["status"] == (
            "archived"
        )
        assert (await client.get(f"/stories/{story_id}/history", headers=READ)).status_code == 200
        archived_ids = {
            s["id"]
            for s in (await client.get("/stories?status=archived", headers=READ)).json()["items"]
        }
        assert story_id in archived_ids

        # A truth-changing call is refused before anything is written.
        with pytest.raises(StoryArchived) as refused:
            await play_turn(engine, story_id, "hello?")
        assert (refused.value.code, refused.value.http_status) == ("story_archived", 409)
        assert await _operations(engine, story_id) == ops
        # Maintenance still runs on an archived story.
        rebuilt = await client.post(f"/admin/stories/{story_id}/rebuild", headers=WRITE)
        assert rebuilt.status_code == 200, rebuilt.text

        # Complement: unarchived, the story plays again.
        unarchived = await client.post(f"/stories/{story_id}/unarchive", headers=WRITE)
        assert _unkeyed(unarchived.json()) == {"status": "active", "timeline_revision": 1}
        await play_turn(engine, story_id, "hello?")
        assert (await client.get(f"/stories/{story_id}", headers=READ)).json()[
            "timeline_revision"
        ] == 2

        # Delete: the story, its characters and every story-scoped row are gone.
        deleted = await client.delete(f"/stories/{story_id}", headers=WRITE)
        assert deleted.status_code == 204, deleted.text
        assert set((await _rows_left(engine, story_id)).values()) == {0}
        gone = await client.get(f"/stories/{story_id}", headers=READ)
        assert (gone.status_code, gone.json()["error"]["code"]) == (404, "not_found")
        assert (await client.delete(f"/stories/{story_id}", headers=WRITE)).status_code == 404
        record = await rows(
            engine,
            "SELECT o.story_id, o.kind, o.principal, e.category FROM events e "
            "JOIN operations o ON o.operation_id = e.operation_id "
            "WHERE e.type = 'story_deleted' AND e.payload->>'story_id' = :s",
            s=story_id,
        )
        assert [tuple(r) for r in record] == [(None, "control", "p-author", "audit")]


async def test_archive_and_delete_wait_for_work_in_flight(
    engine: AsyncEngine, settings: Settings
) -> None:
    """While a turn is in flight, archive and delete return 423 ``story_busy`` and write
    nothing; once it commits, both go through. A deleted story's archive is 404."""
    lib = Engine(settings, db=engine)
    async with rest(settings, engine) as client:
        story_id = (await create(client, story_body("Ada")))["story_id"]
        turn = Operation(engine, story_id=story_id, kind=OperationKind.TURN, principal="p")
        await turn.begin()
        ops = await _operations(engine, story_id)
        for call in (lib.archive_story, lib.delete_story):
            with pytest.raises(StoryBusy) as busy:
                await call(AUTHOR, story_id)
            assert (busy.value.http_status, busy.value.details["reason"]) == (423, "in_flight")
            assert busy.value.details["operation_id"] == turn.operation_id
        busy_rest = await client.post(f"/stories/{story_id}/archive", headers=WRITE)
        assert (busy_rest.status_code, busy_rest.json()["error"]["code"]) == (423, "story_busy")
        assert await _operations(engine, story_id) == ops
        turn.append("player_input", {"text": "hi"}, provenance=Provenance.PLAYER)
        await turn.commit()

        assert (await lib.archive_story(AUTHOR, story_id)).changed
        assert (await client.delete(f"/stories/{story_id}", headers=WRITE)).status_code == 204
        missing = await client.post(f"/stories/{story_id}/archive", headers=WRITE)
        assert missing.status_code == 404


async def test_create_refuses_bad_requests(engine: AsyncEngine, settings: Settings) -> None:
    """Negative: a character without ``true_age`` is 422 naming ``characters[0].true_age`` and
    creates no story; so do the other refusals (cast size, duplicate names, unknown settings,
    an omniscient or unknown default viewpoint, an unknown rating, a non-integer age)."""
    async with rest(settings, engine) as client:
        before = await count(engine, "SELECT count(*) FROM stories")
        operations_before = await count(engine, "SELECT count(*) FROM operations")

        no_age = story_body("Ada")
        del no_age["characters"][0]["true_age"]
        response = await client.post("/stories", json=no_age, headers=WRITE)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["field"] == "body.characters[0].true_age"

        cases: list[tuple[dict[str, object], str, str | None]] = []
        too_many = story_body(*(f"C{i}" for i in range(21)))
        cases.append((too_many, "validation_error", "body.characters"))
        cases.append((story_body(), "validation_error", None))
        cases[-1][0]["characters"] = []
        dup = story_body("Ada", "ADA")
        cases.append((dup, "validation_error", "body.characters[1].name"))
        cases.append(
            (story_body("Ada", settings={"bogus": 1}), "validation_error", "body.settings.bogus")
        )
        cases.append(
            (
                story_body("Ada", settings={"default_output_viewpoint": "omniscient"}),
                "validation_error",
                "body.settings.default_output_viewpoint",
            )
        )
        cases.append(
            (
                story_body("Ada", settings={"default_output_viewpoint": "Nobody"}),
                "viewpoint_unresolved",
                "body.settings.default_output_viewpoint",
            )
        )
        cases.append((story_body("Ada", content_rating_id="0" * 26), "rating_retired", None))
        old = story_body("Ada")
        old["characters"][0]["true_age"] = 151
        cases.append((old, "validation_error", "body.characters[0].true_age"))
        text_age = story_body("Ada")
        text_age["characters"][0]["true_age"] = "30"
        cases.append((text_age, "validation_error", "body.characters[0].true_age"))
        for body, code, field in cases:
            refused = await client.post("/stories", json=body, headers=WRITE)
            assert refused.status_code == 422, (body, refused.text)
            assert refused.json()["error"]["code"] == code
            if field is not None:
                assert refused.json()["error"]["details"]["field"] == field
        assert await count(engine, "SELECT count(*) FROM stories") == before
        assert await count(engine, "SELECT count(*) FROM operations") == operations_before


async def test_default_viewpoint_rules(engine: AsyncEngine, settings: Settings) -> None:
    """The default viewpoint resolves a character name (any case) to its id; with no name it
    is the sole player character, and with two or none it stays unset."""
    async with rest(settings, engine) as client:
        named = await create(
            client, story_body("Ada", "Brann", settings={"default_output_viewpoint": "brann"})
        )
        two = story_body("Ada", "Brann")
        two["characters"][1]["is_player"] = True
        both = await create(client, two)
        none = story_body("Ada")
        none["characters"][0]["is_player"] = False
        nobody = await create(client, none)
        found = {}
        for created in (named, both, nobody):
            story = await client.get(f"/stories/{created['story_id']}", headers=READ)
            found[created["story_id"]] = story.json()["settings"]["default_output_viewpoint"]
        brann = named["characters"][1]["id"]
        assert found == {
            named["story_id"]: f"character:{brann}",
            both["story_id"]: None,
            nobody["story_id"]: None,
        }


async def test_library_create_and_failed_genesis(
    engine: AsyncEngine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library creates the same story as REST (``Engine.create_story``) and stores each
    character's ``safe_fallback_text`` for #68. A genesis commit that fails leaves no story,
    no character and no operation behind."""
    lib = Engine(settings, db=engine)
    spec = StorySpec.model_validate(
        {
            "name": "Library story",
            "characters": [
                {
                    "name": "Ada",
                    "definition": "A courier.",
                    "true_age": 34,
                    "safe_fallback_text": "Ada shrugs.",
                }
            ],
        }
    )
    created = await lib.create_story(AUTHOR, spec)
    fallback = await rows(
        engine, "SELECT safe_fallback_text FROM characters WHERE story_id = :s", s=created.story_id
    )
    assert [r[0] for r in fallback] == ["Ada shrugs."]

    class FailingGenesis(Operation):
        async def commit(self, decide: Decide | None = None) -> None:
            async def induced(conn: AsyncConnection) -> None:
                raise RuntimeError("induced genesis failure")

            self.add_commit_step(induced)
            await super().commit(decide)

    monkeypatch.setattr(stories_module, "Operation", FailingGenesis)
    totals = [
        await count(engine, f"SELECT count(*) FROM {table}")  # noqa: S608
        for table in ("stories", "characters", "operations", "events")
    ]
    with pytest.raises(RuntimeError, match="induced"):
        await lib.create_story(AUTHOR, spec)
    assert [
        await count(engine, f"SELECT count(*) FROM {table}")  # noqa: S608
        for table in ("stories", "characters", "operations", "events")
    ] == totals


# -- M7: story export (#249) ----------------------------------------------------------------------


@pytest.mark.part("M7", "FR-STORE-7; FR-ADMIN-4")
async def test_i_10__m7_export_contents(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """A story with three turns is exported (202, then the job): the
    archive's first member is ``manifest.json`` (format ``1.0.0``, the source story, the
    revision, each file's SHA-256 matching), every line is canonical JSON, every committed
    operation and event of the story is present, and no member is a projection, an index, a
    job, an idempotency result or the audit log. Two exports of one revision are identical but
    for the manifest's ``exported_at``/``exported_by``. Downloads before the archive is
    written are 409 ``export_not_ready``."""
    import hashlib

    from sqlalchemy import text

    from epistrel.core import bundles

    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        for turn in range(3):
            await play_turn(engine, story, f"Line {turn}.", principal="p-export")
        started = await client.post(f"/admin/stories/{story}/export", headers=WRITE)
        assert started.status_code == 202, started.text
        job = started.json()["job_id"]
        assert started.headers["Location"].endswith(job)
        early = await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)
        assert early.status_code == 409, early.text
        assert early.json()["error"]["code"] == "export_not_ready"
        await bundles.run_export(engine, job, tmp_path)
        shown = await client.get(f"/admin/stories/exports/{job}", headers=READ)
        assert shown.json()["status"] == "ready", shown.text
        got = await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)
        assert got.status_code == 200
        assert got.headers["content-type"] == "application/gzip"
        members = bundles.read_archive(got.content)
        assert members[0][0] == "manifest.json"
        manifest = json.loads(members[0][1])
        assert (manifest["format_version"], manifest["source_story_id"]) == ("1.0.0", story)
        by_path = dict(members[1:])
        for entry in manifest["files"]:
            assert hashlib.sha256(by_path[entry["path"]]).hexdigest() == entry["sha256"]
        events = [json.loads(line) for line in by_path["events.jsonl"].splitlines()]
        stored = await rows(engine, "SELECT id FROM events WHERE story_id = :s", s=story)
        assert {e["id"] for e in events} == {str(r.id) for r in stored}
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
        names = set(by_path)
        for never in ("jobs", "idempotency_keys", "audit_log", "subject_references", "story_heads"):
            assert f"records/{never}.jsonl" not in names
        again = await client.post(f"/admin/stories/{story}/export", headers=WRITE)
        second = again.json()["job_id"]
        await bundles.run_export(engine, second, tmp_path)
        other = bundles.read_archive(
            (await client.get(f"/admin/stories/exports/{second}/bundle", headers=READ)).content
        )
        assert [m for m in other[1:]] == members[1:]
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE kind = 'story_export'"))


@pytest.mark.part("M7", "FR-STORE-7")
def test_bundle_secret_scan() -> None:
    """#249 (NFR-SEC-3): a member holding a configured secret, raw, in base64 or in hex, is
    found; a clean archive passes."""
    from epistrel.core.bundles import scan

    secret = "canary-secret-value-123"
    for form in (
        secret.encode(),
        base64.b64encode(secret.encode()),
        secret.encode().hex().encode(),
    ):
        assert scan([("a.jsonl", b"x" + form + b"y")], [secret]) == "a.jsonl"
    assert scan([("a.jsonl", b"nothing here")], [secret]) is None


@pytest.mark.part("M7", "FR-STORE-7")
def test_bundle_tables_coverage() -> None:
    """Every table with a ``story_id`` column is classified for a bundle."""
    from epistrel.core.bundles import classify, story_tables

    assert [t.name for t in story_tables() if classify(t.name) is None] == []


# -- M7: story import (#250) ----------------------------------------------------------------------


def _tar(members: list[tuple[str, bytes]], *, kind: bytes = b"0") -> bytes:
    """A gzip-compressed tar of ``members`` (``kind``: the tar member type of each)."""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.type = kind
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def _exported(client: Any, engine: AsyncEngine, story: str, tmp_path: Any) -> bytes:
    from epistrel.core import bundles

    started = await client.post(f"/admin/stories/{story}/export", headers=WRITE)
    assert started.status_code == 202, started.text
    job = started.json()["job_id"]
    await bundles.run_export(engine, job, tmp_path)
    got = await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)
    assert got.status_code == 200, got.text
    return bytes(got.content)


class _Imported:
    """An import through REST (#250): the request's answer and, once its job ran, its state."""

    def __init__(self, started: Any, status: dict[str, Any] | None) -> None:
        self.started = started
        self.status = status

    @property
    def ready(self) -> bool:
        return self.status is not None and self.status["status"] == "ready"

    @property
    def story_id(self) -> str:
        return str(self.started.json()["story_id"])

    @property
    def error(self) -> dict[str, Any]:
        """The refusal: the request's (up front) or the job's."""
        if self.status is None:
            found: dict[str, Any] = self.started.json()["error"]
            return found
        assert self.status["error"] is not None, self.status
        return dict(self.status["error"])


async def _import(
    client: Any,
    engine: AsyncEngine,
    data: bytes,
    headers: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> _Imported:
    """POST the bundle as #250's multipart request, then run its ``story_import`` job and read
    its state."""
    started = await client.post(
        "/admin/stories/import",
        files={
            "metadata": (None, json.dumps(metadata or {}), "application/json"),
            "file": ("story.epistrel.tgz", data, "application/gzip"),
        },
        headers={**WRITE, **(headers or {})},
    )
    if started.status_code != 202:
        return _Imported(started, None)
    job = started.json()["job_id"]
    await _run_import_job(engine, job)
    state = await client.get(f"/admin/stories/imports/{job}", headers=READ)
    assert state.status_code == 200, state.text
    return _Imported(started, state.json())


async def _run_import_job(engine: AsyncEngine, bundle_id: str) -> None:
    """Claim the import's ``story_import`` job and run it through a worker (as the deployment's
    worker would), until it is no longer queued."""
    from datetime import timedelta

    from sqlalchemy import text

    from epistrel.core import clock
    from epistrel.orchestrator.jobs.types import ClaimedJob
    from tests.offboarding_support import worker

    runner = worker(engine)
    for _ in range(5):
        now = clock.now()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE jobs SET status = 'running', lease_until = :lease, "
                        "attempt = attempt + 1, worker_id = 'w:test:0', updated_at = :now "
                        "WHERE id = (SELECT job_id FROM bundle_jobs WHERE id = :b) "
                        "AND status = 'queued' RETURNING *"
                    ),
                    {"b": bundle_id, "now": now, "lease": now + timedelta(seconds=60)},
                )
            ).one_or_none()
        if row is None:
            return
        await runner.execute(
            ClaimedJob(
                id=row.id,
                kind=row.kind,
                story_id=row.story_id,
                payload=dict(row.payload or {}),
                attempt=int(row.attempt),
                failures=int(row.failures),
                due_at=row.due_at,
                lease_until=row.lease_until,
                worker_id=row.worker_id,
            )
        )


async def _nothing_left(engine: AsyncEngine, stories_before: int) -> None:
    """#250's complement after a refusal: no story, no import job queued or running."""
    assert await count(engine, "SELECT count(*) FROM stories") == stories_before
    assert (
        await count(
            engine,
            "SELECT count(*) FROM jobs WHERE kind = 'story_import' "
            "AND status IN ('queued', 'running')",
        )
        == 0
    )


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7; FR-ADMIN-4")
async def test_i_10__m7_import_round_trip(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """A story with three turns is exported and imported (202, then its job makes it ready):
    the new story has a new id,
    the same number of operations, events and characters, no id of the source, the original
    recording times, and the source's transcript; the source is untouched. Two imports of one
    bundle give two independent stories; repeating a request with the same ``Idempotency-Key``
    returns the first. A turn on the import commits."""
    from sqlalchemy import text

    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        for turn in range(3):
            await play_turn(engine, story, f"Line {turn}.", principal="p-export")
        data = await _exported(client, engine, story, tmp_path)
        before = await _rows_left(engine, story)

        key = {"Idempotency-Key": "01JB0000000000000000000IMP"}
        first = await _import(client, engine, data, key)
        assert first.started.status_code == 202, first.started.text
        assert first.started.headers["Location"].startswith("/admin/stories/imports/")
        assert first.ready, first.status
        imported = first.story_id
        assert imported != story
        again = await _import(client, engine, data, key)
        assert again.started.status_code == 202
        assert again.story_id == imported
        assert again.started.headers.get("Idempotent-Replay") == "true"
        second = await _import(client, engine, data)
        assert second.ready, second.status
        assert second.story_id not in (story, imported)
        (origin,) = await rows(
            engine,
            "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'import'",
            s=imported,
        )
        assert origin.turn_report["imported_from"]["source_story_id"] == story

        for table in ("operations", "events", "characters"):
            sql = f"SELECT count(*) FROM {table} WHERE story_id = :s"  # noqa: S608
            # #705: the import adds its own ``import`` operation.
            extra = 1 if table == "operations" else 0
            assert await count(engine, sql, s=imported) == before[table] + extra, table
        source_ids = {
            str(r.id)
            for r in await rows(engine, "SELECT id FROM events WHERE story_id = :s", s=story)
        }
        new_ids = {
            str(r.id)
            for r in await rows(engine, "SELECT id FROM events WHERE story_id = :s", s=imported)
        }
        assert not source_ids & new_ids
        times = await rows(
            engine,
            "SELECT tx_time FROM events WHERE story_id = :s ORDER BY seq",
            s=story,
        )
        imported_times = await rows(
            engine,
            "SELECT tx_time FROM events WHERE story_id = :s ORDER BY seq",
            s=imported,
        )
        assert [r.tx_time for r in times] == [r.tx_time for r in imported_times]
        dumped = await rows(
            engine,
            "SELECT payload::text AS p FROM events WHERE story_id = :s",
            s=imported,
        )
        assert not any(story in r.p for r in dumped)
        source = await client.get(f"/stories/{story}/history", headers=READ)
        target = await client.get(f"/stories/{imported}/history", headers=READ)
        assert source.status_code == target.status_code == 200, target.text

        def texts(body: dict[str, Any]) -> list[Any]:
            return [(i["turn"], i["kind"], i["text"]) for i in body["items"]]

        assert texts(target.json()) == texts(source.json())
        assert len(texts(source.json())) >= 3
        assert await _rows_left(engine, story) == before
        await play_turn(engine, imported, "One more.", principal="p-import")
        assert await _rows_left(engine, story) == before
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE kind = 'story_export'"))


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7")
async def test_i_10__m7_import_bad_bundles(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """Bad bundles are refused and leave no story: a checksum mismatch, a member outside the
    archive (a climbing path, a symlink), an unreadable archive, a manifest missing its format,
    a member not in the manifest and a line that is not a JSON object are ``corrupt_bundle``; a
    newer major format is ``unsupported_format_version``; a highly compressed member past the
    per-entry limit is ``bundle_too_large``, caught while decompressing."""
    import hashlib

    from sqlalchemy import text

    from epistrel.core import bundles
    from epistrel.core.bundles import importing

    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        await play_turn(engine, story, "Line.", principal="p-export")
        members = bundles.read_archive(await _exported(client, engine, story, tmp_path))
        manifest = json.loads(members[0][1])

        def rebuilt(changed: dict[str, Any], body: list[tuple[str, bytes]]) -> bytes:
            return _tar([("manifest.json", json.dumps({**manifest, **changed}).encode()), *body])

        tampered = [(n, d + b"\n" if n == "events.jsonl" else d) for n, d in members[1:]]
        bad_line = [(n, b"[1]\n" if n == "events.jsonl" else d) for n, d in members[1:]]
        files = [
            {**f, "sha256": hashlib.sha256(b"[1]\n").hexdigest()}
            if f["path"] == "events.jsonl"
            else f
            for f in manifest["files"]
        ]
        cases: list[tuple[bytes, str, str | None]] = [
            (rebuilt({}, tampered), "corrupt_bundle", "checksum_mismatch"),
            (
                rebuilt({}, [*members[1:], ("../evil", b"x")]),
                "corrupt_bundle",
                "entry_outside_archive",
            ),
            (
                _tar([("manifest.json", members[0][1])], kind=b"2"),
                "corrupt_bundle",
                "entry_outside_archive",
            ),
            (b"not a gzip stream", "corrupt_bundle", "unreadable_archive"),
            (rebuilt({"format_version": None}, members[1:]), "corrupt_bundle", "manifest_invalid"),
            (
                rebuilt({}, [*members[1:], ("extra.jsonl", b"")]),
                "corrupt_bundle",
                "unexpected_entry",
            ),
            (rebuilt({"files": files}, bad_line), "corrupt_bundle", "schema_violation"),
            (rebuilt({"format_version": "2.0.0"}, members[1:]), "unsupported_format_version", None),
        ]
        stories_before = await count(engine, "SELECT count(*) FROM stories")
        for data, code, reason in cases:
            refused = await _import(client, engine, data)
            assert refused.error["code"] == code, (reason, refused.error)
            if reason is not None:
                assert refused.error["details"]["reason"] == reason, refused.error
            if refused.status is not None:
                # Found by the job: the import is refused, its story gone, its record kept.
                assert (refused.status["status"], refused.status["story_id"]) == (
                    "refused",
                    None,
                )
                assert refused.status["report"]["errors"], refused.status
            await _nothing_left(engine, stories_before)
        # The staged uploads are deleted with the refusals (their records keep no path).
        assert (
            await count(
                engine,
                "SELECT count(*) FROM bundle_jobs WHERE kind = 'import' "
                "AND status IN ('refused', 'failed') AND path IS NOT NULL",
            )
            == 0
        )
        # #250: the limits are the deployment's (here the per-entry one at its 1 MiB minimum).
        operator = {**WRITE, "X-Epistrel-Principal": "p-operator"}
        lowered = await client.put(
            "/admin/settings", json={"bundle_max_entry_bytes": 1024 * 1024}, headers=operator
        )
        assert lowered.status_code == 200, lowered.text
        try:
            bomb = _tar([("manifest.json", b"\0" * (1024 * 1024 + 1))])
            assert len(bomb) < 100_000  # it inflates more than tenfold
            too_large = await _import(client, engine, bomb)
            assert too_large.started.status_code == 413, too_large.started.text
            assert too_large.error["details"]["limit"] == "bundle_max_entry_bytes"
        finally:
            restored = await client.put(
                "/admin/settings",
                json={"bundle_max_entry_bytes": importing.MAX_ENTRY_BYTES},
                headers=operator,
            )
            assert restored.status_code == 200, restored.text
        await _nothing_left(engine, stories_before)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE kind = 'story_export'"))


# -- M7: export follow-ups (#704) -----------------------------------------------------------------


async def _export(client: Any, engine: AsyncEngine, story: str, tmp_path: Any) -> str:
    from epistrel.core import bundles
    from tests import bundle_support as bs

    started = await client.post(f"/admin/stories/{story}/export", headers=WRITE)
    assert started.status_code == 202, started.text
    job = str(started.json()["job_id"])
    await bundles.run_export(engine, job, tmp_path, blobs=bs.read_blob)
    return job


@pytest.mark.part("M7", "FR-STORE-7; FR-ADMIN-4")
async def test_i_10__m7_export_rich(settings: Settings, engine: AsyncEngine, tmp_path: Any) -> None:
    """#704: the rich story's archive: members in the format's order (the manifest, the story,
    ``media.jsonl``, ``catalog/``, operations, events, the other records, ``media/<sha256>``);
    the manifest's ``content_rating`` and ``engine_profile`` blocks and ``counts.media``; the
    character's image as one ``media.jsonl`` line and its bytes; every line of every member
    valid against the bundle JSON Schema published in ``docs/openapi.json``; the discarded
    tail's operations present with their membership. Complement: no memory line carries an
    ``embedding``, and a missing blob fails the export ``media_missing`` with no file."""
    from epistrel.core import bundles
    from epistrel.core.bundles.format import bundle_schemas, validate_line
    from tests import bundle_support as bs

    async with rest(settings, engine) as client:
        rich = await bs.build_rich_story(client, engine)
        job = await _export(client, engine, rich.story_id, tmp_path)
        got = await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)
        assert got.status_code == 200, got.text
        opened = bs.open_bundle(got.content)
        names = list(opened.members)
        assert names[:3] == [
            "records/story.json",
            "media.jsonl",
            "catalog/character_snapshots.jsonl",
        ]
        assert names.index("records/operations.jsonl") < names.index("events.jsonl")
        assert names[-1] == f"media/{bs.IMAGE_SHA}"
        assert opened.members[f"media/{bs.IMAGE_SHA}"] == bs.IMAGE
        (media,) = opened.lines("media.jsonl")
        assert (media["id"], media["sha256"]) == (rich.media_id, bs.IMAGE_SHA)
        manifest = opened.manifest
        assert manifest["counts"]["media"] == 1
        assert manifest["content_rating"]["id"]
        assert manifest["engine_profile"]["version"] >= 1
        validate_line("manifest.json", manifest)
        for path in names:
            if path.startswith("media/"):
                continue
            for line in opened.lines(path):
                validate_line(path, line)
        published = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text())
        assert set(bundle_schemas()) <= set(published["components"]["schemas"])
        ops = {o["operation_id"]: o["membership"] for o in opened.lines("records/operations.jsonl")}
        assert {ops[o] for o in rich.discarded} == {"discarded"}
        for memory in opened.lines("records/memories.jsonl"):
            assert "embedding" not in memory

        bs.BLOBS.pop(bs.IMAGE_SHA)
        try:
            missing = await client.post(f"/admin/stories/{rich.story_id}/export", headers=WRITE)
            failing = str(missing.json()["job_id"])
            await bundles.run_export(engine, failing, tmp_path, blobs=bs.read_blob)
            shown = (await client.get(f"/admin/stories/exports/{failing}", headers=READ)).json()
            assert shown["status"] == "failed"
            assert shown["error"]["reason"] == "media_missing"
            assert not (tmp_path / f"{failing}.epistrel.tgz").exists()
        finally:
            bs.BLOBS[bs.IMAGE_SHA] = bs.IMAGE


@pytest.mark.part("M7", "FR-ADMIN-9")
async def test_i_10__m7_export_erasure_race(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """#704: an erasure of the story lands while its export is held after the snapshot: the
    export restarts and the finished archive carries the new ledger entry; ``attempts`` is 2.
    Complement: an erasure in another story does not restart it."""
    import asyncio

    from sqlalchemy import text

    from epistrel.core import bundles
    from epistrel.core.ids import new_ulid
    from epistrel.testing import pauses
    from tests import bundle_support as bs

    async def ledger(conn: AsyncConnection, story_ref: str) -> str:
        entry = new_ulid()
        await conn.execute(
            text(
                "INSERT INTO erasure_ledger (id, subject_kind, subject_id, scope, story_ref, "
                "principal, counts) VALUES (:i, 'character', :sub, 'story', :s, 'p-op', "
                "'{}'::jsonb)"
            ),
            {"i": entry, "sub": new_ulid(), "s": story_ref},
        )
        return entry

    async with rest(settings, engine) as client:
        rich = await bs.build_rich_story(client, engine)
        other = (await create(client, story_body("Wren", "Ada")))["story_id"]
        for target, restarts in ((other, False), (rich.story_id, True)):
            started = await client.post(f"/admin/stories/{rich.story_id}/export", headers=WRITE)
            job = str(started.json()["job_id"])
            with pauses.held(pauses.BUNDLE_EXPORT_AFTER_SNAPSHOT) as hold:
                task = asyncio.ensure_future(
                    bundles.run_export(engine, job, tmp_path, blobs=bs.read_blob)
                )
                await hold.arrived()
                async with engine.begin() as conn:
                    entry = await ledger(conn, target)
            await task
            shown = (await client.get(f"/admin/stories/exports/{job}", headers=READ)).json()
            assert shown["status"] == "ready", shown
            (attempts,) = await rows(
                engine, "SELECT attempts FROM bundle_jobs WHERE id = :j", j=job
            )
            assert attempts.attempts == (2 if restarts else 1)
            got = await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)
            opened = bs.open_bundle(got.content)
            carried = {e["id"] for e in opened.lines("records/erasure_ledger.jsonl")}
            assert (entry in carried) is restarts


@pytest.mark.part("M7", "FR-STORE-30")
async def test_i_10__m7_export_lifecycle(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """#704: an archive past ``bundle_export_ttl_hours`` is expired by the retention sweep
    (410 ``export_expired``, the file gone); a story-scope erasure expires a stored archive
    (error ``erased``, the file gone) and leaves another story's; deleting a story deletes its
    exports and their files."""
    from datetime import timedelta
    from typing import cast

    from epistrel.core import bundles, clock
    from epistrel.core.erasure.erase import ERASURE_TARGETS
    from tests import bundle_support as bs

    async with rest(settings, engine) as client:
        rich = await bs.build_rich_story(client, engine)
        other = (await create(client, story_body("Wren", "Ada")))["story_id"]
        aged = await _export(client, engine, rich.story_id, tmp_path)
        (path,) = await rows(engine, "SELECT path FROM bundle_jobs WHERE id = :j", j=aged)
        assert await _exists(path.path)
        assert await bundles.expire_bundles(engine, clock.now() + timedelta(hours=1)) == 0
        assert await bundles.expire_bundles(engine, clock.now() + timedelta(hours=25)) >= 1
        gone = await client.get(f"/admin/stories/exports/{aged}/bundle", headers=READ)
        assert gone.status_code == 410, gone.text
        assert not await _exists(path.path)

        stored = await _export(client, engine, rich.story_id, tmp_path)
        kept = await _export(client, engine, other, tmp_path)
        (target,) = [t for t in ERASURE_TARGETS if t.name == "story_exports"]
        async with engine.begin() as conn:
            assert (
                await target.erase(
                    conn, story_id=rich.story_id, redactor=cast(Any, None), write=True
                )
                == 1
            )
        shown = (await client.get(f"/admin/stories/exports/{stored}", headers=READ)).json()
        assert (shown["status"], shown["error"]) == ("expired", {"code": "erased"})
        assert (await client.get(f"/admin/stories/exports/{kept}", headers=READ)).json()[
            "status"
        ] == "ready"

        (kept_path,) = await rows(engine, "SELECT path FROM bundle_jobs WHERE id = :j", j=kept)
        deleted = await client.delete(f"/stories/{other}", headers=WRITE)
        assert deleted.status_code in (200, 204), deleted.text
        assert await count(engine, "SELECT count(*) FROM bundle_jobs WHERE id = :j", j=kept) == 0
        assert not await _exists(kept_path.path)


@pytest.mark.part("M7", "FR-STORE-7; FR-ADMIN-4")
async def test_i_10__m7_export_snapshot(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """#704 (FR-CONC-10): an export runs while 20 turns commit on the story: the archive is the
    story at exactly its ``exported_revision``, never a mix: every committed operation at or
    below the revision is in it with all its events, and none above it."""
    import asyncio

    from epistrel.core import bundles
    from tests import bundle_support as bs

    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        await play_turn(engine, story, "First.", principal="p-export")
        started = await client.post(f"/admin/stories/{story}/export", headers=WRITE)
        job = str(started.json()["job_id"])

        async def turns() -> None:
            for n in range(20):
                await play_turn(engine, story, f"Busy {n}.", principal="p-export")

        await asyncio.gather(turns(), bundles.run_export(engine, job, tmp_path))
        got = await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)
        opened = bs.open_bundle(got.content)
        revision = opened.manifest["exported_revision"]
        exported = {o["operation_id"] for o in opened.lines("records/operations.jsonl")}
        stored = await rows(
            engine,
            "SELECT operation_id, timeline_revision FROM operations WHERE story_id = :s "
            "AND status = 'committed'",
            s=story,
        )
        at_or_below = {
            str(r.operation_id)
            for r in stored
            if r.timeline_revision is not None and r.timeline_revision <= revision
        }
        above = {
            str(r.operation_id)
            for r in stored
            if r.timeline_revision is not None and r.timeline_revision > revision
        }
        assert at_or_below <= exported
        assert not (above & exported)
        events = {e["operation_id"] for e in opened.lines("events.jsonl")}
        assert events <= exported


async def _exists(path: str) -> bool:
    import asyncio
    import os

    return await asyncio.to_thread(os.path.exists, path)


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7; FR-ADMIN-4")
async def test_i_10__m7_import_follow_ups(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """#705: the rich story (a character image, a discarded tail) exported and imported.

    - **Media.** The image is a new asset of the importer (a new id, the same checksum), and
      the imported character's image names it.
    - **The import operation.** One committed ``import`` operation records ``imported_from``
      (the source, the exported revision, the format version, the bundle's SHA-256); the
      story's revision is the exported one plus one; the embed job is queued.
    - **Validation.** A record line with a field its table lacks, and an event whose payload
      breaks its type, are ``schema_violation`` (with the member, the line and the field); two
      members swapped are ``member_order``; none of them leaves a story.
    - **A newer MINOR** of the format with that extra field imports, the field listed under
      ``warnings``."""
    import hashlib

    from epistrel.core import bundles
    from tests import bundle_support as bs

    async with rest(settings, engine) as client:
        rich = await bs.build_rich_story(client, engine)
        job = await _export(client, engine, rich.story_id, tmp_path)
        data = (await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)).content
        members = bundles.read_archive(data)
        manifest = json.loads(members[0][1])

        made = await _import(client, engine, data)
        assert made.ready, made.status
        imported = made.story_id
        (images,) = await rows(
            engine,
            "SELECT images FROM characters WHERE story_id = :s AND name = 'Ada'",
            s=imported,
        )
        (image,) = images.images
        assert image["media_id"] != rich.media_id
        (asset,) = await rows(
            engine,
            "SELECT owner_principal, checksum FROM media_assets WHERE id = :m",
            m=image["media_id"],
        )
        assert (asset.owner_principal, asset.checksum) == (
            WRITE["X-Epistrel-Principal"],
            bs.IMAGE_SHA,
        )
        (op,) = await rows(
            engine,
            "SELECT operation_id, turn_report, timeline_revision FROM operations "
            "WHERE story_id = :s AND kind = 'import'",
            s=imported,
        )
        assert op.turn_report["imported_from"] == {
            "source_story_id": rich.story_id,
            "exported_revision": manifest["exported_revision"],
            "format_version": manifest["format_version"],
            "bundle_sha256": hashlib.sha256(data).hexdigest(),
        }
        revision = await count(
            engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=imported
        )
        assert revision == manifest["exported_revision"] + 1
        assert await count(
            engine,
            "SELECT count(*) FROM jobs WHERE story_id = :s AND kind = 'embed'",
            s=imported,
        )

        def rebuilt(body: list[tuple[str, bytes]], **changed: Any) -> bytes:
            files = [
                {**f, "sha256": hashlib.sha256(dict(body)[f["path"]]).hexdigest()}
                if f["path"] in dict(body)
                else f
                for f in manifest["files"]
            ]
            head = {**manifest, "files": files, **changed}
            return _tar([("manifest.json", json.dumps(head).encode()), *body])

        def changed(path: str, edit: Any) -> list[tuple[str, bytes]]:
            out = []
            for name, body in members[1:]:
                if name == path:
                    lines = [json.loads(x) for x in body.splitlines() if x]
                    lines[0] = edit(lines[0])
                    body = b"".join(json.dumps(x).encode() + b"\n" for x in lines)
                out.append((name, body))
            return out

        extra = changed("records/characters.jsonl", lambda r: {**r, "nickname": "x"})
        broken = changed(
            "events.jsonl", lambda r: {**r, "payload": {"not_a_field": 1}, "type": "player_input"}
        )
        swapped = list(members[1:])
        story_at = next(n for n, (p, _) in enumerate(swapped) if p == "records/story.json")
        swapped.insert(len(swapped) - 1, swapped.pop(story_at))
        stories_before = await count(engine, "SELECT count(*) FROM stories")
        for bundle, reason, entry, field_ in (
            (rebuilt(extra), "schema_violation", "records/characters.jsonl", None),
            (rebuilt(broken), "schema_violation", "events.jsonl", "payload"),
            (rebuilt(swapped), "member_order", None, None),
        ):
            refused = await _import(client, engine, bundle)
            assert refused.error["code"] == "corrupt_bundle", refused.error
            details = refused.error["details"]
            assert details["reason"] == reason, details
            if entry is not None:
                assert (details["entry"], details["line"]) == (entry, 1), details
            if field_ is not None:
                assert details["field"] == field_, details
        assert await count(engine, "SELECT count(*) FROM stories") == stories_before

        newer = await _import(client, engine, rebuilt(extra, format_version="1.9.0"))
        assert newer.ready, newer.status
        assert newer.status is not None
        assert {
            "code": "unknown_fields",
            "entry": "records/characters.jsonl",
            "fields": ["nickname"],
        } in newer.status["report"]["warnings"]


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-RATE-7")
async def test_i_10__m7_import_unchecked(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """I-10 M7 (FR-RATE-7, #250's import rule, #251's gate): no imported stamp is trusted: the
    active history is checked at import and stamped with this deployment's set, and the
    inactive turns keep none, so every turn an untruncate of the imported story's discarded
    tail would reactivate is unchecked (#120's guard holds that edit for a rating check), while
    the source's are not."""
    from tests import bundle_support as bs

    async with rest(settings, engine) as client:
        rich = await bs.build_rich_story(client, engine)
        job = await _export(client, engine, rich.story_id, tmp_path)
        data = (await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)).content
        made = await _import(client, engine, data)
        assert made.ready, made.status
        imported = made.story_id

        async def stamps(story: str, membership: str) -> list[Any]:
            return [
                r.checked_guardrails
                for r in await rows(
                    engine,
                    "SELECT checked_guardrails FROM operations WHERE story_id = :s "
                    "AND kind = 'turn' AND membership::text = :m",
                    s=story,
                    m=membership,
                )
            ]

        assert all(s is not None for s in await stamps(rich.story_id, "active"))
        # #251: the active history passed this deployment's check at import and is stamped;
        # the inactive turns were never checked here.
        assert await stamps(imported, "active")
        assert all(s is not None for s in await stamps(imported, "active"))
        assert await stamps(imported, "discarded")
        assert all(s is None for s in await stamps(imported, "discarded"))
        from epistrel.core.ratings import reactivation

        async def pending(story: str) -> list[Any]:
            async with engine.connect() as conn:
                _, _, current = await reactivation.current_set(conn, story)
                planned = await reactivation.activates(
                    conn,
                    reactivation.Continuation(
                        operation=reactivation.Edit.UNTRUNCATE,
                        story_id=story,
                        principal="p-import",
                    ),
                )
                assert planned
                return await reactivation.unchecked(conn, story, planned, current)

        # The source's tail passed this deployment's guardrails; the imported one did not
        # (here), so its untruncate is held for the reactivation check.
        assert await pending(rich.story_id) == []
        assert len(await pending(imported)) == len(rich.discarded)


# -- M7: the import job and its gates (#250, #251) ---------------------------------------------


async def _started(client: Any, data: bytes, metadata: dict[str, Any] | None = None) -> Any:
    """The import request alone (its job not run)."""
    return await client.post(
        "/admin/stories/import",
        files={
            "metadata": (None, json.dumps(metadata or {}), "application/json"),
            "file": ("story.epistrel.tgz", data, "application/gzip"),
        },
        headers=WRITE,
    )


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7")
async def test_i_10__m7_importing(settings: Settings, engine: AsyncEngine, tmp_path: Any) -> None:
    """#250: while the import job has not run, the story exists ``importing``: its routes are
    409 ``story_importing`` with the job, its stage and its progress (a read, a turn, a
    history read, its deletion), and nothing is written; an operation on it is refused at
    ``begin``; its status route answers ``queued``. Complement: another story answers
    normally, and once the job ran the story is served."""
    from epistrel.core.errors import StoryImporting
    from tests.respond_support import body as respond_body

    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        await play_turn(engine, story, "Line.", principal="p-export")
        data = await _exported(client, engine, story, tmp_path)
        started = await _started(client, data)
        assert started.status_code == 202, started.text
        imported, job = started.json()["story_id"], started.json()["job_id"]
        operations = await _operations(engine, imported)
        cast = await rows(engine, "SELECT id FROM characters WHERE story_id = :s", s=imported)
        for method, path, body in (
            ("GET", f"/stories/{imported}", None),
            ("GET", f"/stories/{imported}/history", None),
            ("POST", f"/stories/{imported}/respond", respond_body(story, "Hi.")),
            ("DELETE", f"/stories/{imported}", None),
        ):
            refused = await client.request(method, path, json=body, headers=WRITE)
            assert refused.status_code == 409, (path, refused.text)
            error = refused.json()["error"]
            assert error["code"] == "story_importing", (path, error)
            assert error["details"]["job_id"] == job
            assert error["details"]["stage"] == "queued"
        assert cast == []
        assert await _operations(engine, imported) == operations
        with pytest.raises(StoryImporting):
            async with Operation(
                engine, story_id=imported, kind=OperationKind.CONTROL, principal="p-x"
            ) as op:
                await op.commit()
        state = await client.get(f"/admin/stories/imports/{job}", headers=READ)
        assert (state.json()["status"], state.json()["story_id"]) == ("queued", imported)
        assert (await client.get(f"/stories/{story}", headers=READ)).status_code == 200
        await _run_import_job(engine, job)
        served = await client.get(f"/stories/{imported}", headers=READ)
        assert served.status_code == 200, served.text
        assert served.json()["status"] == "active"


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7; FR-RATE-7")
async def test_i_10__m7_import_guardrails(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """#251: the whole active history is checked against the chosen rating's guardrails (a
    global policy among them) before the story is served: a history whose player line breaks a
    global policy is refused ``rating_check_failed`` with the violation listed (its position
    and policy), and leaves no story. Complement: once the policy is retired, the same bundle
    imports and its active turns carry this deployment's stamp."""
    from tests.policy_support import unique

    operator = {**WRITE, "X-Epistrel-Principal": "p-operator"}
    word = f"zarquon{unique('w').lower().replace('-', '')}"
    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        await play_turn(engine, story, f"I carry the {word}.", principal="p-export")
        data = await _exported(client, engine, story, tmp_path)
        made = await client.post(
            "/admin/policies",
            json={
                "scope": "global",
                "name": unique("import-gate"),
                "rule": {"type": "wordlist", "words": [word]},
                "message": "Not here.",
            },
            headers=operator,
        )
        assert made.status_code == 201, made.text
        policy = made.json()["id"]
        stories_before = await count(engine, "SELECT count(*) FROM stories")
        try:
            refused = await _import(client, engine, data)
            assert refused.status is not None
            assert refused.status["status"] == "refused", refused.status
            assert refused.error["code"] == "rating_check_failed", refused.error
            violations = refused.error["details"]["violations"]
            assert {v["policy_id"] for v in violations} == {policy}
            assert {v["position"].get("turn") for v in violations} == {1}
            assert word not in json.dumps(refused.status)
            await _nothing_left(engine, stories_before)
        finally:
            gone = await client.delete(f"/admin/policies/{policy}", headers=operator)
            assert gone.status_code in (200, 204), gone.text
        passed = await _import(client, engine, data)
        assert passed.ready, passed.status
        stamps = await rows(
            engine,
            "SELECT checked_guardrails FROM operations WHERE story_id = :s AND kind = 'turn'",
            s=passed.story_id,
        )
        assert stamps
        assert all(s.checked_guardrails is not None for s in stamps)


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7; FR-ADMIN-9")
async def test_i_10__m7_import_erasure(pg_url: str, engine: AsyncEngine, tmp_path: Any) -> None:
    """#251: a subject erased here after the bundle was made is erased from the imported story
    before it is served: Ada is erased from the source after its export; the import of that
    earlier bundle applies the erasure (its ledger entry concerns the bundle's source story),
    so the imported story holds no record naming her, and its ledger holds a copy of the entry
    (``copied_from``). Complement: the source's own record of it is untouched."""
    from tests import erasure_support as es

    settings = es.erasure_settings(pg_url)
    async with rest(settings, engine) as client:
        made = await create(client, story_body("Wren", "Ada"))
        story = made["story_id"]
        ada = next(c["id"] for c in made["characters"] if c["name"] == "Ada")
        await play_turn(engine, story, "Ada waves at the door.", principal="p-export")
        data = await _exported(client, engine, story, tmp_path)
        await es.erase(client, "character", ada, story, confirm_all=True)
        (entry,) = await rows(
            engine,
            "SELECT id FROM erasure_ledger WHERE story_ref = :s AND copied_from IS NULL",
            s=story,
        )
        imported = await _import(client, engine, data)
        assert imported.ready, imported.status
        assert imported.status is not None
        assert imported.status["report"]["erasure"]["applied"] == 1
        names = await rows(
            engine, "SELECT name FROM characters WHERE story_id = :s", s=imported.story_id
        )
        assert "Ada" not in {n.name for n in names}
        lines = await rows(
            engine,
            "SELECT text FROM transcript_messages WHERE story_id = :s",
            s=imported.story_id,
        )
        assert lines
        assert not any(es.naming(str(r.text), ["Ada"]) for r in lines)
        (copied,) = await rows(
            engine,
            "SELECT copied_from FROM erasure_ledger WHERE story_ref = :s",
            s=imported.story_id,
        )
        assert str(copied.copied_from) == str(entry.id)


@pytest.mark.usefixtures("drained_embedding_jobs", "states_delivered")
@pytest.mark.part("M7", "FR-STORE-7")
async def test_i_10__m7_import_outbox(pg_url: str, engine: AsyncEngine, tmp_path: Any) -> None:
    """#251 (FR-EXT-22): an outbox row still pending in the bundle is imported parked: status
    ``imported_unsettled``, never executable, with a ``reconciliation_marks`` row naming its
    source row, listed by ``GET /stories/{id}/reconciliation`` and resolved only by the
    author's resolve action. The provider receives no call."""
    from tests import late_settlement_support as ls
    from tests import staged_support as sp
    from tests.respond_support import respond_client

    settings = auth_settings_stub(pg_url)
    async with respond_client(settings, engine) as (client, epistrel):
        story, cal, bank, fake = await sp.sync_story(client, engine, epistrel)
        outbox_id, _ = await ls.intent(engine, story, bank, cal, [])
        data = await _exported(client, engine, story, tmp_path)
        imported = await _import(client, engine, data, metadata={"ignore_warnings": True})
        assert imported.ready, imported.status
        (row,) = await rows(
            engine,
            "SELECT id, status, executable FROM external_effect_outbox WHERE story_id = :s",
            s=imported.story_id,
        )
        assert (row.status, row.executable) == ("imported_unsettled", False)
        (mark,) = await rows(
            engine,
            "SELECT reason, detail, resolved_by FROM reconciliation_marks WHERE outbox_id = :o",
            o=row.id,
        )
        assert (mark.reason, mark.resolved_by) == ("imported_unsettled", None)
        assert mark.detail == {"source_outbox_id": outbox_id, "source_story_id": story}
        listed = await client.get(
            f"/stories/{imported.story_id}/reconciliation",
            params={"viewpoint": "omniscient"},
            headers=READ,
        )
        assert listed.status_code == 200, listed.text
        assert [(e["outbox_id"], e["reason"]) for e in listed.json()["listings"]] == [
            (str(row.id), "imported_unsettled")
        ]
        resolved = await client.post(
            f"/stories/{imported.story_id}/reconciliation/{row.id}/resolve",
            json={"resolution": "abandoned"},
            headers=WRITE,
        )
        assert resolved.status_code == 200, resolved.text
        (after,) = await rows(
            engine, "SELECT resolved_by FROM reconciliation_marks WHERE outbox_id = :o", o=row.id
        )
        assert after.resolved_by == "abandoned"
        assert fake.writes == []


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7")
async def test_i_10__m7_import_impact(
    settings: Settings, engine: AsyncEngine, tmp_path: Any
) -> None:
    """#251 (FR-PROF-7,8): a Lite story's bundle imported under Full warns (data-producing
    capability gained over its history): without ``ignore_warnings`` the request answers 200
    ``{applied: false, impact}`` and creates nothing; with it, the import runs, attaches Full
    and starts a gap-fill backfill as the story leaves ``importing`` (the story ``paused``,
    cause ``backfill``). A bundle whose rating name matches none needs a named rating."""
    from epistrel.core.seeds.profiles import FULL_PRESET_ID

    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        await play_turn(engine, story, "Line.", principal="p-export")
        data = await _exported(client, engine, story, tmp_path)
        stories_before = await count(engine, "SELECT count(*) FROM stories")
        warned = await _started(client, data, {"engine_profile_id": FULL_PRESET_ID})
        assert warned.status_code == 200, warned.text
        assert warned.json()["applied"] is False
        assert warned.json()["impact"]["warnings"]
        assert await count(engine, "SELECT count(*) FROM stories") == stories_before
        full = await _import(
            client,
            engine,
            data,
            metadata={"engine_profile_id": FULL_PRESET_ID, "ignore_warnings": True},
        )
        assert full.ready, full.status
        (row,) = await rows(
            engine,
            "SELECT status, engine_profile_id FROM stories WHERE id = :s",
            s=full.story_id,
        )
        assert (row.status, str(row.engine_profile_id)) == ("paused", FULL_PRESET_ID)
        (backfill,) = await rows(
            engine, "SELECT kind FROM story_backfills WHERE story_id = :s", s=full.story_id
        )
        assert backfill.kind == "gap_fill"

        members = bundles_read(data)
        manifest = json.loads(members[0][1])
        renamed = {**manifest, "content_rating": {**manifest["content_rating"], "name": "Nope"}}
        nameless = _tar([("manifest.json", json.dumps(renamed).encode()), *members[1:]])
        needs = await _started(client, nameless)
        assert needs.status_code == 422, needs.text
        assert needs.json()["error"]["code"] == "rating_required"
        assert needs.json()["error"]["details"]["bundle_rating_name"] == "Nope"
        async with engine.begin() as conn:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "DELETE FROM jobs WHERE kind = 'backfill' AND story_id = :s"
                ),
                {"s": full.story_id},
            )


def bundles_read(data: bytes) -> list[tuple[str, bytes]]:
    from epistrel.core import bundles

    return bundles.read_archive(data)


def auth_settings_stub(pg_url: str) -> Settings:
    from tests.auth_support import auth_settings

    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M7", "FR-STORE-7")
async def test_i_10__m7_import_older_format(
    settings: Settings, engine: AsyncEngine, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#250: a bundle of an older format is migrated line by line through
    ``FORMAT_MIGRATIONS`` (here a test-registered step, with this deployment's format
    monkeypatched to ``1.1.0``) and imports, every line through the step; a registered version
    whose chain to the current format has a gap is ``unsupported_format_version``."""
    from epistrel.core.bundles import format as bundle_format
    from epistrel.core.bundles import importing

    async with rest(settings, engine) as client:
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        await play_turn(engine, story, "Line.", principal="p-export")
        data = await _exported(client, engine, story, tmp_path)
        seen: list[str] = []

        def step(path: str, line: dict[str, Any]) -> dict[str, Any]:
            seen.append(path)
            return line

        monkeypatch.setattr(importing, "FORMAT_VERSION", "1.1.0")
        monkeypatch.setitem(bundle_format.FORMAT_MIGRATIONS, "1.0.0", ("1.1.0", step))
        migrated = await _import(client, engine, data)
        assert migrated.ready, migrated.status
        assert "records/story.json" in seen
        assert "events.jsonl" in seen
        monkeypatch.setitem(bundle_format.FORMAT_MIGRATIONS, "1.0.0", ("1.0.9", step))
        gap = await _import(client, engine, data)
        assert gap.error["code"] == "unsupported_format_version", gap.error
