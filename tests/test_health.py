import pytest
from fastapi.testclient import TestClient

from epistrel.api import create_app
from tests.auth_support import auth_settings


@pytest.fixture
def client(dummy_dsn: str) -> TestClient:
    return TestClient(create_app(auth_settings(dummy_dsn)))


def test_health_reports_ok(client: TestClient) -> None:
    """ffmpeg and ffprobe are installed wherever the suite runs (#107: CI installs them, and the
    poster tests need them), so both checks read ``ok``."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "database": "skipped",
            "workers": {"alive": None, "last_heartbeat_s": None},
            "oldest_overdue_timer_s": None,
            "oldest_overdue_job_s": None,
            "ffmpeg": "ok",
            "ffprobe": "ok",
            "erasure_pending": None,
        },
        "content_logging": False,
    }


def test_health_reports_missing_ffmpeg_without_failing(dummy_dsn: str) -> None:
    """Without ffmpeg (``EPISTREL_FFMPEG_PATH`` naming nothing) ``/health`` says ``missing``
    for both tools (ffprobe is taken from beside ffmpeg), and the status stays ``ok`` (#107)."""
    client = TestClient(create_app(auth_settings(dummy_dsn, ffmpeg_path="/nowhere/ffmpeg")))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert (body["checks"]["ffmpeg"], body["checks"]["ffprobe"]) == ("missing", "missing")


def test_old_scaffold_routes_are_gone(client: TestClient) -> None:
    assert client.get("/").status_code == 404
    assert client.get("/hello/User").status_code == 404
