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

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

CONFIG_KEY = base64.urlsafe_b64encode(b"epistrel-test-config-key-0000000").decode()
VERDICT = '{"violation": false, "confidence": 1.0, "reason": ""}'


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
    or a guardrail verdict when ``response_format`` asks for JSON. With
    ``legacy_max_tokens`` it answers ``max_completion_tokens`` with a 400, as older servers do."""

    port: int = 0
    requests: list[Recorded] = field(default_factory=list)
    legacy_max_tokens: bool = False
    fail_with: int | None = None

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reply(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.fail_with is not None:
            return self.fail_with, {"error": {"message": "mock failure"}}
        if self.legacy_max_tokens and "max_completion_tokens" in body:
            return 400, {"error": {"message": "Unrecognized request argument: max_completion"}}
        if "response_format" in body:
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
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
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

    await reset()
    try:
        yield
    finally:
        await reset()
