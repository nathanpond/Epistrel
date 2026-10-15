"""ERASURE-RESURRECTION: once a subject is erased, nothing in flight or cached writes them back
(FR-ADMIN-9; #127).

``resurrection_clauses`` erases Linda from a played Lite story while a turn is generating and a
summary job is summarising a window that names her, then checks the three resurrection clauses,
each with its complement:

- the in-flight turn is abandoned (409 ``timeline_changed`` naming the erasure, no events,
  status ``abandoned``), a turn queued behind it fails ``timeline_changed`` naming the erasure
  (409, status ``failed``, no events), and the summary job's output is dropped and the job
  requeued; a turn started after the erasure that does not name her commits normally, and so
  does one whose player text names her (a consumer typing the name afterwards is not refused);
- a worker result built after the erasure that names her is refused (409
  ``erased_subject_referenced``, no segment, the operation ``abandoned``), as is a model reply
  naming her, after its regeneration budget; a clean summary job commits;
- the stored idempotency result naming her is purged: a replay answers the reference only,
  while a result stored after the erasure replays in full.

docs/04 has no ERASURE-RESURRECTION row: S-ERASURE-BOUNDARY's Resurrection clause runs these
clauses as its ``__in_flight`` slice (``tests/erasure/test_s_erasure_boundary.py``, #344), so the
coroutine here is a helper that slice calls, not a test of its own.

The other tests: the write-path registry covers every content writer
(``guard.CONTENT_WRITE_PATHS``), the n-gram matcher finds a name exactly where the prose matcher
would, a catalog-scope erasure refuses a new structured reference to the erased id (a scenario
role, a medium's ``depicted_subjects``), a fork carries the story's ledger rows, and a fork whose
copy predates an erasure that commits before its fence retries.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import secrets
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import fork as fork_module
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind
from epistrel.core.erasure import guard
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.erasure.erase import term_hashes
from epistrel.core.erasure.index import SUBJECT_WRITERS, mentions
from epistrel.core.erasure.ledger import LEDGER_FILE
from epistrel.core.media.assets import DECLARED_FIELD
from epistrel.orchestrator.jobs.queue import CommitOutcome
from tests import erasure_support as e
from tests import genesis_support as g
from tests import media_support as media
from tests import scenario_support as scenarios
from tests.auth_support import ERASURE_LEDGER_DIR
from tests.conformance.conftest import migrated_database
from tests.event_support import rows, scalar
from tests.model_support import config_key
from tests.respond_support import body, respond, respond_client
from tests.summary_support import run_due_jobs, segment_rows, summary_jobs

pytestmark = pytest.mark.integration

SUMMARY_KIND = OperationKind.CONSOLIDATION.value
"""The kind of a summary job's operation (#72)."""
LATE = f"{e.NAME} and {e.OTHER} argued about the deed again."
CLEAN = f"{e.OTHER} and the eldest argued about the deed."


@pytest.fixture
def erasure_settings(pg_url: str) -> Settings:
    return e.erasure_settings(pg_url)


async def _in_flight_turn(engine: AsyncEngine, story_id: str) -> str:
    found = await scalar(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'in_flight'",
        s=story_id,
    )
    assert found is not None, "no turn is generating"
    return str(found)


async def _queued_turn(engine: AsyncEngine, story_id: str) -> str | None:
    found = await scalar(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'queued'",
        s=story_id,
    )
    return None if found is None else str(found)


async def _operation(engine: AsyncEngine, operation_id: str) -> tuple[str, int]:
    status = await scalar(
        engine, "SELECT status::text FROM operations WHERE operation_id = :o", o=operation_id
    )
    written = await scalar(
        engine, "SELECT count(*) FROM events WHERE operation_id = :o", o=operation_id
    )
    return str(status), int(written)


async def _until(predicate: Any, timeout_s: float = 10.0) -> None:
    async with asyncio.timeout(timeout_s):
        while True:
            if await predicate():
                return
            await asyncio.sleep(0.02)


