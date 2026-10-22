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
  left out; ``characters`` names cast members only), ``effects`` is empty in M4;
- ``propositions`` are #140's text sketches (subject, relation, object, time, negated, and the
  predicate's qualifiers it names, #151);
- ``introduces`` names each new character a beat introduces (``{name, age?, age_range?,
  clue?}``): an age is 0 to 150, inside the range when one is given.

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

from epistrel.core.tables.characters import TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.turns.beats import BEAT_TEXT_MAX, CHARACTER_KINDS, NARRATOR_KINDS
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


_SAY_ONLY = {"intent": frozenset({"say"}), "visibility": frozenset({"latent"})}
"""The beat fields one kind alone declares (``RenderedBeats._structure``)."""
_KIND_GROUPS = (("say",), ("latent",), ("think", "do", "narration"))


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


class BeatDecl(_Strict):
    """One beat as a render returns it."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_per_kind)

    kind: Kind
    text: str = Field(max_length=BEAT_TEXT_MAX)
    actor_character_id: str | None = None
    intent: IntentName | None = None
    propositions: list[PropositionSketch] = Field(default_factory=list, max_length=MAX_PROPOSITIONS)
    effects: list[dict[str, Any]] = Field(default_factory=list)
    visibility: VisibilityDecl | None = None


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

    actor_id: ClassVar[str | None] = None
    """The rendering character (``None``: the narrator's render)."""
    player_id: ClassVar[str | None] = None
    cast_ids: ClassVar[frozenset[str]] = frozenset()
    allowed: ClassVar[frozenset[str]] = CHARACTER_KINDS
    limit: ClassVar[int] = 6

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
            if beat.effects:
                raise ValueError(f"{where}.effects: state effects are not delivered yet")
        kinds = {b.kind for b in self.beats}
        if narrator and "narration" not in kinds:
            raise ValueError("beats: the narrator's render has a narration beat")
        if not narrator and not kinds & {"say", "do"}:
            raise ValueError("beats: a character's render has a say or a do beat")
        names = [i.name.strip().casefold() for i in self.introduces]
        if len(names) != len(set(names)) or not all(names):
            raise ValueError("introduces: each new character once, by name")
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
) -> type[RenderedBeats]:
    """The beat schema of one render: a character's (``actor_id``) or the narrator's
    (``None``), with ``render.max_beats`` as the beats' ``max_items``."""
    name = "NarratorRender" if actor_id is None else "CharacterRender"
    model = create_model(
        name,
        __base__=RenderedBeats,
        beats=(Annotated[list[BeatDecl], Field(min_length=1, max_length=max_beats)], ...),
    )
    model.actor_id = actor_id
    model.player_id = player_id
    model.cast_ids = frozenset(cast_ids)
    model.allowed = NARRATOR_KINDS if actor_id is None else CHARACTER_KINDS
    model.limit = max_beats
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
    model = create_model("AgeRepairAnswer", __base__=AgeRepair)
    model.expected = {k.strip().casefold(): v for k, v in expected.items()}
    model.asked = {k.strip().casefold(): k.strip() for k in expected}
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
    "BeatDecl",
    "IntroductionDecl",
    "PropositionSketch",
    "RenderedBeats",
    "VisibilityDecl",
    "age_schema",
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
