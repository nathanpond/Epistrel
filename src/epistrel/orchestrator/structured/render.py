"""A structured render: the beat schema, the generation call, structure validation and the age
repair rung (FR-TURN-1,4,6, FR-CAT-12, FR-MODEL-5,6,7; #150).

**The beat schema** (``render_schema``) is a Pydantic model built for each render, so the ids it
may name are part of it: the rendering character (or the narrator), the story's cast (a
``latent`` scope), the beat kinds the render may return and ``render.max_beats`` (the beats'
``max_items``). It is the ``generation`` call's ``response_schema`` (#53's ``structured_call``:
constrained decoding where the endpoint honours it, else parse-validate-retry), and its
structural rules are validators inside it, so a violation goes back to the model with what was
wrong and is retried within the deployment's ``structured_retry_budget``:

- a character render returns ``say``, ``think``, ``do`` and ``latent`` beats and at least one
  ``say`` or ``do``; the narrator's returns ``narration`` and ``latent`` and at least one
  ``narration``; any other kind is invalid;
- every beat's text is 1 to ``BEAT_TEXT_MAX`` characters; a beat names no actor but the
  rendering character (never the player, never another character; the narrator names none);
- ``intent`` is a ``say``'s alone (``sincere``, ``deceptive``, ``joking``, ``unspecified``;
  ``unspecified`` when it is left out), ``visibility`` a ``latent``'s alone (``objective`` when
  left out; ``characters`` names cast members only); ``hidden``
  (``{noticers, observed: fact | signal}``, cast members only) and ``holder`` (``canon`` only:
  any other is ``viewpoint_cannot_assign_truth``, FR-EPI-8) a ``latent``'s alone (#169);
- the perception qualifiers (#161) are optional and kind-bound: ``whisper_to``, ``aside`` and
  ``language`` a ``say``'s, ``addressee``, ``manner``, ``eavesdrop_by``, ``remote_channel`` and
  ``partial_text`` a ``say``'s or a ``do``'s, ``location`` a narration's; what they name is
  validated at routing (``core.epistemic.perception``), where one that cannot be honored is
  repaired, not refused;
- ``effects`` (#187, FR-TURN-11) are the beat's State effects, ``{kind, state, scope_key?,
  delta? | value?, counterparty?, reason, justification?, justified_by?, consideration?,
  silent?, contingency?, prerequisites?}``. Only ``kind = change``, ``reason`` (1 to 200
  characters) and ``silent`` are delivered: any other kind, or a non-null field another story
  owns (#188's ``counterparty``, ``justification``, ``justified_by``; #189's
  ``consideration``; #216's ``contingency``, ``prerequisites``), is invalid. So is an effect
  naming no State of the story (``world_clock`` included: a time advance is #194's), a
  ``delta`` on a non-number, both a ``delta`` and a ``value``, a value of the wrong type, a
  scope key on a story State or naming no cast member, a character-scoped State with no
  scope key in the narrator's render (a character's defaults to the character), a character
  render naming a ``hidden`` State (only the narrator's may), an effect on a ``think`` beat,
  and any effect while the story's ``states.level`` is not ``on`` (named by module). An
  effect with neither a delta nor a value is structurally valid: it is ``effect_unresolved``,
  repaired (``orchestrator.structured.effects``);
- ``propositions`` are #140's text sketches (subject, relation, object, time, negated, and the
  predicate's qualifiers it names, #151);
- ``introduces`` names each new character a beat introduces (``{name, age?, age_range?,
  clue?}``): an age is 0 to 150, inside the range when one is given;
- ``steps`` (#168, FR-TURN-12; a character's render only, at most ``render.max_beats``) declares
  the render's causal order when its beliefs change within it: ``{index, kind: observe |
  believe | act, cites, proposition?, confidence?}``, indices ``1..n`` in order; an ``observe``
  and a ``believe`` name a proposition, an ``observe`` cites nothing, a step cites only existing
  steps other than itself (a ``believe`` never an ``act``) and records (ids), without cycles; a
  ``say`` or ``do`` names the ``act`` step that owns it (``step``); a ``think``'s record
  citations are some step's. Order (a later cite) is verified, not structural
  (``orchestrator.structured.steps``).

The stub answers a render schema with one ``think``, one ``say`` (intent ``sincere``) and one
``do`` for the rendering character (one ``narration`` for the narrator), their texts from the
request's hash (``stub_example``).

**The call** (``render_beats``): ``structured_call`` through the Model Access Layer and #325's
routing, under the story's generation settings with ``max_tokens`` four times the Lite mapping
of the response length; a ``length`` finish is invalid structure. When the retries are spent the
turn fails: 503 ``model_unavailable`` with ``details.reason = invalid_structure`` and
``details.structural_errors`` (what was wrong, never the answer's text), and nothing is recorded.

**The length** (#464, FR-RESP-3): ``max_tokens`` covers the JSON, not the reply, so the
character render's prompt names the requested length as a target in words (``length_line``)
and the pipeline holds each validated render to it (``hold_length``) before verification:
beats past the length, counted over the beats the output viewpoint receives, are dropped in
turn order, as if the model had answered fewer.

**The age repair** (``repair_ages``): one batched call per render for every character it
introduced without an age (``generation`` with ``age_repair.j2``, temperature 0), validated
against each clue's range (or 0 to 150); an implausible or failed answer is retried within the
same budget, then the turn fails with 503 ``model_unavailable``, ``reason = age_unrepaired``.
Each name asked about takes one answer (``match_ages``, #480): the exact case-folded name, else
the same words (case, punctuation and a leading article aside), else one name's words inside
the other's, and only when the match is unique both ways (two candidates are never guessed
between). An answer for someone not asked about is ignored. The retry's correction names the
characters asked about, the missing and ambiguous ones and the answers not asked about;
``details.structural_errors`` keeps only the counts.

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from epistrel.core.states.model import WORLD_CLOCK
from epistrel.core.tables.characters import TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.turns.beats import (
    BEAT_TEXT_MAX,
    CHARACTER_KINDS,
    NARRATOR_KINDS,
    DeclaredEffect,
)
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.registry import GENERATION, GenerationInputs
from epistrel.orchestrator.models.structured import Uncertain, feedback

log = logging.getLogger(__name__)

STRUCTURED = "structured"
NARRATOR = "narrator"
AGE_REPAIR = "age_repair"
INVALID_STRUCTURE = "invalid_structure"
AGE_UNREPAIRED = "age_unrepaired"
RENDER_TOKEN_FACTOR = 4
"""A beat render's ``max_tokens``: this many times the Lite mapping of the response length."""
CHARS_PER_TOKEN = 4
"""The response length's token budget in characters (#464), as the composition's draft
estimate and E-06 count it."""
WORDS_PER_TOKEN = 0.75
"""The render prompt's length target in words (#464): a token is about three quarters of a
word."""
ACTION_KINDS = frozenset({"say", "do"})
NAME_MAX = 120
CLUE_MAX = 500
SKETCH_TEXT_MAX = 200
MAX_PROPOSITIONS = 20
MAX_QUALIFIERS = 10
MAX_INTRODUCTIONS = 10
DEFAULT_REPAIRED_AGE = 40
"""The stub's age-repair answer (inside a clue's range when it has one)."""