async def resurrection_clauses(erasure_settings: Settings, engine: AsyncEngine) -> None:
    """The three resurrection clauses (in-flight turn abandoned; worker result refused; replay
    purged), each with its complement; see the module docstring."""
    async with respond_client(erasure_settings, engine) as (client, lib):
        story = await e.played_story(client, lib, engine)
        sid, stub = story.story_id, lib.models.stub
        assert stub is not None
        # A turn arriving while one generates waits in the queue (#63) instead of 423.
        queueing = await client.patch(
            f"/stories/{sid}/settings", json={"turn_concurrency": "queue"}, headers=e.WRITE
        )
        assert queueing.status_code == 200, queueing.text
        # Two more turns naming her leave a summary window due.
        stub.script("generation", [f"{e.NAME} sets the table.", f"{e.ALIAS} pours the tea."])
        for n in range(2):
            ok = await respond(client, sid, body(story.player, f"More tea ({n})?"))
            assert ok.status_code == 200, ok.text
        stored = await _pins(client, sid, PINNED, NAMED_KEY)
        assert stored.status_code == 200, stored.text
        assert e.naming(stored.text, e.TERMS)  # the stored result names her
        assert [j.status for j in await summary_jobs(engine, sid)][-1] == "queued"

        # In flight at the erasure: a summary job and a turn, both mid-generation.
        # The held call's window still names her; the rerun's is redacted.
        stub.when((e.NAME,), LATE, "lite_summary")
        stub.when((), CLEAN, "lite_summary")
        summarising = stub.hold("lite_summary")
        job_run = asyncio.ensure_future(run_due_jobs(engine, lib.models, sid, limit=2))
        await asyncio.wait_for(summarising.entered.wait(), 10)
        generating = stub.hold("generation")
        stub.script("generation", [f"{e.NAME} hands {e.OTHER} the deed."])
        turn = asyncio.ensure_future(respond(client, sid, body(story.player, "The deed?")))
        await asyncio.wait_for(generating.entered.wait(), 10)
        held = await _in_flight_turn(engine, sid)
        # A second turn waits in the queue behind the held one (#63).
        waiting = asyncio.ensure_future(respond(client, sid, body(story.player, "And the will?")))
        await _until(lambda: _queued_turn(engine, sid))
        queued = await _queued_turn(engine, sid)
        assert queued is not None

        erasing = asyncio.ensure_future(e.erase(client, "character", story.mother, sid))

        async def erased() -> bool:
            n = await scalar(
                engine, "SELECT count(*) FROM erasure_ledger WHERE story_ref = :s", s=sid
            )
            return bool(n)

        await _until(erased)
        # The erasure cleared the held turn's lease itself (#63); the held turn still runs. (The
        # drainer, which read the queued turn as its head before the erasure, may briefly take
        # the freed slot for it, find it failed, and give the slot back.)
        lease = await scalar(engine, "SELECT in_flight_turn FROM stories WHERE id = :s", s=sid)
        assert lease in (None, queued)
        summarising.release()  # its output names her; its source turns moved under it
        done = await erasing
        generating.release()
        abandoned = await turn
        assert abandoned.status_code == 409, abandoned.text
        error = abandoned.json()["error"]
        assert error["code"] == "timeline_changed"
        assert error["details"]["control_operation_id"] == done["operation_id"]
        assert await _operation(engine, held) == ("abandoned", 0)
        # The queued turn failed in the erasure's own transaction, naming it.
        failed = await waiting
        assert failed.status_code == 409, failed.text
        queued_error = failed.json()["error"]
        assert queued_error["code"] == "timeline_changed"
        assert queued_error["details"]["control_operation_id"] == done["operation_id"]
        (record,) = await rows(
            engine,
            "SELECT status::text AS status, error_code, error FROM operations "
            "WHERE operation_id = :o",
            o=queued,
        )
        assert (record.status, record.error_code) == ("failed", "timeline_changed")
        assert record.error["details"]["control_operation_id"] == done["operation_id"]
        assert await _operation(engine, queued) == ("failed", 0)

        async def slot_free() -> bool:
            held_by = await scalar(
                engine, "SELECT in_flight_turn FROM stories WHERE id = :s", s=sid
            )
            return held_by is None

        await _until(slot_free)

        outcomes = await job_run
        assert outcomes[0] == CommitOutcome.DROPPED  # dropped and requeued (#70)
        jobs_now = await summary_jobs(engine, sid)
        assert outcomes[1] == CommitOutcome.COMMITTED, jobs_now[-1].result
        texts = {s.text for s in await segment_rows(engine, sid)}
        assert LATE not in texts
        assert CLEAN in texts

        # Complement: a turn started after the erasure that does not name her commits, and so
        # does one whose player text names her (new consumer content is not refused).
        stub.reset()
        stub.script("generation", ["The kettle sings.", "The rain eases off."])
        clean = await respond(client, sid, body(story.player, "Is the kettle on?"))
        assert clean.status_code == 200, clean.text
        typed = await respond(client, sid, body(story.player, f"Where did {e.NAME} go?"))
        assert typed.status_code == 200, typed.text
        assert e.NAME in typed.text

        # A model reply naming her, after the erasure: regenerated within the budget, then 409.
        budget = int(
            await scalar(
                engine,
                "SELECT coalesce((settings->>'regenerate_retry_budget')::int, 2) "
                "FROM stories WHERE id = :s",
                s=sid,
            )
        )
        stub.script("generation", [f"{e.NAME} is back at the stove."] * (budget + 1))
        refused = await respond(client, sid, body(story.player, "Who is cooking?"))
        assert refused.status_code == 409, refused.text
        refusal = refused.json()["error"]
        assert refusal["code"] == "erased_subject_referenced"
        assert refusal["details"]["erasure_id"] == done["erasure_id"]
        assert refusal["details"]["match_count"] >= 1
        assert "rebuild your context" in refusal["message"]
        (attempts,) = await rows(
            engine,
            "SELECT attempts, status::text AS status FROM operations WHERE story_id = :s "
            "AND kind = 'turn' ORDER BY started_at DESC LIMIT 1",
            s=sid,
        )
        assert attempts.status == "abandoned"
        assert [a.get("reason") for a in attempts.attempts] == ["erased_subject_referenced"] * (
            budget + 1
        )

        # A worker result built after the erasure that names her is refused (late result).
        stub.reset()
        stub.script("generation", ["The rain stops.", "The kettle whistles."])
        for n in range(2):
            ok = await respond(client, sid, body(story.player, f"Anything else ({n})?"))
            assert ok.status_code == 200, ok.text
        stub.script("lite_summary", [LATE])
        before = await segment_rows(engine, sid)
        (outcome,) = await run_due_jobs(engine, lib.models, sid, limit=1)
        assert outcome == "failed"  # a counted failure; the next trigger re-enqueues
        # Refused for the erasure, not any failure (#352).
        assert (await summary_jobs(engine, sid))[-1].result == {"error": "ErasedSubjectReferenced"}
        assert await segment_rows(engine, sid) == before
        late = await rows(
            engine,
            "SELECT status::text AS status FROM operations WHERE story_id = :s "
            "AND kind = :k ORDER BY started_at DESC LIMIT 1",
            s=sid,
            k=SUMMARY_KIND,
        )
        assert late[0].status == "abandoned"
        assert sum((await e.residue(engine, sid, e.TERMS[:2])).values()) <= _typed_mentions(
            await e.story_values(engine, sid)
        )

        # Replay of the purged stored result: the reference only.
        replay = await _pins(client, sid, PINNED, NAMED_KEY)
        assert replay.status_code == 200, replay.text
        assert replay.headers.get("Idempotent-Replay") == "true"
        assert set(replay.json()) == {
            "operation_id",
            "reference",
            "timeline_revision",
            "idempotency_key",
        }
        assert not e.naming(replay.text, e.TERMS)
        # Complement: a result stored after the erasure replays in full.
        kept = await _pins(client, sid, [f"{e.OTHER} mends the fence."], "resurrection-kept")
        again = await _pins(client, sid, [f"{e.OTHER} mends the fence."], "resurrection-kept")
        assert kept.status_code == again.status_code == 200
        assert again.headers.get("Idempotent-Replay") == "true"
        assert again.json() == kept.json()
        assert "items" in again.json() or "pinned" in again.json()


