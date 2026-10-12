"""Owners and relations over REST and the library (#104): validation, unknown entities, writes
in any state, idempotency, deletion, forks, concurrency and the delivered-type guard. The
milestone parts are in ``tests/integration/test_i_21.py``."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import (
    EntityNotFound,
    InvalidRequest,
    OwnerTransferRequest,
    RelationRequest,
    RequestContext,
)
from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core import ownership, turn_gate
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from epistrel.core.ownership import REGISTRY
from epistrel.core.tables.ownership import EntityType
from epistrel.testing import pauses
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows, scalar
from tests.integration.ownership_fixtures import (
    CALLS,
    KINDS,
    NEW_OWNER,
    OWNER,
    RELATED,
    STRANGER,
    create_character,
    create_story,
    headers,
    ok,
    owner_of,
    served_entity_routes,
)
from tests.respond_support import respond_client

READ = bearer()


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _code(response: Any) -> tuple[int, str, str | None]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"].get("field")


async def _counts(engine: AsyncEngine) -> tuple[int, int, int]:
    """``(ownership_events, entity_relations, operations)`` row counts."""
    found = await rows(
        engine,
        "SELECT (SELECT count(*) FROM ownership_events), (SELECT count(*) FROM entity_relations), "
        "(SELECT count(*) FROM operations)",
    )
    return tuple(int(v) for v in found[0])  # type: ignore[return-value]


async def test_every_delivered_type_is_covered(dummy_dsn: str, engine: AsyncEngine) -> None:
    """Every owned type whose table exists has its four routes mounted, a factory in
    ``ownership_fixtures.KINDS``, and a request in ``CALLS`` for every route under its entities.
    So the nothing-gates matrix and the creation checks cover every delivered type, and a
    story that delivers a type must extend them. Complement: the scenario, persona, media and
    profile types are in the registry, and none has a table yet."""
    app = create_app(auth_settings(dummy_dsn))
    async with engine.connect() as conn:
        delivered = await ownership.delivered(conn)
    assert set(KINDS) == set(delivered)
    assert set(REGISTRY) == set(EntityType)
    served = {route for kind in KINDS.values() for route in served_entity_routes(app, kind)}
    assert served == set(CALLS)
    operation_ids = {
        getattr(context.original_route, "operation_id", None)
        for context in iter_route_contexts(app.routes)
    }
    for entity_type in delivered:
        for method in ("transfer_owner", "add_relation", "remove_relation", "list_relations"):
            assert f"{method}_{entity_type.value}" in operation_ids
    assert set(EntityType) - set(delivered) == {
        EntityType.SCENARIO,
        EntityType.PERSONA,
        EntityType.MEDIA,
        EntityType.ENGINE_PROFILE,
    }


async def test_principals_and_names_are_validated(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A transfer or relation that names ``system``, a blank, overlong or control-character
    principal, or a relation name outside ``[A-Za-z0-9_-]{1,64}`` is 422 at its field, and
    nothing is written. Names and principals are trimmed of spaces and tabs; names are
    case-sensitive; a principal may contain ``/`` and ``:``; a relation may name the owner. The
    list filters refuse what cannot be a principal or a relation."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create_story(client, OWNER, None)
        base = f"/stories/{story.id}"
        before = await _counts(engine)
        for principal in ("system", "   ", "x" * 257, "a\x01b"):
            refused = await client.put(
                f"{base}/owner", json={"principal": principal}, headers=headers(OWNER)
            )
            assert _code(refused) == (422, "validation_error", "body.principal"), principal
            refused = await client.post(
                f"{base}/relations",
                json={"principal": principal, "relation": "editor"},
                headers=headers(OWNER),
            )
            assert _code(refused) == (422, "validation_error", "body.principal"), principal
        for name in ("", "co author", "a:b", "a/b", "x" * 65, "édith"):
            refused = await client.post(
                f"{base}/relations",
                json={"principal": STRANGER, "relation": name},
                headers=headers(OWNER),
            )
            assert _code(refused) == (422, "validation_error", "body.relation"), name
        extra = await client.put(
            f"{base}/owner", json={"principal": STRANGER, "why": "x"}, headers=headers(OWNER)
        )
        assert _code(extra) == (422, "validation_error", "body.why")
        for path, field in (
            (f"{base}/relations/co%20author/{STRANGER}", "path.relation"),
            (f"{base}/relations/editor/system", "path.principal"),
            (f"{base}/relations/editor/%20%20", "path.principal"),
        ):
            refused = await client.delete(path, headers=headers(OWNER))
            assert _code(refused) == (422, "validation_error", field), path
        for params, field in (
            ({"owner": ""}, "query.owner"),
            ({"owner": "  "}, "query.owner"),
            ({"relation": "co author:p"}, "query.relation[0]"),
            ({"relation": "editor"}, "query.relation[0]"),
            ({"relation": ["editor:", "editor:\x01"]}, "query.relation[1]"),
        ):
            refused = await client.get("/stories", params=params, headers=READ)
            assert _code(refused) == (422, "validation_error", field), params
        assert await _counts(engine) == before

        trimmed = await client.post(
            f"{base}/relations",
            json={"principal": " team/ana:1\t", "relation": "\teditor "},
            headers=headers(OWNER),
        )
        assert (await ok(trimmed, 201))["relation"] == "editor"
        upper = await client.post(
            f"{base}/relations",
            json={"principal": "team/ana:1", "relation": "Editor"},
            headers=headers(OWNER),
        )
        await ok(upper, 201)
        listing = await ok(await client.get(f"{base}/relations", headers=READ))
        assert [(i["relation"], i["principal"]) for i in listing["items"]] == [
            ("Editor", "team/ana:1"),
            ("editor", "team/ana:1"),
        ]
        filtered = await client.get(
            "/stories", params={"relation": "editor:team/ana:1"}, headers=READ
        )
        assert [s["id"] for s in (await ok(filtered))["items"]] == [story.id]
        removed = await client.delete(f"{base}/relations/editor/team/ana:1", headers=headers(OWNER))
        assert removed.status_code == 204, removed.text
        encoded = await client.delete(
            f"{base}/relations/Editor/{quote('team/ana:1', safe='')}", headers=headers(OWNER)
        )
        assert encoded.status_code == 204, encoded.text
        assert (await ok(await client.get(f"{base}/relations", headers=READ)))["items"] == []
        own = await client.post(
            f"{base}/relations",
            json={"principal": OWNER, "relation": "co_author"},
            headers=headers(STRANGER),
        )
        assert (await ok(own, 201))["principal"] == OWNER  # a relation may name the owner


async def test_unknown_entities_and_types(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Owner and relation calls on an entity that does not exist are 404 ``not_found`` on both
    surfaces, and so is a call on a type not delivered yet; an unknown type name is 422."""
    ghost = "0" * 26
    async with respond_client(stub_settings, engine) as (client, lib):
        for kind in KINDS.values():
            for method, suffix, body in (
                ("PUT", "/owner", {"principal": NEW_OWNER}),
                ("POST", "/relations", {"principal": NEW_OWNER, "relation": "editor"}),
                ("DELETE", "/relations/editor/p-x", None),
                ("GET", "/relations", None),
            ):
                response = await client.request(
                    method, kind.path(ghost, suffix), json=body, headers=headers(OWNER)
                )
                assert _code(response)[:2] == (404, "not_found"), (kind.entity_type, method)
        ctx = RequestContext(principal=OWNER)
        for entity_type in (EntityType.PERSONA, EntityType.MEDIA, EntityType.ENGINE_PROFILE):
            with pytest.raises(EntityNotFound) as missing:
                await lib.transfer_owner(
                    ctx, entity_type, ghost, OwnerTransferRequest(principal=NEW_OWNER)
                )
            assert missing.value.details == {"entity_type": entity_type.value, "entity_id": ghost}
            with pytest.raises(EntityNotFound):
                await lib.list_relations(ctx, entity_type, ghost)
        with pytest.raises(InvalidRequest) as unknown:
            await lib.list_relations(ctx, "planet", ghost)
        assert unknown.value.details["field"] == "entity_type"


