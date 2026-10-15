"""Helpers for the erasure ledger's tests (#125): a PostgreSQL container of their own (its
``pg_dump``/``pg_restore`` run through ``docker exec``), a fresh migrated database per test, the
app started with its lifespan, and a story that names a subject.

The session database is never used: these tests restore over a database, delete ledger rows
and compare the whole ledger with a file, which would disturb every other test's rows. With
``EPISTREL_TEST_DATABASE_URL`` set they fail rather than skip: they need a server they own.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from alembic import command
from sqlalchemy import text

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.ids import new_ulid
from epistrel.engine import Engine
from tests import erasure_support as e
from tests.conftest import OVERRIDE_VAR, PG_IMAGE, alembic_config
from tests.respond_support import new_story
from tests.story_support import WRITE

SUBJECT = "Marisol Quenby"
"""The subject the ledger tests erase: a name no other test uses."""
BYSTANDER = "Tobiah Fennimore"
PLAYER_NAME = "Pell"
PIN = f"{SUBJECT} keeps the lighthouse key; {BYSTANDER} keeps the boat."
OTHER_PIN = f"{BYSTANDER} mends nets on the quay."


@dataclass(frozen=True)
class Server:
    """A PostgreSQL container of the test module's own."""

    container: Any
    url: str

    @property
    def container_id(self) -> str:
        return str(self.container.get_wrapped_container().id)

    def database_url(self, name: str) -> str:
        base, _, _ = self.url.rpartition("/")
        return f"{base}/{name}"

    def fresh(self) -> str:
        """A new database on the server, migrated to head: its URL."""
        name = f"epistrel_ledger_{new_ulid().lower()}"
        self.psql(f'CREATE DATABASE "{name}"', database=self.container.dbname)
        url = self.database_url(name)
        command.upgrade(alembic_config(url), "head")
        return url

    def _exec(
        self, *args: str, stdin: Any = None, stdout: Any = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["docker", "exec", "-i", self.container_id, *args],  # noqa: S607
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )

    def psql(self, sql: str, *, database: str) -> None:
        done = self._exec(
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            self.container.username,
            "-d",
            database,
            "-c",
            sql,
        )
        assert done.returncode == 0, done.stderr.decode()

    def dump(self, url: str, target: Path) -> Path:
        """``pg_dump -Fc`` of ``url``'s database, inside the container, to ``target``."""
        with target.open("wb") as out:
            done = self._exec(
                "pg_dump", "-Fc", "-U", self.container.username, "-d", _name(url), stdout=out
            )
        assert done.returncode == 0, done.stderr.decode()
        return target

    def restore(self, url: str, dump: Path) -> None:
        """``pg_restore --clean --if-exists`` of ``dump`` over ``url``'s database."""
        with dump.open("rb") as archive:
            done = self._exec(
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "-U",
                self.container.username,
                "-d",
                _name(url),
                stdin=archive,
            )
        assert done.returncode == 0, done.stderr.decode()

    def pg_restore_wrapper(self, directory: Path) -> Path:
        """An executable that runs the container's ``pg_restore`` with its arguments and
        standard input: what ``epistrel backup verify --pg-restore`` is given."""
        script = directory / "pg_restore"
        script.write_text(f'#!/bin/sh\nexec docker exec -i {self.container_id} pg_restore "$@"\n')
        script.chmod(0o755)
        return script


def _name(url: str) -> str:
    return url.rpartition("/")[2]


@contextmanager
def dedicated_server() -> Iterator[Server]:
    """A PostgreSQL container of the caller's own (fails, never skips, without Docker); each
    test module wraps it in a module-scoped fixture."""
    if os.environ.get(OVERRIDE_VAR):
        pytest.fail(
            f"The erasure ledger's tests restore over databases of a server they own; they need "
            f"Docker and cannot run against {OVERRIDE_VAR}.",
            pytrace=False,
        )
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(PG_IMAGE, driver="psycopg")
        container.start()
    except Exception as exc:
        pytest.fail(f"Docker is needed: {type(exc).__name__}: {exc}", pytrace=False)
    try:
        yield Server(container, container.get_connection_url())
    finally:
        container.stop()


def settings(url: str, ledger_dir: Path, **overrides: Any) -> Settings:
    return e.erasure_settings(url, erasure_ledger_dir=ledger_dir, **overrides)


@asynccontextmanager
async def started(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, Engine]]:
    """The app with its lifespan run (the ledger compared, the reapply started), and a client."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app.state.epistrel


@dataclass(frozen=True)
class Stories:
    """The subject's story (with ``subject`` and ``bystander``) and one that never named her."""

    named: str
    subject: str
    bystander: str
    other: str


async def two_stories(client: httpx.AsyncClient, **fields: Any) -> Stories:
    """``fields`` (a ``name``, say) go to the subject's story."""
    named, ids = await new_story(client, PLAYER_NAME, SUBJECT, BYSTANDER, **fields)
    other, _ = await new_story(client, PLAYER_NAME, BYSTANDER)
    for story, pin in ((named, PIN), (other, OTHER_PIN)):
        done = await client.put(
            f"/stories/{story}/pinned-memories", json={"items": [{"text": pin}]}, headers=WRITE
        )
        assert done.status_code == 200, done.text
    return Stories(named, ids[SUBJECT], ids[BYSTANDER], other)


async def pins(url: str, story_id: str) -> list[str]:
    db = make_engine_for(url)
    try:
        async with db.connect() as conn:
            found = await conn.scalar(
                text("SELECT pinned FROM story_texts WHERE story_id = :s"), {"s": story_id}
            )
    finally:
        await db.dispose()
    return [item["text"] for item in found or []]


def make_engine_for(url: str) -> Any:
    return make_engine(e.erasure_settings(url))


async def ledger_rows(url: str) -> list[Any]:
    db = make_engine_for(url)
    try:
        async with db.connect() as conn:
            return list(await conn.execute(text("SELECT * FROM erasure_ledger ORDER BY at, id")))
    finally:
        await db.dispose()


async def sql(url: str, statement: str, **params: Any) -> list[Any]:
    """Run one statement in its own transaction; its rows (when it returns any)."""
    db = make_engine_for(url)
    try:
        async with db.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result) if result.returns_rows else []
    finally:
        await db.dispose()


__all__ = [
    "BYSTANDER",
    "OTHER_PIN",
    "PIN",
    "PLAYER_NAME",
    "SUBJECT",
    "Server",
    "Stories",
    "dedicated_server",
    "ledger_rows",
    "pins",
    "settings",
    "sql",
    "started",
    "two_stories",
]
