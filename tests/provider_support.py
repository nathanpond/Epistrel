"""The shared fake external-data provider (#204; reused by #205 to #213).

Two providers a test can register and drive:

- ``ScriptedProvider``: an in-process provider (``Engine.register_in_process_provider``) whose
  reads answer a scripted sequence per ``(value, binding)`` (the last item repeats), with error
  and timeout injection (``Fail``, ``Timeout``), a ``echo`` mode answering one of the binding's
  ids back (so two stories bound with different ids read different values), a call counter, and,
  for writes, a log of the idempotency keys it was sent, a dedupe counter (a key seen before
  answers its first outcome again and changes nothing) and scripted outcomes.
- ``StubServer``: a loopback HTTP server on ``127.0.0.1`` (any free port) for
  ``HttpManifestProvider``, answering scripted ``Reply``s per ``(method, path)`` and recording
  every request it received (method, path, raw query, headers, body). Use it with
  ``allowed_hosts(engine, [stub.host])`` (the deployment's ``provider_allowed_hosts``,
  restored afterwards).

Registration bodies: ``in_process_spec`` and ``http_spec`` (and ``read_template``,
``write_template``) build ``POST /admin/external-providers`` bodies; ``register`` writes one
straight through the registry (no app needed), ``unique_name`` gives a name no other test on
the shared database uses, and ``bind`` binds a story without the app.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import providers as core_providers
from epistrel.core.ids import new_ulid
from epistrel.orchestrator.providers import registry
from epistrel.orchestrator.providers.manifest import ProviderRegistration
from epistrel.orchestrator.providers.port import (
    Binding,
    Effect,
    ProviderUnavailable,
    WriteOutcome,
)

PRINCIPAL = "p-operator"
LOOPBACK = "127.0.0.1"


def unique_name(prefix: str = "ledger") -> str:
    """A provider name no other test on the shared database registers."""
    return f"{prefix}-{new_ulid().lower()}"


# -- registration bodies --------------------------------------------------------------------------


def in_process_spec(
    name: str | None = None,
    values: Mapping[str, str] | None = None,
    *,
    writable: Sequence[str] = (),
    **fields: Any,
) -> dict[str, Any]:
    """A registration body for an ``in_process`` provider owning ``values`` (``{name:
    type}``; default ``{"gold": "number"}``), the ``writable`` ones writable (which makes it
    write-capable, synchronous, deduplicating and returning resulting values unless ``fields``
    say otherwise)."""
    owned = values or {"gold": "number"}
    body: dict[str, Any] = {
        "trust_tier": "authoritative",
        "transport": "in_process",
        "manifest": {
            "values": {v: {"type": t, "writable": v in writable} for v, t in owned.items()}
        },
    }
    if writable:
        body.update(
            write_capable=True,
            confirmation="synchronous",
            dedupes_dispatch=True,
            returns_resulting_values=True,
        )
    if name is not None:
        body["name"] = name
    body.update(fields)
    return body


def read_template(
    base_url: str, path: str = "/players/{binding.player}/gold", **fields: Any
) -> dict[str, Any]:
    """An HTTP read template: ``GET <base_url><path>``, the value at ``$.balance``."""
    return {"method": "GET", "url": f"{base_url}{path}", "value_path": "$.balance", **fields}


def write_template(
    base_url: str, path: str = "/players/{binding.player}/spend", **fields: Any
) -> dict[str, Any]:
    """An HTTP write template: ``POST <base_url><path>`` with the delta, the key in
    ``Idempotency-Key``, ``$.status`` mapped ``ok`` → confirmed, ``declined`` → failed, and the
    resulting values at ``$.values``."""
    return {
        "method": "POST",
        "url": f"{base_url}{path}",
        "body": {"amount": "{effect.delta}", "note": "spend {effect.delta}"},
        "idempotency_header": "Idempotency-Key",
        "outcome_path": "$.status",
        "outcome_map": {"confirmed": ["ok"], "failed": ["declined"]},
        "resulting_values_path": "$.values",
        **fields,
    }


def http_spec(
    base_url: str,
    name: str | None = None,
    *,
    value: str = "gold",
    value_type: str = "number",
    read: Mapping[str, Any] | None = None,
    write: Mapping[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A registration body for an ``http`` provider owning one value, read with ``read``
    (default ``read_template(base_url)``) and, given ``write``, written with it (the provider
    then write-capable, synchronous, deduplicating, returning resulting values)."""
    owned: dict[str, Any] = {"type": value_type, "read": dict(read or read_template(base_url))}
    body: dict[str, Any] = {
        "trust_tier": "authoritative",
        "transport": "http",
        "manifest": {"values": {value: owned}},
    }
    if write is not None:
        owned["write"] = dict(write)
        body.update(
            write_capable=True,
            confirmation="synchronous",
            dedupes_dispatch=True,
            returns_resulting_values=True,
        )
    if name is not None:
        body["name"] = name
    body.update(fields)
    return body