Kind = Literal["say", "think", "do", "latent", "narration"]
IntentName = Literal["sincere", "deceptive", "joking", "unspecified"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AttributionDecl(_Strict):
    """Whom a declared claim attributes its proposition to (#167): "Bobby thinks it was Marcus"
    declares ``knocker = Marcus`` with ``{holder: "Bobby"}``; ``mode = said`` for what someone
    said ("Bobby *said* it was Marcus"); ``factive`` when the speaker also asserts it ("Jenni
    knows it was George"). ``holder`` is a name or id, or ``I`` for the speaker's own belief
    ("I figured it was the delivery guy")."""

    holder: str = Field(min_length=1, max_length=SKETCH_TEXT_MAX)
    mode: Literal["believes", "said"] = "believes"
    factive: bool = False


class PropositionSketch(_Strict):
    """A claim a beat declares (#140's text sketch): normalized by the pipeline."""

    subject: str = Field(min_length=1, max_length=SKETCH_TEXT_MAX)
    relation: str = Field(min_length=1, max_length=SKETCH_TEXT_MAX)
    object: str | None = Field(default=None, max_length=SKETCH_TEXT_MAX)
    time: str | None = Field(default=None, max_length=SKETCH_TEXT_MAX)
    negated: bool = False
    qualifiers: dict[str, str] = Field(default_factory=dict, max_length=MAX_QUALIFIERS)
    """The predicate's qualifiers the claim names (a degree's ``degree_id``, a membership's
    ``role``; #151): normalization keeps only the declared ones."""
    attribution: AttributionDecl | None = None
    """#167: the claim reports someone's belief (or speech act) rather than asserting it."""


def _per_scope(schema: dict[str, Any]) -> None:
    """A scope's JSON Schema as one alternative per shape, as ``_per_kind`` does for beats: a
    ``characters`` scope names one or more ids, ``objective`` and ``public`` name none, so
    constrained decoding cannot write ids under a scope that names none (the validator still
    enforces it)."""
    properties: dict[str, Any] = schema["properties"]
    scope, characters = properties["scope"], properties["characters"]
    named = {k: v for k, v in scope.items() if k != "default"}
    schema["anyOf"] = [
        {
            "type": "object",
            "properties": {"scope": {**scope, "enum": ["objective", "public"]}},
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "scope": {**named, "enum": ["characters"]},
                "characters": {**characters, "minItems": 1},
            },
            "required": ["scope", "characters"],
            "additionalProperties": False,
        },
    ]
    for key in ("properties", "required", "additionalProperties", "type"):
        schema.pop(key, None)


