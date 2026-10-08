import pytest
from fastapi.testclient import TestClient

from epistrel.api import create_app
from epistrel.config import Settings


@pytest.fixture
def client(dummy_dsn: str) -> TestClient:
    return TestClient(create_app(Settings(database_url=dummy_dsn)))


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "skipped"}}


def test_old_scaffold_routes_are_gone(client: TestClient) -> None:
    assert client.get("/").status_code == 404
    assert client.get("/hello/User").status_code == 404
