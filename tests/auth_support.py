"""Shared fixtures for the trust-boundary tests: credentials, settings, and a test-only router."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.config import Settings

CREDENTIAL_A = "test-credential-a-0123456789abcdef0123"
CREDENTIAL_B = "test-credential-b-0123456789abcdef0123"
TEST_PREFIX = "/_test"
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="epistrel-test-media-"))
"""The blob store every test app uses unless it names its own (#105): one per test process,
removed when the process exits, so a test never writes media under the repository."""
atexit.register(shutil.rmtree, MEDIA_ROOT, ignore_errors=True)
ERASURE_LEDGER_DIR = Path(tempfile.mkdtemp(prefix="epistrel-test-ledger-"))
"""The erasure ledger's file every test app uses unless it names its own (#125): one per test
process, like ``MEDIA_ROOT``, so it matches the session database and never lands in the
repository (``conftest`` also sets ``EPISTREL_ERASURE_LEDGER_DIR`` to it)."""
atexit.register(shutil.rmtree, ERASURE_LEDGER_DIR, ignore_errors=True)
SCRATCH_LEDGERS = Path(tempfile.mkdtemp(prefix="epistrel-test-scratch-ledgers-"))
"""One erasure ledger directory per other database (a scratch database of
``migrated_database``, a server of a test's own), under this root, so that each database has its
own ledger file, as a deployment does (#125). ``ERASURE_LEDGER_DIR`` belongs to the session
database alone: an erasure on a scratch database that wrote the session's file would come back
as a pending entry in the session database the next time an app there reconciles."""
atexit.register(shutil.rmtree, SCRATCH_LEDGERS, ignore_errors=True)
_session_database: str | None = None


def database_key(dsn: str) -> str:
    """The server and database a URL names (no credentials, no driver), as a directory name."""
    parts = urlsplit(dsn)
    return f"{parts.hostname}-{parts.port}-{parts.path.lstrip('/')}"


def note_session_database(dsn: str) -> None:
    """Record the session database (``pg_url``): its apps use ``ERASURE_LEDGER_DIR``."""
    global _session_database
    _session_database = database_key(dsn)


def ledger_dir_for(dsn: str) -> Path:
    """``ERASURE_LEDGER_DIR`` for the session database (or before one is known); for any other
    database, a directory of its own under ``SCRATCH_LEDGERS``, shared by every app on it."""
    key = database_key(dsn)
    if _session_database is None or key == _session_database:
        return ERASURE_LEDGER_DIR
    path = SCRATCH_LEDGERS / key
    path.mkdir(exist_ok=True)
    return path


def forget_ledger(dsn: str) -> None:
    """Remove a scratch database's ledger directory (it was created or dropped), so a database
    made again under the same name starts with an empty ledger."""
    if database_key(dsn) != _session_database:
        shutil.rmtree(SCRATCH_LEDGERS / database_key(dsn), ignore_errors=True)


def auth_settings(dsn: str, **overrides: object) -> Settings:
    # ``api`` unless a test asks for workers: an app under test must not start a job worker
    # that fires the seeded sweeps behind the test's back (#71; X-07 starts its own).
    values: dict[str, object] = {
        "database_url": dsn,
        "service_credentials": [CREDENTIAL_A, CREDENTIAL_B],
        "mode": "api",
        "media_root": MEDIA_ROOT,
        "erasure_ledger_dir": ledger_dir_for(dsn),
        # Tests change configuration behind the revisions (test-only rules, raw SQL): resolve it
        # every time (#113; ``test_the_cache_follows_every_layer`` turns it on).
        "effective_config_cache": False,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def bearer(credential: str = CREDENTIAL_A) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


class Payload(BaseModel):
    value: int


def make_test_router() -> APIRouter:
    """A mutating route and a read route, registered only by tests (never by `create_app`)."""
    router = APIRouter(prefix=TEST_PREFIX)

    @router.post("/write")
    async def write(payload: Payload, ctx: ContextDep, engine: EngineDep) -> dict[str, object]:
        return {"principal": await engine.noop(ctx), "value": payload.value}

    @router.get("/read")
    async def read(ctx: ContextDep) -> dict[str, object]:
        return {
            "principal": ctx.principal,
            "idempotency_key": ctx.idempotency_key,
            "expected_revision": ctx.expected_revision,
            "viewpoint": ctx.viewpoint,
        }

    return router
