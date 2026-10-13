"""Shared fixtures for the trust-boundary tests: credentials, settings, and a test-only router."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

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


def auth_settings(dsn: str, **overrides: object) -> Settings:
    # ``api`` unless a test asks for workers: an app under test must not start a job worker
    # that fires the seeded sweeps behind the test's back (#71; X-07 starts its own).
    values: dict[str, object] = {
        "database_url": dsn,
        "service_credentials": [CREDENTIAL_A, CREDENTIAL_B],
        "mode": "api",
        "media_root": MEDIA_ROOT,
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
