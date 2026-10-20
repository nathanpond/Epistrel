"""E-12: engine interventions and prose, in aggregate (docs/04 §8; NFR-QUAL-14, NFR-QUAL-8;
#158).

Each case is one intervention scenario (``suites.scenario_play``), played turn by turn through
``Engine.respond``. Every turn where a repair rung, a constrained regeneration, the composition
or its polish **changed** the first draft gives one **pair**: the first draft (the raw renders
in order, put together by #152's deterministic assembly) and the final message. The pair is
masked (``judging.mask``: the cast, the places and things, dates, quantities) and judged by
``quality_judge`` on voice consistency and readability, factual differences excluded, in both
orders (``judging.compare``: a disagreement between them is a tie, an ``Uncertain`` answer
excludes the pair). A turn that changed nothing is not a pair. A turn where several
interventions fired counts for each of them.

**Metrics**, per configuration fingerprint (``structured``: ``Structured (M4)``; ``gated``: its
claim-extraction-gated variant) and intervention type (``scenario_play.INTERVENTION_TYPES``):
``nfr_qual_14.<config>.<type>`` is the **first-draft preference rate** over the type's non-tied
pairs (a pair whose draft the judge preferred is a bad observation, one whose final message it
preferred is a good one, ties are left out). It is judged by the ``relative`` rule: at least 300
non-tied pairs per seed, then mean + 1 SD ≤ 55 % passes (an aggregate target: individual
messages may still read worse than their drafts); fewer pairs is ``insufficient_evidence``,
never a pass. Each row carries its pooled Wilson 95 % interval. ``e12.<config>.changed``
(report-only) is the share of turns that changed their draft. The report's notes list each
configuration's fingerprint and the types the floor covers on this run (300 pairs or more).

Every row is judge-based: before the run the judge must be calibrated (``judging``),
and below 75 % agreement every row is ``report_only``. The judge only reads the case's own
scratch stories.

On the stub, the scenarios are scripted so that each turn triggers its family's intervention,
and the judge's preference is scripted per turn (``stub_judge``). CI's stub run proves the
plumbing and reports ``insufficient_evidence``: the stub set is far below 300 pairs a type. The
local model accumulates real pairs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.judging import Preference, mask, script_pair
from epistrel.evals.metrics import wilson
from epistrel.evals.suites import Aggregate, CaseModel, Counts, Outcome, Suite, Summary, register
from epistrel.evals.suites.scenario_play import (
    CONFIG_DIALS,
    CONFIGS,
    INTERVENTION_TYPES,
    ScenarioCase,
    play,
    script,
)
from epistrel.orchestrator.models.stub import StubAdapter

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

FLOOR = 0.55
SET_PATH = "scenarios/cases.jsonl"
MIN_SCENARIOS = 21

type Side = Literal["draft", "final", "tie"]
_SIDE: Mapping[Preference, Side] = {"a": "draft", "b": "final", "tie": "tie"}
_STUB: Mapping[Side, Preference] = {"draft": "a", "final": "b", "tie": "tie"}


def metric_id(config: str, kind: str) -> str:
    return f"nfr_qual_14.{config}.{kind}"


def changed_id(config: str) -> str:
    return f"e12.{config}.changed"


def _metrics() -> tuple[Metric, ...]:
    found: list[Metric] = []
    for config in CONFIGS:
        for kind in INTERVENTION_TYPES:
            found.append(
                Metric(
                    metric_id(config, kind),
                    Rule.relative,
                    FLOOR,
                    Direction.at_most,
                    f"{config}: the judge preferred the first draft over the final message, "
                    f"over the non-tied pairs where {kind.replace('_', ' ')} changed it",
                )
            )
        found.append(
            Metric(
                changed_id(config),
                Rule.informational,
                direction=Direction.at_least,
                description=f"{config}: the share of turns whose final message changed its draft",
            )
        )
    return tuple(found)


def _pairs(outcome: Outcome) -> list[Mapping[str, Any]]:
    found = outcome.detail.get("pairs") or []
    return [p for p in found if isinstance(p, Mapping)]


@register
class E12Suite(Suite):
    name = "e12"
    title = "E-12 (M4): engine interventions and prose, in aggregate"
    set_path = SET_PATH
    min_cases = MIN_SCENARIOS
    guarantees = ("quality_judge",)
    ci_gate = True
    case_model = ScenarioCase
    metrics = _metrics()
    judge_metrics = frozenset(
        metric_id(config, kind) for config in CONFIGS for kind in INTERVENTION_TYPES
    )
    judge_calibration = "pairwise"
    reading_notes = (
        "E-12 (M4) pairs each turn's first draft (its raw renders, assembled deterministically) "
        "with its final message wherever a repair rung, the composition or its polish changed "
        "it; names, places, dates and quantities are masked alike in both, and the judge "
        "compares them in both orders on voice and readability only (a disagreement is a tie). "
        "A row is the first-draft preference rate over the type's non-tied pairs, with its "
        "pooled Wilson 95% interval.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ScenarioCase)  # noqa: S101 - load() validated it
        played = await play(ctx, case)
        pairs: list[dict[str, Any]] = []
        untyped = 0
        for turn in played.turns:
            if not turn.changed:
                continue
            if not turn.types:
                # A change no intervention type names (an output-policy regeneration): reported,
                # never judged under a type.
                untyped += 1
                continue
            judged = await ctx.judge(
                played.story_id,
                mask(turn.draft, played.names, played.places),
                mask(turn.final, played.names, played.places),
                "voice_readability",
                key=f"t{turn.index}",
            )
            pairs.append(
                {
                    "turn": turn.index,
                    "types": list(turn.types),
                    "preferred": None if judged.preferred is None else _SIDE[judged.preferred],
                }
            )
        detail = {
            "seed": ctx.seed,
            "config": case.config,
            "turns": len(played.turns),
            "changed": sum(t.changed for t in played.turns),
            "untyped_changes": untyped,
            "pairs": pairs,
            "types": [list(t.types) for t in played.turns],
        }
        return Outcome({}, output=played.turns[-1].final if played.turns else "", detail=detail)

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        """Per configuration and type, the seed's non-tied pairs as (final preferred, draft
        preferred); per configuration, the turns that changed and those that did not."""
        prefer: dict[str, list[int]] = {m.id: [0, 0] for m in self.metrics}
        for outcome in outcomes:
            config = str(outcome.detail.get("config"))
            changed = int(outcome.detail.get("changed") or 0)
            turns = int(outcome.detail.get("turns") or 0)
            tally = prefer[changed_id(config)]
            tally[0] += changed
            tally[1] += turns - changed
            for pair in _pairs(outcome):
                side = pair.get("preferred")
                if side not in ("final", "draft"):
                    continue
                for kind in pair.get("types") or []:
                    tally = prefer[metric_id(config, str(kind))]
                    tally[0 if side == "final" else 1] += 1
        return {key: Counts(good, bad) for key, (good, bad) in prefer.items()}

    def summarize(self, outcomes: Sequence[Outcome], model: str) -> Summary:
        """Each row's pooled Wilson interval over every seed; the fingerprints and the types the
        floor covers."""
        from epistrel.evals.gate import RELATIVE_MIN_PAIRS
        from epistrel.evals.runner import run_fingerprint

        pooled: dict[str, list[int]] = {}
        for metric_key, counts in self.aggregate(outcomes).items():
            assert isinstance(counts, Counts)  # noqa: S101 - aggregate gives counts
            pooled[metric_key] = [counts.good, counts.bad]
        metrics: dict[str, dict[str, Any]] = {}
        for key, (good, bad) in pooled.items():
            # A preference row's interval is of the first-draft rate (the bad side).
            interval = wilson(bad if key.startswith("nfr_qual_14.") else good, good + bad)
            if interval is not None:
                metrics[key] = {"ci95": list(interval)}
        by_seed: dict[object, list[Outcome]] = {}
        for outcome in outcomes:
            by_seed.setdefault(outcome.detail.get("seed"), []).append(outcome)
        per_seed = [self.aggregate(group) for group in by_seed.values()]
        covered = sorted(
            key
            for key in pooled
            if key.startswith("nfr_qual_14.")
            and per_seed
            and min(_n(found[key]) for found in per_seed) >= RELATIVE_MIN_PAIRS
        )
        notes = [
            f"E-12 fingerprint `{config}`: `{run_fingerprint(model, CONFIG_DIALS[config])}`."
            for config in CONFIGS
        ]
        notes.append(
            "E-12 types the floor covers on this run (at least 300 non-tied pairs per seed): "
            + (", ".join(f"`{k}`" for k in covered) or "none")
            + "."
        )
        return Summary(metrics=metrics, notes=notes)

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        assert isinstance(case, ScenarioCase)  # noqa: S101 - load() validated it
        script(case, stub)
        for index, turn in enumerate(case.turns, start=1):
            script_pair(stub, f"{case.id}.t{index}", _STUB[turn.stub_judge])


def _n(found: Aggregate) -> int:
    return found.good + found.bad if isinstance(found, Counts) else found.n


__all__ = ["FLOOR", "E12Suite", "changed_id", "metric_id"]