class VisibilityDecl(_Strict):
    """A ``latent`` beat's scope: ``objective``, ``public`` or ``characters`` (with ids)."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_per_scope)

    scope: Literal["objective", "public", "characters"] = "objective"
    characters: list[str] = Field(default_factory=list)


class HiddenDecl(_Strict):
    """A ``latent`` beat's hidden commitment (#169, FR-COMMIT-4): who noticed it (``noticers``,
    character ids; the rendering character when left out) and what they perceived: the hidden
    truth itself (``fact``) or only a signal of it (``signal``: the truth is scoped away from
    everyone, the noticer included; the signal is a ``characters`` latent of its own)."""

    noticers: list[str] = Field(default_factory=list, max_length=10)
    observed: Literal["fact", "signal"] = "fact"


_SPEECH = frozenset({"say"})
_ACTS = frozenset({"say", "do"})
_SAY_ONLY: dict[str, frozenset[str]] = {
    "intent": _SPEECH,
    "visibility": frozenset({"latent"}),
    "hidden": frozenset({"latent"}),
    "holder": frozenset({"latent"}),
    "addressee": _ACTS,
    "manner": _ACTS,
    "whisper_to": _SPEECH,
    "aside": _SPEECH,
    "eavesdrop_by": _ACTS,
    "remote_channel": _ACTS,
    "language": _SPEECH,
    "partial_text": _ACTS,
    "location": frozenset({"narration"}),
    "cites": frozenset({"think"}),
    "step": _ACTS,
}
"""The beat fields only some kinds declare (``RenderedBeats._structure``): ``intent`` a
``say``'s, ``visibility`` a ``latent``'s, the perception qualifiers (#161) the kinds they
apply to: narrowing a ``say``'s, widening a ``say``'s or a ``do``'s, ``location`` a
narration's; ``cites`` a ``think``'s (#166: the cause a new belief rests on); and ``step`` a
``say``'s or a ``do``'s (#168: the ``act`` step that owns it)."""
_KIND_GROUPS = (("say",), ("latent",), ("do",), ("think",), ("narration",))


def _per_kind(schema: dict[str, Any]) -> None:
    """A beat's JSON Schema as one alternative per kind group (#480): ``intent`` only on a
    ``say``, ``visibility`` only on a ``latent``, so constrained decoding cannot write the
    field a kind may not declare (the validator still enforces it). The validation is the
    model's own; only the schema the model is sent changes."""
    properties: dict[str, Any] = schema["properties"]
    variants = []
    for kinds in _KIND_GROUPS:
        fields = {
            name: spec
            for name, spec in properties.items()
            if name not in _SAY_ONLY or set(kinds) <= _SAY_ONLY[name]
        }
        fields["kind"] = {**properties["kind"], "enum": list(kinds)}
        variants.append(
            {
                "type": "object",
                "properties": fields,
                "required": list(schema.get("required", [])),
                "additionalProperties": False,
            }
        )
    for key in ("properties", "required", "additionalProperties", "type"):
        schema.pop(key, None)
    schema["anyOf"] = variants


QUALIFIER_TEXT_MAX = 200
LANGUAGE_MAX = 32
CHANNEL_ID_MAX = 64
MAX_NAMED = 10
MAX_CITES = 10
CITE_MAX = 64
MAX_STEP_INDEX = 64


class RemoteChannelDecl(_Strict):
    """A ``remote_channel`` qualifier (#161): the channel's token and, optionally, the
    participants the render names on it (character ids)."""

    channel_id: str = Field(min_length=1, max_length=CHANNEL_ID_MAX)
    participants: list[str] = Field(default_factory=list, max_length=MAX_NAMED)


EFFECT_REASON_MAX = 200
"""A declared effect's reason: at most this many characters (FR-STATE-4's reason)."""
MAX_EFFECTS = 8
"""The most effects one beat declares."""
STATE_NAME_MAX = 80
EffectKind = Literal["change", "transfer", "time_advance", "reschedule"]
DELIVERED_EFFECT_KINDS = frozenset({"change"})
"""The effect kinds this deployment validates (#187); ``transfer`` is #188's, ``time_advance``
#194's, ``reschedule`` #198's."""
UNDELIVERED_EFFECT_FIELDS = (
    "counterparty",
    "justification",
    "justified_by",
    "consideration",
    "contingency",
    "prerequisites",
)
"""The effect fields later stories validate (#188, #189, #216): non-null is invalid until then."""
STATES_ON = "on"


class CounterpartyDecl(_Strict):
    """A transfer's other side (#188): the State and its scope key."""

    state: str = Field(min_length=1, max_length=STATE_NAME_MAX)
    scope_key: str | None = Field(default=None, max_length=NAME_MAX)


class EffectDecl(_Strict):
    """One State effect a beat declares (#187; see the module docstring)."""

    kind: EffectKind = "change"
    state: str = Field(min_length=1, max_length=STATE_NAME_MAX)
    scope_key: str | None = Field(default=None, max_length=NAME_MAX)
    delta: int | float | None = None
    value: bool | int | float | str | None = None
    counterparty: CounterpartyDecl | None = None
    reason: str = Field(min_length=1, max_length=EFFECT_REASON_MAX)
    justification: str | None = Field(default=None, max_length=EFFECT_REASON_MAX)
    justified_by: int | None = Field(default=None, ge=0, le=64)
    consideration: str | None = Field(default=None, max_length=NAME_MAX)
    silent: bool | None = None
    contingency: Literal["unconditional", "reservation", "conditional"] | None = None
    prerequisites: list[Annotated[str, Field(max_length=CITE_MAX)]] | None = Field(
        default=None, max_length=MAX_CITES
    )


def declared_of(effects: Sequence[EffectDecl]) -> tuple[DeclaredEffect, ...]:
    """A beat's validated effects as Core takes them (``core.turns.beats.DeclaredEffect``)."""
    return tuple(
        DeclaredEffect(
            state=e.state,
            reason=e.reason.strip(),
            scope_key=e.scope_key,
            delta=e.delta,
            value=e.value,
            silent=bool(e.silent),
        )
        for e in effects
    )


@dataclass(frozen=True, slots=True)
class TrackedState:
    """What a render's schema knows of one of the story's States (#187): its type, scope and
    visibility (``core.states.StateDefinition``'s)."""

    type: str
    scope: str
    visibility: str


def _value_fits(state_type: str, value: Any) -> bool:
    if state_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if state_type == "boolean":
        return isinstance(value, bool)
    return isinstance(value, str)


def effect_errors(
    where: str,
    kind: str,
    effects: Sequence[EffectDecl],
    *,
    narrator: bool,
    states: Mapping[str, TrackedState],
    level: str | None,
    cast_ids: frozenset[str],
) -> list[str]:
    """The structural errors of one beat's ``effects`` (see the module docstring): codes and
    indices, never a State's name or a value (``details.structural_errors``)."""
    if not effects:
        return []
    if level != STATES_ON:
        return [
            f"{where}.effects: module states is {level or 'off'}: a render declares no State effect"
        ]
    if kind == "think":
        return [f"{where}.effects: a think changes nothing"]
    errors: list[str] = []
    for m, effect in enumerate(effects):
        at = f"{where}.effects.{m}"
        if effect.kind not in DELIVERED_EFFECT_KINDS:
            errors.append(f"{at}.kind: only a change is delivered")
        for name in UNDELIVERED_EFFECT_FIELDS:
            if getattr(effect, name) is not None:
                errors.append(f"{at}.{name}: not delivered yet")
        if not effect.reason.strip():
            errors.append(f"{at}.reason: empty")
        if effect.state == WORLD_CLOCK:
            errors.append(f"{at}.state: the world clock moves only by a time advance")
            continue
        target = states.get(effect.state)
        if target is None:
            errors.append(f"{at}.state: not one of the story's States")
            continue
        if not narrator and target.visibility == "hidden":
            errors.append(f"{at}.state: a hidden State changes only through the narrator")
        if effect.delta is not None and effect.value is not None:
            errors.append(f"{at}: a delta or a value, not both")
        if effect.delta is not None and (target.type != "number" or isinstance(effect.delta, bool)):
            errors.append(f"{at}.delta: a delta applies to a number State")
        if effect.value is not None and not _value_fits(target.type, effect.value):
            errors.append(f"{at}.value: not a {target.type} value")
        if target.scope == "story" and effect.scope_key is not None:
            errors.append(f"{at}.scope_key: the State belongs to the story")
        if target.scope == "character":
            if effect.scope_key is None and narrator:
                errors.append(f"{at}.scope_key: name the character whose State it is")
            if effect.scope_key is not None and effect.scope_key not in cast_ids:
                errors.append(f"{at}.scope_key: not one of the story's characters")
    return errors


class BeatDecl(_Strict):
    """One beat as a render returns it. The perception qualifiers (#161) are optional and
    explicit: ``addressee`` and ``manner`` (who it is visibly addressed to, how),
    ``whisper_to``/``aside`` (narrowing a ``say``), ``eavesdrop_by``/``remote_channel``
    (widening a ``say`` or a ``do``; ``partial_text`` the words an eavesdropper caught when
    the render limits them), ``language`` (a ``say``'s language code) and ``location`` (where
    a narration happens: a place's id or name). Character ids name the story's cast; one that
    does not, or a widening with no plausible path, is repaired at routing, not refused here."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_per_kind)

    kind: Kind
    text: str = Field(max_length=BEAT_TEXT_MAX)
    actor_character_id: str | None = None
    intent: IntentName | None = None
    propositions: list[PropositionSketch] = Field(default_factory=list, max_length=MAX_PROPOSITIONS)
    effects: list[EffectDecl] = Field(default_factory=list, max_length=MAX_EFFECTS)
    """#187 (FR-TURN-11): the beat's State effects (``EffectDecl``)."""
    visibility: VisibilityDecl | None = None
    hidden: HiddenDecl | None = None
    """#169: a ``latent``'s hidden commitment (``HiddenDecl``)."""
    holder: str | None = Field(default=None, max_length=NAME_MAX)
    """#169: a ``latent``'s canon is ``canon``'s; any other holder is refused (FR-EPI-8:
    ``viewpoint_cannot_assign_truth``)."""
    addressee: str | None = Field(default=None, max_length=NAME_MAX)
    manner: str | None = Field(default=None, max_length=QUALIFIER_TEXT_MAX)
    whisper_to: list[str] = Field(default_factory=list, max_length=MAX_NAMED)
    aside: bool = False
    eavesdrop_by: list[str] = Field(default_factory=list, max_length=MAX_NAMED)
    remote_channel: RemoteChannelDecl | None = None
    language: str | None = Field(default=None, min_length=1, max_length=LANGUAGE_MAX)
    partial_text: str | None = Field(default=None, max_length=BEAT_TEXT_MAX)
    location: str | None = Field(default=None, max_length=NAME_MAX)
    cites: list[Annotated[str, Field(min_length=1, max_length=CITE_MAX)]] = Field(
        default_factory=list, max_length=MAX_CITES
    )
    """A ``think``'s citations (#166, FR-TURN-8): the ids of the records (an event, a fact) a
    new belief it reasons from rests on. Checked, never trusted: a citation is confirmed only
    when it names the antecedent the engine's own search finds."""
    step: int | None = Field(default=None, ge=1, le=MAX_STEP_INDEX)
    """The ``act`` step that owns this ``say`` or ``do`` (#168, FR-TURN-12; ``None``: none)."""

    def qualifiers_declared(self) -> list[str]:
        """The perception qualifiers this beat declares (#161)."""
        return [name for name in _QUALIFIER_FIELDS if getattr(self, name) not in (None, [], False)]


_QUALIFIER_FIELDS = (
    "addressee",
    "manner",
    "whisper_to",
    "aside",
    "eavesdrop_by",
    "remote_channel",
    "language",
    "partial_text",
    "location",
)


StepKind = Literal["observe", "believe", "act"]
ConfidenceName = Literal["unknown", "suspected", "believed", "known"]


class StepDecl(_Strict):
    """One declared causal step of a character's render (#168, FR-TURN-12): ``observe`` (what
    the character perceives: a ``proposition``), ``believe`` (what it concludes: a
    ``proposition`` and ``cites``, the earlier steps (indices) or records (ids) it rests on) or
    ``act`` (what it does: the ``say``/``do`` beats naming its index). ``confidence`` is a
    ``believe``'s (``believed`` when left out)."""

    index: int = Field(ge=1, le=MAX_STEP_INDEX)
    kind: StepKind
    cites: list[int | Annotated[str, Field(min_length=1, max_length=CITE_MAX)]] = Field(
        default_factory=list, max_length=MAX_CITES
    )
    proposition: PropositionSketch | None = None
    confidence: ConfidenceName | None = None

    def step_cites(self) -> list[int]:
        """The earlier steps this step cites (indices)."""
        return [c for c in self.cites if isinstance(c, int)]

    def record_cites(self) -> list[str]:
        """The records this step cites (ids)."""
        return [c for c in self.cites if isinstance(c, str)]


def _steps_structure(steps: Sequence[StepDecl], beats: Sequence[BeatDecl]) -> None:
    """The declared steps' structural rules (#168; see the module docstring): indices ``1..n``
    in order, a ``believe`` and an ``observe`` name a proposition, a step cites existing steps
    other than itself and never an ``act``, the step citations are acyclic, every beat's
    ``step`` names an ``act`` step, and a ``think``'s record citations are some step's."""
    if not steps:
        for n, beat in enumerate(beats):
            if beat.step is not None:
                raise ValueError(f"beats.{n}.step: the render declares no steps")
        return
    indices = [s.index for s in steps]
    if indices != list(range(1, len(steps) + 1)):
        raise ValueError("steps: indices run 1..n in order, each once")
    kinds = {s.index: s.kind for s in steps}
    for n, step in enumerate(steps):
        where = f"steps.{n}"
        if step.kind in ("observe", "believe") and step.proposition is None:
            raise ValueError(f"{where}.proposition: an {step.kind} step names its proposition")
        if step.kind != "believe" and step.confidence is not None:
            raise ValueError(f"{where}.confidence: only a believe step declares a confidence")
        if step.kind == "observe" and step.cites:
            raise ValueError(f"{where}.cites: an observe step cites nothing")
        for cited in step.step_cites():
            if cited == step.index:
                raise ValueError(f"{where}.cites: a step cannot cite itself")
            if cited not in kinds:
                raise ValueError(f"{where}.cites: no step {cited}")
            if step.kind == "believe" and kinds[cited] == "act":
                raise ValueError(f"{where}.cites: a believe step never cites an act")
    graph = {s.index: s.step_cites() for s in steps}
    state: dict[int, int] = {}

    def visit(node: int) -> None:
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                raise ValueError("steps.cites: the step citations form a cycle")
            if state.get(nxt) is None:
                visit(nxt)
        state[node] = 2

    for node in graph:
        if state.get(node) is None:
            visit(node)
    for n, beat in enumerate(beats):
        if beat.step is not None and kinds.get(beat.step) != "act":
            raise ValueError(f"beats.{n}.step: names no act step")
    records = {c for s in steps for c in s.record_cites()}
    for n, beat in enumerate(beats):
        if beat.kind == "think" and beat.cites and not set(beat.cites) <= records:
            raise ValueError(f"beats.{n}.cites: disagrees with the steps' citations")


REACTION_TEXT_MAX = 300
MAX_REACTIONS = 6


class ReactionDecl(_Strict):
    """A reaction a character's render narrates for another present character (#176,
    FR-MULTI-9): the reactor's id, the reaction's text (observable only: a glance, a flinch),
    its kind (``nonverbal``; ``interjection`` for a brief remark, when the story allows it) and
    the beat it reacts to (``reacts_to``: the render's 0-based beat index; omitted: the beat just
    before). Checked against the reactor's viewpoint, never trusted (``orchestrator.structured.
    roles``)."""

    character_id: str = Field(min_length=1, max_length=NAME_MAX)
    text: str = Field(min_length=1, max_length=REACTION_TEXT_MAX)
    kind: Literal["nonverbal", "interjection"] = "nonverbal"
    reacts_to: int | None = Field(default=None, ge=0, le=MAX_STEP_INDEX)


class IntroductionDecl(_Strict):
    """A new character a render introduces: its name (or the descriptive phrase for an unnamed
    figure), its age when the render gave one, the range its clue implies and the clue."""

    name: str = Field(min_length=1, max_length=NAME_MAX)
    age: int | None = Field(default=None, ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)
    age_range: list[int] | None = None
    clue: str | None = Field(default=None, max_length=CLUE_MAX)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.age_range is not None:
            if len(self.age_range) != 2:
                raise ValueError("age_range is [low, high]")
            low, high = self.age_range
            if not TRUE_AGE_MIN <= low <= high <= TRUE_AGE_MAX:
                raise ValueError("age_range is ordered and within 0 to 150")
            if self.age is not None and not low <= self.age <= high:
                raise ValueError("age is inside age_range")
        return self

    def bounds(self) -> tuple[int, int]:
        if self.age_range is not None:
            return self.age_range[0], self.age_range[1]
        return TRUE_AGE_MIN, TRUE_AGE_MAX


class RenderedBeats(_Strict):
    """What one render returns (see the module docstring). Subclassed per render by
    ``render_schema``, which fixes the class variables below."""

    beats: list[BeatDecl]
    introduces: list[IntroductionDecl] = Field(default_factory=list, max_length=MAX_INTRODUCTIONS)
    steps: list[StepDecl] = Field(default_factory=list)
    """The render's declared causal steps (#168, FR-TURN-12; none when its beliefs do not change
    within it): a character's render only."""
    reactions: list[ReactionDecl] = Field(default_factory=list, max_length=MAX_REACTIONS)
    """#176 (FR-MULTI-9): the reactions this render narrates for other present characters (a
    character's render only)."""

    actor_id: ClassVar[str | None] = None
    """The rendering character (``None``: the narrator's render)."""
    player_id: ClassVar[str | None] = None
    cast_ids: ClassVar[frozenset[str]] = frozenset()
    allowed: ClassVar[frozenset[str]] = CHARACTER_KINDS
    limit: ClassVar[int] = 6
    states: ClassVar[Mapping[str, TrackedState]] = {}
    """#187: the story's States by name (what an effect may name)."""
    states_level: ClassVar[str | None] = None
    """#187: the story's ``states.level`` (``None``: off); effects need ``on``."""

    @model_validator(mode="after")
    def _structure(self) -> Self:
        cls = type(self)
        narrator = cls.actor_id is None
        if not self.beats:
            raise ValueError("at least one beat")
        if len(self.beats) > cls.limit:
            raise ValueError(f"at most {cls.limit} beats")
        for n, beat in enumerate(self.beats):
            where = f"beats.{n}"
            if beat.kind not in cls.allowed:
                raise ValueError(f"{where}.kind: not a kind this render may return")
            if not beat.text.strip():
                raise ValueError(f"{where}.text: empty")
            if beat.actor_character_id is not None and (
                narrator
                or beat.actor_character_id != cls.actor_id
                or beat.actor_character_id == cls.player_id
            ):
                raise ValueError(f"{where}.actor_character_id: not the rendering character")
            if beat.intent is not None and beat.kind != "say":
                raise ValueError(f"{where}.intent: only a say declares an intent")
            if beat.cites and beat.kind != "think":
                raise ValueError(f"{where}.cites: only a think cites a cause")
            if beat.step is not None and beat.kind not in _ACTS:
                raise ValueError(f"{where}.step: only a say or a do belongs to an act step")
            if beat.visibility is not None:
                if beat.kind != "latent":
                    raise ValueError(f"{where}.visibility: only a latent declares a scope")
                scope = beat.visibility
                if scope.scope == "characters":
                    if not scope.characters:
                        raise ValueError(f"{where}.visibility: a characters scope names some")
                    if not set(scope.characters) <= cls.cast_ids:
                        raise ValueError(f"{where}.visibility: an unknown character")
                elif scope.characters:
                    raise ValueError(f"{where}.visibility: only a characters scope names some")
            if (beat.hidden is not None or beat.holder is not None) and beat.kind != "latent":
                raise ValueError(f"{where}.hidden: only a latent commits a hidden fact")
            if beat.hidden is not None and not set(beat.hidden.noticers) <= cls.cast_ids:
                raise ValueError(f"{where}.hidden.noticers: an unknown character")
            if beat.holder is not None and beat.holder != "canon":
                raise ValueError(
                    f"{where}.holder: viewpoint_cannot_assign_truth: a latent's truth is canon's "
                    "(a character's view is a belief)"
                )
            found = effect_errors(
                where,
                beat.kind,
                beat.effects,
                narrator=narrator,
                states=cls.states,
                level=cls.states_level,
                cast_ids=cls.cast_ids,
            )
            if found:
                raise ValueError("; ".join(found))
            for name in beat.qualifiers_declared():
                if beat.kind not in _SAY_ONLY[name]:
                    raise ValueError(f"{where}.{name}: not a qualifier a {beat.kind} declares")
        kinds = {b.kind for b in self.beats}
        if narrator and "narration" not in kinds:
            raise ValueError("beats: the narrator's render has a narration beat")
        if not narrator and not kinds & {"say", "do"}:
            raise ValueError("beats: a character's render has a say or a do beat")
        names = [i.name.strip().casefold() for i in self.introduces]
        if len(names) != len(set(names)) or not all(names):
            raise ValueError("introduces: each new character once, by name")
        if narrator and self.steps:
            raise ValueError("steps: only a character's render declares steps")
        if narrator and self.reactions:
            raise ValueError("reactions: only a character's render narrates reactions")
        for n, reaction in enumerate(self.reactions):
            if not reaction.text.strip():
                raise ValueError(f"reactions.{n}.text: empty")
        if len(self.steps) > cls.limit:
            raise ValueError(f"steps: at most {cls.limit} steps")
        _steps_structure(self.steps, self.beats)
        return self

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Message]) -> dict[str, Any]:
        """The stub's default answer (FR-MODEL-11): see the module docstring."""
        del messages
        tag = digest[:12]
        if cls.actor_id is None:
            return {"beats": [{"kind": "narration", "text": f"[stub narration {tag}]"}]}
        return {
            "beats": [
                {"kind": "think", "text": f"[stub think {tag}]"},
                {"kind": "say", "text": f"[stub say {tag}]", "intent": "sincere"},
                {"kind": "do", "text": f"[stub do {tag}]"},
            ]
        }


def render_schema(
    *,
    actor_id: str | None,
    player_id: str | None,
    cast_ids: Sequence[str],
    max_beats: int,
    states: Mapping[str, TrackedState] | None = None,
    states_level: str | None = None,
) -> type[RenderedBeats]:
    """The beat schema of one render: a character's (``actor_id``) or the narrator's
    (``None``), with ``render.max_beats`` as the beats' ``max_items``; #187: the story's States
    and its ``states.level``, which its effects are validated against."""
    name = "NarratorRender" if actor_id is None else "CharacterRender"
    model = create_model(
        name,
        __base__=RenderedBeats,
        beats=(Annotated[list[BeatDecl], Field(min_length=1, max_length=max_beats)], ...),
        steps=(Annotated[list[StepDecl], Field(max_length=max_beats)], Field(default_factory=list)),
    )
    model.actor_id = actor_id
    model.player_id = player_id
    model.cast_ids = frozenset(cast_ids)
    model.allowed = NARRATOR_KINDS if actor_id is None else CHARACTER_KINDS
    model.limit = max_beats
    model.states = dict(states or {})
    model.states_level = states_level
    return model


# -- the age repair ------------------------------------------------------------------------------


class AgeAnswer(_Strict):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    age: int = Field(ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)


class AgeRepair(_Strict):
    """The age repair's answer: one age per introduced character, inside its bounds. Subclassed
    per call by ``age_schema``."""

    ages: list[AgeAnswer]

    expected: ClassVar[Mapping[str, tuple[int, int]]] = {}
    """Each character asked about (case-folded name) and the bounds its age must lie in."""
    asked: ClassVar[Mapping[str, str]] = {}
    """Each case-folded name as the prompt listed it (the retry names it so; #480)."""

    @model_validator(mode="after")
    def _answers(self) -> Self:
        cls = type(self)
        found = match_ages(list(cls.expected), [a.name for a in self.ages])
        if found.missing or found.ambiguous:
            raise feedback(found.correction(cls.asked, self.ages), found.public(len(cls.expected)))
        for key, n in found.matched.items():
            low, high = cls.expected[key]
            if not low <= self.ages[n].age <= high:
                name = cls.asked.get(key, key)
                raise feedback(
                    f"ages.{n}.age: {name!r} is {low} to {high} years old",
                    f"ages.{n}.age: outside the plausible range",
                )
        return self

    def resolved(self) -> dict[str, int]:
        """Each case-folded name asked about and its answered age (a validated answer)."""
        found = match_ages(list(type(self).expected), [a.name for a in self.ages])
        return {key: self.ages[n].age for key, n in found.matched.items()}

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Message]) -> dict[str, Any]:
        del digest, messages
        return {
            "ages": [
                {"name": name, "age": min(max(DEFAULT_REPAIRED_AGE, low), high)}
                for name, (low, high) in cls.expected.items()
            ]
        }