NAMED_KEY = "resurrection-named"
PINNED = [f"{e.NAME} keeps the spare key."]


async def _pins(client: Any, story_id: str, items: list[str], key: str) -> Any:
    """Set the story's pinned memories under ``key``: a keyed call whose request hash does not
    depend on the cast (a turn's covers its render set, which the erasure changed)."""
    return await client.put(
        f"/stories/{story_id}/pinned-memories",
        json={"items": [{"text": t} for t in items]},
        headers={**e.WRITE, "Idempotency-Key": key},
    )


def _typed_mentions(values: dict[str, list[str]]) -> int:
    """How many of the story's values name her because the player typed her name after the
    erasure (``Where did Linda go?``): the only mentions allowed to remain."""
    return sum(
        1
        for found in values.values()
        for value in found
        if "Where did" in value and e.naming(value, (e.NAME,))
    )


def test_every_content_write_path_is_guarded() -> None:
    """``CONTENT_WRITE_PATHS`` is exactly the subject index's writer modules plus ingest and the
    job commit, and each entry's claim holds in its module's source: an ``OPERATION`` path
    writes through ``Operation`` (whose ``_write`` runs ``check_content``), a catalog path calls
    ``assert_not_erased``, the fork runs its ledger fence, and an exemption gives its reason."""
    registered = {p.module for p in guard.CONTENT_WRITE_PATHS}
    writers = {module for entry in SUBJECT_WRITERS for module in entry.writers}
    assert registered == writers | {"epistrel.core.ingest", "epistrel.orchestrator.jobs.queue"}
    assert len(registered) == len(guard.CONTENT_WRITE_PATHS)
    operation = inspect.getsource(importlib.import_module("epistrel.core.events.operation"))
    assert "erasure_guard.check_content(" in operation
    for path in guard.CONTENT_WRITE_PATHS:
        source = inspect.getsource(importlib.import_module(path.module))
        if path.how == guard.OPERATION:
            assert path.module.endswith("events.operation") or (
                "Operation(" in source or "write_in(" in source or "OperationBatch(" in source
            ), path.module
        elif path.how == guard.CATALOG_REFERENCES:
            assert "assert_not_erased(" in source, path.module
        elif path.how == guard.FORK_FENCE:
            assert "_erasure_fence(" in source, path.module
        else:
            assert path.how == guard.EXEMPT, path.module
            assert path.why, path.module


