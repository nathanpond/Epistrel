"""pytest plugin C-08 loads into the suite run it scans (``-p tests.conformance.telemetry_canary``;
#74).

At start-up it routes every logger at DEBUG through Epistrel's own formatter (exactly what
``epistrel serve`` writes to stdout) into a buffer, installs Epistrel's tracing with an
in-memory exporter behind the production content filter, and starts harvesting content: every
string of every event payload an ``Operation`` stages and every reply the stub adapter returns.
At the end it writes one JSON document to ``$EPISTREL_TEST_TELEMETRY_OUT``: the log lines, the
finished spans, the ``/metrics`` text and the harvested content.

``EPISTREL_TEST_TELEMETRY_CONTENT_DEBUG=1`` turns operator-only content debug logging on for the
run (C-08's complement)."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from epistrel.core.events import operation as operation_module
from epistrel.observability import tracing
from epistrel.observability.instrument import TelemetryMiddleware
from epistrel.observability.logging import configure_logging
from epistrel.observability.metrics import render_metrics
from epistrel.orchestrator.models.stub import StubAdapter

OUT_VAR = "EPISTREL_TEST_TELEMETRY_OUT"
CONTENT_DEBUG_VAR = "EPISTREL_TEST_TELEMETRY_CONTENT_DEBUG"
MIN_HARVEST = 12
"""Harvested content must be at least this long and contain a space (prose, not an id)."""


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)
    elif hasattr(value, "model_dump"):
        yield from _strings(value.model_dump(mode="json"))


class _Capture:
    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.exporter = InMemorySpanExporter()
        self.harvest: set[str] = set()

    def keep(self, value: Any) -> None:
        for text in _strings(value):
            if len(text) >= MIN_HARVEST and " " in text.strip():
                self.harvest.add(text)


CAPTURE = _Capture()


def pytest_configure(config: pytest.Config) -> None:
    content_debug = os.environ.get(CONTENT_DEBUG_VAR) == "1"
    configure_logging(level="DEBUG", content_debug=content_debug, stream=CAPTURE.stream)
    tracing.install(tracing.provider_for(CAPTURE.exporter, processor=SimpleSpanProcessor))

    append = operation_module.Operation.append

    def harvesting_append(self: Any, event_type: str, payload: Any, *args: Any, **kw: Any) -> Any:
        CAPTURE.keep(payload)
        return append(self, event_type, payload, *args, **kw)

    operation_module.Operation.append = harvesting_append  # type: ignore[method-assign,assignment]

    complete = StubAdapter.complete

    async def harvesting_complete(self: StubAdapter, *args: Any, **kw: Any) -> Any:
        result = await complete(self, *args, **kw)
        CAPTURE.keep(result.text)
        return result

    StubAdapter.complete = harvesting_complete  # type: ignore[method-assign]

    call = TelemetryMiddleware.__call__

    async def harvesting_call(
        self: TelemetryMiddleware, scope: Any, receive: Any, send: Any
    ) -> None:
        async def receiving() -> Any:
            message = await receive()
            if message.get("type") == "http.request" and message.get("body"):
                try:
                    CAPTURE.keep(json.loads(message["body"]))
                except ValueError:
                    CAPTURE.keep(message["body"].decode("utf-8", "replace"))
            return message

        await call(self, scope, receiving, send)

    TelemetryMiddleware.__call__ = harvesting_call  # type: ignore[method-assign]


def pytest_unconfigure(config: pytest.Config) -> None:
    target = os.environ.get(OUT_VAR)
    if not target:
        return
    tracing.flush()
    document = {
        "logs": CAPTURE.stream.getvalue().splitlines(),
        "spans": [json.loads(span.to_json()) for span in CAPTURE.exporter.get_finished_spans()],
        "metrics": render_metrics().decode("utf-8"),
        "harvest": sorted(CAPTURE.harvest),
    }
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