def age_schema(expected: Mapping[str, tuple[int, int]]) -> type[AgeRepair]:
    """The age repair's answer type for ``expected``. Its JSON Schema (the one the model is sent)
    takes exactly one answer per name asked about, each that name as the prompt listed it and an
    age inside its bounds, so constrained decoding cannot answer for nobody or name someone else
    (as ``_per_kind`` does for beats). ``match_ages`` still validates the answer."""
    shown = {k.strip(): v for k, v in expected.items()}

    def closed(schema: dict[str, Any]) -> None:
        answers = [
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [name]},
                    "age": {
                        "type": "integer",
                        "minimum": max(low, TRUE_AGE_MIN),
                        "maximum": min(high, TRUE_AGE_MAX),
                    },
                },
                "required": ["name", "age"],
                "additionalProperties": False,
            }
            for name, (low, high) in shown.items()
        ]
        schema["properties"]["ages"] = {
            "type": "array",
            "items": {"anyOf": answers},
            "minItems": len(answers),
            "maxItems": len(answers),
        }
        schema.pop("$defs", None)

    model = create_model(
        "AgeRepairAnswer",
        __base__=AgeRepair,
        __cls_kwargs__={"json_schema_extra": closed},
    )
    model.expected = {k.casefold(): v for k, v in shown.items()}
    model.asked = {k.casefold(): k for k in shown}
    return model


