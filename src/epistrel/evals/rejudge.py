"""Re-judge a run's E-03 reflections offline with another judge (#747).

``evals rejudge --suite e03 --from-journal <journal>`` reads the E-03 lines of a results journal
(``evals.journal``: each line keeps the reflections the consolidation wrote, as its ``output``)
and judges them again with the run's ``entailment`` judge, normally a candidate judge model
named with ``--judge-model-for entailment=<model>``. No consolidation runs, and nothing is
written to the journal: only judge calls are made.

Each window's sources are rebuilt from its labelled case as the turn commit wrote them
(``records.render_line``: ``<Name> does: <text>``), so the judge sees what E-03's judge saw.
Each case's two labelled pairs are judged once (not per seed): they are the same text on every
seed.

**The cause of a failing window** (one the journal's judge found unfaithful). The window is
``consolidation`` when the candidate judge also finds one of its reflections not entailed and
it judged both of that window's labelled pairs correctly. Otherwise it is ``judge``: the
candidate entails every reflection, or it mis-called a labelled pair of that window, so its
verdict there is no evidence against the consolidation (the planner's tie rule). The issue's
cause is the majority, with the counts.

The result also gives ``nfr_qual_3.faithfulness`` and the per-pair ``e03.judge_agreement`` as the
candidate judges them, per seed and under the gate's rules, so the trust gate (ADR-204) can be
read before any fresh run.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epistrel.core.memory.records import render_line
from epistrel.evals.gate import Judgement, SeedTally, judge
from epistrel.evals.judging import CALIBRATION_FLOOR
from epistrel.evals.suites import SUITES
from epistrel.evals.suites.e03_consolidation import (
    FAITHFULNESS,
    JUDGE_AGREEMENT,
    ConsolidationCase,
)
from epistrel.evals.suites.e03_consolidation import judge as entailed_by
from epistrel.orchestrator.models.access import ModelAccessLayer, eval_scope

SUITE = "e03"
CAUSE_JUDGE = "judge"
CAUSE_CONSOLIDATION = "consolidation"


@dataclass(frozen=True, slots=True)
class Line:
    """One journalled E-03 case and seed: the reflections it wrote and how its judge called
    them."""

    case: str
    seed: int
    reflections: tuple[str, ...]
    entailed: tuple[bool, ...]
    faithful: bool | None


@dataclass
class Window:
    """One line re-judged: the candidate's verdict per reflection, and the window's cause when
    the journal's judge found it unfaithful."""

    case: str
    seed: int
    reflections: list[str]
    before: list[bool]
    after: list[bool]
    pairs: list[bool]
    cause: str | None = None

    @property
    def faithful_before(self) -> bool | None:
        return all(self.before) if self.reflections else None

    @property
    def faithful_after(self) -> bool | None:
        return all(self.after) if self.reflections else None


@dataclass
class Rejudged:
    """What ``rejudge`` found: each window, and the two metrics as the candidate judges them."""

    judge: str
    journal: str
    windows: list[Window] = field(default_factory=list)
    faithfulness: Judgement | None = None
    agreement: Judgement | None = None

    @property
    def causes(self) -> Counter[str]:
        return Counter(w.cause for w in self.windows if w.cause is not None)

    @property
    def cause(self) -> str | None:
        """The majority cause; a tie is ``judge``."""
        counts = self.causes
        if not counts:
            return None
        if counts[CAUSE_CONSOLIDATION] > counts[CAUSE_JUDGE]:
            return CAUSE_CONSOLIDATION
        return CAUSE_JUDGE

    @property
    def trusted(self) -> bool:
        mean = self.agreement.mean if self.agreement is not None else None
        return mean is not None and mean >= CALIBRATION_FLOOR

    def as_json(self) -> dict[str, Any]:
        def judged(found: Judgement | None) -> dict[str, Any] | None:
            if found is None:
                return None
            return {
                "verdict": found.verdict.value,
                "mean": found.mean,
                "stdev": found.stdev,
                "per_seed": list(found.per_seed),
                "sample_size": found.sample_size,
            }

        return {
            "suite": SUITE,
            "judge": self.judge,
            "journal": self.journal,
            "cause": self.cause,
            "causes": dict(self.causes),
            "trusted": self.trusted,
            FAITHFULNESS: judged(self.faithfulness),
            JUDGE_AGREEMENT: judged(self.agreement),
            "failing": [
                {
                    "case": w.case,
                    "seed": w.seed,
                    "cause": w.cause,
                    "reflections": w.reflections,
                    "before": w.before,
                    "after": w.after,
                    "pairs": w.pairs,
                }
                for w in self.windows
                if w.cause is not None
            ],
        }


def read_lines(path: Path) -> list[Line]:
    """The journal's E-03 lines that wrote reflections or were judged (a model-error line, with
    no outcome to re-judge, is left out). The last line of a case and seed wins."""
    found: dict[tuple[str, int], Line] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue  # a torn last line (journal docstring)
        if line.get("suite") != SUITE:
            continue
        detail = line.get("detail") or {}
        if "model_error" in detail:
            continue
        reflections = tuple(json.loads(line.get("output") or "[]"))
        found[(line["case"], int(line["seed"]))] = Line(
            case=line["case"],
            seed=int(line["seed"]),
            reflections=reflections,
            entailed=tuple(bool(e) for e in detail.get("entailed", ())),
            faithful=(line.get("observations") or {}).get(FAITHFULNESS),
        )
    return list(found.values())


