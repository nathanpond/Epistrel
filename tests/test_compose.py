"""docker-compose.yml carries the dev-only guard rails the story requires."""

from __future__ import annotations

import base64
import re
from typing import Any

import pytest
import yaml

from epistrel.config import load_settings
from epistrel.testing import postgres
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


AUTH_DEV_ONLY_COMMENT = "# dev-only default — override for anything but local use"


def test_dev_only_credential_and_config_key_are_labelled() -> None:
    """The engine starts under compose with no prior configuration, and both secrets say so."""
    text = COMPOSE.read_text()
    for name in ("EPISTREL_SERVICE_CREDENTIALS", "EPISTREL_CONFIG_KEY"):
        line = next(line for line in text.splitlines() if f"{name}:" in line)
        assert AUTH_DEV_ONLY_COMMENT in line, name
    environment = _compose()["services"]["engine"]["environment"]
    assert len(environment["EPISTREL_SERVICE_CREDENTIALS"]) >= 32
    assert len(base64.urlsafe_b64decode(environment["EPISTREL_CONFIG_KEY"])) == 32


def test_compose_environment_loads_as_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _compose()["services"]["engine"]["environment"].items():
        monkeypatch.setenv(name, str(value))
    assert load_settings().service_credentials


MINIO_OVERRIDE = REPO_ROOT / "docker" / "compose.minio.yml"


def test_the_minio_override_is_a_separate_file_on_the_tests_image() -> None:
    """MinIO for local S3 development (#107) is an override file, not a profile of the default
    stack (which stays ``engine`` and ``db``). It runs the image the S3 tests pin, on a loopback
    port, creates the bucket before the engine starts, and points the engine at it; its
    secrets say they are dev-only."""
    from tests.conftest import MINIO_IMAGE

    assert set(_compose()["services"]) == {"engine", "db"}
    override: dict[str, Any] = yaml.safe_load(MINIO_OVERRIDE.read_text())
    services = override["services"]
    assert services["minio"]["image"] == MINIO_IMAGE
    assert services["minio-bucket"]["image"] == MINIO_IMAGE
    assert services["minio"]["ports"] == ["127.0.0.1:9000:9000"]
    assert "restart" not in services["minio"]
    bucket = services["engine"]["depends_on"]["minio-bucket"]
    assert bucket["condition"] == "service_completed_successfully"
    engine = services["engine"]["environment"]
    assert engine["EPISTREL_MEDIA_STORE"] == "s3"
    assert engine["EPISTREL_MEDIA_S3_BUCKET"] in services["minio-bucket"]["command"][0]
    text = MINIO_OVERRIDE.read_text()
    for name in ("MINIO_ROOT_PASSWORD", "EPISTREL_MEDIA_S3_SECRET_KEY"):
        line = next(line for line in text.splitlines() if f"{name}:" in line)
        assert AUTH_DEV_ONLY_COMMENT in line, name


def _server_settings(command: list[str]) -> dict[str, str]:
    """The ``-c name=value`` settings of a ``postgres`` command line."""
    assert command[0] == "postgres", command
    flags = command[1:]
    assert len(flags) % 2 == 0, command
    assert all(f == "-c" for f in flags[::2]), command
    return dict(flag.split("=", 1) for flag in flags[1::2])


def _megabytes(size: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmg]?)b?", size.strip().lower())
    assert match, size
    return float(match[1]) * {"k": 1 / 1024, "m": 1, "g": 1024, "": 1 / 1024**2}[match[2]]


def test_the_lock_table_is_sized_for_the_stated_story_count() -> None:
    """#784 option (a), ADR-191: compose's database runs with a ``max_locks_per_transaction``
    and a ``/dev/shm`` that ADR-188's rule needs for ``COVERED_STORIES`` stories on its
    connections, and with the value every test server runs (``epistrel.testing.postgres``)."""
    db = _compose()["services"]["db"]
    settings = _server_settings(db["command"])
    locks = int(settings["max_locks_per_transaction"])
    connections = int(settings.get("max_connections", postgres.DEFAULT_MAX_CONNECTIONS)) + int(
        settings.get("max_prepared_transactions", 0)
    )
    assert locks >= postgres.required_locks(postgres.COVERED_STORIES, connections)
    assert locks == postgres.MAX_LOCKS_PER_TRANSACTION
    assert postgres.server_args() == ["-c", f"max_locks_per_transaction={locks}"]
    shm = _megabytes(str(db["shm_size"]))
    assert shm >= postgres.required_shm_mb(postgres.COVERED_STORIES)
    assert shm == postgres.SHM_SIZE_MB
    # The rule's own arithmetic (ADR-188): 16 * 6,400 / 100 = 1,024; 64 + 8 * 6.4 = 115.2 MB.
    assert postgres.required_locks(6400) == 1024
    assert postgres.required_locks(6401) == 2048
    assert postgres.required_locks(100) == 64
    assert postgres.required_shm_mb(6400) == pytest.approx(115.2)
