"""A report's run conditions (#818): each story purpose's structured mode on the endpoint, and
the structured requests the trace saw, with the parse-validate-retry ones by schema."""

from __future__ import annotations

import json
from pathlib import Path

from epistrel.evals import trace
from epistrel.evals.report import SUMMARY_RE, Host, Report, RunConditions, render_markdown
from epistrel.evals.run_conditions import run_conditions, structured_modes
from epistrel.evals.trace import CallTrace, TraceFile
from epistrel.orchestrator.models.call_trace import CALL_TRACE, Exchange

ANTHROPIC = "https://api.anthropic.com/v1"
STRICT = {"type": "json_schema", "json_schema": {"name": "V", "strict": True, "schema": {}}}


def test_modes_follow_the_endpoint() -> None:
    """On api.anthropic.com every schema with a strict wire form is strict and
    ``directive_parse`` (``DirectiveParseResult``) is parse-validate-retry; on any other
    endpoint every purpose asks for ``json_schema``."""
    hosted = structured_modes(ANTHROPIC)
    assert hosted["directive_parse"] == "parse_validate_retry"
    assert hosted["entailment"] == "json_schema_strict"
    assert {m for p, m in hosted.items() if p != "directive_parse"} == {"json_schema_strict"}
    assert set(structured_modes("http://localhost:1235/v1").values()) == {"json_schema"}


def test_the_trace_counts_fallback_requests_without_the_judge(tmp_path: Path) -> None:
    sink = CallTrace(model="m", engine_sha="abc", file=TraceFile(tmp_path / "t.calls.jsonl.gz"))
    body = {"model": "m", "messages": []}
    token = CALL_TRACE.set(sink)
    try:
        call = {"purpose": "directive_parse", "schema": "DirectiveParseResult"}
        sink.record({**call, "attempt": 1}, [Exchange(body=body)])
        sink.record({**call, "attempt": 2}, [Exchange(body=body)])
        sink.record(
            {"purpose": "entailment", "attempt": 1, "schema": "V"},
            [Exchange(body={**body, "response_format": STRICT})],
        )
        sink.record({"purpose": "generation", "attempt": 1}, [Exchange(body=body)])
        with trace.judging():
            sink.record(
                {"purpose": "quality_judge", "attempt": 1, "schema": "J"}, [Exchange(body=body)]
            )
    finally:
        CALL_TRACE.reset(token)
    found = run_conditions(ANTHROPIC, sink)
    assert found.parse_validate_retry == {"DirectiveParseResult": 2}
    assert found.observed == {
        "DirectiveParseResult": {"parse_validate_retry": 2},
        "V": {"json_schema_strict": 1},
    }
    assert found.note is None
    resumed = run_conditions(ANTHROPIC, sink, reused_cases=7)
    assert resumed.note is not None
    assert "7 cases" in resumed.note
    untraced = run_conditions(ANTHROPIC, None)
    assert untraced.parse_validate_retry is None
    assert untraced.observed is None
    assert untraced.note == "untraced run (--no-trace)"


def test_a_preset_counts_only_the_requests_after_its_baseline(tmp_path: Path) -> None:
    """#988: certify plays every preset on one trace. The second preset's run conditions count
    what it sent after its baseline (``CallTrace.counts`` at its start), and a schema it never
    sent is left out. With no baseline (a single-preset run), the counts are the whole
    trace's, as before."""
    sink = CallTrace(model="m", engine_sha="abc", file=TraceFile(tmp_path / "t.calls.jsonl.gz"))
    body = {"model": "m", "messages": []}
    strict = {**body, "response_format": STRICT}
    parse = {"purpose": "directive_parse", "schema": "DirectiveParseResult", "attempt": 1}
    token = CALL_TRACE.set(sink)
    try:
        sink.record(parse, [Exchange(body=body)])
        sink.record({"purpose": "entailment", "attempt": 1, "schema": "V"}, [Exchange(body=strict)])
        first = sink.counts()
        sink.record(parse, [Exchange(body=body)])
        sink.record(parse, [Exchange(body=body)])
    finally:
        CALL_TRACE.reset(token)
    assert first == {
        "DirectiveParseResult": {"parse_validate_retry": 1},
        "V": {"json_schema_strict": 1},
    }
    second = run_conditions(ANTHROPIC, sink, baseline=first)
    assert second.observed == {"DirectiveParseResult": {"parse_validate_retry": 2}}
    assert second.parse_validate_retry == {"DirectiveParseResult": 2}
    whole = run_conditions(ANTHROPIC, sink)
    assert whole == run_conditions(ANTHROPIC, sink, baseline={})
    assert whole.observed == {
        "DirectiveParseResult": {"parse_validate_retry": 3},
        "V": {"json_schema_strict": 1},
    }
    assert sink.counts() is not sink.structured


def _report(conditions: RunConditions | None) -> Report:
    return Report(
        run_id="r",
        started_at="2026-11-18T00:00:00+00:00",
        model="m",
        fingerprint="sha256:" + "a" * 64,
        fingerprint_profile="Full",
        adapter="openai",
        engine_sha="abc",
        host=Host(kind="hosted"),
        seed_base=1,
        seeds=[1],
        run_conditions=conditions,
    )


def test_the_report_states_its_run_conditions() -> None:
    conditions = RunConditions(
        structured_modes={
            "directive_parse": "parse_validate_retry",
            "entailment": "json_schema_strict",
        },
        observed={
            "DirectiveParseResult": {"parse_validate_retry": 3},
            "V": {"json_schema_strict": 9},
        },
        parse_validate_retry={"DirectiveParseResult": 3},
    )
    text = render_markdown(_report(conditions))
    (line,) = [x for x in text.splitlines() if x.startswith("- **Structured calls:**")]
    assert "`json_schema_strict` for `entailment`" in line
    assert "`parse_validate_retry` for `directive_parse`" in line
    assert "`DirectiveParseResult` 3" in line
    assert "9 of 12 traced structured requests strict" in line
    embedded = SUMMARY_RE.search(text)
    assert embedded is not None
    assert json.loads(embedded.group("json"))["run_conditions"]["parse_validate_retry"] == {
        "DirectiveParseResult": 3
    }
    parsed = Report.model_validate_json(_report(conditions).model_dump_json())
    assert parsed.run_conditions == conditions


def test_a_report_without_run_conditions_is_unchanged() -> None:
    """A stub run, and every report before #818, has none: no line and no summary key."""
    text = render_markdown(_report(None))
    assert "Structured calls" not in text
    embedded = SUMMARY_RE.search(text)
    assert embedded is not None
    assert "run_conditions" not in json.loads(embedded.group("json"))