async def register(engine: AsyncEngine, body: Mapping[str, Any]) -> core_providers.ProviderRow:
    """Register ``body`` (with its ``name``) straight through the registry."""
    write = await registry.register(
        engine, ProviderRegistration.model_validate(dict(body)), principal=PRINCIPAL
    )
    assert write.provider is not None
    return write.provider


async def bind(
    engine: AsyncEngine, story_id: str, name: str, external_ids: Mapping[str, str]
) -> core_providers.ProviderBinding:
    """Bind the story to provider ``name`` straight through Core."""
    async with engine.begin() as conn:
        return await core_providers.bind(conn, story_id, name, external_ids)


@asynccontextmanager
async def allowed_hosts(engine: AsyncEngine, hosts: Sequence[str]) -> AsyncIterator[None]:
    """The deployment's ``provider_allowed_hosts`` set to ``hosts`` for the block, then
    restored (the database is shared)."""
    async with engine.begin() as conn:
        before = await conn.scalar(
            text("SELECT settings -> 'provider_allowed_hosts' FROM deployment_state")
        )
        await conn.execute(
            text(
                "UPDATE deployment_state SET settings = settings || "
                "jsonb_build_object('provider_allowed_hosts', CAST(:h AS jsonb))"
            ),
            {"h": json.dumps(list(hosts))},
        )
    try:
        yield
    finally:
        async with engine.begin() as conn:
            if before is None:
                await conn.execute(
                    text(
                        "UPDATE deployment_state SET settings = settings - 'provider_allowed_hosts'"
                    )
                )
            else:
                await conn.execute(
                    text(
                        "UPDATE deployment_state SET settings = settings || "
                        "jsonb_build_object('provider_allowed_hosts', CAST(:h AS jsonb))"
                    ),
                    {"h": json.dumps(before)},
                )


# -- the scripted in-process provider ---------------------------------------------------------


@dataclass(frozen=True)
class Fail:
    """A scripted read that fails as an adapter error with ``reason``."""

    reason: str = "unreachable"


@dataclass(frozen=True)
class Timeout:
    """A scripted read that takes ``seconds`` and then fails with ``timeout``."""

    seconds: float = 0.05


