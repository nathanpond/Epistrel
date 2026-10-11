"""One summary line, one span and one count per unit of work (FR-OBS-1; #74).

- ``instrument_engine(Engine)`` wraps every public coroutine method of ``Engine`` (operations and
  ``library_only`` methods alike, lifecycle methods excepted). The outermost call opens a
  ``Scope`` and a span named after the method, and on exit logs one ``operation`` line with
  ``operation_id``, ``story_id``, ``principal``, ``kind`` (the method), ``duration_ms`` and
  ``outcome`` (``ok``, ``rejected`` for a 4xx error, ``error`` otherwise) and counts
  ``epistrel_operations_total{kind,outcome}``. Calls an ``Engine`` method makes on itself are
  part of the outer call. (The planner's ``@instrumented`` decorator, folded into #73's
  operation marking: ``observability`` owns the one wrapper.)
- ``background(kind, ...)`` does the same for a job run, a queued turn and a timer pass:
  principal ``system``, a new trace with no parent.
- ``TelemetryMiddleware`` (hand-written ASGI) gives every HTTP request a scope and a server span
  (continuing an incoming ``traceparent``), observes
  ``epistrel_http_request_duration_seconds{route,method,status}`` and logs one ``request``
  line, which covers what never reaches the Engine (a 401, a 422 from the body check,
  ``/health``).

Every field is an identifier, an enum or a number; exceptions appear as their type and code.
"""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import SpanKind
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.idempotency import ReferenceReplay
from epistrel.core.turn_gate import TurnAccepted
from epistrel.observability import context, tracing
from epistrel.observability.logging import get_logger
from epistrel.observability.metrics import HTTP_DURATION, OPERATIONS, UNMATCHED_ROUTE

log = get_logger("epistrel.operation")
request_log = get_logger("epistrel.request")

NOT_INSTRUMENTED = frozenset({"start_workers", "stop_workers"})
"""Lifecycle methods: they start long-lived tasks, which must not inherit a call's scope."""
INSTRUMENTED_ATTRIBUTE = "__epistrel_instrumented__"
PRINCIPAL_HEADER = b"x-epistrel-principal"
MAX_PRINCIPAL = 256


def outcome_of(error: BaseException | None) -> str:
    """``ok``; ``rejected`` for an error a caller is told is theirs (4xx); ``error`` otherwise.
    A queued turn (202) and a reference replay are ``ok``."""
    if error is None or isinstance(error, TurnAccepted | ReferenceReplay):
        return "ok"
    status = getattr(error, "http_status", None)
    if isinstance(status, int) and status < 500:
        return "rejected"
    return "error"


def _error_fields(error: BaseException | None) -> dict[str, str]:
    if error is None or outcome_of(error) == "ok":
        return {}
    found = {"exc_type": type(error).__name__}
    code = getattr(error, "code", None)
    if isinstance(code, str):
        found["code"] = code
    return found


def _span_attributes(scope: context.Scope) -> dict[str, str]:
    return {f"epistrel.{key}": value for key, value in scope.fields().items()}


@dataclass(slots=True)
class _Run:
    error: BaseException | None = None
    outcome: str | None = None
    """Set by the block to report an outcome it handled itself (a failed job attempt)."""
    quiet: bool = False
    """Set by the block when it did nothing worth an INFO line (an idle timer pass)."""


@contextmanager
def _unit(scope: context.Scope, *, root: bool, line: str) -> Iterator[_Run]:
    """Run the block as ``scope``: its span, then its summary line and count."""
    run = _Run()
    started = time.perf_counter()
    with context.scoped(scope):
        try:
            with tracing.span(scope.kind, _span_attributes(scope), root=root) as span:
                try:
                    yield run
                except BaseException as exc:
                    run.error = exc
                    raise
                finally:
                    outcome = run.outcome or outcome_of(run.error)
                    span.set_attribute("epistrel.outcome", outcome)
                    span.set_attribute("epistrel.operation_id", scope.operation_id)
                    if scope.story_id is not None:
                        span.set_attribute("epistrel.story_id", scope.story_id)
        finally:
            outcome = run.outcome or outcome_of(run.error)
            OPERATIONS.labels(scope.kind, outcome).inc()
            level = "warning" if outcome == "error" else "debug" if run.quiet else "info"
            getattr(log, level)(
                line,
                duration_ms=round((time.perf_counter() - started) * 1000),
                outcome=outcome,
                **_error_fields(run.error),
            )