_ARTICLES = frozenset({"the", "a", "an"})


def name_words(name: str) -> tuple[str, ...]:
    """A name's words for matching (#480): case-folded, punctuation dropped, without a leading
    article (``"The old man."`` → ``("old", "man")``)."""
    words = tuple(re.findall(r"\w+", name.casefold()))
    if len(words) > 1 and words[0] in _ARTICLES:
        return words[1:]
    return words


def _contains(outer: tuple[str, ...], inner: tuple[str, ...]) -> bool:
    size = len(inner)
    return bool(size) and any(outer[i : i + size] == inner for i in range(len(outer) - size + 1))


def _exact(key: str, given: str) -> bool:
    return given.strip().casefold() == key


def _same_words(key: str, given: str) -> bool:
    return bool(name_words(key)) and name_words(key) == name_words(given)


def _containing(key: str, given: str) -> bool:
    asked, answered = name_words(key), name_words(given)
    return _contains(asked, answered) or _contains(answered, asked)


_MATCH_RULES = (_exact, _same_words, _containing)
"""The age repair's name rules, strictest first: the exact case-folded name, the same words
(case, punctuation and a leading article aside), then one name's words inside the other's."""


@dataclass(frozen=True, slots=True)
class AgeMatch:
    """How an age repair's answers matched the names asked about (``match_ages``)."""

    matched: dict[str, int]
    """Each case-folded name asked about and the index of its answer."""
    missing: list[str]
    ambiguous: list[str]
    extra: list[int]
    """The answers no name asked about matched (ignored when nothing is missing)."""

    def correction(self, asked: Mapping[str, str], ages: Sequence[AgeAnswer]) -> str:
        """The retry's correction: the names asked about, then what was wrong, by name."""
        listed = ", ".join(repr(asked.get(k, k)) for k in asked) or "none"
        parts = [f"ages: answer once for each character asked about, copying its name: {listed}"]
        if self.missing:
            parts.append("no answer for " + ", ".join(repr(asked.get(k, k)) for k in self.missing))
        if self.ambiguous:
            parts.append(
                "more than one answer could be "
                + ", ".join(repr(asked.get(k, k)) for k in self.ambiguous)
            )
        if self.extra:
            parts.append("not asked about: " + ", ".join(repr(ages[n].name) for n in self.extra))
        return "; ".join(parts)

    def public(self, asked: int) -> str:
        """The content-free form of ``correction`` (counts, never names)."""
        return (
            f"ages: one answer for each character asked about, by name ({asked} asked about, "
            f"{len(self.missing)} missing, {len(self.ambiguous)} ambiguous, "
            f"{len(self.extra)} not asked about)"
        )


