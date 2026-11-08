"""A stdlib replay of OpenRouter's image API for the #255 tests, and the fixture files.

``replay()`` starts a ``ThreadingHTTPServer`` on 127.0.0.1 (respx does not intercept httpx2,
#51; ``tests/model_support.py::mock_openai`` is the pattern). A test queues answers per
``(method, path)``: ``serve_fixture`` takes one fixture file's ``response`` (and its request's
method and URL), ``serve`` takes a status and a body. Answers for one route are served in the
order they were queued, the last one repeating until another is queued. Every request is
recorded in ``seen`` with its headers and JSON body.

Fixtures live in ``tests/fixtures/openrouter/``: one JSON per call,
``{request: {method, url, headers, body}, response: {status, headers, body}, meta}``.
``synthetic/`` holds the hand-written cases one real run cannot produce. ``recorded/`` and
``verdict.json`` are written by the owner's ``tools.record_openrouter_fixtures`` run; until they
exist, the tests that need them skip (``needs-owner-action``).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr

from epistrel.orchestrator.images.adapters.openrouter import OpenRouterImageGenerator
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.images.references import ReferenceResolver
from epistrel.orchestrator.images.registry import ImageAdapterConfig

FIXTURES = Path(__file__).parent / "fixtures" / "openrouter"
SYNTHETIC = FIXTURES / "synthetic"
RECORDED = FIXTURES / "recorded"
VERDICT = FIXTURES / "verdict.json"
REFERENCE_PNG = FIXTURES / "ref.png"
SYNTHETIC_MODEL = "synthetic-author/synthetic-image"
API_KEY = "sk-or-test-key-0000"

needs_recording = pytest.mark.skipif(
    not VERDICT.exists(),
    reason="needs-owner-action: run tools.record_openrouter_fixtures (#255)",
)


def fixture(name: str, directory: Path = SYNTHETIC) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / name).read_text())
    return loaded


def recorded_calls() -> list[dict[str, Any]]:
    """The owner's recorded calls in order (empty until they are committed)."""
    if not RECORDED.is_dir():
        return []
    return [fixture(path.name, RECORDED) for path in sorted(RECORDED.glob("[0-9][0-9]_*.json"))]


def verdict() -> dict[str, Any] | None:
    """``verdict.json`` (``{dedupes, inconclusive, model, recorded_at, total_cost}``), or
    ``None`` until it is committed."""
    if not VERDICT.exists():
        return None
    loaded: dict[str, Any] = json.loads(VERDICT.read_text())
    return loaded


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


@dataclass
class Answer:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None
    """Served as is, instead of ``body`` as JSON (malformed answers)."""
    delay_s: float = 0.0


@dataclass
class Replay:
    port: int = 0
    routes: dict[tuple[str, str], list[Answer]] = field(default_factory=dict)
    served: dict[tuple[str, str], int] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def serve(self, method: str, path: str, status: int, body: Any = None, **options: Any) -> None:
        with self.lock:
            self.routes.setdefault((method, path), []).append(Answer(status, body, **options))

    def serve_fixture(self, data: dict[str, Any], **options: Any) -> None:
        request, response = data["request"], data["response"]
        path = urlsplit(request["url"]).path
        headers = {k: v for k, v in response.get("headers", {}).items() if k.lower() == "location"}
        self.serve(
            request["method"],
            path,
            response["status"],
            response["body"],
            headers=headers,
            **options,
        )

    def reset(self) -> None:
        with self.lock:
            self.routes.clear()
            self.served.clear()
            self.seen.clear()

    def answer(self, method: str, path: str) -> Answer:
        with self.lock:
            queue = self.routes.get((method, path))
            if not queue:
                return Answer(404, {"error": {"code": 404, "message": "no replay route"}})
            position = self.served.get((method, path), 0)
            self.served[(method, path)] = position + 1
            return queue[min(position, len(queue) - 1)]

    def requests(self, method: str, path: str | None = None) -> list[Seen]:
        return [s for s in self.seen if s.method == method and (path is None or s.path == path)]


@contextmanager
def replay() -> Iterator[Replay]:
    state = Replay()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            path = urlsplit(self.path).path
            with state.lock:
                state.seen.append(Seen(method, path, dict(self.headers.items()), body))
            answer = state.answer(method, path)
            if answer.delay_s:
                time.sleep(answer.delay_s)
            encoded = answer.raw if answer.raw is not None else json.dumps(answer.body).encode()
            try:
                self.send_response(answer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                for name, value in answer.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(encoded)
            except BrokenPipeError, ConnectionResetError:
                return  # the client gave up (a timeout test)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def config(
    base_url: str | None,
    *,
    model: str = SYNTHETIC_MODEL,
    references: ReferenceResolver | None = None,
    **params: Any,
) -> ImageAdapterConfig:
    return ImageAdapterConfig(
        adapter="openrouter",
        model=model,
        base_url=base_url,
        api_key=SecretStr(API_KEY),
        params=ImageGenerationParams(adapter="openrouter", **params),
        revision=1,
        references=references,
    )


def adapter(
    server: Replay,
    *,
    model: str = SYNTHETIC_MODEL,
    references: ReferenceResolver | None = None,
    **params: Any,
) -> OpenRouterImageGenerator:
    return OpenRouterImageGenerator(
        config(server.root, model=model, references=references, **params)
    )


@dataclass
class FixedReferences:
    """A ``ReferenceResolver`` that answers ``https://media.example/<id>.png`` and records
    what it was asked."""

    asked: list[str] = field(default_factory=list)

    async def resolve(self, media_id: str) -> str:
        self.asked.append(media_id)
        return f"https://media.example/{media_id}.png"


__all__ = [
    "API_KEY",
    "FIXTURES",
    "RECORDED",
    "REFERENCE_PNG",
    "SYNTHETIC",
    "SYNTHETIC_MODEL",
    "VERDICT",
    "Answer",
    "FixedReferences",
    "Replay",
    "Seen",
    "adapter",
    "config",
    "fixture",
    "needs_recording",
    "recorded_calls",
    "replay",
    "verdict",
]
