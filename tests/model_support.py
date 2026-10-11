"""Helpers for the Model Access Layer tests: a local OpenAI-compatible HTTP server that records
every request, a throwaway config key, and a context that restores ``model_config``."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2 as httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.engine import Engine
from tests.auth_support import bearer

CONFIG_KEY = base64.urlsafe_b64encode(b"epistrel-test-config-key-0000000").decode()
VERDICT = '{"violation": false, "confidence": 1.0}'


def config_key() -> SecretStr:
    return SecretStr(CONFIG_KEY)


@dataclass
class Recorded:
    path: str
    authorization: str | None
    body: dict[str, Any]


@dataclass
class MockOpenAI:
    """A Chat Completions endpoint on 127.0.0.1. Replies ``[mock <model>] <last user text>``,
    or a guardrail verdict when ``response_format`` asks for JSON or the system message carries
    a JSON Schema. With ``legacy_max_tokens`` it answers ``max_completion_tokens`` with a 400,
    as older servers do; with ``reject_response_format`` it answers ``response_format`` with a
    400 naming it; ``raw_body`` is served as an HTML page with status 200."""

    port: int = 0
    requests: list[Recorded] = field(default_factory=list)
    legacy_max_tokens: bool = False
    reject_response_format: bool = False
    fail_with: int | None = None
    raw_body: bytes | None = None
    script: list[str] = field(default_factory=list)
    """Answers served first, in order, whatever the request asks for."""

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reply(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.fail_with is not None:
            return self.fail_with, {"error": {"message": "mock failure"}}
        if self.legacy_max_tokens and "max_completion_tokens" in body:
            return 400, {"error": {"message": "Unrecognized request argument: max_completion"}}
        if self.reject_response_format and "response_format" in body:
            return 400, {"error": {"message": "response_format json_schema is not supported"}}
        system = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
        if self.script:
            content = self.script.pop(0)
        elif "response_format" in body or "JSON Schema" in system:
            content = VERDICT
        else:
            last = next(
                (m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), ""
            )
            content = f"[mock {body['model']}] {last}"
        return 200, {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }


@contextmanager
def mock_openai(**options: Any) -> Iterator[MockOpenAI]:
    mock = MockOpenAI(**options)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            mock.requests.append(Recorded(self.path, self.headers.get("Authorization"), body))
            status, payload = mock.reply(body)
            encoded, kind = json.dumps(payload).encode(), "application/json"
            if mock.raw_body is not None:
                status, encoded, kind = 200, mock.raw_body, "text/html"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    mock.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield mock
    finally:
        server.shutdown()
        server.server_close()


@asynccontextmanager
async def restored_model_config(engine: AsyncEngine) -> AsyncIterator[None]:
    """Run with a clean ``model_config`` (only an unset ``default`` row) and restore it after:
    the tests share one database."""

    async def reset() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM model_config WHERE purpose <> 'default'"))
            await conn.execute(
                text(
                    "UPDATE model_config SET base_url = NULL, model = NULL, "
                    "api_key_ciphertext = NULL, params = '{}'::jsonb, updated_by = 'system'"
                )
            )
            # Move the revision too, so no Model Access Layer keeps a cached copy of the rows.
            await conn.execute(
                text("UPDATE deployment_state SET config_revision = config_revision + 1")
            )

    await reset()
    try:
        yield
    finally:
        await reset()


OPERATOR_HEADERS = {**bearer(), "X-Epistrel-Principal": "p-operator"}


@asynccontextmanager
async def admin_client(settings: Settings, engine: Engine) -> AsyncIterator[httpx.AsyncClient]:
    """The REST app serving ``engine`` itself (no lifespan), so a test can change the
    configuration over REST and call models through the same Engine."""
    app = create_app(settings)
    app.state.epistrel = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def put(client: httpx.AsyncClient, purpose: str, body: dict[str, Any]) -> int:
    """``PUT /admin/model-config/{purpose}``; returns the ``config_revision`` it reports."""
    response = await client.put(
        f"/admin/model-config/{purpose}", json=body, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 200, response.text
    return int(response.json()["config_revision"])


async def put_key(client: httpx.AsyncClient, purpose: str, api_key: str) -> None:
    response = await client.put(
        f"/admin/model-config/{purpose}/api-key",
        json={"api_key": api_key},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 204, response.text
