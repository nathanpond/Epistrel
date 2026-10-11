"""The telemetry pieces C-08 relies on (#74): the content filter, content debug logging's guard
rails, exception summaries, ``/metrics``, ``traceparent`` and the span content filter."""

from __future__ import annotations

import io
import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from epistrel import cli
from epistrel.api import create_app
from epistrel.config import ConfigError, Settings, load_settings
from epistrel.core.errors import InvalidRequest
from epistrel.observability import content, metrics, tracing
from epistrel.observability import logging as telemetry_logging
from epistrel.observability.instrument import background, outcome_of
from tests.auth_support import auth_settings, bearer

SECRET = "the lighthouse keeper hid the key under the third stone"


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Epistrel's JSON handler on a buffer for the test; the root logger restored after."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    stream = io.StringIO()
    handler = telemetry_logging.configure_logging(level="DEBUG", stream=stream)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.handlers[:] = handlers
        root.setLevel(level)
        telemetry_logging.CONTENT_POLICY.disable()


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    previous = tracing.install(tracing.provider_for(exporter, processor=SimpleSpanProcessor))
    try:
        yield exporter
    finally:
        tracing.install(previous)


def test_content_filter_drops_content_and_cuts_unknown_strings() -> None:
    """Content fields vanish at any depth (``*_text`` too), unknown strings are cut to 64
    characters, identifiers and the principal pass. Complement: a known field is not cut."""
    principal = "p-" + "x" * 200
    cleaned = content.scrub(
        {
            "text": SECRET,
            "nested": {"say": SECRET, "items": [{"prompt": SECRET}], "reply_text": SECRET},
            "note": SECRET * 2,
            "principal": principal,
            "operation_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
            "count": 3,
        }
    )
    assert cleaned == {
        "nested": {"items": [{}]},
        "note": (SECRET * 2)[: content.MAX_UNKNOWN_CHARS],
        "principal": principal,
        "operation_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
        "count": 3,
    }
    assert content.is_content_field("epistrel.summary")
    assert not content.is_content_field("epistrel.story_id")


def test_log_lines_are_json_without_content(captured: io.StringIO) -> None:
    """A structlog line and a stdlib line both leave as JSON through the filter."""
    telemetry_logging.get_logger("epistrel.test").info("drafted", text=SECRET, attempt=1)
    logging.getLogger("epistrel.test.stdlib").info("plain %s", "line")
    first, second = lines(captured)
    assert first["event"] == "drafted"
    assert first["attempt"] == 1
    assert "text" not in first
    assert {"timestamp", "level", "logger"} <= first.keys()
    assert second["event"] == "plain line"
    assert second["logger"] == "epistrel.test.stdlib"
    assert SECRET not in captured.getvalue()


def test_exceptions_are_logged_as_type_and_code(captured: io.StringIO) -> None:
    """Never the message; frames (no locals) only for an unexpected error."""
    log = telemetry_logging.get_logger("epistrel.test")
    try:
        raise InvalidRequest(SECRET, {"field": "body.say"})
    except InvalidRequest:
        log.info("refused", exc_info=True)
    try:
        raise RuntimeError(SECRET)
    except RuntimeError:
        logging.getLogger("epistrel.test.stdlib").exception("boom")
    refused, boom = lines(captured)
    assert refused["exc_type"] == "InvalidRequest"
    assert refused["code"] == "validation_error"
    assert "frames" not in refused
    assert boom["exc_type"] == "RuntimeError"
    assert boom["frames"]
    assert SECRET not in captured.getvalue()


