"""I-10, the story lifecycle: create with an inline cast, archive and unarchive, delete."""

from __future__ import annotations

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
