"""Fixtures for the deployment smoke (C-10, #77): a live compose stack and one fake model.

The stack is started outside pytest (``docker compose -f docker-compose.yml -f
tests/smoke/compose.override.yml up -d --no-build --wait``); these fixtures only reach it. The
settings are read when this module is imported, because the root conftest strips every
``EPISTREL_*`` variable around each test:

- ``EPISTREL_SMOKE_BASE_URL`` (required; e.g. ``http://127.0.0.1:8000``): the engine. Unset is a
  failure, never a skip: ``-m smoke`` was asked for and must prove something.
- ``EPISTREL_SMOKE_CREDENTIAL``: a service credential; defaults to the compose file's dev-only one.
- ``EPISTREL_SMOKE_FAKE_PORT`` (default 1234): where ``fake_openai.py`` listens on the host.
- ``EPISTREL_SMOKE_MODEL_HOST`` (default ``host.docker.internal``): the host as the engine
  container sees it.
- ``EPISTREL_SMOKE_COMPOSE_PROJECT`` (default ``epistrel``): the compose project to inspect and
  disturb (``docker compose ps``, stopping ``db``, restarting ``engine``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
import yaml

from tests.conftest import REPO_ROOT

SMOKE_DIR = Path(__file__).resolve().parent
COMPOSE_FILES = (REPO_ROOT / "docker-compose.yml", SMOKE_DIR / "compose.override.yml")
READY_TIMEOUT_S = 120.0
PRINCIPAL = "smoke-runner"


def _dev_credential() -> str:
    compose = yaml.safe_load(COMPOSE_FILES[0].read_text())
    return str(compose["services"]["engine"]["environment"]["EPISTREL_SERVICE_CREDENTIALS"])


BASE_URL = os.environ.get("EPISTREL_SMOKE_BASE_URL", "").rstrip("/")
CREDENTIAL = os.environ.get("EPISTREL_SMOKE_CREDENTIAL") or _dev_credential()
FAKE_PORT = int(os.environ.get("EPISTREL_SMOKE_FAKE_PORT", "1234"))
MODEL_HOST = os.environ.get("EPISTREL_SMOKE_MODEL_HOST", "host.docker.internal")
PROJECT = os.environ.get("EPISTREL_SMOKE_COMPOSE_PROJECT", "epistrel")


@dataclass(frozen=True)
class Stack:
    """The running deployment: an authenticated client and the compose project behind it."""

    client: httpx.Client
    fake: httpx.Client

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.get(path, **kwargs)

    def write(self, method: str, path: str, json: Any = None) -> httpx.Response:
        return self.client.request(
            method, path, json=json, headers={"X-Epistrel-Principal": PRINCIPAL}
        )

    def calls(self) -> list[dict[str, Any]]:
        response = self.fake.get("/_calls")
        response.raise_for_status()
        calls: list[dict[str, Any]] = response.json()
        return calls


def compose(*args: str, stdin: str | None = None, timeout: float = 180.0) -> str:
    """``docker compose -p <project> -f … <args>``; its stdout. A failure fails the test."""
    files = [arg for path in COMPOSE_FILES for arg in ("-f", str(path))]
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, *files, *args],  # noqa: S607 - docker on PATH
        cwd=REPO_ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, f"docker compose {' '.join(args)}: {result.stderr}"
    return result.stdout


def sql(statement: str, **params: str) -> str:
    """One statement through ``psql`` in the ``db`` container, its unaligned output. Values are
    bound as psql variables (``:'name'`` in the statement), never formatted into the text."""
    variables = [arg for name, value in params.items() for arg in ("-v", f"{name}={value}")]
    return compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "epistrel",
        "-d",
        "epistrel",
        "-v",
        "ON_ERROR_STOP=1",
        "-tAq",
        *variables,
        "-f",
        "-",
        stdin=statement,
    ).strip()


def wait_for_health(client: httpx.Client, *, want: str = "ok") -> dict[str, Any]:
    """Poll ``/health`` until its status is ``want`` with a live worker; the last body."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            response = client.get("/health")
            body = response.json()
            alive = (body.get("checks", {}).get("workers") or {}).get("alive") or 0
            if body.get("status") == want and (want != "ok" or alive >= 1):
                return body
        except httpx.HTTPError, ValueError:
            pass
        time.sleep(1.0)
    pytest.fail(f"/health did not reach {want!r} with a live worker in {READY_TIMEOUT_S}s: {body}")


@pytest.fixture(scope="session")
def fake_model() -> Iterator[httpx.Client]:
    """``fake_openai.py`` on the host, bound to every interface so the container reaches it."""
    process = subprocess.Popen(
        [sys.executable, str(SMOKE_DIR / "fake_openai.py"), "--port", str(FAKE_PORT)],
        cwd=REPO_ROOT,
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{FAKE_PORT}", timeout=5.0)
    try:
        deadline = time.monotonic() + 30.0
        while True:
            assert process.poll() is None, f"fake_openai.py exited {process.returncode}"
            try:
                if client.get("/v1/models").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "fake_openai.py did not start in 30 s"
            time.sleep(0.2)
        yield client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def stack(fake_model: httpx.Client) -> Iterator[Stack]:
    """The live engine, healthy, with ``default`` pointed at the fake: one model endpoint."""
    if not BASE_URL:
        pytest.fail(
            "the smoke suite needs a running stack: set EPISTREL_SMOKE_BASE_URL (e.g. "
            "http://127.0.0.1:8000) after `docker compose -f docker-compose.yml "
            "-f tests/smoke/compose.override.yml up -d --wait`"
        )
    client = httpx.Client(
        base_url=BASE_URL, headers={"Authorization": f"Bearer {CREDENTIAL}"}, timeout=60.0
    )
    try:
        wait_for_health(client)
        running = Stack(client, fake_model)
        configured = running.write(
            "PUT",
            "/admin/model-config/default",
            {"base_url": f"http://{MODEL_HOST}:{FAKE_PORT}/v1", "model": "fake-1"},
        )
        assert configured.status_code == 200, configured.text
        keyed = running.write(
            "PUT", "/admin/model-config/default/api-key", {"api_key": "fake-key-not-a-secret"}
        )
        assert keyed.status_code in (200, 204), keyed.text
        yield running
    finally:
        client.close()
