"""`/health` reflects the database: ok when it answers, 503 when it does not; the app survives."""

from __future__ import annotations

import socket

from fastapi.testclient import TestClient

from epistrel.api import create_app
from epistrel.config import Settings


def _closed_port() -> int:
    """Bind an ephemeral port and release it, so nothing is listening there."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_health_reports_database_ok(pg_url: str) -> None:
    with TestClient(create_app(Settings(database_url=pg_url))) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_health_reports_database_error_and_keeps_serving() -> None:
    dsn = f"postgresql+psycopg://u:p@127.0.0.1:{_closed_port()}/nowhere"
    with TestClient(create_app(Settings(database_url=dsn))) as client:
        first = client.get("/health")
        second = client.get("/health")  # the process did not exit: a second request is answered
    assert first.status_code == 503
    assert first.json() == {"status": "unavailable", "checks": {"database": "error"}}
    assert second.status_code == 503
    assert second.json()["checks"]["database"] == "error"