@dataclass
class ScriptedProvider:
    """An in-process provider for tests (see the module docstring). ``name`` is the provider it
    serves (register it with ``attach``). Without a script, a read answers ``default``; in
    ``echo`` mode (``echo_key``), the binding's ``external_ids[echo_key]``."""

    name: str
    default: Any = Decimal(100)
    echo_key: str | None = None
    scripts: dict[tuple[str, str | None], list[Any]] = field(default_factory=dict)
    outcomes: dict[str, list[Any]] = field(default_factory=dict)
    reads: list[tuple[str, str, str | None]] = field(default_factory=list)
    """``(value, story_id, scope_key)`` of every read, in order."""
    writes: list[Effect] = field(default_factory=list)
    idempotency_keys: list[str] = field(default_factory=list)
    deduped: int = 0
    _first: dict[str, WriteOutcome] = field(default_factory=dict)

    @property
    def calls(self) -> int:
        return len(self.reads) + len(self.writes)

    def script(self, value: str, *items: Any, story_id: str | None = None) -> None:
        """Script ``value``'s reads (for one story's binding, or any): each read takes the next
        item, the last one repeating. An item is a value, a ``Fail`` or a ``Timeout``."""
        self.scripts[(value, story_id)] = list(items)

    def script_writes(self, value: str, *outcomes: Any) -> None:
        """Script ``value``'s write outcomes (a status, a ``WriteOutcome`` or a ``Fail``)."""
        self.outcomes[value] = list(outcomes)

    def _next(self, queue: list[Any]) -> Any:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def read(self, value: str, binding: Binding, scope_key: str | None) -> Any:
        self.reads.append((value, binding.story_id, scope_key))
        queue = self.scripts.get((value, binding.story_id)) or self.scripts.get((value, None))
        if queue:
            item = self._next(queue)
        elif self.echo_key is not None:
            item = binding.external_ids[self.echo_key]
        else:
            item = self.default
        if isinstance(item, Timeout):
            await asyncio.sleep(item.seconds)
            raise ProviderUnavailable(self.name, value, "timeout")
        if isinstance(item, Fail):
            raise ProviderUnavailable(self.name, value, item.reason)
        return item

    async def write(self, effect: Effect, binding: Binding) -> WriteOutcome:
        self.writes.append(effect)
        self.idempotency_keys.append(effect.idempotency_key)
        if effect.idempotency_key in self._first:
            self.deduped += 1
            return self._first[effect.idempotency_key]
        queue = self.outcomes.get(effect.value)
        item: Any = self._next(queue) if queue else "confirmed"
        if isinstance(item, Fail):
            raise ProviderUnavailable(self.name, effect.value, item.reason)
        outcome = item if isinstance(item, WriteOutcome) else WriteOutcome(status=item)
        self._first[effect.idempotency_key] = outcome
        return outcome

    def attach(self, epistrel: Any) -> ScriptedProvider:
        """Register this provider's callables on an ``Engine`` (in-process registry)."""
        epistrel.register_in_process_provider(self.name, read=self.read, write=self.write)
        return self


# -- the loopback HTTP stub ---------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """A scripted HTTP answer: status, JSON body (or raw ``content``), headers and a delay."""

    status: int = 200
    body: Any = None
    content: bytes | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    delay: float = 0.0


@dataclass(frozen=True)
class Received:
    """One request the stub received."""

    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: Any


class StubServer:
    """A loopback HTTP server (see the module docstring). ``routes`` maps ``(method, path)`` to
    a ``Reply`` or a list of them (each request takes the next, the last repeating); an
    unknown route answers 404."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Reply]] = {}
        self.received: list[Received] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _handle(self) -> None:
                raw_path, _, query = self.path.partition("?")
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode(errors="replace")
                with stub._lock:
                    stub.received.append(
                        Received(self.command, raw_path, query, dict(self.headers), body)
                    )
                    queue = stub.routes.get((self.command, raw_path))
                    reply = (queue.pop(0) if len(queue) > 1 else queue[0]) if queue else None
                if reply is None:
                    reply = Reply(404, {"error": "no route"})
                if reply.delay:
                    threading.Event().wait(reply.delay)
                content = (
                    reply.content if reply.content is not None else json.dumps(reply.body).encode()
                )
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(content)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_PATCH = _handle
            do_DELETE = _handle

        self._server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def host(self) -> str:
        """``127.0.0.1:<port>``: the allowed-hosts entry for this stub."""
        return f"{LOOPBACK}:{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"

    def route(self, method: str, path: str, *replies: Reply) -> None:
        with self._lock:
            self.routes[(method, path)] = list(replies)

    def start(self) -> StubServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def stub_server() -> Iterator[StubServer]:
    """A started ``StubServer``, stopped afterwards."""
    stub = StubServer().start()
    try:
        yield stub
    finally:
        stub.stop()


__all__ = [
    "LOOPBACK",
    "PRINCIPAL",
    "Fail",
    "Received",
    "Reply",
    "ScriptedProvider",
    "StubServer",
    "Timeout",
    "allowed_hosts",
    "bind",
    "http_spec",
    "in_process_spec",
    "read_template",
    "register",
    "stub_server",
    "unique_name",
    "write_template",
]
