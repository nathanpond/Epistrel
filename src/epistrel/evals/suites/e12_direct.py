"""E-12's direct mode (docs/04 §8; NFR-QUAL-14; #936, #937; ADR-195, ADR-196).

NFR-QUAL-14 needs 300 non-tied pairs a seed per intervention type, and an intervention fires on
few live turns, so the full-turn suite (``e12_interventions``) reaches them slowly. A **direct
case** is a prepared first draft with its trigger already in it: the render's beats and their
declared claims, any introductions, over S-COAT's canon (``scenario_play.plant``) plus the
case's own. One turn plays through ``Engine.respond`` on a scratch story, with the render call
pinned to the draft (``CaseContext.pinned``: its ``RenderedBeats`` schema goes to the run's
stub, scripted with the draft). Detection, verification, the repair ladder, the age repair and
the composition are the production pipeline's own, on the run's model. A
``composition_fallback`` case also pins the polish (``ComposedMessage``) to its prepared
``polish``, which the composition's check must refuse.

The observed types come from the turn report (``scenario_play.first_draft``). How a case counts
(``direct_outcome``):

- **judged types** (``repair_rung_1``..``repair_rung_4``, ``composition_polished``): a draft the
  turn left unchanged is ``e12.direct.not_triggered.<type>``; one changed while its declared
  type did not fire is ``e12.direct.mismatch.<type>``; otherwise the masked draft and final
  message are judged in both orders, as in full mode, and a non-tied judgement is an
  observation of ``nfr_qual_14.direct.<type>`` (the relative rule: mean + SD ≤ 55 % on at least
  300 non-tied pairs a seed);
- **identity types** (``age_repair``, ``composition_fallback``; the owner's R3, 2026-11-16):
  these release the draft's prose unchanged by construction. A fired case whose released prose
  differs from the draft under ``normalized`` raises ``IdentityBroken``, a harness error naming
  the case; a fired, unchanged case is a good observation of ``nfr_qual_14.direct.<type>``,
  judged by ``Rule.identity`` (at least 50 fired cases over at least two seeds, none differing).
  No judge call is made and the row is not judge-gated. A case where the type did not fire is
  ``not_triggered`` (``mismatch`` when something else changed it).

The rows run on the ``structured`` configuration only and are never pooled with the full-mode
rows. ``--e12-mode full|direct|both`` selects the suites (``select_mode``). On the stub the
examples (``evals/sets/interventions/examples.jsonl``, CI only) script the regeneration, the
polish and the judge's side.
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
from epistrel.evals.suites.e12_interventions import E12, FLOOR
from epistrel.evals.suites.scenario_play import (
    CAST,
    CONFIG_DIALS,
    INTERVENTION_TYPES,
    ITEMS,
    PLACES,
    PLAYER,
    RENDERER,
    Config,
    first_draft,
    plant,
    user_line,
)
from epistrel.orchestrator.compose.compose import COMPOSITION_TEMPLATE, ComposedMessage
from epistrel.orchestrator.lite.schemas import RespondRequest
from epistrel.orchestrator.models.adapter import EVAL_CASE_HEADER
from epistrel.orchestrator.models.registry import GENERATION, QUALITY_JUDGE, REGENERATION
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.orchestrator.structured.render import RenderedBeats

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

E12_DIRECT = "e12_direct"
DIRECT_CONFIG: Config = "structured"
"""Direct rows run on the ``structured`` configuration only (no ``gated`` variant)."""
DirectType = Literal[
    "repair_rung_1",
    "repair_rung_2",
    "repair_rung_3",
    "repair_rung_4",
    "composition_polished",
    "composition_fallback",
    "age_repair",
]
DIRECT_TYPES: tuple[DirectType, ...] = INTERVENTION_TYPES
IDENTITY_TYPES: frozenset[str] = frozenset({"age_repair", "composition_fallback"})
"""The types that release the draft's prose unchanged by construction (R3, #937)."""
JUDGED_TYPES: tuple[str, ...] = tuple(t for t in DIRECT_TYPES if t not in IDENTITY_TYPES)
DIRECT_SET_PATH = "interventions/examples.jsonl"
DIRECT_MIN_PER_TYPE = 5
E12_MODES = ("full", "direct", "both")
DIRECT_COMPLETED = "e12.direct.completed"
"""The share of direct cases that played (a model error fails its case, never silently)."""
ADULT = 18