def match_ages(expected: Sequence[str], given: Sequence[str]) -> AgeMatch:
    """Each case-folded name in ``expected`` matched to one of the ``given`` answer names
    (#480). The rules (``_MATCH_RULES``) are tried strictest first; under each, a name takes an
    answer only when that answer is its one candidate and it is the answer's one candidate. Two
    candidates either way is ambiguous and never guessed. An answer no name takes is extra."""
    pending = list(expected)
    free = list(range(len(given)))
    matched: dict[str, int] = {}
    ambiguous: list[str] = []
    contested: set[int] = set()
    for rule in _MATCH_RULES:
        candidates = {key: [n for n in free if rule(key, given[n])] for key in pending}
        for key, found in candidates.items():
            if not found:
                continue  # perhaps under a looser rule
            pending.remove(key)
            if len(found) == 1 and sum(found[0] in c for c in candidates.values()) == 1:
                matched[key] = found[0]
            else:
                ambiguous.append(key)
                contested.update(found)
        free = [n for n in free if n not in matched.values() and n not in contested]
    return AgeMatch(matched, pending, ambiguous, free)


# -- the calls -----------------------------------------------------------------------------------


def render_params(base: ModelParams, max_tokens: int) -> ModelParams:
    """A beat render's parameters: the story's generation settings, ``max_tokens`` four times
    the Lite mapping, no stop sequence (the structure says who speaks)."""
    return ModelParams(
        temperature=base.temperature,
        top_p=base.top_p,
        max_tokens=max_tokens * RENDER_TOKEN_FACTOR,
    )


