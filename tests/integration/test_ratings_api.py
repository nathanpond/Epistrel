"""``/admin/content-ratings`` and ``/admin/settings`` over REST (FR-RATE-1,2, FR-CONC-14):
versioned writes recorded in ``config_changes``, the effective-guardrail closure with sources,
the refusals, and the seeded rows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.ids import new_ulid
from epistrel.core.seeds.ratings import (
    DEFAULT_RATING_ID,
    EVERYONE_ID,
    EXPLICIT_ID,
    MATURE_ID,
    SEED_RATINGS,
    TEEN_ID,
)
from epistrel.core.settings_schema import DeploymentSettings
from tests.auth_support import auth_settings, bearer
from tests.conftest import alembic_config
from tests.event_support import rows

BASE = "/admin/content-ratings"
WRITE = {**bearer(), "X-Epistrel-Principal": "p-operator"}


def _db[T](url: str, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def run() -> T:
        engine = make_engine(auth_settings(url))
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _changes(url: str, setting: str) -> list[tuple[Any, ...]]:
    found = _db(
        url,
        lambda e: rows(
            e,
            'SELECT config_revision, old, new, "class", actor, scope FROM config_changes '
            "WHERE setting = :s ORDER BY config_revision",
            s=setting,
        ),
    )
    return [tuple(r) for r in found]


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(auth_settings(pg_url))) as c:
        yield c


def _unique_name(prefix: str) -> str:
    return f"{prefix} {new_ulid()}"


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    fields.setdefault("name", _unique_name("custom"))
    response = client.post(BASE, json=fields, headers=WRITE)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _rating_policy(client: TestClient, slug: str) -> str:
    """A live rating policy (#49) a rating can list; ``slug`` plus a unique suffix."""
    policy_id = f"{slug}-{new_ulid().lower()}"
    response = client.post(
        "/admin/policies",
        json={
            "id": policy_id,
            "scope": "rating",
            "scope_id": EXPLICIT_ID,
            "name": policy_id,
            "rule": {"type": "wordlist", "words": [slug]},
            "message": "Not at this rating.",
        },
        headers=WRITE,
    )
    assert response.status_code == 201, response.text
    return policy_id


def _replacement(rating: dict[str, Any], **changes: Any) -> dict[str, Any]:
    keys = (
        "name",
        "description",
        "display_order",
        "prompt_guidance",
        "image_guidance",
        "policy_ids",
        "inherits_from",
    )
    return {**{k: rating[k] for k in keys}, **changes}


def test_create_get_update_and_versions(client: TestClient, pg_url: str) -> None:
    """Create is version 1 (``config_changes``: old null, new the version id, class
    interpretive, the principal); a PUT is a new version recorded the same way; a no-op PUT
    writes nothing; ``/versions`` lists newest first."""
    p1 = _rating_policy(client, "p1")
    created = _create(client, description="d", prompt_guidance="g1", policy_ids=[p1, p1])
    assert created["version"] == 1
    assert created["policy_ids"] == [p1]
    assert created["created_by"] == "p-operator"
    assert created["retired_at"] is None
    setting = f"rating:{created['id']}"
    assert _changes(pg_url, setting) == [
        (
            created["config_revision"],
            None,
            created["version_id"],
            "interpretive",
            "p-operator",
            "deployment",
        )
    ]

    updated = client.put(
        f"{BASE}/{created['id']}",
        json=_replacement(created, prompt_guidance="g2"),
        headers=WRITE,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert (body["version"], body["prompt_guidance"]) == (2, "g2")
    assert body["config_revision"] == created["config_revision"] + 1
    assert _changes(pg_url, setting)[-1][:3] == (
        body["config_revision"],
        created["version_id"],
        body["version_id"],
    )

    same = client.put(f"{BASE}/{created['id']}", json=_replacement(body), headers=WRITE)
    assert same.status_code == 200
    assert (same.json()["version_id"], same.json()["config_revision"]) == (
        body["version_id"],
        body["config_revision"],
    )
    assert len(_changes(pg_url, setting)) == 2

    got = client.get(f"{BASE}/{created['id']}", headers=bearer()).json()
    assert (got["version"], got["prompt_guidance"]) == (2, "g2")
    assert got["effective_guardrails"] == [{"policy_id": p1, "source_rating_id": created["id"]}]
    versions = client.get(f"{BASE}/{created['id']}/versions", headers=bearer()).json()["items"]
    assert [(v["version"], v["prompt_guidance"]) for v in versions] == [(2, "g2"), (1, "g1")]


def test_put_is_a_full_replacement(client: TestClient) -> None:
    created = _create(client)
    partial = client.put(f"{BASE}/{created['id']}", json={"name": "x"}, headers=WRITE)
    assert partial.status_code == 422
    assert partial.json()["error"]["code"] == "validation_error"


def test_effective_guardrails_with_sources(client: TestClient) -> None:
    """A three-level chain with a shared policy: own first, then by source display order, each
    policy once, attributed to the nearest rating."""
    shared, top_only, mid_only, low_only = (
        _rating_policy(client, slug) for slug in ("shared", "top-only", "mid-only", "low-only")
    )
    top = _create(client, display_order=900, policy_ids=[shared, top_only])
    mid = _create(client, display_order=800, policy_ids=[mid_only], inherits_from=[top["id"]])
    low = _create(
        client,
        display_order=700,
        policy_ids=[low_only, shared],
        inherits_from=[mid["id"], top["id"]],
    )
    got = client.get(f"{BASE}/{low['id']}", headers=bearer()).json()["effective_guardrails"]
    assert got == [
        {"policy_id": low_only, "source_rating_id": low["id"]},
        {"policy_id": shared, "source_rating_id": low["id"]},
        {"policy_id": mid_only, "source_rating_id": mid["id"]},
        {"policy_id": top_only, "source_rating_id": top["id"]},
    ]
    listed = client.get(BASE, headers=bearer()).json()["items"]
    assert all("effective_guardrails" not in item for item in listed)


def test_seeded_chain_resolves_through_the_api(client: TestClient) -> None:
    got = client.get(f"{BASE}/{EVERYONE_ID}", headers=bearer()).json()
    assert got["inherits_from"] == [TEEN_ID]
    assert got["effective_guardrails"] == []  # the seeds list no policies (#49)


def test_inheritance_cycle_refused(client: TestClient, pg_url: str) -> None:
    """Saving a cycle is 422 ``inheritance_cycle`` naming the loop; nothing is written."""
    a = _create(client)
    b = _create(client, inherits_from=[a["id"]])
    cyclic = client.put(
        f"{BASE}/{a['id']}", json=_replacement(a, inherits_from=[b["id"]]), headers=WRITE
    )
    assert cyclic.status_code == 422
    error = cyclic.json()["error"]
    assert error["code"] == "inheritance_cycle"
    assert error["details"]["cycle"] == [a["id"], b["id"], a["id"]]
    assert client.get(f"{BASE}/{a['id']}", headers=bearer()).json()["version"] == 1
    assert len(_changes(pg_url, f"rating:{a['id']}")) == 1

    to_self = client.put(
        f"{BASE}/{a['id']}", json=_replacement(a, inherits_from=[a["id"]]), headers=WRITE
    )
    assert to_self.json()["error"]["code"] == "inheritance_cycle"


def test_validation_refusals(client: TestClient) -> None:
    """Unknown parents, a live-name clash (any case) and over-long fields are 422; a retired
    rating's name is reusable."""
    unknown = client.post(
        BASE, json={"name": _unique_name("u"), "inherits_from": ["0" * 26]}, headers=WRITE
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["unknown"] == ["0" * 26]

    taken = _create(client)
    clash = client.post(BASE, json={"name": taken["name"].upper()}, headers=WRITE)
    assert clash.status_code == 422
    assert clash.json()["error"]["details"]["field"] == "body.name"

    assert client.delete(f"{BASE}/{taken['id']}", headers=WRITE).status_code == 200
    _create(client, name=taken["name"])

    for field, size in (("name", 81), ("description", 4001), ("prompt_guidance", 8001)):
        long = client.post(BASE, json={"name": "n", field: "x" * size}, headers=WRITE)
        assert long.status_code == 422, field
    assert client.post(BASE, json={"name": "  "}, headers=WRITE).status_code == 422
    # #366: the length is checked as sent, as the schema's maxLength says (C-01 found it).
    padded = client.post(BASE, json={"name": " " + "*" * 80}, headers=WRITE)
    assert padded.status_code == 422, padded.text
    assert padded.json()["error"]["details"]["field"] == "body.name"
    assert client.get(BASE, headers=WRITE).text.count("*" * 80) == 0
    _create(client, name="*" * 80)
    assert client.post(BASE, json={"name": "n", "extra": 1}, headers=WRITE).status_code == 422


def test_display_order_default_and_listing(client: TestClient) -> None:
    """Default display order is after every live rating; the list is by display order then
    name, retired ratings only with ``include_retired=1``."""
    first = _create(client)
    second = _create(client)
    assert second["display_order"] == first["display_order"] + 10
    assert client.delete(f"{BASE}/{second['id']}", headers=WRITE).status_code == 200

    listed = client.get(BASE, headers=bearer()).json()["items"]
    keys = [(r["display_order"], r["name"].casefold()) for r in listed]
    assert keys == sorted(keys)
    ids = {r["id"] for r in listed}
    assert first["id"] in ids
    assert second["id"] not in ids
    with_retired = client.get(BASE, params={"include_retired": 1}, headers=bearer()).json()
    retired = next(r for r in with_retired["items"] if r["id"] == second["id"])
    assert retired["retired_at"] is not None


def test_display_order_is_int32(scratch_database: str) -> None:
    """``display_order`` is bounded to the ``INTEGER`` column (#280): the int32 ends are accepted,
    one past either end is 422 at ``body.display_order`` on create and replace, and an assigned
    order after a rating at the maximum stops there instead of overflowing. Runs on a scratch
    database so the maximum never leaks into other tests' default orders."""
    command.upgrade(alembic_config(scratch_database), "head")
    with TestClient(create_app(auth_settings(scratch_database))) as c:
        top = _create(c, display_order=2**31 - 1)
        bottom = _create(c, display_order=-(2**31))
        assert (top["display_order"], bottom["display_order"]) == (2**31 - 1, -(2**31))
        assigned = _create(c)
        assert assigned["display_order"] == 2**31 - 1

        for value in (2**31, -(2**31) - 1):
            created = c.post(
                BASE, json={"name": _unique_name("o"), "display_order": value}, headers=WRITE
            )
            assert created.status_code == 422, created.text
            assert created.json()["error"]["details"]["field"] == "body.display_order"
            replaced = c.put(
                f"{BASE}/{top['id']}", json=_replacement(top, display_order=value), headers=WRITE
            )
            assert replaced.status_code == 422, replaced.text
            assert replaced.json()["error"]["details"]["field"] == "body.display_order"
        assert c.get(f"{BASE}/{top['id']}", headers=bearer()).json()["version"] == top["version"]


def test_retire(client: TestClient, pg_url: str) -> None:
    """DELETE retires (200, recorded with new ``retired``); again → 204; a retired rating
    still answers GET and refuses PUT (409); the default → 409 and stays listed."""
    rating = _create(client)
    retired = client.delete(f"{BASE}/{rating['id']}", headers=WRITE)
    assert retired.status_code == 200
    assert retired.json()["retired_at"] is not None
    assert _changes(pg_url, f"rating:{rating['id']}")[-1][1:3] == (rating["version_id"], "retired")

    again = client.delete(f"{BASE}/{rating['id']}", headers=WRITE)
    assert again.status_code == 204
    assert len(_changes(pg_url, f"rating:{rating['id']}")) == 2
    assert client.get(f"{BASE}/{rating['id']}", headers=bearer()).status_code == 200

    edit = client.put(f"{BASE}/{rating['id']}", json=_replacement(rating), headers=WRITE)
    assert edit.status_code == 409
    assert edit.json()["error"]["details"]["reason"] == "retired"

    default = client.get("/admin/settings", headers=bearer()).json()["default_content_rating_id"]
    refused = client.delete(f"{BASE}/{default}", headers=WRITE)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "validation_error"
    assert refused.json()["error"]["details"]["reason"] == "is_default"
    assert default in {r["id"] for r in client.get(BASE, headers=bearer()).json()["items"]}


def test_not_found_and_principal(client: TestClient) -> None:
    missing = "0" * 26
    for method, path in (
        ("GET", f"{BASE}/{missing}"),
        ("GET", f"{BASE}/{missing}/versions"),
        ("DELETE", f"{BASE}/{missing}"),
    ):
        response = client.request(method, path, headers=WRITE)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"
    assert client.post(BASE, json={"name": "n"}, headers=bearer()).status_code == 400


def test_settings_read_and_partial_update(client: TestClient, pg_url: str) -> None:
    """GET returns the deployment state; PUT merges, writes one interpretive row per changed
    field and one revision; a no-op PUT writes nothing; an unknown or retired default is 422
    ``rating_retired``; the threshold is bounded 0 to 120."""
    start = client.get("/admin/settings", headers=bearer()).json()
    assert set(start) == {"config_revision", *DeploymentSettings.model_fields}  # #64: all

    changed = client.put(
        "/admin/settings",
        json={"default_content_rating_id": TEEN_ID, "minor_age_threshold": 21},
        headers=WRITE,
    )
    try:
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert body == {
            **start,
            "idempotency_key": changed.headers["Idempotency-Key"],
            "config_revision": start["config_revision"] + 1,
            "default_content_rating_id": TEEN_ID,
            "minor_age_threshold": 21,
        }
        assert _changes(pg_url, "minor_age_threshold")[-1] == (
            body["config_revision"],
            start["minor_age_threshold"],
            21,
            "interpretive",
            "p-operator",
            "deployment",
        )
        assert _changes(pg_url, "default_content_rating_id")[-1][:3] == (
            body["config_revision"],
            start["default_content_rating_id"],
            TEEN_ID,
        )

        same = client.put("/admin/settings", json={"minor_age_threshold": 21}, headers=WRITE)
        assert same.json()["config_revision"] == body["config_revision"]

        retired = _create(client)
        client.delete(f"{BASE}/{retired['id']}", headers=WRITE)
        revision = client.get("/admin/settings", headers=bearer()).json()["config_revision"]
        for rating_id, reason in ((retired["id"], "retired"), ("0" * 26, "unknown")):
            refused = client.put(
                "/admin/settings", json={"default_content_rating_id": rating_id}, headers=WRITE
            )
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "rating_retired"
            assert refused.json()["error"]["details"]["reason"] == reason
        for bad in (
            {"minor_age_threshold": 121},
            {"minor_age_threshold": -1},
            {"minor_age_threshold": None},
            {"unknown": 1},
        ):
            assert client.put("/admin/settings", json=bad, headers=WRITE).status_code == 422
        after = client.get("/admin/settings", headers=bearer()).json()
        assert after["config_revision"] == revision
        assert after["default_content_rating_id"] == TEEN_ID
    finally:
        restored = client.put(
            "/admin/settings",
            json={
                "default_content_rating_id": start["default_content_rating_id"],
                "minor_age_threshold": start["minor_age_threshold"],
            },
            headers=WRITE,
        )
        assert restored.status_code == 200


def test_seed_rows(scratch_database: str) -> None:
    """A fresh database holds the four seeded ratings at version 1, inheriting Everyone → Teen →
    Mature → Explicit, Everyone the default, revision 0, and no ``config_changes`` rows."""
    command.upgrade(alembic_config(scratch_database), "head")
    engine = make_sync_engine(scratch_database)
    try:
        with engine.connect() as conn:
            seeded = conn.execute(
                text(
                    "SELECT r.id, v.name, v.version, v.display_order, v.inherits_from, "
                    "v.policy_ids, v.prompt_guidance FROM content_ratings r "
                    "JOIN content_rating_versions v ON v.version_id = r.current_version_id "
                    "ORDER BY v.display_order"
                )
            ).all()
            state = conn.execute(
                text(
                    "SELECT config_revision, default_content_rating_id, minor_age_threshold "
                    "FROM deployment_state"
                )
            ).all()
            changes = conn.execute(text("SELECT count(*) FROM config_changes")).scalar()
    finally:
        engine.dispose()
    assert [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in seeded] == [
        (EVERYONE_ID, "Everyone", 1, 10, [TEEN_ID], []),
        (TEEN_ID, "Teen", 1, 20, [MATURE_ID], []),
        (MATURE_ID, "Mature", 1, 30, [EXPLICIT_ID], []),
        (EXPLICIT_ID, "Explicit", 1, 40, [], []),
    ]
    assert [r[6] for r in seeded] == [seed.prompt_guidance for seed in SEED_RATINGS]
    assert all("starter guidance" in r[6].lower() for r in seeded)
    assert [tuple(r) for r in state] == [(0, DEFAULT_RATING_ID, 18)]
    assert changes == 0