type Side = Literal["draft", "final", "tie"]
_SIDE: Mapping[Preference, Side] = {"a": "draft", "b": "final", "tie": "tie"}
_STUB: Mapping[Side, Preference] = {"draft": "a", "final": "b", "tie": "tie"}


class IdentityBroken(RuntimeError):
    """An identity intervention fired and the released prose differs from the draft (#937)."""


def direct_metric_id(kind: str) -> str:
    return f"nfr_qual_14.direct.{kind}"


def not_triggered_id(kind: str) -> str:
    return f"e12.direct.not_triggered.{kind}"


def mismatch_id(kind: str) -> str:
    return f"e12.direct.mismatch.{kind}"


NAMES = frozenset({PLAYER, RENDERER, *ITEMS, *PLACES})


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


class DirectIntroduction(BaseModel):
    """A new character the draft introduces, as the render declares it (``IntroductionDecl``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    age: int | None = Field(default=None, ge=0, le=150)
    age_range: list[int] | None = Field(default=None, min_length=2, max_length=2)
    clue: str | None = Field(default=None, max_length=200)


class DirectStub(BaseModel):
    """A CI example's scripted answers: the regeneration (rungs 3 and 4), the polish
    (``composition_polished``) and the judge's side."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    regeneration: str | None = None
    composition: str | None = None
    judge: Literal["final", "draft", "tie"] = "final"


class DirectCase(CaseModel):
    """A prepared first draft with its trigger in it (module docstring)."""

    id: str = Field(pattern=r"^direct-[A-Za-z0-9_.:-]{1,72}$")
    type: DirectType
    say: str = Field(min_length=1, max_length=500)
    beats: list[DirectBeat] = Field(min_length=1, max_length=8)
    introduces: list[DirectIntroduction] = Field(default_factory=list, max_length=4)
    canon: list[DirectClaim] = Field(default_factory=list)
    polish: str | None = Field(default=None, min_length=1, max_length=4_000)
    """``composition_fallback``: the prepared polish (pinned), which the check must refuse."""
    stub: DirectStub | None = None

    @model_validator(mode="after")
    def _well_formed(self) -> DirectCase:
        for fact in self.canon:
            unknown = {fact.subject, fact.object} - NAMES
            if unknown:
                raise ValueError(f"canon names unknown entities {sorted(unknown)}")
            if fact.relation not in CORE_BY_NAME:
                raise ValueError(f"canon relation {fact.relation!r} is not a core predicate")
        if (self.polish is not None) != (self.type == "composition_fallback"):
            raise ValueError("a composition_fallback case, and only one, has a prepared polish")
        if any(i.name in NAMES for i in self.introduces):
            raise ValueError("introduces names a character or place the story already has")
        if self.type == "age_repair" and not self.introduces:
            raise ValueError("an age_repair case introduces a new character")
        if any(i.age is None and (i.age_range or [0])[0] < ADULT for i in self.introduces):
            raise ValueError("an ageless introduction is an adult (age_range from 18)")
        return self

    def render(self) -> str:
        """The draft as the render call answers it."""
        found: dict[str, Any] = {"beats": [b.model_dump(exclude_defaults=True) for b in self.beats]}
        if self.introduces:
            found["introduces"] = [i.model_dump(exclude_none=True) for i in self.introduces]
        return json.dumps(found)


