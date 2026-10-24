"""Eval suites and their registry (#134).

A suite names its metrics (``gate.Metric``: id, rule, floor, direction), the ``GUARANTEES``
rows (#132) it measures, whether CI gates on its stub run (``ci_gate``: its stub floors are
deterministic), and its labeled set: ``evals/sets/<name>/cases.jsonl`` under ``--sets-dir``,
one JSON object per line, each validated by the suite's ``case_model`` (a malformed line fails
the suite with ``MalformedCase``, naming the line). ``evaluate`` plays one case through the
real code paths (``runner.CaseContext``: the in-process ``Engine``, scratch stories) and
returns an ``Outcome``: per metric, good, bad or not observed. ``stub_answers`` scripts the
stub adapter per case (``StubAdapter.case``), so a stub run exercises the harness end to end.

A suite may prepare deployment state its cases share (``setup``) and undo it (``teardown``,
always run). Suites register with ``@register``; the CLI names them by ``name`` (``smoke``;
#155's, #157's and #158's suites by their docs/04 ids: ``e02``, ``e01``, ``q01``, ``e12``, …).
After every seed, ``summarize`` may add a pooled confidence interval per metric and report notes
(#158). A suite whose rows come from the quality judge names them in ``judge_metrics``: the run
makes them ``report_only`` unless the judge is calibrated (``judge_calibration``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from epistrel.evals.gate import Metric

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.models.stub import StubAdapter

CASES_FILE = "cases.jsonl"
CASE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"


class MalformedCase(Exception):
    """A labeled set's line is not a valid case of its suite (or the set is missing)."""


class CaseModel(BaseModel):
    """The base of every suite's case schema: a stable id, unknown keys refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=CASE_ID_PATTERN)


@dataclass(frozen=True, slots=True)
class Counts:
    """A per-seed aggregate as good and bad observations (``Suite.aggregate``)."""

    good: int
    bad: int


@dataclass(frozen=True, slots=True)
class Value:
    """A per-seed aggregate that is a value over ``n`` cases (``Suite.aggregate``); ``None``
    when it is undefined on that seed (a constant side of a correlation)."""

    value: float | None
    n: int


type Aggregate = Counts | Value


@dataclass(frozen=True, slots=True)
class Summary:
    """What a suite adds after every seed (``Suite.summarize``, #158): per metric id, fields of
    its ``MetricResult`` (``ci95``: a pooled 95 % interval), and notes for the report."""

    metrics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    notes: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class Outcome:
    """One case's observations: metric id → good (``True``), bad (``False``) or not observed
    (``None``, or absent), the output to keep for a failure's record, and its latency."""

    observations: Mapping[str, bool | None]
    output: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)


