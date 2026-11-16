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

**Metrics**, per configuration fingerprint (``structured``: ``Structured``; ``gated``: its
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

**Direct mode** (#936, ``E12DirectSuite``, suite ``e12_direct``): a case is a prepared first
draft (the render's beats and their declared claims) with its trigger already in it, over
S-COAT's canon plus the case's own. One turn plays through ``Engine.respond`` on a scratch story
with the render purpose pinned to the draft (``CaseContext.pinned``): detection, verification,
the repair ladder and the composition are the production pipeline's own, and every other
purpose stays on the run's model. A case whose turn left the draft unchanged counts in
``e12.direct.not_triggered.<type>`` and adds no pair; one changed by something other than its
declared type counts in ``e12.direct.mismatch.<type>``; otherwise the draft and the final
message are judged as above. ``nfr_qual_14.direct.<type>`` is the type's first-draft
preference rate under the same relative rule, on the ``structured`` configuration only, and
is never pooled with the full-mode rows (``--e12-mode full|direct|both`` selects the suites:
``select_mode``). This story ships the four repair rungs; #937 adds the composition and identity
types.

On the stub, the scenarios are scripted so that each turn triggers its family's intervention,
and the judge's preference is scripted per turn (``stub_judge``). CI's stub run proves the
plumbing and reports ``insufficient_evidence``: the stub set is far below 300 pairs a type. The
local model accumulates real pairs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from epistrel.core.mutation import FactRequest
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.stories import StorySpec
from epistrel.evals import scenes
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.judging import Preference, mask, normalized, script_pair
from epistrel.evals.metrics import wilson
from epistrel.evals.suites import Aggregate, CaseModel, Counts, Outcome, Suite, Summary, register
from epistrel.evals.suites.scenario_play import (
    CAST,
    CONFIG_DIALS,
    CONFIGS,
    INTERVENTION_TYPES,
    ITEMS,
    PLACES,
    PLAYER,
    RENDERER,
    Config,
    ScenarioCase,
    first_draft,
    plant,
    play,
    script,
    user_line,
)
from epistrel.orchestrator.lite.schemas import RespondRequest
from epistrel.orchestrator.models.adapter import EVAL_CASE_HEADER
from epistrel.orchestrator.models.registry import GENERATION, QUALITY_JUDGE, REGENERATION
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


COMPLETED = "e12.completed"
"""The share of scenario cases that played to the end: a model error fails its case (#134's
rule), which then yields no pair of any type and is counted here, never dropped silently."""


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
    found.append(
        Metric(
            COMPLETED,
            Rule.informational,
            direction=Direction.at_least,
            description="scenario cases that played to the end (a model error fails its case)",
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
    judge_purposes = (QUALITY_JUDGE.name,)
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
            if outcome.detail.get("config") not in CONFIGS:
                # A model error failed this case before it played (no pairs to count).
                prefer[COMPLETED][1] += 1
                continue
            prefer[COMPLETED][0] += 1
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


# -- direct mode (#936) -----------------------------------------------------------------------

E12 = "e12"
E12_DIRECT = "e12_direct"
DIRECT_CONFIG: Config = "structured"
"""Direct rows run on the ``structured`` configuration only (#936; no ``gated`` variant)."""
DirectType = Literal["repair_rung_1", "repair_rung_2", "repair_rung_3", "repair_rung_4"]
DIRECT_TYPES: tuple[DirectType, ...] = (
    "repair_rung_1",
    "repair_rung_2",
    "repair_rung_3",
    "repair_rung_4",
)
"""The types a direct case may declare (#936: the repair rungs; #937 adds the rest)."""
DIRECT_SET_PATH = "interventions/examples.jsonl"
DIRECT_MIN_PER_TYPE = 5
E12_MODES = ("full", "direct", "both")
DIRECT_COMPLETED = "e12.direct.completed"
"""The share of direct cases that played (a model error fails its case, never silently)."""


def direct_metric_id(kind: str) -> str:
    return f"nfr_qual_14.direct.{kind}"


def not_triggered_id(kind: str) -> str:
    return f"e12.direct.not_triggered.{kind}"


def mismatch_id(kind: str) -> str:
    return f"e12.direct.mismatch.{kind}"


class DirectClaim(BaseModel):
    """A declared claim (the render's proposition sketch), or a canon fact the case adds, by
    the names of S-COAT's cast, items and places."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    object: str = Field(min_length=1)


class DirectBeat(BaseModel):
    """One beat of the prepared draft, as the render declares it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["say", "do", "think"]
    text: str = Field(min_length=1, max_length=1_000)
    intent: str | None = None
    propositions: list[DirectClaim] = Field(default_factory=list)


class DirectStub(BaseModel):
    """A CI case's scripted answers: the regeneration (rungs 3 and 4) and the judge's side."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    regeneration: str | None = None
    judge: Literal["final", "draft", "tie"] = "final"


NAMES = frozenset({PLAYER, RENDERER, *ITEMS, *PLACES})


class DirectCase(CaseModel):
    """A prepared first draft with its trigger in it (module docstring)."""

    id: str = Field(pattern=r"^direct-[A-Za-z0-9_.:-]{1,72}$")
    type: DirectType
    say: str = Field(min_length=1, max_length=500)
    beats: list[DirectBeat] = Field(min_length=1, max_length=8)
    canon: list[DirectClaim] = Field(default_factory=list)
    stub: DirectStub | None = None

    @model_validator(mode="after")
    def _known_canon(self) -> DirectCase:
        for fact in self.canon:
            unknown = {fact.subject, fact.object} - NAMES
            if unknown:
                raise ValueError(f"canon names unknown entities {sorted(unknown)}")
            if fact.relation not in CORE_BY_NAME:
                raise ValueError(f"canon relation {fact.relation!r} is not a core predicate")
        return self

    def render(self) -> str:
        """The draft as the render purpose answers it."""
        beats = [b.model_dump(exclude_defaults=True) for b in self.beats]
        return json.dumps({"beats": beats})


def direct_outcome(declared: str, types: Sequence[str], changed: bool) -> str:
    """How a played direct case counts: ``not_triggered`` (the draft came back unchanged),
    ``pair`` (its declared type fired, whatever co-fired) or ``mismatch``."""
    if not changed:
        return "not_triggered"
    return "pair" if declared in types else "mismatch"


def _direct_metrics() -> tuple[Metric, ...]:
    found: list[Metric] = []
    for kind in DIRECT_TYPES:
        found.append(
            Metric(
                direct_metric_id(kind),
                Rule.relative,
                FLOOR,
                Direction.at_most,
                f"direct: the judge preferred the prepared draft over the final message, over "
                f"the non-tied pairs where {kind.replace('_', ' ')} changed it",
            )
        )
        found.append(
            Metric(
                not_triggered_id(kind),
                Rule.informational,
                direction=Direction.at_most,
                description=f"direct {kind} cases whose draft the engine left unchanged",
            )
        )
        found.append(
            Metric(
                mismatch_id(kind),
                Rule.informational,
                direction=Direction.at_most,
                description=f"direct {kind} cases changed by another intervention, not {kind}",
            )
        )
    found.append(
        Metric(
            DIRECT_COMPLETED,
            Rule.informational,
            direction=Direction.at_least,
            description="direct cases that played (a model error fails its case)",
        )
    )
    return tuple(found)


@register
class E12DirectSuite(Suite):
    name = E12_DIRECT
    title = "E-12 (M11): engine interventions on prepared drafts (direct mode)"
    set_path = DIRECT_SET_PATH
    min_cases = DIRECT_MIN_PER_TYPE * len(DIRECT_TYPES)
    guarantees = ("quality_judge",)
    ci_gate = True
    case_model = DirectCase
    calls_per_case = 4
    metrics = _direct_metrics()
    judge_metrics = frozenset(direct_metric_id(kind) for kind in DIRECT_TYPES)
    judge_calibration = "pairwise"
    judge_purposes = (QUALITY_JUDGE.name,)
    reading_notes = (
        "E-12 direct mode (M11) plays one prepared first draft per case through the real turn "
        "pipeline (only the render is pinned to the draft) and pairs the draft with the final "
        "message when the case's declared intervention changed it. Its rows are reported "
        "beside the full-turn rows, never pooled with them; a draft left unchanged or changed "
        "by another intervention adds no pair (`e12.direct.not_triggered.*`, "
        "`e12.direct.mismatch.*`).",
    )

    def __init__(self) -> None:
        self._pinned: set[tuple[int, str]] = set()

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, DirectCase)  # noqa: S101 - load() validated it
        created = await ctx.story(StorySpec(characters=list(CAST)))
        story_id = created.story_id
        await scenes.structured(ctx.engine, ctx.principal, story_id, CONFIG_DIALS[DIRECT_CONFIG])
        await ctx.engine.update_story_settings(ctx.principal, story_id, {"player_view": "story"})
        found = await plant(ctx, story_id, [c.id for c in created.characters])
        ids = {c.name: c.id for c in created.characters}
        found.update(ids)
        for fact in case.canon:
            await ctx.engine.commit_fact(
                ctx.principal,
                story_id,
                FactRequest(
                    proposition=Proposition(
                        subject_id=found[fact.subject],
                        predicate_id=CORE_BY_NAME[fact.relation].id,
                        object_id=found[fact.object],
                        valid_time=Interval.of(None, None),
                    )
                ),
            )
        self._pin(ctx, case)
        request = RespondRequest.model_validate(
            {
                "actor_character_id": ids[PLAYER],
                "player_input": {"say": case.say},
                "render_character_ids": [ids[RENDERER]],
            }
        )
        with ctx.pinned({GENERATION.name}):
            result = await ctx.engine.respond(ctx.principal, story_id, request)
        names = {c.id: c.name for c in created.characters}
        draft, _, types, _ = await first_draft(ctx, result, names)
        final = result.message.text
        counted = direct_outcome(case.type, types, normalized(draft) != normalized(final))
        preferred: str | None = None
        if counted == "pair":
            judged = await ctx.judge(
                story_id,
                mask(draft, (PLAYER, RENDERER), (*ITEMS, *PLACES)),
                mask(final, (PLAYER, RENDERER), (*ITEMS, *PLACES)),
                "voice_readability",
                key="pair",
            )
            preferred = None if judged.preferred is None else _SIDE[judged.preferred]
        detail = {
            "seed": ctx.seed,
            "type": case.type,
            "counted": counted,
            "observed": list(types),
            "preferred": preferred,
        }
        return Outcome({}, output=final, detail=detail)

    def _pin(self, ctx: CaseContext, case: DirectCase) -> None:
        """A real-model run's draft, on the stub the pinned render reaches (once a run per
        case); a stub run scripted it with the case (``script_stub``)."""
        stub = ctx.scripted_stub
        if stub is ctx.engine.models.adapter or (id(stub), case.id) in self._pinned:
            return
        self._pinned.add((id(stub), case.id))
        _script_render(case, stub)

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        """Per type, the seed's non-tied pairs as (final preferred, draft preferred), and its
        not-triggered and mismatched cases as a share of the type's played cases."""
        prefer: dict[str, list[int]] = {m.id: [0, 0] for m in self.metrics}
        for outcome in outcomes:
            kind = outcome.detail.get("type")
            if kind not in DIRECT_TYPES:
                prefer[DIRECT_COMPLETED][1] += 1
                continue
            prefer[DIRECT_COMPLETED][0] += 1
            counted = outcome.detail.get("counted")
            # Both are ``at_most`` shares: the case they name is the bad side.
            prefer[not_triggered_id(str(kind))][1 if counted == "not_triggered" else 0] += 1
            prefer[mismatch_id(str(kind))][1 if counted == "mismatch" else 0] += 1
            side = outcome.detail.get("preferred")
            if counted == "pair" and side in ("final", "draft"):
                prefer[direct_metric_id(str(kind))][0 if side == "final" else 1] += 1
        return {key: Counts(good, bad) for key, (good, bad) in prefer.items()}

    def summarize(self, outcomes: Sequence[Outcome], model: str) -> Summary:
        """Each preference row's pooled Wilson interval, the fingerprint and the types the
        floor covers."""
        from epistrel.evals.gate import RELATIVE_MIN_PAIRS
        from epistrel.evals.runner import run_fingerprint

        totals = self.aggregate(outcomes)
        at_most = {m.id for m in self.metrics if m.direction is Direction.at_most}
        metrics: dict[str, dict[str, Any]] = {}
        for key, counts in totals.items():
            assert isinstance(counts, Counts)  # noqa: S101 - aggregate gives counts
            # An ``at_most`` row's interval is of its bad side (the first-draft rate, …).
            shown = counts.bad if key in at_most else counts.good
            interval = wilson(shown, counts.good + counts.bad)
            if interval is not None:
                metrics[key] = {"ci95": list(interval)}
        by_seed: dict[object, list[Outcome]] = {}
        for outcome in outcomes:
            by_seed.setdefault(outcome.detail.get("seed"), []).append(outcome)
        per_seed = [self.aggregate(group) for group in by_seed.values()]
        covered = sorted(
            kind
            for kind in DIRECT_TYPES
            if per_seed
            and min(_n(found[direct_metric_id(kind)]) for found in per_seed) >= RELATIVE_MIN_PAIRS
        )
        fingerprint = run_fingerprint(model, CONFIG_DIALS[DIRECT_CONFIG])
        notes = [
            f"E-12 direct fingerprint `{DIRECT_CONFIG}`: `{fingerprint}`.",
            "E-12 direct types the floor covers on this run (at least 300 non-tied pairs per "
            "seed): " + (", ".join(f"`{k}`" for k in covered) or "none") + ".",
        ]
        return Summary(metrics=metrics, notes=notes)

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        assert isinstance(case, DirectCase)  # noqa: S101 - load() validated it
        _script_render(case, stub)
        script_pair(stub, f"{case.id}.pair", _STUB[case.stub.judge if case.stub else "final"])


def _script_render(case: DirectCase, stub: StubAdapter) -> None:
    """The case's draft as its render's answer, and on the stub its regeneration."""
    marker = f"{EVAL_CASE_HEADER}: {case.id}"
    stub.when((marker, user_line(case.say)), case.render(), purpose=GENERATION.name)
    if case.stub is not None and case.stub.regeneration is not None:
        for beat in case.beats:
            stub.when((marker, beat.text), case.stub.regeneration, purpose=REGENERATION.name)


def select_mode(names: Sequence[str], mode: str | None, *, default: str) -> list[str]:
    """The suites to run for ``--e12-mode`` (#936): ``e12`` (``full``), ``e12_direct``
    (``direct``) or both, in ``e12``'s place. ``ValueError`` when the mode is given but E-12 is
    not selected; a run that names ``e12_direct`` alone keeps it."""
    chosen = list(names)
    if E12 not in chosen:
        if mode is not None:
            raise ValueError("--e12-mode needs e12 among the suites (`--suite e12` or --all)")
        return chosen
    picked = mode or default
    if picked not in E12_MODES:
        raise ValueError(f"--e12-mode is one of {', '.join(E12_MODES)}")
    played = {"full": [E12], "direct": [E12_DIRECT], "both": [E12, E12_DIRECT]}[picked]
    found: list[str] = []
    for name in chosen:
        if name == E12:
            found += played
        elif name != E12_DIRECT:
            found.append(name)
    return found


def _n(found: Aggregate) -> int:
    return found.good + found.bad if isinstance(found, Counts) else found.n


__all__ = [
    "COMPLETED",
    "DIRECT_COMPLETED",
    "DIRECT_TYPES",
    "E12",
    "E12_DIRECT",
    "E12_MODES",
    "FLOOR",
    "DirectCase",
    "E12DirectSuite",
    "E12Suite",
    "changed_id",
    "direct_metric_id",
    "direct_outcome",
    "metric_id",
    "mismatch_id",
    "not_triggered_id",
    "select_mode",
]