def direct_outcome(declared: str, types: Sequence[str], changed: bool) -> str:
    """How a played direct case counts (module docstring): ``pair``, ``identity``,
    ``not_triggered``, ``mismatch``, or ``broken`` (an identity type fired and the prose
    changed)."""
    if declared in IDENTITY_TYPES:
        if declared in types:
            return "broken" if changed else "identity"
        return "mismatch" if changed else "not_triggered"
    if not changed:
        return "not_triggered"
    return "pair" if declared in types else "mismatch"


def _direct_metrics() -> tuple[Metric, ...]:
    found: list[Metric] = []
    for kind in DIRECT_TYPES:
        label = kind.replace("_", " ")
        if kind in IDENTITY_TYPES:
            found.append(
                Metric(
                    direct_metric_id(kind),
                    Rule.identity,
                    direction=Direction.at_most,
                    description=f"direct: {label} fired and released the draft's prose unchanged",
                )
            )
        else:
            found.append(
                Metric(
                    direct_metric_id(kind),
                    Rule.relative,
                    FLOOR,
                    Direction.at_most,
                    f"direct: the judge preferred the prepared draft over the final message, "
                    f"over the non-tied pairs where {label} changed it",
                )
            )
        found.append(
            Metric(
                not_triggered_id(kind),
                Rule.informational,
                direction=Direction.at_most,
                description=f"direct {kind} cases where {label} did not fire",
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
    judge_metrics = frozenset(direct_metric_id(kind) for kind in JUDGED_TYPES)
    judge_calibration = "pairwise"
    judge_purposes = (QUALITY_JUDGE.name,)
    reading_notes = (
        "E-12 direct mode (M11) plays one prepared first draft per case through the real turn "
        "pipeline (only the render is pinned to the draft) and pairs the draft with the final "
        "message when the case's declared intervention changed it. Age repair and composition "
        "fallback are identity rows: every fired case released the draft unchanged. The direct "
        "rows are reported beside the full-turn rows, never pooled with them.",
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
        pins: tuple[type[Any], ...] = (RenderedBeats,)
        if case.polish is not None:
            pins += (ComposedMessage,)
        with ctx.pinned(pins):
            result = await ctx.engine.respond(ctx.principal, story_id, request)
        names = {c.id: c.name for c in created.characters}
        draft, _, types, _ = await first_draft(ctx, result, names)
        final = result.message.text
        counted = direct_outcome(case.type, types, normalized(draft) != normalized(final))
        if counted == "broken":
            raise IdentityBroken(
                f"{case.id}: {case.type} fired and the released prose differs from the draft"
            )
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
        """A real-model run's pinned answers, on the stub the pinned calls reach (once a run per
        case); a stub run scripted them with the case (``script_stub``)."""
        stub = ctx.scripted_stub
        if stub is ctx.engine.models.adapter or (id(stub), case.id) in self._pinned:
            return
        self._pinned.add((id(stub), case.id))
        _script_pinned(case, stub)

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        """Per judged type, the seed's non-tied pairs as (final preferred, draft preferred); per
        identity type, its fired cases (all unchanged: a changed one raised); per type, its
        not-triggered and mismatched cases as a share of its played cases."""
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
            if counted == "identity":
                prefer[direct_metric_id(str(kind))][0] += 1
            elif counted == "pair" and side in ("final", "draft"):
                prefer[direct_metric_id(str(kind))][0 if side == "final" else 1] += 1
        return {key: Counts(good, bad) for key, (good, bad) in prefer.items()}

    def summarize(self, outcomes: Sequence[Outcome], model: str) -> Summary:
        """Each judged row's pooled Wilson interval, the fingerprint and the types the floor
        covers on this run."""
        from epistrel.evals.gate import IDENTITY_MIN_CASES, MIN_SEEDS, RELATIVE_MIN_PAIRS
        from epistrel.evals.runner import run_fingerprint

        totals = self.aggregate(outcomes)
        at_most = {m.id for m in self.metrics if m.direction is Direction.at_most}
        identity = {direct_metric_id(k) for k in IDENTITY_TYPES}
        metrics: dict[str, dict[str, Any]] = {}
        for key, counts in totals.items():
            assert isinstance(counts, Counts)  # noqa: S101 - aggregate gives counts
            if key in identity:
                continue  # an identity row has no interval: every fired case was unchanged
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
            for kind in JUDGED_TYPES
            if per_seed
            and min(_n(found[direct_metric_id(kind)]) for found in per_seed) >= RELATIVE_MIN_PAIRS
        )
        covered += sorted(
            kind
            for kind in IDENTITY_TYPES
            if len(per_seed) >= MIN_SEEDS
            and _n(totals[direct_metric_id(kind)]) >= IDENTITY_MIN_CASES
        )
        fingerprint = run_fingerprint(model, CONFIG_DIALS[DIRECT_CONFIG])
        notes = [
            f"E-12 direct fingerprint `{DIRECT_CONFIG}`: `{fingerprint}`.",
            "E-12 direct types with enough evidence on this run (300 non-tied pairs per seed; "
            "50 fired cases for an identity type): "
            + (", ".join(f"`{k}`" for k in covered) or "none")
            + ".",
        ]
        return Summary(metrics=metrics, notes=notes)

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        assert isinstance(case, DirectCase)  # noqa: S101 - load() validated it
        _script_pinned(case, stub)
        marker = f"{EVAL_CASE_HEADER}: {case.id}"
        if case.stub is not None:
            if case.stub.regeneration is not None:
                for beat in case.beats:
                    stub.when((marker, beat.text), case.stub.regeneration, REGENERATION.name)
            if case.stub.composition is not None:
                stub.when((marker, case.beats[0].text), case.stub.composition, COMPOSITION_TEMPLATE)
        if case.type not in IDENTITY_TYPES:
            script_pair(stub, f"{case.id}.pair", _STUB[case.stub.judge if case.stub else "final"])


def _script_pinned(case: DirectCase, stub: StubAdapter) -> None:
    """The case's pinned answers: its draft as the render's, and a fallback case's polish."""
    marker = f"{EVAL_CASE_HEADER}: {case.id}"
    stub.when((marker, user_line(case.say)), case.render(), purpose=GENERATION.name)
    if case.polish is not None:
        answer = json.dumps({"text": case.polish})
        stub.when((marker, case.beats[0].text), answer, purpose=COMPOSITION_TEMPLATE)


def _n(found: Aggregate) -> int:
    return found.good + found.bad if isinstance(found, Counts) else found.n


def select_mode(names: Sequence[str], mode: str | None, *, default: str) -> list[str]:
    """The suites to run for ``--e12-mode``: ``e12`` (``full``), ``e12_direct`` (``direct``) or
    both, in ``e12``'s place (direct first: its rows are the certified ones, #937).
    ``ValueError`` when the mode is given but E-12 is not selected; a run that names
    ``e12_direct`` alone keeps it."""
    chosen = list(names)
    if E12 not in chosen:
        if mode is not None:
            raise ValueError("--e12-mode needs e12 among the suites (`--suite e12` or --all)")
        return chosen
    picked = mode or default
    if picked not in E12_MODES:
        raise ValueError(f"--e12-mode is one of {', '.join(E12_MODES)}")
    played = {"full": [E12], "direct": [E12_DIRECT], "both": [E12_DIRECT, E12]}[picked]
    found: list[str] = []
    for name in chosen:
        if name == E12:
            found += played
        elif name != E12_DIRECT:
            found.append(name)
    return found


__all__ = [
    "DIRECT_COMPLETED",
    "DIRECT_TYPES",
    "E12_DIRECT",
    "E12_MODES",
    "IDENTITY_TYPES",
    "JUDGED_TYPES",
    "DirectCase",
    "E12DirectSuite",
    "IdentityBroken",
    "direct_metric_id",
    "direct_outcome",
    "mismatch_id",
    "not_triggered_id",
    "select_mode",
]
