"""The model-quality eval harness (NFR-QUAL-8, docs/04 §8; #134).

Labeled suites (``suites``) run through the real code paths — an in-process ``Engine``, its
Model Access Layer and scratch stories it creates and deletes (``runner``) — against the stub
adapter (CI: plumbing and deterministic floors, no spend) or a real OpenAI-compatible endpoint
run by hand (the reference tier: a local LM Studio model). ``gate`` applies NFR-QUAL-8's rate
and observed-zero rules; ``report`` writes the JSON, the Markdown and ``docs/evals/INDEX.md``;
``check`` cross-checks the suites with the guarantee table (#132). ``cli`` is
``python -m epistrel.evals`` and ``epistrel evals``. Nothing in the engine imports this package:
the harness never runs in a turn.
"""

from __future__ import annotations

from epistrel.evals.gate import (
    ZERO_MIN_CASES,
    Direction,
    Metric,
    Rule,
    SeedTally,
    Verdict,
    gate,
    judge,
    upper_bound_95,
)
from epistrel.evals.report import (
    MetricResult,
    Report,
    SuiteResult,
    render_index,
    render_markdown,
)
from epistrel.evals.runner import (
    AdapterConfig,
    CaseContext,
    run,
    run_suites,
    seeds_from,
)
from epistrel.evals.suites import SUITES, CaseModel, MalformedCase, Outcome, Suite, register

__all__ = [
    "SUITES",
    "ZERO_MIN_CASES",
    "AdapterConfig",
    "CaseContext",
    "CaseModel",
    "Direction",
    "MalformedCase",
    "Metric",
    "MetricResult",
    "Outcome",
    "Report",
    "Rule",
    "SeedTally",
    "Suite",
    "SuiteResult",
    "Verdict",
    "gate",
    "judge",
    "register",
    "render_index",
    "render_markdown",
    "run",
    "run_suites",
    "seeds_from",
    "upper_bound_95",
]
