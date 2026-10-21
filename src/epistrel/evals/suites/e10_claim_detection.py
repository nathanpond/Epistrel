"""E-10: claim-detection recall (docs/04 §8; FR-VERIFY-13, NFR-QUAL-10; #157).

Detection only, separately from verification: each case is one structurally valid render (or
composed message) with one embedded, undeclared factual claim, run through #148's
``ClaimDetector`` exactly as a structured turn runs it — the environment from the story
(``extraction.load_env``: registry, resolution contexts, the speaker's protected index, open
thunks), layers (a) declared structure and (b) deterministic detectors, then (c) the
``extraction_backstop`` purpose, per ``extraction_mode``:

- ``always``: every beat goes to extraction;
- ``gated`` (the profiles' default): only the beats a detector flagged (a risk category, an
  unresolved argument, an uncovered entity mention in a ``say``/``think``, #478, or a
  ``say``/``do`` that declared nothing).

A composed message is detected as the pipeline detects it (``ClaimDetector.message``, extraction
forced on: ``check_composition``), so its case counts the same under both modes. The scene is
one shared scratch story (``setup``): the tavern of S-EXTRACT-ADVERSARIAL with canon hiding
Marcus's real name, age and an acquaintance from everyone but him, so naming them is protected
content for Linda (and for the narrator).

A labeled claim is **detected** when some detected claim on its beat shares at least half of
the shorter span (``metrics.hits``) and has a compatible category (an extraction claim, whose
category is always ``extracted``, is compatible with every label). Its **layer** is the first of
declared, detector, extraction among the layers that found it (a claim's own and ``merged``).

- ``nfr_qual_10.recall_always`` / ``nfr_qual_10.recall_gated`` (rate, ≥ 95 % each).
- ``e10.<mode>.<category>`` (report-only): recall per claim category.
- ``e10.<mode>.layer.<layer>`` (report-only): the share of labeled claims found first by that
  layer.

The stub's extraction answer is scripted per case (the labeled claim), so CI gates on its stub
run; the gated mode's misses on a beat no detector flags are the pipeline's own.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.canon.facts import Visibility
from epistrel.core.mutation import FactRequest
from epistrel.core.propositions.model import (
    Interval,
    LiteralKind,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import StructuredReadSet
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.verify.detect import DeclaredClaim, DetectBeat, DetectedClaim
from epistrel.evals import scenes
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.metrics import hits
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.registry import EXTRACTION_BACKSTOP, SceneContext
from epistrel.orchestrator.verify import extraction
from epistrel.orchestrator.verify.extraction import ClaimDetector, DetectionConfig

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext, Scratch

MODES: tuple[Literal["always", "gated"], ...] = ("always", "gated")
CATEGORIES = ("possession", "quantity", "time", "identity", "protected")
LAYERS = ("declared", "detector", "extraction")
COMPATIBLE: Mapping[str, frozenset[str]] = {
    "possession": frozenset({"possession", "transfer", "consumption"}),
    "quantity": frozenset({"quantity", "possession", "transfer", "consumption"}),
    "time": frozenset({"time"}),
    "identity": frozenset(),
    "protected": frozenset({"protected_content"}),
}
TURN = 1
CALL_DEADLINE_S = 300.0


def recall_id(mode: str) -> str:
    return f"nfr_qual_10.recall_{mode}"


def category_id(mode: str, category: str) -> str:
    return f"e10.{mode}.{category}"


def layer_id(mode: str, layer: str) -> str:
    return f"e10.{mode}.layer.{layer}"


# -- the scene ----------------------------------------------------------------------------------

CAST = (
    CharacterSpec(name="Cal", definition="A traveller.", true_age=30, is_player=True),
    CharacterSpec(name="Linda", definition="A thief with quick hands.", true_age=30),
    CharacterSpec(name="Marcus", definition="The innkeeper, guarded.", true_age=52),
)
GENDERS = {"Linda": "feminine", "Marcus": "masculine", "Cal": "masculine"}
THINGS = (
    *(
        scenes.EntitySpec(name, kind)
        for name, kind in (
            ("key", "item"),
            ("ring", "item"),
            ("purse", "item"),
            ("map", "item"),
            ("lantern", "item"),
            ("tavern", "place"),
            ("cellar", "place"),
            ("harbour", "place"),
            ("the Guild", "organization"),
        )
    ),
    scenes.EntitySpec("Quillon Vexby", "person", "masculine"),
)


class World:
    """The shared scene: the story, its characters' ids and its entities by name."""

    def __init__(self, story_id: str, characters: dict[str, str], entities: dict[str, str]):
        self.story_id = story_id
        self.characters = characters
        self.entities = entities


