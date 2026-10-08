"""docker-compose.yml carries the dev-only guard rails the story requires."""

from __future__ import annotations

from typing import Any

import yaml

from tests.conftest import PG_IMAGE, REPO_ROOT

COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_ONLY_COMMENT = "# dev-only default — override EPISTREL_DATABASE_URL for anything but local use"


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return loaded


def test_dev_only_password_is_labelled() -> None:
    text = COMPOSE.read_text()
    password_line = next(line for line in text.splitlines() if "POSTGRES_PASSWORD" in line)
    assert DEV_ONLY_COMMENT in password_line


def test_engine_reaches_host_model_server() -> None:
    engine = _compose()["services"]["engine"]
    assert "host.docker.internal:host-gateway" in engine["extra_hosts"]


def test_db_has_healthcheck_and_engine_waits_for_it() -> None:
    services = _compose()["services"]
    assert "pg_isready" in " ".join(services["db"]["healthcheck"]["test"])
    assert services["engine"]["depends_on"]["db"]["condition"] == "service_healthy"


def test_compose_uses_the_same_postgres_image_as_tests() -> None:
    assert _compose()["services"]["db"]["image"] == PG_IMAGE


def test_no_restart_policy_and_loopback_ports() -> None:
    services = _compose()["services"]
    assert "restart" not in services["engine"]
    assert "restart" not in services["db"]
    assert "127.0.0.1:5432:5432" in services["db"]["ports"]
    assert "127.0.0.1:8000:8000" in services["engine"]["ports"]