def length_chars(max_tokens: int) -> int:
    """The requested response length in characters (#464)."""
    return max_tokens * CHARS_PER_TOKEN


def length_line(max_tokens: int) -> str:
    """The render prompt's length target (#464): the beats the reader is shown, together."""
    words = max(10, round(max_tokens * WORDS_PER_TOKEN))
    return (
        f"Keep the beats the reader is shown to about {words} words in all; "
        "a short reply is better than a long one."
    )


def hold_length(
    render: RenderedBeats,
    max_chars: int,
    *,
    hidden: frozenset[str] = frozenset(),
    used: int = 0,
) -> tuple[RenderedBeats, int, int]:
    """The render held to the requested length (#464): its beats in order while the text the
    output viewpoint receives (every beat less the ``hidden`` kinds) fits in ``max_chars``
    after ``used`` (an earlier render's share); the first beat past it and every later one
    are dropped, as if the model had answered fewer. A character render always keeps
    its beats up to its first ``say`` or ``do`` (FR-TURN-1), a narrator's render its first beat.
    An introduction named only in a dropped beat is dropped with it. Answers the held render,
    the characters now used and how many beats were dropped."""
    narrator = type(render).actor_id is None
    kept: list[BeatDecl] = []
    acted = False
    for beat in render.beats:
        size = 0 if beat.kind in hidden else len(beat.text.strip())
        anchored = bool(kept) if narrator else acted
        if used + size > max_chars and anchored:
            break
        kept.append(beat)
        used += size
        acted = acted or beat.kind in ACTION_KINDS
    dropped = len(render.beats) - len(kept)
    if not dropped:
        return render, used, 0
    kept_text = " ".join(b.text for b in kept).casefold()
    gone_text = " ".join(b.text for b in render.beats[len(kept) :]).casefold()
    introduces = [
        d
        for d in render.introduces
        if (key := d.name.strip().casefold()) in kept_text or key not in gone_text
    ]
    log.debug("render: %d beats dropped past the requested length", dropped)
    held = render.model_copy(update={"beats": kept, "introduces": introduces})
    return held, used, dropped


