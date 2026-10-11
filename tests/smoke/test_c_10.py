"""C-10 — deployment smoke (NFR-OPS-1,2; docs/04 §5; #77).

A clean ``docker compose up`` plus one model endpoint is a working deployment with no external
broker, cache or scheduler. These tests run against that live stack (``EPISTREL_SMOKE_BASE_URL``,
see ``conftest.py``), with ``fake_openai.py`` on the host as the one endpoint, so every reply
below really crossed the network from the engine container to a model server.

Each slice is its own ``test_c_10__<slice>`` function and names its complement. The last two
disturb the stack (they stop ``db``, then restart ``engine``) and leave it healthy again.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx2 as httpx
import pytest

from tests.smoke.conftest import BASE_URL, CREDENTIAL, Stack, compose, sql, wait_for_health
from tests.smoke.fake_openai import MODEL_ID, REPLY

ADULT_CAST = [
    {
        "name": "Wren",
        "definition": "A traveller, the player's character.",
        "true_age": 24,
        "is_player": True,
    },
    {"name": "Ada", "definition": "An innkeeper who trusts no one.", "true_age": 41},
]


def _story(stack: Stack, name: str) -> dict[str, Any]:
    created = stack.write("POST", "/stories", {"name": name, "characters": ADULT_CAST})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


def _respond(stack: Stack, story: dict[str, Any], say: str) -> dict[str, Any]:
    wren = story["characters"][0]["id"]
    response = stack.write(
        "POST",
        f"/stories/{story['story_id']}/respond",
        {"actor_character_id": wren, "player_input": {"say": say}},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__health(stack: Stack) -> None:
    """``/health`` is ``ok``: the database answers and the workers that own every timed job run
    in the engine process (``all`` mode). Complement: it needs no credential."""
    body = wait_for_health(stack.client)
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["workers"]["alive"] >= 1
    assert body["content_logging"] is False
    anonymous = httpx.get(f"{BASE_URL}/health", timeout=10.0)
    assert anonymous.status_code == 200


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__admin_requires_credential(stack: Stack) -> None:
    """``/admin`` answers the service credential. Complement: no credential, or a wrong one, is
    401 and reveals nothing."""
    assert stack.get("/admin/settings").status_code == 200
    missing = httpx.get(f"{BASE_URL}/admin/settings", timeout=10.0)
    wrong = httpx.get(
        f"{BASE_URL}/admin/settings",
        headers={"Authorization": f"Bearer {'x' * len(CREDENTIAL)}"},
        timeout=10.0,
    )
    for response in (missing, wrong):
        assert response.status_code == 401, response.text
        assert "default_safe_fallback" not in response.text


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__respond(stack: Stack, fake_model: httpx.Client) -> None:
    """Create a story and play a turn: the reply is the fake endpoint's, so the engine called
    the one configured model with its key. Complement: before the turn, nothing called it."""
    fake_model.delete("/_calls")
    story = _story(stack, "Smoke: respond")
    assert stack.calls() == []
    played = _respond(stack, story, "Is there a room for the night?")
    assert played["message"]["text"] == REPLY
    assert played["turn"] == 1
    calls = stack.calls()
    generations = [call for call in calls if not call["structured"]]
    assert generations, calls
    assert all(call["model"] == MODEL_ID and call["authorized"] for call in calls)


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__ingest(stack: Stack) -> None:
    """Ingest records a client-authored event. Complement: the same ``client_event_id`` again
    is not recorded twice."""
    story = _story(stack, "Smoke: ingest")
    body = {
        "events": [
            {"client_event_id": "smoke-1", "kind": "narration", "text": "Rain on the shutters."}
        ]
    }
    first = stack.write("POST", f"/stories/{story['story_id']}/ingest", body)
    assert first.status_code == 201, first.text
    after_first = stack.get(f"/stories/{story['story_id']}").json()
    again = stack.write("POST", f"/stories/{story['story_id']}/ingest", body)
    assert again.status_code in (200, 201), again.text
    (recorded,) = first.json()["results"]
    (replayed,) = again.json()["results"]
    assert recorded["replayed"] is False
    assert replayed == {**recorded, "replayed": True}
    assert stack.get(f"/stories/{story['story_id']}").json() == after_first


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__truncate(stack: Stack) -> None:
    """Truncate rewinds to a turn. Complement: the next turn is numbered from there, not from
    the discarded head."""
    story = _story(stack, "Smoke: truncate")
    _respond(stack, story, "Hello?")
    _respond(stack, story, "Anyone here?")
    truncated = stack.write("POST", f"/stories/{story['story_id']}/truncate", {"to_turn": 1})
    assert truncated.status_code == 200, truncated.text
    assert truncated.json()["next_turn_index"] == 2
    assert _respond(stack, story, "Hello again.")["turn"] == 2


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__fork(stack: Stack) -> None:
    """Fork copies a story through a turn into a new, independent story. Complement: the source
    is unchanged."""
    story = _story(stack, "Smoke: fork")
    _respond(stack, story, "Hello?")
    _respond(stack, story, "Anyone here?")
    before = stack.get(f"/stories/{story['story_id']}").json()
    forked = stack.write(
        "POST", f"/stories/{story['story_id']}/fork", {"at_turn": 1, "name": "Smoke fork"}
    )
    assert forked.status_code == 201, forked.text
    fork = forked.json()
    assert fork["story_id"] != story["story_id"]
    assert fork["next_turn_index"] == 2
    assert stack.get(f"/stories/{fork['story_id']}").status_code == 200
    assert stack.get(f"/stories/{story['story_id']}").json() == before


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__rebuild(stack: Stack) -> None:
    """Rebuilding a story's projections from its event log succeeds. Complement: the story reads
    the same afterwards."""
    story = _story(stack, "Smoke: rebuild")
    _respond(stack, story, "Hello?")
    before = stack.get(f"/stories/{story['story_id']}/history").json()
    rebuilt = stack.write("POST", f"/admin/stories/{story['story_id']}/rebuild")
    assert rebuilt.status_code in (200, 202), rebuilt.text
    assert stack.get(f"/stories/{story['story_id']}/history").json() == before


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__metrics(stack: Stack) -> None:
    """``/metrics`` serves Prometheus text with no credential. Complement: it counts the
    operations the smoke ran."""
    response = httpx.get(f"{BASE_URL}/metrics", timeout=10.0)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "epistrel_operations_total" in response.text


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__compose_services(stack: Stack) -> None:
    """The deployment is exactly ``engine`` and ``db`` (NFR-OPS-2). Complement: no broker,
    cache or scheduler service is defined or running."""
    defined = set(compose("config", "--services").split())
    running = set(compose("ps", "--services", "--status", "running").split())
    assert defined == {"engine", "db"}
    assert running == {"engine", "db"}


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__database_loss(stack: Stack) -> None:
    """With ``db`` stopped, a request is 503 ``database_unavailable`` (#75) and ``/health`` is
    ``unavailable``; started again, the same engine serves it. Complement: never a 500."""
    story = _story(stack, "Smoke: database loss")
    path = f"/stories/{story['story_id']}"
    compose("stop", "db")
    try:
        lost = stack.get(path)
        assert lost.status_code == 503, lost.text
        assert lost.json()["error"]["code"] == "database_unavailable"
        assert stack.get("/health").json()["status"] == "unavailable"
    finally:
        compose("start", "db")
    wait_for_health(stack.client)
    assert stack.get(path).status_code == 200


@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_c_10__orphan_recovery(stack: Stack) -> None:
    """An operation a crash left ``in_flight`` is failed by the start-up recovery pass when the
    engine restarts (#75). Complement: a committed operation is left alone."""
    story = _story(stack, "Smoke: orphan")
    _respond(stack, story, "Hello?")
    orphan = "01" + os.urandom(12).hex().upper()  # 26 Crockford characters: a valid ULID
    sql(
        "INSERT INTO operations (operation_id, story_id, kind, principal, idempotency_key, "
        "started_at) VALUES (:'op', :'story', 'maintenance', 'smoke', 'smoke-orphan', "
        "now() - interval '10 minutes')",
        op=orphan,
        story=story["story_id"],
    )
    assert _status(orphan) == "in_flight"
    compose("restart", "engine")
    wait_for_health(stack.client)
    deadline = time.monotonic() + 60.0
    while _status(orphan) == "in_flight" and time.monotonic() < deadline:
        time.sleep(1.0)
    assert _status(orphan) == "failed"
    committed = sql(
        "SELECT count(*) FROM operations WHERE story_id = :'story' "
        "AND kind = 'turn' AND status = 'committed'",
        story=story["story_id"],
    )
    assert committed == "1"


def _status(operation_id: str) -> str:
    return sql("SELECT status FROM operations WHERE operation_id = :'op'", op=operation_id)