def test_the_span_matcher_finds_what_the_prose_matcher_finds() -> None:
    """The guard compares keyed hashes of word spans, never the names: a text matches exactly
    when the index's whole-word matcher would find the term (possessives, case, whitespace
    runs, multi-word names), and a word that merely contains the term does not."""
    key = config_key().get_secret_value()
    terms = ("Linda Marsh", "Mum's", "Lin")
    hashes = set(term_hashes(terms, key))
    for text in (
        "linda   MARSH waves.",
        "It was Mum's idea.",
        "Lin's coat",
        "LIN!",
        "Ask Linda Marsh-Okafor.",
    ):
        assert guard.span_hashes([text], key) & hashes, text
        assert any(mentions(text, [t]) for t in terms), text
    for text in ("Linden trees", "Marsh alone", "lint", "Mum is home."):
        assert not guard.span_hashes([text], key) & hashes, text
        assert not any(mentions(text, [t]) for t in terms), text


async def test_a_catalog_erasure_refuses_a_new_reference(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """After a catalog-scope erasure of a catalog character, filling a scenario role with her id
    is 409 ``erased_subject_referenced`` at the role's field, and so is uploading a medium whose
    ``depicted_subjects`` names her; filling the role with another catalog character, or
    depicting that character, is accepted (complement)."""
    async with respond_client(erasure_settings, engine) as (client, _):
        erased = await e.linda(client, "Ottoline Brack", "Otty")
        other = await e.linda(client, "Perpetua Vane", "Pet")
        done = await e.erase(client, "catalog_character", erased)
        with guard.recording() as calls:
            refused = await client.post(
                "/catalog/scenarios",
                json=g.scenario_body(
                    g.role("mother", fill="default", character={"catalog_id": erased})
                ),
                headers=scenarios.WRITE,
            )
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "erased_subject_referenced"
        assert error["details"]["erasure_id"] == done["erasure_id"]
        assert error["details"]["field"] == "body.cast.roles[0].character.catalog_id"
        assert "epistrel.core.catalog.scenarios" in calls
        accepted = await client.post(
            "/catalog/scenarios",
            json=g.scenario_body(g.role("mother", fill="default", character={"catalog_id": other})),
            headers=scenarios.WRITE,
        )
        assert accepted.status_code == 201, accepted.text

        # A new medium depicting her is refused at the declared field; one depicting the
        # other character is accepted (complement).
        with guard.recording() as calls:
            depicting = await media.upload(
                client,
                media.png(),
                media.metadata(depicted_subjects=[{"kind": "catalog_character", "id": erased}]),
            )
        assert depicting.status_code == 409, depicting.text
        error = depicting.json()["error"]
        assert error["code"] == "erased_subject_referenced"
        assert error["details"]["erasure_id"] == done["erasure_id"]
        assert error["details"]["field"] == f"body.metadata.{DECLARED_FIELD}"
        assert "epistrel.core.media.assets" in calls
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM media_assets WHERE depicted_subjects @> "
                "jsonb_build_array(jsonb_build_object('id', CAST(:i AS text)))",
                i=erased,
            )
            == 0
        )
        depicted = await media.uploaded(
            client, depicted_subjects=[{"kind": "catalog_character", "id": other}]
        )
        assert depicted["depicted_subjects"] == [{"kind": "catalog_character", "id": other}]