async def build_world(engine: Engine, ctx: RequestContext, scratch: Scratch) -> World:
    created = await scratch.create(StorySpec(characters=list(CAST)))
    story = created.story_id
    await scenes.structured(engine, ctx, story)
    entities = await scenes.add_entities(engine, created, THINGS, genders=GENDERS)
    characters = {c.name: c.id for c in created.characters}
    marcus = entities["Marcus"]
    only_marcus = Visibility(scope="characters", characters=(characters["Marcus"],))
    always = Interval.of(None, None)
    facts = (
        (
            Proposition(
                subject_id=marcus,
                predicate_id=CORE_BY_NAME["holds"].id,
                object_id=entities["key"],
                valid_time=Interval.of(0, None),
            ),
            Visibility(),
        ),
        (
            Proposition(
                subject_id=marcus,
                predicate_id=CORE_BY_NAME["name"].id,
                literal=TypedLiteral(kind=LiteralKind.STR, value="Elias Crane"),
                valid_time=always,
            ),
            only_marcus,
        ),
        (
            Proposition(
                subject_id=marcus,
                predicate_id=CORE_BY_NAME["age"].id,
                literal=TypedLiteral(kind=LiteralKind.INT, value=52),
                valid_time=Interval.of(0, None),
            ),
            only_marcus,
        ),
        (
            Proposition(
                subject_id=marcus,
                predicate_id=CORE_BY_NAME["knows"].id,
                object_id=entities["Quillon Vexby"],
                valid_time=Interval.of(0, None),
            ),
            only_marcus,
        ),
    )
    for proposition, visibility in facts:
        await engine.commit_fact(
            ctx, story, FactRequest(proposition=proposition, visibility=visibility)
        )
    return World(story, characters, entities)


# -- the cases --------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Declared(_Model):
    s: str
    p: str
    o: str


class RenderBeat(_Model):
    channel: Literal["say", "do", "think"]
    text: str = Field(min_length=1, max_length=2000)
    declared: list[Declared] = Field(default_factory=list)


class DetectionInputs(_Model):
    kind: Literal["render", "message"]
    speaker: str
    beats: list[RenderBeat] = Field(default_factory=list)
    message: str | None = None


class LabeledClaim(_Model):
    beat: int | None
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    category: Literal["possession", "quantity", "time", "identity", "protected"]


class DetectionCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: DetectionInputs
    expected: LabeledClaim
    stub_answer: dict[str, Any]


FIRST_BEAT = 2


def found_by(label: LabeledClaim, claims: tuple[DetectedClaim, ...]) -> str | None:
    """The first layer (declared, detector, extraction) that found ``label``, or ``None``."""
    layers: set[str] = set()
    for claim in claims:
        beat, start, end = claim.span
        if beat != label.beat or not hits((label.start, label.end), (start, end)):
            continue
        if claim.layer != "extraction" and claim.category not in COMPATIBLE[label.category]:
            if "extraction" not in claim.merged:
                continue
            layers.add("extraction")
            continue
        layers.add(claim.layer)
        layers.update(claim.merged)
    return next((layer for layer in LAYERS if layer in layers), None)


