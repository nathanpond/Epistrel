"""``/admin/policies`` over REST (FR-GUARD-1,8,9): CRUD with ``kind`` visible, writes recorded
in ``config_changes`` with the principal, the scope rules, and the ``cannot_relax`` refusal."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.core.db import make_engine
from epistrel.core.seeds.ratings import EXPLICIT_ID
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows
from tests.policy_support import unique

BASE = "/admin/policies"
WRITE = {**bearer(), "X-Epistrel-Principal": "p-operator"}


def _db[T](url: str, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def run() -> T:
        engine = make_engine(auth_settings(url))
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _changes(url: str, policy_id: str) -> list[tuple[Any, ...]]:
    found = _db(
        url,
        lambda e: rows(
            e,
            'SELECT config_revision, old, new, "class", actor, scope FROM config_changes '
            "WHERE setting = :s ORDER BY config_revision",
            s=f"policy:{policy_id}",
        ),
    )
    return [tuple(r) for r in found]


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(auth_settings(pg_url))) as c:
        yield c


def _body(**fields: Any) -> dict[str, Any]:
    fields.setdefault("scope", "global")
    fields.setdefault("name", unique("policy"))
    fields.setdefault("rule", {"type": "wordlist", "words": ["forbidden"]})
    fields.setdefault("message", "That is not allowed here.")
    return fields


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    response = client.post(BASE, json=_body(**fields), headers=WRITE)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: Any) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def test_create_get_update_retire(client: TestClient, pg_url: str) -> None:
    """Create (v1, ``kind`` shown, global default targets), PUT (v2 in place), no-op PUT
    (nothing written), DELETE (retired, 200), DELETE again (204); each write is one
    ``config_changes`` row with the principal and class interpretive."""
    slug = unique("no-gore")
    created = _create(client, id=slug)
    assert (created["id"], created["version"], created["kind"]) == (slug, 1, "deterministic")
    assert created["applies_to"] == ["text_input", "text_output"]
    assert (created["created_by"], created["updated_by"], created["effect"]) == (
        "p-operator",
        "p-operator",
        "deny",
    )
    first = _changes(pg_url, slug)
    assert len(first) == 1
    assert first[0][0] == created["config_revision"]
    assert first[0][1] is None
    assert first[0][2]["rule"] == {"type": "wordlist", "words": ["forbidden"]}
    assert first[0][3:] == ("interpretive", "p-operator", "deployment")

    replacement = {
        "name": created["name"],
        "applies_to": ["text_output", "image_prompt"],
        "rule": {"type": "model_assisted", "prompt": "Is this gory?"},
        "age_condition": None,
        "message": "No gore.",
    }
    updated = client.put(
        f"{BASE}/{slug}", json=replacement, headers={**WRITE, "X-Epistrel-Principal": "p-two"}
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert (body["version"], body["kind"], body["updated_by"]) == (2, "model_assisted", "p-two")
    assert body["created_by"] == "p-operator"
    assert body["config_revision"] == created["config_revision"] + 1
    assert _changes(pg_url, slug)[-1][2]["kind"] == "model_assisted"

    same = client.put(f"{BASE}/{slug}", json=replacement, headers=WRITE)
    assert (same.json()["version"], same.json()["config_revision"]) == (
        2,
        body["config_revision"],
    )
    assert len(_changes(pg_url, slug)) == 2

    retired = client.delete(f"{BASE}/{slug}", headers=WRITE)
    assert retired.status_code == 200
    assert retired.json()["retired_at"] is not None
    assert _changes(pg_url, slug)[-1][2] == "retired"
    assert client.delete(f"{BASE}/{slug}", headers=WRITE).status_code == 204
    assert len(_changes(pg_url, slug)) == 3

    again = client.put(f"{BASE}/{slug}", json=replacement, headers=WRITE)
    assert again.status_code == 409
    assert _error(again)["details"]["reason"] == "retired"
    assert client.get(f"{BASE}/{slug}", headers=bearer()).json()["retired_at"] is not None


def test_list_shows_kind_scope_and_hides_retired(client: TestClient) -> None:
    """Every listed policy carries ``kind`` (its model-call cost); ``?scope`` filters;
    retired policies appear only with ``?include_retired=1``."""
    model = _create(client, rule={"type": "model_assisted", "prompt": "Cruel?"})
    rating = _create(client, scope="rating", scope_id=EXPLICIT_ID)
    gone = _create(client)
    client.delete(f"{BASE}/{gone['id']}", headers=WRITE)

    listed = {p["id"]: p for p in client.get(BASE, headers=bearer()).json()["items"]}
    assert listed[model["id"]]["kind"] == "model_assisted"
    assert listed[rating["id"]]["applies_to"] == ["text_output"]
    assert gone["id"] not in listed
    assert all({"kind", "scope", "applies_to"} <= p.keys() for p in listed.values())

    scoped = client.get(BASE, params={"scope": "rating"}, headers=bearer()).json()["items"]
    assert {p["scope"] for p in scoped} == {"rating"}
    everything = client.get(BASE, params={"include_retired": 1}, headers=bearer()).json()
    assert gone["id"] in {p["id"] for p in everything["items"]}


@pytest.mark.parametrize("effect", ["allow", "exempt", "permit"])
def test_relaxing_effect_refused_even_globally(client: TestClient, effect: str) -> None:
    """``deny`` is the only effect: ``allow``/``exempt`` are ``cannot_relax`` at every scope."""
    name = unique("relax")
    response = client.post(BASE, json=_body(name=name, effect=effect), headers=WRITE)
    assert response.status_code == 422
    assert _error(response)["code"] == "cannot_relax"
    assert _error(response)["details"]["reason"] == "effect"
    listed = client.get(BASE, params={"include_retired": 1}, headers=bearer()).json()["items"]
    assert name not in {p["name"] for p in listed}

    existing = _create(client)
    put = client.put(
        f"{BASE}/{existing['id']}",
        json={
            "name": existing["name"],
            "applies_to": existing["applies_to"],
            "rule": existing["rule"],
            "age_condition": None,
            "message": existing["message"],
            "effect": effect,
        },
        headers=WRITE,
    )
    assert _error(put)["code"] == "cannot_relax"


def test_validation_refusals(client: TestClient) -> None:
    """Empty targets, entity lists on global, a missing or unknown rating, a global scope_id,
    story scopes on the operator surface, name clashes (any case), taken ids, and limits."""

    def refused(status: int = 422, **fields: Any) -> dict[str, Any]:
        response = client.post(BASE, json=_body(**fields), headers=WRITE)
        assert response.status_code == status, response.text
        return _error(response)

    refused(applies_to=[])
    refused(applies_to=["speech"])
    assert refused(rule={"type": "entity_list", "names": ["Mara"]})["details"]["reason"] == (
        "entity_list_on_global"
    )
    assert refused(scope="rating")["details"]["reason"] == "required"
    assert refused(scope="rating", scope_id="nope")["details"]["reason"] == "unknown"
    assert refused(scope_id=EXPLICIT_ID)["details"]["reason"] == "global"
    refused(scope="story")
    refused(name="x" * 81)
    refused(message="m" * 501)
    refused(id="bad id!")
    refused(kind="deterministic")

    first = _create(client)
    clash = refused(name=first["name"].upper())
    assert clash["details"]["conflict"] == first["id"]
    assert refused(409, id=first["id"])["details"]["reason"] == "id_taken"
    assert client.get(f"{BASE}/missing-id", headers=bearer()).status_code == 404
    assert (
        client.put(
            f"{BASE}/missing-id",
            json={
                "name": "n",
                "applies_to": ["text_output"],
                "rule": {"type": "wordlist", "words": ["w"]},
                "age_condition": None,
                "message": "m",
            },
            headers=WRITE,
        ).status_code
        == 404
    )


def test_idempotent_create(client: TestClient, pg_url: str) -> None:
    """A repeated ``Idempotency-Key`` returns the first policy and writes nothing more."""
    key = unique("key")
    headers = {**WRITE, "Idempotency-Key": key}
    body = _body()
    first = client.post(BASE, json=body, headers=headers)
    second = client.post(BASE, json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["config_revision"] == second.json()["config_revision"]
    assert len(_changes(pg_url, first.json()["id"])) == 1


def test_mutations_need_a_principal(client: TestClient) -> None:
    response = client.post(BASE, json=_body(), headers=bearer())
    assert response.status_code == 400
    assert _error(response)["code"] == "principal_required"


def test_rating_policy_ids_must_be_live_rating_policies(client: TestClient) -> None:
    """#48's ``policy_ids`` now resolve here: a global, retired or unknown id is refused."""
    rating_policy = _create(client, scope="rating", scope_id=EXPLICIT_ID)
    global_policy = _create(client)
    retired = _create(client, scope="rating", scope_id=EXPLICIT_ID)
    client.delete(f"{BASE}/{retired['id']}", headers=WRITE)

    def rating(policy_ids: list[str]) -> Any:
        return client.post(
            "/admin/content-ratings",
            json={"name": unique("rating"), "policy_ids": policy_ids},
            headers=WRITE,
        )

    assert rating([rating_policy["id"]]).status_code == 201
    for bad in (global_policy["id"], retired["id"], "no-such-policy"):
        response = rating([rating_policy["id"], bad])
        assert response.status_code == 422, bad
        assert _error(response)["details"] == {"field": "body.policy_ids", "unknown": [bad]}