class Suite:
    """A labeled eval suite (see the module docstring); subclasses set the class attributes."""

    name: ClassVar[str]
    title: ClassVar[str]
    metrics: ClassVar[tuple[Metric, ...]]
    guarantees: ClassVar[tuple[str, ...]] = ()
    ci_gate: ClassVar[bool] = False
    reading_notes: ClassVar[tuple[str, ...]] = ()
    """Suite-specific scoring notes for the report's "How to read" section (#437)."""
    case_model: ClassVar[type[CaseModel]] = CaseModel
    set_path: ClassVar[str | None] = None
    """The labeled set's file under ``--sets-dir`` (default ``<name>/cases.jsonl``; #157's sets
    are named by what they label: ``entailment/cases.jsonl``, ``retrieval/recall.jsonl``)."""
    min_cases: ClassVar[int] = 1
    """The fewest cases the committed set may hold (#157's sizes; ``--check-sets``)."""
    judge_metrics: ClassVar[frozenset[str]] = frozenset()
    """The metrics whose values come from the quality judge (#158): an uncalibrated judge makes
    them ``report_only``."""
    judge_calibration: ClassVar[Literal["pairwise", "none"]] = "none"
    """``pairwise``: the suite's judge rows are pairwise judgements, which the stored pairwise
    calibration covers (a run fails fast without one for its judge model); ``none``: its judge
    rows (single-message scores, E-13) have no calibration set in M4 and are ``uncalibrated``."""
    dials: ClassVar[Mapping[str, Any] | None] = None
    """The settings merge patch the suite's stories run over the seeded profile (#179's M5
    modules), named by ``profile_label``; its result reports that configuration's fingerprint.
    ``None``: the seeded profile as it is."""
    profile_label: ClassVar[str | None] = None
    judge_purposes: ClassVar[tuple[str, ...]] = ()
    """The purposes the suite's harness-side judge calls use (#475): E-03's ``entailment``,
    E-12/E-13's ``quality_judge``. A suite that names any is a **judged suite**. Its judge calls
    go through ``CaseContext.judge_models`` (the run's judge model when one is set), and its
    result records which model judged."""

    @property
    def set_file(self) -> str:
        """``set_path``, else ``<name>/cases.jsonl``."""
        return self.set_path or f"{self.name}/{CASES_FILE}"

    def load(self, sets_dir: Path) -> list[CaseModel]:
        """The suite's cases from ``<sets_dir>/<set_file>``."""
        path = sets_dir / self.set_file
        if not path.is_file():
            raise MalformedCase(f"{self.name}: no labeled set at {path}")
        cases: list[CaseModel] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                cases.append(self.case_model.model_validate(json.loads(line)))
            except (ValueError, ValidationError) as exc:
                reason = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                raise MalformedCase(f"{self.name}: {path.name} line {number}: {reason}") from None
        ids = [case.id for case in cases]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise MalformedCase(f"{self.name}: duplicate case ids {duplicates}")
        if not cases:
            raise MalformedCase(f"{self.name}: {path} holds no cases")
        return cases

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        raise NotImplementedError

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        """Purpose → the stub's answer for ``case`` (empty: the stub's default answers)."""
        return {}

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        """Script the stub for ``case`` before a stub run: by default ``stub_answers`` through
        ``StubAdapter.case``. A suite whose case makes several calls of one purpose with
        different answers overrides it (``StubAdapter.when`` keyed on the case's
        ``X-Eval-Case`` line and a needle of the request; #157)."""
        for purpose, answer in self.stub_answers(case).items():
            stub.case(case.id, answer, purpose=purpose)

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        """Per-seed values of metrics that are not one boolean per case, from one seed's
        outcomes: ``(good, bad)`` counts (micro recall over several relevant items a case) or
        a value with the cases it covers (``(value, n)``: a rank correlation). A metric named
        here takes this instead of the per-case tally; nothing by default."""
        return {}

    def summarize(self, outcomes: Sequence[Outcome], model: str) -> Summary:
        """After every seed, from all their outcomes: per-metric additions (a pooled
        confidence interval) and report notes for the run's ``model``; nothing by default."""
        return Summary()

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        """Before the suite's first case (deployment state its cases share; #155's E-02
        proposes a dedicated test shape's threshold); nothing by default."""

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        """After its last case, even when one failed: undo ``setup``; nothing by default."""


SUITES: dict[str, Suite] = {}
"""Every registered suite by name, in registration order."""


def register[S: type[Suite]](cls: S) -> S:
    """Register one instance of the suite class ``cls`` under its ``name``."""
    if cls.name in SUITES:
        raise ValueError(f"suite {cls.name!r} is registered twice")
    SUITES[cls.name] = cls()
    return cls


def get_suites(names: Sequence[str] | None = None) -> list[Suite]:
    """The named suites (every registered one for ``None``); ``KeyError`` for an unknown name."""
    if names is None:
        return list(SUITES.values())
    return [SUITES[name] for name in names]


# The suites shipped with the engine register on import.
from epistrel.evals.suites import e01_entailment as _e01  # noqa: E402, F401
from epistrel.evals.suites import e01_viewpoint as _e01v  # noqa: E402, F401
from epistrel.evals.suites import e02_escalation as _e02  # noqa: E402, F401
from epistrel.evals.suites import e03_consolidation as _e03  # noqa: E402, F401
from epistrel.evals.suites import e04_importance as _e04  # noqa: E402, F401
from epistrel.evals.suites import e05_salience as _e05  # noqa: E402, F401
from epistrel.evals.suites import e06_conformance as _e06  # noqa: E402, F401
from epistrel.evals.suites import e07_structured_output as _e07  # noqa: E402, F401
from epistrel.evals.suites import e10_claim_detection as _e10  # noqa: E402, F401
from epistrel.evals.suites import e12_interventions as _e12  # noqa: E402, F401
from epistrel.evals.suites import e13_signals as _e13  # noqa: E402, F401
from epistrel.evals.suites import priv_think_leak as _think  # noqa: E402, F401
from epistrel.evals.suites import q01_recall as _q01  # noqa: E402, F401
from epistrel.evals.suites import q02_resolution as _q02  # noqa: E402, F401
from epistrel.evals.suites import q03_abstract as _q03  # noqa: E402, F401
from epistrel.evals.suites import q04_asymmetry as _q04  # noqa: E402, F401
from epistrel.evals.suites import smoke as _smoke  # noqa: E402, F401

__all__ = [
    "CASES_FILE",
    "SUITES",
    "Aggregate",
    "CaseModel",
    "Counts",
    "MalformedCase",
    "Outcome",
    "Suite",
    "Summary",
    "Value",
    "get_suites",
    "register",
]