def sources_of(case: ConsolidationCase) -> list[str]:
    """The window's episodes as the turn commit wrote them (module docstring)."""
    return [render_line("do", e.by, e.text) for e in case.inputs.episodes]


def cause_of(after: Sequence[bool], pairs: Sequence[bool]) -> str:
    """A failing window's cause (module docstring)."""
    if not all(after) and all(pairs):
        return CAUSE_CONSOLIDATION
    return CAUSE_JUDGE


async def rejudge(
    layer: ModelAccessLayer,
    journal: Path,
    cases: Mapping[str, ConsolidationCase],
    *,
    judge_model: str,
    concurrency: int = 4,
) -> Rejudged:
    """Re-judge ``journal``'s E-03 lines through ``layer`` (module docstring)."""
    lines = [line for line in read_lines(journal) if line.case in cases]
    slots = asyncio.Semaphore(max(1, concurrency))

    async def judged(case_id: str, claim: str) -> bool:
        async with slots:
            # The case's own scope, as E-03 judged it: the stub's script keys on it.
            with eval_scope(case_id):
                return await entailed_by(layer, claim, sources_of(cases[case_id]))

    async def pairs_of(case_id: str) -> list[bool]:
        labels = cases[case_id].expected
        entailed, non_entailed = await asyncio.gather(
            judged(case_id, labels.entailed), judged(case_id, labels.non_entailed)
        )
        return [entailed, not non_entailed]

    ids = sorted({line.case for line in lines})
    pairs = dict(zip(ids, await asyncio.gather(*(pairs_of(i) for i in ids)), strict=True))

    async def window(line: Line) -> Window:
        after = await asyncio.gather(*(judged(line.case, r) for r in line.reflections))
        found = Window(
            case=line.case,
            seed=line.seed,
            reflections=list(line.reflections),
            before=list(line.entailed),
            after=list(after),
            pairs=pairs[line.case],
        )
        if line.faithful is False:
            found.cause = cause_of(found.after, found.pairs)
        return found

    windows = await asyncio.gather(*(window(line) for line in lines))
    result = Rejudged(judge=judge_model, journal=str(journal), windows=list(windows))
    result.faithfulness, result.agreement = _metrics(result.windows, pairs)
    return result


def _metrics(
    windows: Sequence[Window], pairs: Mapping[str, Sequence[bool]]
) -> tuple[Judgement, Judgement]:
    suite = SUITES[SUITE]
    by_id = {m.id: m for m in suite.metrics}
    faithful: dict[int, SeedTally] = defaultdict(lambda: SeedTally(0))
    agreed: dict[int, SeedTally] = defaultdict(lambda: SeedTally(0))
    for w in windows:
        f = faithful[w.seed]
        if w.faithful_after is not None:
            faithful[w.seed] = SeedTally(
                w.seed, f.good + w.faithful_after, f.bad + (not w.faithful_after)
            )
        a = agreed[w.seed]
        good = sum(pairs[w.case])
        agreed[w.seed] = SeedTally(w.seed, a.good + good, a.bad + len(pairs[w.case]) - good)
    seeds = sorted(set(faithful) | set(agreed))
    return (
        judge(by_id[FAITHFULNESS], [faithful[s] for s in seeds]),
        judge(by_id[JUDGE_AGREEMENT], [agreed[s] for s in seeds]),
    )


def summary_text(found: Rejudged) -> str:
    """The lines ``evals rejudge`` prints."""
    lines = [f"rejudge {SUITE} with {found.judge} over {found.journal}"]
    for name, judgement in (
        (FAITHFULNESS, found.faithfulness),
        (JUDGE_AGREEMENT, found.agreement),
    ):
        if judgement is None or judgement.mean is None:
            lines.append(f"  {name}: not measured")
            continue
        sd = f" ± {judgement.stdev:.2%}" if judgement.stdev is not None else ""
        lines.append(
            f"  {name}: {judgement.mean:.2%}{sd} (n {judgement.sample_size}, "
            f"{judgement.verdict.value})"
        )
    lines.append(f"  trusted (agreement ≥ {CALIBRATION_FLOOR:.0%}): {found.trusted}")
    counts = found.causes
    lines.append(
        f"  failing windows: {sum(counts.values())} (judge {counts[CAUSE_JUDGE]}, "
        f"consolidation {counts[CAUSE_CONSOLIDATION]}); cause: {found.cause or '-'}"
    )
    return "\n".join(lines)


__all__ = [
    "CAUSE_CONSOLIDATION",
    "CAUSE_JUDGE",
    "Line",
    "Rejudged",
    "Window",
    "cause_of",
    "read_lines",
    "rejudge",
    "sources_of",
    "summary_text",
]