def _call_fields(
    signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[str | None, str | None]:
    try:
        bound = signature.bind_partial(*args, **kwargs).arguments
    except TypeError:
        return None, None
    ctx = bound.get("ctx")
    principal = getattr(ctx, "principal", None)
    story_id = bound.get("story_id")
    return (
        story_id if isinstance(story_id, str) else None,
        principal[:MAX_PRINCIPAL] if isinstance(principal, str) else None,
    )


def instrumented[F: Callable[..., Any]](name: str, method: F) -> F:
    """``method`` (an ``Engine`` coroutine method) with its span, summary line and count."""
    signature = inspect.signature(method)

    @functools.wraps(method)
    async def call(*args: Any, **kwargs: Any) -> Any:
        outer = context.current()
        if outer is not None and outer.in_engine:
            return await method(*args, **kwargs)
        story_id, principal = _call_fields(signature, args, kwargs)
        scope = context.Scope(
            kind=name,
            story_id=story_id,
            principal=principal,
            parent=outer,
            in_engine=True,
        )
        if outer is not None:
            scope.operation_id = outer.operation_id
            outer.kind, outer.story_id = name, outer.story_id or story_id
            outer.principal = outer.principal or principal
        with _unit(scope, root=False, line="operation"):
            result = await method(*args, **kwargs)
            if scope.story_id is None:
                created = getattr(result, "story_id", None)
                if isinstance(created, str):
                    scope.story_id = created
                    if outer is not None:
                        outer.story_id = created
            return result

    setattr(call, INSTRUMENTED_ATTRIBUTE, True)
    return call  # type: ignore[return-value]


def instrument_engine(engine_class: type) -> None:
    """Wrap every public coroutine method ``engine_class`` defines (once)."""
    for name, member in list(vars(engine_class).items()):
        if name.startswith("_") or name in NOT_INSTRUMENTED:
            continue
        if not inspect.iscoroutinefunction(member) or getattr(
            member, INSTRUMENTED_ATTRIBUTE, False
        ):
            continue
        setattr(engine_class, name, instrumented(name, member))


@contextmanager
def background(
    kind: str,
    *,
    story_id: str | None = None,
    operation_id: str | None = None,
    principal: str = SYSTEM_PRINCIPAL,
    job_id: str | None = None,
) -> Iterator[_Run]:
    """A job run, queued turn or timer pass: its own scope (principal ``system``, or the queued
    turn's requester) and a new trace. Set ``run.outcome`` to report a failure the block handled
    itself."""
    scope = context.Scope(kind=kind, story_id=story_id, principal=principal[:MAX_PRINCIPAL])
    if operation_id is not None:
        scope.operation_id = operation_id
    if job_id is not None:
        scope.extra["job_id"] = job_id
    with _unit(scope, root=True, line="background") as run:
        yield run


def route_name(scope: Scope) -> str:
    """The matched route's ``operation_id`` (or name); ``unmatched`` when none matched."""
    route = scope.get("route")
    name = getattr(route, "operation_id", None) or getattr(route, "name", None)
    return name if isinstance(name, str) and name else UNMATCHED_ROUTE


def _principal(headers: list[tuple[bytes, bytes]]) -> str | None:
    for key, value in headers:
        if key.lower() == PRINCIPAL_HEADER:
            return value.decode("latin-1").strip()[:MAX_PRINCIPAL] or None
    return None


def _headers(headers: list[tuple[bytes, bytes]]) -> Mapping[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in headers}


class TelemetryMiddleware:
    """Scope, server span, latency histogram and one ``request`` line per HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = list(scope.get("headers") or [])
        method = str(scope.get("method", "GET")).lower()
        current = context.Scope(kind="request", principal=_principal(raw_headers))
        status = 500
        error: BaseException | None = None
        started = time.perf_counter()

        async def sending(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        with (
            context.scoped(current),
            tracing.span(
                f"http {method}",
                {"http.request.method": method},
                kind=SpanKind.SERVER,
                parent=tracing.incoming(_headers(raw_headers)),
            ) as span,
        ):
            try:
                await self.app(scope, receive, sending)
            except BaseException as exc:
                error, status = exc, 500
                raise
            finally:
                route = route_name(scope)
                params = scope.get("path_params") or {}
                story = params.get("story_id")
                if current.story_id is None and isinstance(story, str):
                    current.story_id = story
                outcome = "ok" if status < 400 else "rejected" if status < 500 else "error"
                span.update_name(f"http {method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                span.set_attribute("epistrel.outcome", outcome)
                duration = time.perf_counter() - started
                HTTP_DURATION.labels(route, method, str(status)).observe(duration)
                getattr(request_log, "warning" if outcome == "error" else "info")(
                    "request",
                    route=route,
                    method=method,
                    status=status,
                    duration_ms=round(duration * 1000),
                    outcome=outcome,
                    **_error_fields(error),
                )


__all__ = [
    "TelemetryMiddleware",
    "background",
    "instrument_engine",
    "instrumented",
    "outcome_of",
    "route_name",
]
