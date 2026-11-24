"""A run's structured-call conditions (#818; ``Report.run_conditions``).

**Structured modes** are computed from the registry and the endpoint, not observed: for each
story purpose with a response schema, ``json_schema_strict`` when the endpoint is
api.anthropic.com and the schema has a strict wire form, ``parse_validate_retry`` when it is
api.anthropic.com and the schema has none (``provider_schema.json_schema_mode``; today
``DirectiveParseResult``), and ``json_schema`` on any other endpoint, which may still fall back
to parse-validate-retry once its probe is refused (#759). A call that names a schema other than
its purpose's is not listed here; the trace's count covers it.

**The counts** are the trace's (``CallTrace.structured``): every structured request this preset
sent in this session, by schema and mode (so ``generation``'s per-call render schemas show
too), retries included, the harness's judge calls excluded; ``parse_validate_retry`` is its
fallback share. ``epistrel evals certify`` plays its presets on one trace, so each preset's run
counts only what it sent after its start (``since``, the trace's ``counts`` then; #988).
Reports committed before #988 show a running total across a certify run's presets instead. With
no trace both are ``None``. A resumed run's journalled cases were not played in this session,
so their requests are not in it, and the note says how many cases that leaves out.
"""

from __future__ import annotations

from collections.abc import Mapping

from epistrel.evals.report import RunConditions
from epistrel.evals.trace import CallTrace


def structured_modes(base_url: str) -> dict[str, str]:
    """Each story purpose's structured mode on ``base_url`` (module docstring)."""
    from epistrel.orchestrator.models.provider_schema import is_anthropic, json_schema_mode
    from epistrel.orchestrator.models.registry import STORY_PURPOSES

    found: dict[str, str] = {}
    for name, purpose in sorted(STORY_PURPOSES.items()):
        schema = purpose.response_schema
        if schema is None:
            continue
        if not is_anthropic(base_url):
            found[name] = "json_schema"
        elif json_schema_mode(base_url, schema):
            found[name] = "json_schema_strict"
        else:
            found[name] = "parse_validate_retry"
    return found


def since(
    now: Mapping[str, Mapping[str, int]], before: Mapping[str, Mapping[str, int]]
) -> dict[str, dict[str, int]]:
    """The requests counted in ``now`` and not in ``before``, by schema and mode (none: left
    out)."""
    found: dict[str, dict[str, int]] = {}
    for schema, modes in sorted(now.items()):
        delta = {mode: count - before.get(schema, {}).get(mode, 0) for mode, count in modes.items()}
        kept = {mode: n for mode, n in sorted(delta.items()) if n}
        if kept:
            found[schema] = kept
    return found


def run_conditions(
    base_url: str,
    trace: CallTrace | None,
    reused_cases: int = 0,
    *,
    baseline: Mapping[str, Mapping[str, int]] | None = None,
) -> RunConditions:
    """This preset's ``RunConditions``: its modes on ``base_url`` and the requests ``trace``
    counted after ``baseline`` (the trace's ``counts`` when the preset's run started; none: all
    of them)."""
    notes: list[str] = []
    if trace is None:
        notes.append("untraced run (--no-trace)")
    elif reused_cases:
        notes.append(f"this session only: {reused_cases} cases came from the results journal")
    observed = None if trace is None else since(trace.structured, baseline or {})
    fallback = (
        None
        if observed is None
        else {
            k: v["parse_validate_retry"] for k, v in observed.items() if "parse_validate_retry" in v
        }
    )
    return RunConditions(
        structured_modes=structured_modes(base_url),
        observed=observed,
        parse_validate_retry=fallback,
        note="; ".join(notes) or None,
    )


__all__ = ["run_conditions", "since", "structured_modes"]