async def test_a_fork_carries_the_story_ledger(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """A fork taken after an erasure copies the source's ledger rows (``copied_from`` set, the
    same hashes), so the guard refuses the erased subject in the fork as well; the reconciler's
    pending set ignores the copies."""
    async with respond_client(erasure_settings, engine) as (client, lib):
        story = await e.played_story(client, lib, engine)
        sid, stub = story.story_id, lib.models.stub
        assert stub is not None
        done = await e.erase(client, "character", story.mother, sid)
        forked = await client.post(f"/stories/{sid}/fork", json={}, headers=e.WRITE)
        assert forked.status_code == 201, forked.text
        fork_id = forked.json()["story_id"]
        (source,) = await rows(
            engine, "SELECT id, scan_term_hashes FROM erasure_ledger WHERE story_ref = :s", s=sid
        )
        (copy,) = await rows(
            engine,
            "SELECT id, copied_from, scan_term_hashes, story_ref FROM erasure_ledger "
            "WHERE story_ref = :s",
            s=fork_id,
        )
        assert (copy.copied_from, copy.scan_term_hashes) == (source.id, source.scan_term_hashes)
        assert copy.id != source.id == done["erasure_id"]
        player = next(
            c["id"] for c in forked.json()["characters"] if c["source_id"] == story.player
        )
        stub.script("generation", [f"{e.NAME} is back."] * 5)
        refused = await respond(client, fork_id, body(player, "Who is there?"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "erased_subject_referenced"
        pending = await scalar(
            engine,
            "SELECT count(*) FROM erasure_ledger WHERE applied_at IS NULL AND copied_from IS NULL",
        )
        assert pending == 0


async def test_a_scratch_database_erasure_stays_out_of_the_session_ledger(
    pg_url: str, erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """A catalog erasure on a scratch database (``migrated_database``) writes that database's own
    ledger file, never the session's (#125): before, it wrote ``ERASURE_LEDGER_DIR`` and the next
    app on the session database reconciled it in as a pending entry that never settled, which
    failed the pending count above (found by the M3 group C gate, from C-15's operation-stages
    test). Complement: the session database's apps keep the session's ledger."""
    assert erasure_settings.erasure_ledger_dir == ERASURE_LEDGER_DIR
    session_file = ERASURE_LEDGER_DIR / LEDGER_FILE
    with migrated_database(pg_url, f"epistrel_scratch_ledger_{secrets.token_hex(4)}") as url:
        scratch = e.erasure_settings(url)
        own = scratch.erasure_ledger_dir
        assert own != ERASURE_LEDGER_DIR
        assert e.erasure_settings(url).erasure_ledger_dir == own  # every app on it shares it
        db = make_engine(scratch)
        try:
            async with respond_client(scratch, db) as (client, _):
                done = await e.erase(client, "catalog_character", await e.linda(client))
        finally:
            await db.dispose()
        assert done["erasure_id"] in (own / LEDGER_FILE).read_text(encoding="utf-8")
    session_lines = session_file.read_text(encoding="utf-8") if session_file.exists() else ""
    assert done["erasure_id"] not in session_lines
    assert not own.exists()  # dropped with its database
    # The session database's reconcile has nothing of it to take in.
    await erasure_ledger.mark(engine, erasure_ledger.LedgerFile(ERASURE_LEDGER_DIR))
    found = "SELECT count(*) FROM erasure_ledger WHERE id = :i"
    assert await scalar(engine, found, i=done["erasure_id"]) == 0


async def test_a_fork_retries_when_an_erasure_commits_under_it(
    erasure_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork whose snapshot predates an erasure of its source (the erasure committed after
    the copy, before the fence) retries (``ErasedMeanwhile``): the fork it returns holds none
    of her text and carries the new ledger row (``copied_from`` set). The pause sits just
    before the real fence, so the fence's own race check is what catches it."""
    real = fork_module._erasure_fence
    calls: list[str] = []
    paused, resume = asyncio.Event(), asyncio.Event()

    async def fence(db: AsyncEngine, conn: Any, source: str, fork_id: str) -> None:
        calls.append(fork_id)
        if len(calls) == 1:
            paused.set()
            await resume.wait()
        await real(db, conn, source, fork_id)

    monkeypatch.setattr(fork_module, "_erasure_fence", fence)
    async with respond_client(erasure_settings, engine) as (client, lib):
        story = await e.played_story(client, lib, engine)
        sid = story.story_id
        assert sum((await e.residue(engine, sid, e.TERMS)).values()) > 0
        forking = asyncio.ensure_future(
            client.post(f"/stories/{sid}/fork", json={}, headers=e.WRITE)
        )
        await asyncio.wait_for(paused.wait(), 10)
        # The first attempt's copy is taken and still names her; the erasure commits now.
        done = await asyncio.wait_for(e.erase(client, "character", story.mother, sid), 30)
        resume.set()
        forked = await asyncio.wait_for(forking, 30)
        assert forked.status_code == 201, forked.text
        fork_id = forked.json()["story_id"]
        assert len(calls) == 2, calls  # the first attempt was refused and retried
        assert fork_id == calls[1] != calls[0]
        assert await scalar(engine, "SELECT count(*) FROM stories WHERE id = :s", s=calls[0]) == 0
        assert sum((await e.residue(engine, fork_id, e.TERMS)).values()) == 0
        (copy,) = await rows(
            engine,
            "SELECT copied_from FROM erasure_ledger WHERE story_ref = :s",
            s=fork_id,
        )
        assert copy.copied_from == done["erasure_id"]
