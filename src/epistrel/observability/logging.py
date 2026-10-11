"""Structured JSON logs with no story content (FR-OBS-1, NFR-SEC-4, invariant 3; #74).

Every log line is one JSON object on stdout: ``timestamp``, ``level``, ``logger``, ``event`` and,
inside an operation, ``operation_id``, ``story_id``, ``principal``, ``kind`` (from the current
``observability.context.Scope``) and the current span's ``trace_id``/``span_id``. With
``EPISTREL_DEBUG`` on and stdout a terminal, a console renderer replaces JSON.

``ContentFilter`` is the last processor before rendering, for structlog loggers (``get_logger``)
and stdlib loggers alike (they share one ``ProcessorFormatter``): it drops every field named in
``content.CONTENT_FIELDS`` or ending in ``_text``, at any depth, and cuts unknown string fields to
64 characters (``content.scrub``). Exceptions are logged as their type and ``code`` (and, for an
unexpected one, its frames as ``file:line function``, never locals or the message).
``sqlalchemy.engine``, ``httpx``, ``httpcore`` and ``openai`` are capped at WARNING, because
below that they echo SQL parameters and request bodies; uvicorn's access log is off (the
telemetry middleware writes one line per request instead).

**Content debug logging** (``EPISTREL_CONTENT_DEBUG_LOGGING=1``) is opt-in and operator-only: it
is refused unless ``EPISTREL_DEBUG`` is on (start-up exits 78), logs a WARNING at start-up,
shows as ``content_logging: true`` on ``GET /health``, and switches itself off after
``CONTENT_LOGGING_HOURS`` (24 h) of process time. While it is on, content fields pass and
unknown strings are not cut; the Lite pipeline's DEBUG lines then carry the prompt and the
draft. Retain such logs at most 7 days (docs/02 §11).

What this does NOT cover: the ``event`` text of a third-party logger below the capped ones (a
library that formats content into its own message would pass, cut to nothing); content an
operator's own log handlers attach outside this formatter; and stderr written without logging
(a crash before logging is configured). The guard in ``tests/conformance/test_c_08.py`` scans
what the scenario suite and every operation actually log.
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog
from structlog.stdlib import BoundLogger, ProcessorFormatter
from structlog.types import EventDict, Processor, WrappedLogger

from epistrel.observability import context, tracing
from epistrel.observability.content import scrub

CONTENT_LOGGING_HOURS = 24.0
"""Content debug logging switches itself off after this much process time (NFR-SEC-4)."""
CONTENT_LOG_RETENTION_DAYS = 7
"""How long an operator may keep logs written with content debug logging on (docs/02)."""
CAPPED_LOGGERS = ("sqlalchemy.engine", "httpx", "httpcore", "openai", "uvicorn.access")
MAX_FRAMES = 30

_HANDLER_NAME = "epistrel"


class _ContentPolicy:
    """Whether content debug logging is on now; it expires ``CONTENT_LOGGING_HOURS`` after it
    was enabled."""

    def __init__(self) -> None:
        self.until: float | None = None
        self.expired = False

    def enable(self, hours: float = CONTENT_LOGGING_HOURS) -> None:
        self.until = time.monotonic() + hours * 3600
        self.expired = False

    def disable(self) -> None:
        self.until = None

    def active(self) -> bool:
        if self.until is None:
            return False
        if time.monotonic() < self.until:
            return True
        self.until = None
        self.expired = True
        logging.getLogger(__name__).warning(
            "content debug logging switched itself off after %d hours", int(CONTENT_LOGGING_HOURS)
        )
        return False


CONTENT_POLICY = _ContentPolicy()


def content_logging_active() -> bool:
    """Whether log lines may carry story content right now (``GET /health.content_logging``)."""
    return CONTENT_POLICY.active()


def add_scope(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    """The current scope's identifiers and the current span's ids (fields already set win)."""
    scope = context.current()
    if scope is not None:
        for key, value in scope.fields().items():
            event.setdefault(key, value)
    ids = tracing.current_ids()
    if ids is not None:
        event.setdefault("trace_id", ids[0])
        event.setdefault("span_id", ids[1])
    return event


def summarize_exception(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    """Replace ``exc_info`` with ``exc_type``, ``code`` and (unexpected errors only) frames."""
    exc_info = event.pop("exc_info", None)
    event.pop("exception", None)
    event.pop("stack_info", None)
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not isinstance(exc_info, tuple) or exc_info[0] is None:
        return event
    kind, error, tb = exc_info
    event["exc_type"] = kind.__name__
    code = getattr(error, "code", None)
    if isinstance(code, str):
        event["code"] = code
    status = getattr(error, "http_status", None)
    if not (isinstance(status, int) and status < 500):
        event["frames"] = [
            f"{frame.filename}:{frame.lineno} {frame.name}"
            for frame in traceback.extract_tb(tb)[-MAX_FRAMES:]
        ]
    return event


def content_filter(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    """``ContentFilter``: content fields dropped and unknown strings cut, unless content debug
    logging is on."""
    if CONTENT_POLICY.active():
        event["content_logging"] = True
        return event
    meta = {key: event[key] for key in ("_record", "_from_structlog") if key in event}
    clean: MutableMapping[str, Any] = scrub(
        {key: value for key, value in event.items() if key not in meta}
    )
    clean.update(meta)
    return dict(clean)


ContentFilter = content_filter

SHARED: tuple[Processor, ...] = (
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    add_scope,
    summarize_exception,
    content_filter,
)


def get_logger(name: str) -> BoundLogger:
    """A structlog logger for ``name`` whose lines pass the ``ContentFilter`` before any handler
    sees them, whatever structlog's global configuration (an embedder's stays untouched)."""
    logger: BoundLogger = structlog.wrap_logger(
        logging.getLogger(name),
        processors=[
            structlog.stdlib.filter_by_level,
            *SHARED,
            ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=BoundLogger,
    )
    return logger


def formatter(*, console: bool = False) -> ProcessorFormatter:
    """The formatter every Epistrel log line is rendered by (JSON, or console in debug)."""
    renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=False)
        if console
        else structlog.processors.JSONRenderer(sort_keys=True)
    )
    return ProcessorFormatter(
        foreign_pre_chain=list(SHARED),
        processors=[ProcessorFormatter.remove_processors_meta, renderer],
    )


def configure_logging(
    *,
    level: str = "INFO",
    content_debug: bool = False,
    debug: bool = False,
    stream: TextIO | None = None,
) -> logging.Handler:
    """Route every logger to one handler on ``stream`` (stdout) through ``formatter()``.

    Replaces a handler an earlier call installed; caps the chatty libraries at WARNING; turns
    content debug logging on (with its start-up WARNING) or off."""
    out = stream or sys.stdout
    handler = logging.StreamHandler(out)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter(console=debug and out.isatty()))
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    for name in CAPPED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if content_debug:
        CONTENT_POLICY.enable()
        logging.getLogger(__name__).warning(
            "content debug logging is ON: log lines carry story content for the next %d hours; "
            "keep these logs at most %d days and restrict them to operators",
            int(CONTENT_LOGGING_HOURS),
            CONTENT_LOG_RETENTION_DAYS,
        )
    else:
        CONTENT_POLICY.disable()
    return handler


__all__ = [
    "CAPPED_LOGGERS",
    "CONTENT_LOGGING_HOURS",
    "CONTENT_LOG_RETENTION_DAYS",
    "CONTENT_POLICY",
    "ContentFilter",
    "configure_logging",
    "content_filter",
    "content_logging_active",
    "formatter",
    "get_logger",
]