def test_content_debug_logging_is_visible_and_time_bounded(
    captured: io.StringIO, dummy_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On: a start-up WARNING, content passes, lines say ``content_logging``, ``/health`` says
    so; after 24 h it switches itself off. Complement: off by default, and off again after."""
    client = TestClient(create_app(auth_settings(dummy_dsn)))
    assert client.get("/health").json()["content_logging"] is False
    log = telemetry_logging.get_logger("epistrel.test")

    telemetry_logging.configure_logging(level="DEBUG", content_debug=True, stream=captured)
    log.debug("drafted", text=SECRET)
    assert client.get("/health").json()["content_logging"] is True
    (warning,) = [line for line in lines(captured) if "debug logging is ON" in line["event"]]
    assert warning["level"] == "warning"
    (drafted,) = [line for line in lines(captured) if line["event"] == "drafted"]
    assert drafted["text"] == SECRET
    assert drafted["content_logging"] is True

    monkeypatch.setattr(telemetry_logging.CONTENT_POLICY, "until", time.monotonic() - 1)
    log.debug("drafted", text=SECRET)
    assert any("switched itself off" in line["event"] for line in lines(captured))
    after = [line for line in lines(captured) if line["event"] == "drafted"][-1]
    assert "text" not in after
    assert client.get("/health").json()["content_logging"] is False


def test_content_debug_logging_needs_debug(
    dummy_dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused without ``EPISTREL_DEBUG`` (``serve`` exits 78). Complement: accepted with it."""
    with pytest.raises(ValidationError, match="EPISTREL_CONTENT_DEBUG_LOGGING"):
        auth_settings(dummy_dsn, content_debug_logging=True)
    assert auth_settings(dummy_dsn, content_debug_logging=True, debug=True).content_debug_logging

    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", "c" * 40)
    monkeypatch.setenv("EPISTREL_CONTENT_DEBUG_LOGGING", "1")
    with pytest.raises(ConfigError, match="EPISTREL_CONTENT_DEBUG_LOGGING"):
        load_settings()
    assert cli.main(["serve"]) == cli.EX_CONFIG
    assert "EPISTREL_CONTENT_DEBUG_LOGGING" in capsys.readouterr().err


def test_otlp_endpoint_setting(dummy_dsn: str) -> None:
    """An http(s) URL or nothing; tracing stays off without one."""
    assert auth_settings(dummy_dsn).otlp_endpoint is None
    with pytest.raises(ValidationError, match="otlp_endpoint"):
        auth_settings(dummy_dsn, otlp_endpoint="collector:4318")
    assert tracing.traces_url("http://collector:4318/") == "http://collector:4318/v1/traces"
    assert tracing.traces_url("http://c:4318/v1/traces") == "http://c:4318/v1/traces"
    assert tracing.configure_tracing(None, version="t", debug=False) is tracing.enabled()


def test_configure_tracing_installs_an_otlp_exporter() -> None:
    previous = tracing.install(None)
    try:
        assert tracing.configure_tracing("http://127.0.0.1:9/", version="t", debug=True)
        assert tracing.enabled()
    finally:
        mine = tracing.install(previous)
        if mine is not None:
            mine.shutdown()


def test_metrics_route_is_public_in_every_mode(dummy_dsn: str) -> None:
    """Served without a credential, by an ``api`` and a ``worker`` process, in the Prometheus
    text format, with the request histogram and without the default process collectors."""
    for mode in ("api", "worker"):
        client = TestClient(create_app(auth_settings(dummy_dsn, mode=mode)))
        client.get("/health")
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "epistrel_http_request_duration_seconds_bucket" in response.text
        assert 'route="health"' in response.text
        assert "process_cpu_seconds_total" not in response.text
    unmatched = TestClient(create_app(auth_settings(dummy_dsn)))
    assert unmatched.get("/no-such-route", headers=bearer()).status_code == 404
    assert 'route="unmatched"' in metrics.render_metrics().decode()


def test_gauges_are_read_at_scrape(pg_url: str) -> None:
    """Bound to a database, a scrape carries the job, timer and worker gauges."""
    metrics.GAUGES.bind(pg_url)
    try:
        text = metrics.render_metrics().decode()
    finally:
        metrics.GAUGES.unbind()
    for name in ("epistrel_jobs_running", "epistrel_timers_overdue_seconds"):
        assert name in text
    assert "epistrel_workers_alive" in text


def test_traceparent_is_honoured(dummy_dsn: str, spans: InMemorySpanExporter) -> None:
    """The request's server span continues the caller's trace."""
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    client = TestClient(create_app(auth_settings(dummy_dsn)))
    client.get("/health", headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"})
    (server,) = [s for s in spans.get_finished_spans() if s.name == "http get health"]
    assert format(server.context.trace_id, "032x") == trace_id
    assert server.parent is not None
    assert format(server.parent.span_id, "016x") == "00f067aa0ba902b7"
    assert server.attributes is not None
    assert server.attributes["http.route"] == "health"


def test_span_content_filter(spans: InMemorySpanExporter) -> None:
    """Content attributes, exception events and status descriptions never leave."""

    def work() -> None:
        with tracing.span("work", {"epistrel.kind": "work"}) as span:
            span.set_attribute("epistrel.text", SECRET)
            span.set_attribute("note", SECRET)
            span.record_exception(RuntimeError(SECRET))
            raise RuntimeError(SECRET)

    with pytest.raises(RuntimeError):
        work()
    (exported,) = spans.get_finished_spans()
    assert exported.attributes == {
        "epistrel.kind": "work",
        "note": SECRET[: content.MAX_UNKNOWN_CHARS],
    }
    assert exported.events == ()
    assert exported.status.description is None
    assert exported.status.status_code.name == "ERROR"


def test_background_work_is_counted(captured: io.StringIO) -> None:
    """A job run gets a line with principal ``system``, its job id and outcome."""
    before = metrics.OPERATIONS.labels("test_job", "error")._value.get()
    with background("test_job", job_id="01J9Z8Q4N5V6W7X8Y9Z0A1B2C3") as run:
        run.outcome = "error"
    (line,) = [entry for entry in lines(captured) if entry["event"] == "background"]
    assert line["principal"] == "system"
    assert line["job_id"] == "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
    assert line["outcome"] == "error"
    assert metrics.OPERATIONS.labels("test_job", "error")._value.get() == before + 1
    assert outcome_of(InvalidRequest("x")) == "rejected"
    assert outcome_of(RuntimeError()) == "error"


def test_settings_field_names_are_documented() -> None:
    assert {"content_debug_logging", "otlp_endpoint"} <= set(Settings.model_fields)
