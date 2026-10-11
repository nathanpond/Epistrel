"""OpenTelemetry tracing, exported over OTLP/HTTP only when ``EPISTREL_OTLP_ENDPOINT`` is set
(FR-OBS-1; #74).

Off by default: without an endpoint every span is a no-op and costs nothing. With one, Epistrel
keeps its own ``TracerProvider`` (``service.name = epistrel``, ``service.version``,
``deployment.environment``; an always-on sampler) and leaves the process-global provider to the
embedder. The exporter honours the standard ``OTEL_EXPORTER_OTLP_*`` variables (headers,
timeout, compression); ``OTEL_RESOURCE_ATTRIBUTES`` adds resource attributes.

Spans: one server span per HTTP request (an incoming ``traceparent`` is honoured), one span per
``Engine`` call, job and queued turn, and child spans for each model call attempt and each
commit transaction. Attributes are identifiers only (``epistrel.operation_id``,
``epistrel.story_id``, ``epistrel.principal``, ``epistrel.kind``, ``epistrel.outcome``, model
purpose and sanitised name). A span's status is its code only; exceptions are never recorded on
a span, because an exception's message can quote what it was given.

``ContentFilteringSpanProcessor`` sits in front of the exporter and applies
``observability.content.scrub`` to every finished span's attributes, drops every span event and
the status description: a span that somebody later decorates with content still leaves without
it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import NoOpTracer, Span, SpanKind, Status, StatusCode, Tracer

from epistrel.observability.content import scrub

SERVICE_NAME = "epistrel"
TRACES_PATH = "/v1/traces"

_provider: TracerProvider | None = None
_tracer: Tracer = NoOpTracer()


class ContentFilteringSpanProcessor(SpanProcessor):
    """Hands ``delegate`` a copy of each finished span with content attributes dropped, unknown
    strings cut, no events and a status without its description."""

    def __init__(self, delegate: SpanProcessor) -> None:
        self.delegate = delegate

    def on_start(self, span: Any, parent_context: Context | None = None) -> None:
        self.delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        self.delegate.on_end(
            ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=span.resource,
                attributes=scrub(dict(span.attributes or {})),
                events=(),
                links=span.links,
                kind=span.kind,
                status=Status(span.status.status_code),
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            )
        )

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.delegate.force_flush(timeout_millis)


def _resource(version: str, environment: str) -> Resource:
    """Epistrel's resource; a ``deployment.environment`` in ``OTEL_RESOURCE_ATTRIBUTES`` wins."""
    attributes = {"service.name": SERVICE_NAME, "service.version": version}
    if "deployment.environment=" not in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", ""):
        attributes["deployment.environment"] = environment
    return Resource.create(attributes)


def provider_for(
    exporter: SpanExporter,
    *,
    processor: type[SpanProcessor] | None = None,
    version: str = "unknown",
    environment: str = "production",
) -> TracerProvider:
    """A provider exporting through ``exporter`` behind the content filter (a batch processor
    unless ``processor`` names another, e.g. ``SimpleSpanProcessor`` in tests)."""
    provider = TracerProvider(resource=_resource(version, environment), sampler=ALWAYS_ON)
    inner = (processor or BatchSpanProcessor)(exporter)  # type: ignore[call-arg]
    provider.add_span_processor(ContentFilteringSpanProcessor(inner))
    return provider


def install(provider: TracerProvider | None) -> TracerProvider | None:
    """Use ``provider`` for Epistrel's spans (``None``: no-op). Returns the previous one, which
    the caller shuts down if it is done with it."""
    global _provider, _tracer
    previous = _provider
    _provider = provider
    _tracer = provider.get_tracer(SERVICE_NAME) if provider is not None else NoOpTracer()
    return previous


def traces_url(endpoint: str) -> str:
    """The OTLP/HTTP traces URL for ``endpoint`` (``/v1/traces`` appended unless present)."""
    base = endpoint.rstrip("/")
    return base if base.endswith(TRACES_PATH) else base + TRACES_PATH


def configure_tracing(endpoint: str | None, *, version: str, debug: bool) -> bool:
    """Export over OTLP/HTTP to ``endpoint`` (``EPISTREL_OTLP_ENDPOINT``); ``None`` leaves
    tracing off. Returns whether tracing is on."""
    if not endpoint:
        return _provider is not None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    environment = "development" if debug else "production"
    previous = install(
        provider_for(
            OTLPSpanExporter(endpoint=traces_url(endpoint)),
            version=version,
            environment=environment,
        )
    )
    if previous is not None:
        previous.shutdown()
    return True


def tracer() -> Tracer:
    return _tracer


def enabled() -> bool:
    return _provider is not None


def flush() -> None:
    if _provider is not None:
        _provider.force_flush()


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    root: bool = False,
    parent: Context | None = None,
) -> Iterator[Span]:
    """A span around the block: ``root`` starts a new trace (background work has no parent);
    ``parent`` continues an extracted context. An exception marks it ``ERROR`` (code only)."""
    context = Context() if root else parent
    with _tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException:
            current.set_status(Status(StatusCode.ERROR))
            raise


def finished_span(name: str, duration_s: float, attributes: Mapping[str, Any]) -> None:
    """Record a child span that has just finished and took ``duration_s`` (a model call)."""
    if _provider is None:
        return
    end = time.time_ns()
    start = end - max(0, int(duration_s * 1_000_000_000))
    child = _tracer.start_span(name, attributes=_attributes(attributes), start_time=start)
    if attributes.get("epistrel.outcome") not in (None, "ok"):
        child.set_status(Status(StatusCode.ERROR))
    child.end(end_time=end)


def incoming(headers: Mapping[str, str]) -> Context:
    """The trace context an incoming request carries (``traceparent``/``tracestate``)."""
    return extract(dict(headers))


def current_ids() -> tuple[str, str] | None:
    """The current span's ``(trace_id, span_id)`` in hex, or ``None`` outside a valid span."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def _attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in scrub(attributes or {}).items()
        if isinstance(value, str | bool | int | float)
    }


__all__ = [
    "ContentFilteringSpanProcessor",
    "configure_tracing",
    "current_ids",
    "enabled",
    "finished_span",
    "flush",
    "incoming",
    "install",
    "provider_for",
    "span",
    "tracer",
    "traces_url",
]