async def test_writes_are_allowed_in_any_state(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Ownership writes do not care about the entity's state: an archived story or catalog
    entry, and a story whose turn is in flight (where an archive is 423 ``story_busy``), take
    transfers and relation changes. No timeline or configuration revision moves."""
    async with respond_client(stub_settings, engine) as (client, _):
        archived = await create_story(client, OWNER, None)
        busy = await create_story(client, OWNER, None)
        entry = await create_character(client, OWNER, None)
        await ok(await client.post(f"/stories/{archived.id}/archive", headers=headers(OWNER)))
        await ok(
            await client.post(f"/catalog/characters/{entry.id}/archive", headers=headers(OWNER))
        )
        turn = Operation(engine, story_id=busy.id, kind=OperationKind.TURN, principal="p-turn")
        await turn.begin()
        assert await turn_gate.acquire(engine, busy.id, turn.operation_id, lease_s=60) is None
        refused = await client.post(f"/stories/{busy.id}/archive", headers=headers(OWNER))
        assert _code(refused)[:2] == (423, "story_busy")

        config = await scalar(engine, "SELECT config_revision FROM deployment_state")
        revisions = {
            story.id: await scalar(
                engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story.id
            )
            for story in (archived, busy)
        }
        paths = (
            f"/stories/{archived.id}",
            f"/stories/{busy.id}",
            f"/catalog/characters/{entry.id}",
        )
        for path in paths:
            moved = await ok(
                await client.put(
                    f"{path}/owner", json={"principal": NEW_OWNER}, headers=headers(STRANGER)
                )
            )
            assert moved["previous_owner"] == OWNER
            await ok(
                await client.post(
                    f"{path}/relations",
                    json={"principal": RELATED, "relation": "editor"},
                    headers=headers(STRANGER),
                ),
                201,
            )
            dropped = await client.delete(
                f"{path}/relations/editor/{RELATED}", headers=headers(STRANGER)
            )
            assert dropped.status_code == 204, dropped.text
            if path.startswith("/stories"):
                assert moved["timeline_revision"] == revisions[path.rsplit("/", 1)[-1]]
            else:
                assert moved["config_revision"] == config
        assert await scalar(engine, "SELECT config_revision FROM deployment_state") == config
        for story_id, revision in revisions.items():
            now = await scalar(
                engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story_id
            )
            assert now == revision


async def test_writes_are_idempotent_and_story_writes_check_the_revision(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A retry under the same ``Idempotency-Key`` answers the stored result with
    ``Idempotent-Replay: true`` and records nothing more; the key with another body is 409
    ``idempotency_conflict``. On a story, a stale ``X-Epistrel-Expected-Revision`` is 409
    ``revision_conflict`` before anything is written; the current one passes."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create_story(client, OWNER, None)
        key = f"own-{secrets.token_hex(6)}"
        first = await client.put(
            f"/stories/{story.id}/owner",
            json={"principal": NEW_OWNER},
            headers=headers(STRANGER, key),
        )
        await ok(first)
        before = await _counts(engine)
        again = await client.put(
            f"/stories/{story.id}/owner",
            json={"principal": NEW_OWNER},
            headers=headers(STRANGER, key),
        )
        assert (again.status_code, again.headers.get("Idempotent-Replay")) == (200, "true")
        assert {**again.json(), "idempotency_key": None} == {
            **first.json(),
            "idempotency_key": None,
        }
        assert await _counts(engine) == before
        other = await client.put(
            f"/stories/{story.id}/owner", json={"principal": OWNER}, headers=headers(STRANGER, key)
        )
        assert _code(other)[:2] == (409, "idempotency_conflict")

        add_key = f"rel-{secrets.token_hex(6)}"
        body = {"principal": RELATED, "relation": "player"}
        added = await client.post(
            f"/stories/{story.id}/relations", json=body, headers=headers(OWNER, add_key)
        )
        replayed = await client.post(
            f"/stories/{story.id}/relations", json=body, headers=headers(OWNER, add_key)
        )
        assert (added.status_code, replayed.status_code) == (201, 201)
        assert replayed.headers.get("Idempotent-Replay") == "true"
        remove_key = f"del-{secrets.token_hex(6)}"
        for _ in range(2):
            removed = await client.delete(
                f"/stories/{story.id}/relations/player/{RELATED}",
                headers=headers(OWNER, remove_key),
            )
            assert removed.status_code == 204, removed.text
        assert removed.headers.get("Idempotent-Replay") == "true"

        revision = (await ok(await client.get(f"/stories/{story.id}", headers=READ)))[
            "timeline_revision"
        ]
        before = await _counts(engine)
        stale = await client.put(
            f"/stories/{story.id}/owner",
            json={"principal": OWNER},
            headers={**headers(STRANGER), "X-Epistrel-Expected-Revision": str(revision + 1)},
        )
        assert _code(stale)[:2] == (409, "revision_conflict")
        assert await _counts(engine) == before
        current = await client.put(
            f"/stories/{story.id}/owner",
            json={"principal": OWNER},
            headers={**headers(STRANGER), "X-Epistrel-Expected-Revision": str(revision)},
        )
        assert (await ok(current))["owner_principal"] == OWNER


async def test_deleting_an_entity_drops_its_relations(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Deleting a story or a catalog character removes its relations and records a ``deleted``
    event with the acting principal, the owner and the relations removed; the earlier ownership
    events stay. Complement: another entity's relation to the same principal stays."""
    async with respond_client(stub_settings, engine) as (client, _):
        kept = await create_story(client, OWNER, None)
        await ok(
            await client.post(
                f"/stories/{kept.id}/relations",
                json={"principal": RELATED, "relation": "player"},
                headers=headers(OWNER),
            ),
            201,
        )
        for kind in KINDS.values():
            entity = await kind.create(client, OWNER, NEW_OWNER)
            for name in ("player", "editor"):
                await ok(
                    await client.post(
                        kind.path(entity.id, "/relations"),
                        json={"principal": RELATED, "relation": name},
                        headers=headers(OWNER),
                    ),
                    201,
                )
            deleted = await client.delete(kind.path(entity.id), headers=headers(STRANGER))
            assert deleted.status_code == 204, deleted.text
            assert await owner_of(engine, kind, entity.id) is None
            remaining = await scalar(
                engine, "SELECT count(*) FROM entity_relations WHERE entity_id = :e", e=entity.id
            )
            assert remaining == 0
            events = await rows(
                engine,
                "SELECT kind, old, new, acting_principal FROM ownership_events "
                "WHERE entity_id = :e ORDER BY at, id",
                e=entity.id,
            )
            assert [e.kind for e in events] == ["relation_added", "relation_added", "deleted"]
            assert tuple(events[-1])[1:] == (
                {
                    "owner_principal": NEW_OWNER,
                    "relations": [
                        {"principal": RELATED, "relation": "editor"},
                        {"principal": RELATED, "relation": "player"},
                    ],
                },
                None,
                STRANGER,
            )
        listing = await ok(await client.get(f"/stories/{kept.id}/relations", headers=READ))
        assert [(i["relation"], i["principal"]) for i in listing["items"]] == [("player", RELATED)]


async def test_creations_name_their_owner(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``owner`` on a story or fork creation follows the principal rule (never ``system``:
    422 at ``body.owner``); a fork starts without the source's relations; reads and the list
    show the owner."""
    async with respond_client(stub_settings, engine) as (client, _):
        source = await create_story(client, OWNER, None)
        await ok(
            await client.post(
                f"/stories/{source.id}/relations",
                json={"principal": RELATED, "relation": "player"},
                headers=headers(OWNER),
            ),
            201,
        )
        for path, body in (
            ("/stories", {**_story(), "owner": "system"}),
            (f"/stories/{source.id}/fork", {"at_turn": 0, "owner": "system"}),
            ("/stories", {**_story(), "owner": "  "}),
        ):
            refused = await client.post(path, json=body, headers=headers(OWNER))
            assert _code(refused) == (422, "validation_error", "body.owner"), path
        fork = await ok(
            await client.post(
                f"/stories/{source.id}/fork",
                json={"at_turn": 0, "owner": " p-heir\t"},
                headers=headers(STRANGER),
            ),
            201,
        )
        fork_id = fork["story_id"]
        assert await owner_of(engine, KINDS[EntityType.STORY], fork_id) == "p-heir"
        relations = await ok(await client.get(f"/stories/{fork_id}/relations", headers=READ))
        assert (relations["owner_principal"], relations["items"]) == ("p-heir", [])
        listed = await ok(await client.get("/stories", params={"owner": "p-heir"}, headers=READ))
        assert fork_id in {s["id"] for s in listed["items"]}
        assert all(s["owner_principal"] == "p-heir" for s in listed["items"])


def _story() -> dict[str, Any]:
    return {
        "name": "A story",
        "characters": [
            {"name": "Wren", "definition": "The player.", "true_age": 30, "is_player": True}
        ],
    }


async def test_concurrent_adds_record_one_relation(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Two simultaneous adds of the same relation under different keys: one is 201 and
    recorded, the other 200 with the same row and nothing recorded. A transfer bumps a catalog
    entry's ``updated_at``; a relation change does not."""
    async with respond_client(stub_settings, engine) as (client, _):
        entry = await create_character(client, OWNER, None)
        path = f"/catalog/characters/{entry.id}"
        body = {"principal": RELATED, "relation": "editor"}
        answers = await asyncio.gather(
            client.post(f"{path}/relations", json=body, headers=headers(OWNER)),
            client.post(f"{path}/relations", json=body, headers=headers(STRANGER)),
        )
        assert sorted(a.status_code for a in answers) == [200, 201]
        assert answers[0].json()["created_at"] == answers[1].json()["created_at"]
        recorded = await scalar(
            engine, "SELECT count(*) FROM ownership_events WHERE entity_id = :e", e=entry.id
        )
        assert recorded == 1

        updated = "SELECT updated_at FROM catalog.character_catalog WHERE id = :e"
        first = await scalar(engine, updated, e=entry.id)
        dropped = await client.delete(f"{path}/relations/editor/{RELATED}", headers=headers(OWNER))
        assert dropped.status_code == 204, dropped.text
        assert await scalar(engine, updated, e=entry.id) == first
        moved = await client.put(
            f"{path}/owner", json={"principal": NEW_OWNER}, headers=headers(OWNER)
        )
        await ok(moved)
        assert await scalar(engine, updated, e=entry.id) > first


LOCK_WAITERS = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
)


async def _waits_or_ends(engine: AsyncEngine, task: asyncio.Future[Any]) -> bool:
    """Wait until ``task`` has ended or a request of this database waits on a lock; whether one
    waits."""
    deadline = time.monotonic() + 10.0
    while not task.done():
        if await scalar(engine, LOCK_WAITERS) > 0:
            return True
        assert time.monotonic() < deadline, "the request neither ended nor waited on a lock"
        await asyncio.sleep(0.02)
    return False


async def test_an_add_racing_the_removal_of_its_relation(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """An add of a relation that exists and the removal of that relation, at the same moment:
    the add has found the relation (held there) when the removal starts. The removal waits on
    the entity's row lock until the add commits, so the add answers 200 with the stored row and
    records nothing, and the removal answers 204 and records its event. For every delivered
    type; neither request errors."""
    async with respond_client(stub_settings, engine) as (client, _):
        for kind in KINDS.values():
            entity = await kind.create(client, OWNER, None)
            path = kind.path(entity.id, "/relations")
            body = {"principal": RELATED, "relation": "editor"}
            first = await ok(await client.post(path, json=body, headers=headers(OWNER)), 201)
            with pauses.held(pauses.OWNERSHIP_ADD_FOUND) as hold:
                add = asyncio.ensure_future(client.post(path, json=body, headers=headers(STRANGER)))
                await hold.arrived(1)
                removal = asyncio.ensure_future(
                    client.delete(f"{path}/editor/{RELATED}", headers=headers(NEW_OWNER))
                )
                waited = await _waits_or_ends(engine, removal)
                hold.release()
                added, removed = await asyncio.gather(add, removal)
            assert added.status_code == 200, (kind.entity_type, added.text)
            assert added.json()["created_at"] == first["created_at"]
            assert removed.status_code == 204, (kind.entity_type, removed.text)
            assert waited, "the removal waits for the add, which holds the entity's row lock"
            listing = await ok(await client.get(path, headers=READ))
            assert listing["items"] == []
            events = await rows(
                engine,
                "SELECT kind, acting_principal FROM ownership_events "
                "WHERE entity_id = :e ORDER BY at, id",
                e=entity.id,
            )
            assert [tuple(e) for e in events] == [
                ("relation_added", OWNER),
                ("relation_removed", NEW_OWNER),
            ]


async def test_library_matches_rest(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The library's ownership calls take the same requests and give the same answers, and
    the library refuses ``system`` and bad names the same way (a validation error)."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story = await create_story(client, OWNER, None)
        ctx = RequestContext(principal=STRANGER)
        transfer = await lib.transfer_owner(
            ctx, "story", story.id, OwnerTransferRequest(principal=NEW_OWNER)
        )
        assert (transfer.previous_owner, transfer.owner_principal, transfer.changed) == (
            OWNER,
            NEW_OWNER,
            True,
        )
        assert transfer.timeline_revision is not None
        assert transfer.config_revision is None
        write = await lib.add_relation(
            ctx, EntityType.STORY, story.id, RelationRequest(principal=RELATED, relation="player")
        )
        assert (write.created, write.relation.created_by) == (True, STRANGER)
        listed = await lib.list_relations(ctx, EntityType.STORY, story.id)
        assert [(r.relation, r.principal) for r in listed.items] == [("player", RELATED)]
        with pytest.raises(InvalidRequest) as bad:
            await lib.remove_relation(ctx, EntityType.STORY, story.id, "pl ayer", RELATED)
        assert bad.value.details["field"] == "path.relation"
        with pytest.raises(InvalidRequest):
            await lib.remove_relation(ctx, EntityType.STORY, story.id, "player", "system")
        stories = await lib.list_stories(ctx, relation=[f"player:{RELATED}"], owner=NEW_OWNER)
        assert story.id in {s.id for s in stories}
        for refused in ({"principal": "system"}, {"principal": ""}):
            with pytest.raises(ValueError, match="principal"):
                OwnerTransferRequest.model_validate(refused)
        removed = await lib.remove_relation(ctx, "story", story.id, "player", RELATED)
        assert (removed.relation, removed.principal) == ("player", RELATED)
        assert (await lib.list_relations(ctx, "story", story.id)).items == ()