@register
class E10Suite(Suite):
    name = "e10"
    title = "E-10: claim-detection recall per layer, mode and category"
    set_path = "claim_detection/cases.jsonl"
    min_cases = 300
    guarantees = ("claim_extraction",)
    ci_gate = True
    case_model = DetectionCase
    metrics = (
        *(
            Metric(
                recall_id(mode),
                Rule.rate,
                0.95,
                Direction.at_least,
                f"an embedded claim was detected (extraction_mode {mode})",
            )
            for mode in MODES
        ),
        *(
            Metric(
                category_id(mode, category),
                Rule.informational,
                description=f"{category} claims detected ({mode})",
            )
            for mode in MODES
            for category in CATEGORIES
        ),
        *(
            Metric(
                layer_id(mode, layer),
                Rule.informational,
                description=f"claims found first by the {layer} layer ({mode})",
            )
            for mode in MODES
            for layer in LAYERS
        ),
    )
    reading_notes = (
        "E-10 scores detection only: a labeled claim is detected when a detected claim on its "
        "beat covers at least half of the shorter span with a compatible category (any "
        "extraction claim is compatible). A composed message is detected with extraction "
        "forced on, as the pipeline does, so it counts alike under both modes. The "
        "`identity-declared` family puts an identity claim in a beat that declares something "
        "else; the entity it names is covered by no declaration, which flags the beat "
        "(#478), so `gated` extraction reads it.",
    )

    def __init__(self) -> None:
        self.world: World | None = None
        self.scratch: Scratch | None = None

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        from epistrel.evals.runner import Scratch

        self.scratch = Scratch(engine, "e10-world")
        self.world = await build_world(engine, ctx, self.scratch)

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        if self.scratch is not None:
            await self.scratch.delete_all()
        self.world, self.scratch = None, None

    def _beats(self, case: DetectionCase, speaker: str) -> list[DetectBeat]:
        world = self.world
        assert world is not None  # noqa: S101 - setup built it
        beats = []
        for n, beat in enumerate(case.inputs.beats):
            declared = tuple(
                DeclaredClaim(
                    f"d{FIRST_BEAT + n}.{k}",
                    proposition=Proposition(
                        subject_id=world.entities[d.s],
                        predicate_id=CORE_BY_NAME[d.p].id,
                        object_id=world.entities[d.o],
                        valid_time=Interval.of(None, None),
                    ),
                )
                for k, d in enumerate(beat.declared)
            )
            beats.append(DetectBeat(FIRST_BEAT + n, beat.channel, beat.text, speaker, declared))
        return beats

    async def _detect(
        self, ctx: CaseContext, case: DetectionCase, mode: Literal["always", "gated"]
    ) -> tuple[tuple[DetectedClaim, ...], list[str]]:
        world = self.world
        assert world is not None  # noqa: S101 - setup built it
        message = case.inputs.kind == "message"
        speaker = NARRATOR if message else world.entities[case.inputs.speaker]
        config = DetectionConfig(
            structured=True, level="entailment", extraction_mode="always" if message else mode
        )
        async with ctx.engine.db.connect() as conn:
            env = await extraction.load_env(
                conn,
                world.story_id,
                StructuredReadSet(),
                position=(TURN, 0),
                speakers=[speaker],
                config=config,
            )
        tavern = world.entities["tavern"]
        env = replace(
            env,
            scene_entity=tavern,
            placement={world.entities[n]: tavern for n in ("Cal", "Linda", "Marcus")},
        )
        detector = ClaimDetector(
            config,
            env,
            models=ctx.models,
            budget=TurnBudget(deadline_s=CALL_DEADLINE_S),
            scene_context=SceneContext(present_entities=["Cal", "Linda", "Marcus"]),
        )
        operation = f"eval-{case.id}"
        if message:
            outcome = await detector.message(
                case.inputs.message or "", (TURN, 0), operation_id=operation, speaker=speaker
            )
        else:
            outcome = await detector.render(
                self._beats(case, speaker), (TURN, 0), operation_id=operation
            )
        return outcome.claims, list(outcome.degradation)

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, DetectionCase)  # noqa: S101 - load() validated it as case_model
        observations: dict[str, bool | None] = {}
        detail: dict[str, Any] = {"category": case.expected.category}
        cached: tuple[tuple[DetectedClaim, ...], list[str]] | None = None
        for mode in MODES:
            if case.inputs.kind == "message" and cached is not None:
                claims, degradation = cached
            else:
                claims, degradation = await self._detect(ctx, case, mode)
                cached = (claims, degradation)
            layer = found_by(case.expected, claims)
            observations[recall_id(mode)] = layer is not None
            observations[category_id(mode, case.expected.category)] = layer is not None
            for name in LAYERS:
                observations[layer_id(mode, name)] = layer == name
            detail[mode] = {
                "layer": layer,
                "claims": [[c.layer, c.category, list(c.span), list(c.merged)] for c in claims],
                "degradation": degradation,
            }
        return Outcome(observations, output=json.dumps(detail[MODES[0]]["claims"]), detail=detail)

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, DetectionCase)  # noqa: S101 - load() validated it as case_model
        return {EXTRACTION_BACKSTOP.name: json.dumps(case.stub_answer)}


__all__ = ["CATEGORIES", "LAYERS", "MODES", "DetectionCase", "E10Suite", "found_by", "recall_id"]