async def render_beats(
    models: ModelAccessLayer,
    inputs: GenerationInputs,
    schema: type[RenderedBeats],
    *,
    variant: str,
    params: ModelParams,
    budget: TurnBudget,
) -> tuple[RenderedBeats, int]:
    """One render: its validated beats and how many structural failures were retried; 503
    ``model_unavailable`` (``invalid_structure``) when the retries are spent."""
    failures: list[str] = []
    found = await models.structured_call(
        GENERATION.name,
        schema,
        inputs,
        params=params,
        budget=budget,
        variant=variant,
        failures=failures,
    )
    if isinstance(found, Uncertain):
        raise _unavailable(INVALID_STRUCTURE, found, failures)
    log.debug("render: %d beats after %d structural retries", len(found.beats), len(failures))
    return found, len(failures)


async def repair_ages(
    models: ModelAccessLayer,
    inputs: GenerationInputs,
    expected: Mapping[str, tuple[int, int]],
    *,
    params: ModelParams,
    budget: TurnBudget,
) -> dict[str, int]:
    """The age repair (see the module docstring): each case-folded name's age."""
    failures: list[str] = []
    schema = age_schema(expected)
    found = await models.structured_call(
        GENERATION.name,
        schema,
        inputs,
        params=params.merged(ModelParams(temperature=0.0)),
        budget=budget,
        variant=AGE_REPAIR,
        failures=failures,
    )
    if isinstance(found, Uncertain):
        raise _unavailable(AGE_UNREPAIRED, found, failures)
    return found.resolved()


def _unavailable(reason: str, found: Uncertain, failures: Sequence[str]) -> ModelUnavailable:
    error = ModelUnavailable(GENERATION.name, reason, attempts=found.attempts)
    error.details["structural_errors"] = list(failures) or [found.reason]
    return error


__all__ = [
    "AGE_REPAIR",
    "AGE_UNREPAIRED",
    "INVALID_STRUCTURE",
    "NARRATOR",
    "RENDER_TOKEN_FACTOR",
    "STRUCTURED",
    "AgeMatch",
    "AgeRepair",
    "AttributionDecl",
    "BeatDecl",
    "EffectDecl",
    "IntroductionDecl",
    "PropositionSketch",
    "RenderedBeats",
    "StepDecl",
    "TrackedState",
    "VisibilityDecl",
    "age_schema",
    "effect_errors",
    "hold_length",
    "length_chars",
    "length_line",
    "match_ages",
    "name_words",
    "render_beats",
    "render_params",
    "render_schema",
    "repair_ages",
]
