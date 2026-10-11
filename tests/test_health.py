import pytest
from fastapi.testclient import TestClient

from epistrel.api import create_app
from tests.auth_support import auth_settings


@pytest.fixture
def client(dummy_dsn: str) -> TestClient:
    return TestClient(create_app(auth_settings(dummy_dsn)))


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "database": "skipped",
            "workers": {"alive": None, "last_heartbeat_s": None},
            "oldest_overdue_timer_s": None,
            "oldest_overdue_job_s": None,
        },
    }


def test_old_scaffold_routes_are_gone(client: TestClient) -> None:
    assert client.get("/").status_code == 404
    assert client.get("/hello/User").status_code == 404
