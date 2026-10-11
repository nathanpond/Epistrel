"""C-09: secrets handling (docs/04), the M2 part: model API keys never appear in story data,
idempotency results, or telemetry (NFR-SEC-3).

A canary key is configured over ``/admin/model-config`` and used for real calls against a local
OpenAI-compatible server, alongside story writes, failed calls and refused requests. Then every
table except ``model_config`` (enumerated from ``information_schema``: every table with story
scope, ``config_changes``, and ``idempotency_keys`` and ``jobs`` once they exist), every log
record captured at DEBUG, every response, and ``/metrics`` are searched for the canary.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterable
from typing import Any

import httpx2 as httpx
import openai
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.orchestrator.models.config import KeyBox
from epistrel.orchestrator.models.prompts import example_instance
from epistrel.orchestrator.models.registry import DELIVERED
from tests.auth_support import auth_settings, bearer
from tests.model_support import (
    OPERATOR_HEADERS,
    admin_client,
    config_key,
    mock_openai,
    put,
    put_key,
    restored_model_config,
)
from tests.story_support import WRITE, public_tables, story_body

EXCLUDED_TABLES = frozenset({"model_config", "alembic_version"})
NAMED_TABLES = ("config_changes", "idempotency_keys", "jobs")


def find(canaries: Iterable[str], where: str, haystack: str) -> list[str]:
    """Each canary found in ``haystack``, labelled with where it was found."""
    return [f"{where}: {canary[:14]}…" for canary in canaries if canary in haystack]


async def story_scoped_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        found = await conn.scalars(
            text(
                "SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'story_id'"
            )
        )
        return {*found, "stories"}


async def table_hits(engine: AsyncEngine, canaries: list[str]) -> tuple[set[str], list[str]]:
    scanned = set(await public_tables(engine)) - EXCLUDED_TABLES
    hits: list[str] = []
    async with engine.connect() as conn:
        for table in sorted(scanned):
            rows = await conn.scalars(text(f'SELECT t::text FROM "{table}" t'))  # noqa: S608
            hits.extend(find(canaries, f"table {table}", "\n".join(rows)))
    return scanned, hits


def log_text(records: Iterable[logging.LogRecord]) -> str:
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    lines = []
    for record in records:
        lines.append(formatter.format(record))
        lines.append(repr(record.args))
        lines.append(repr(record.__dict__))
    return "\n".join(lines)


async def exercise(
    epistrel: Engine, client: httpx.AsyncClient, base: str, canaries: list[str]
) -> list[httpx.Response]:
    """Configure the keys and use them: model calls (good and failing), story writes and
    reads, refused requests, key removal and re-entry."""
    seen: list[httpx.Response] = []

    async def send(method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = await client.request(method, url, **kwargs)
        seen.append(response)
        return response

    await put(client, "default", {"base_url": f"{base}/v1", "model": "house"})
    await put_key(client, "default", canaries[0])
    await put_key(client, "generation", canaries[1])
    refused = await send(
        "PUT",
        "/admin/model-config/generation/api-key",
        json={"api_key": canaries[1] * 40},
        headers=OPERATOR_HEADERS,
    )
    assert refused.status_code == 422
    smuggled = await send(
        "PUT",
        "/admin/model-config/generation",
        json={"model": "m", "api_key": canaries[1]},
        headers=OPERATOR_HEADERS,
    )
    assert smuggled.status_code == 422
    await send("GET", "/admin/model-config", headers=bearer())
    await send("GET", "/admin/model-config/generation", headers=bearer())

    created = await send("POST", "/stories", json=story_body("Ada", "Bo"), headers=WRITE)
    assert created.status_code == 201, created.text
    story_id = created.json()["story_id"]
    await send("GET", f"/stories/{story_id}?viewpoint=omniscient", headers=bearer())
    await send("GET", f"/stories/{story_id}/history", headers=bearer())
    await send("POST", f"/admin/stories/{story_id}/rebuild", headers=OPERATOR_HEADERS)

    for name, purpose in DELIVERED.items():
        await epistrel.models.call(name, example_instance(purpose.input_schema))
    return seen


@pytest.mark.integration
@pytest.mark.part("M2", "NFR-SEC-3")
async def test_c_09(pg_url: str, engine: AsyncEngine, caplog: pytest.LogCaptureFixture) -> None:
    """No configured key in any table but ``model_config``, in any log record, or in any
    response. Complement asserted: the same search finds both keys in ``model_config``'s
    ciphertext once decrypted in-process, and the keys really were sent to the provider, so
    an empty result means absence, not a blind search."""
    caplog.set_level(logging.DEBUG)
    canaries = [f"sk-canary-{secrets.token_hex(12)}", f"sk-canary-{secrets.token_hex(12)}"]
    settings = auth_settings(pg_url, model_adapter="openai", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            with mock_openai() as mock:
                async with admin_client(settings, epistrel) as client:
                    seen = await exercise(epistrel, client, mock.root, canaries)
                    mock.fail_with = 500
                    with pytest.raises(openai.InternalServerError):
                        await epistrel.models.call(
                            "generation", example_instance(DELIVERED["generation"].input_schema)
                        )
                    deleted = await client.delete(
                        "/admin/model-config/generation/api-key", headers=OPERATOR_HEADERS
                    )
                    assert deleted.status_code == 204
                    await put_key(client, "generation", canaries[1])
                    metrics = await client.get("/metrics")
                    seen.append(metrics)
            sent = {r.authorization for r in mock.requests}

            scanned, hits = await table_hits(engine, canaries)
            async with engine.connect() as conn:
                stored = list(
                    await conn.scalars(
                        text(
                            "SELECT api_key_ciphertext FROM model_config "
                            "WHERE api_key_ciphertext IS NOT NULL"
                        )
                    )
                )
                at_rest = "\n".join(await conn.scalars(text("SELECT m::text FROM model_config m")))
        finally:
            await epistrel.models.aclose()

    # Coverage of the search: every story-scoped table and the named stores that exist.
    present = set(await public_tables(engine))
    assert await story_scoped_tables(engine) <= scanned
    assert {name for name in NAMED_TABLES if name in present} <= scanned
    assert "config_changes" in scanned

    for response in seen:
        hits.extend(find(canaries, f"response {response.request.url.path}", response.text))
        hits.extend(find(canaries, "response headers", repr(dict(response.headers))))
    hits.extend(find(canaries, "log records", log_text(caplog.records)))
    hits.extend(find(canaries, "log text", caplog.text))
    hits.extend(find(canaries, "model_config at rest", at_rest))
    assert hits == []
    assert caplog.records, "nothing was logged, so the log search proves nothing"

    # The complement: the keys exist, were used, and the search finds them where they live.
    keybox = KeyBox(config_key())
    decrypted = "\n".join(keybox.decrypt(c, purpose="x").get_secret_value() for c in stored)
    assert sorted(find(canaries, "decrypted", decrypted)) == sorted(
        f"decrypted: {canary[:14]}…" for canary in canaries
    )
    assert {f"Bearer {canary}" for canary in canaries} <= sent
