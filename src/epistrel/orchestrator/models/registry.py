"""The purpose registry (FR-MODEL-2, FR-MODEL-10, FR-MODEL-13; docs/02 §5).

Every model purpose is declared here: its name, the ``model_config`` row it defaults to, its
capability floor, and a typed schema of every input its requests may carry, each field tagged
with a ``ContentCategory``. Prompt templates (``prompts.py``) and the payload builder
(``access.ModelAccessLayer.call``) use only declared inputs, so a prompt can carry only what its
purpose declares.

``DELIVERED`` holds the purposes a model call may use today; ``RESERVED`` names every other
purpose (and the two separate ports) FR-MODEL-2 enumerates, with the milestone that delivers
it. Purposes join ``DELIVERED`` as the capabilities that use them land (FR-MODEL-13, §22).

The registry validates itself at import: a field with no category (its own, or one on an
enclosing field) or a category the purpose ``never_receives`` raises ``RegistryError``.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContentCategory(StrEnum):
    """What kind of data an input field carries (FR-MODEL-13's data-flow table)."""

    RATING_GUIDANCE = "rating_guidance"
    CUSTOM_INSTRUCTIONS = "custom_instructions"
    CHARACTER_DEFINITION = "character_definition"
    PINNED_MEMORY = "pinned_memory"
    ROLLING_SUMMARY = "rolling_summary"
    RECENT_MESSAGE = "recent_message"
    PLAYER_INPUT = "player_input"
    POLICY_TEXT = "policy_text"
    SUBJECT_TRUE_AGE = "subject_true_age"
    CHECKED_TEXT = "checked_text"


class StructuredOutput(StrEnum):
    """The structured-output capability a purpose needs (FR-MODEL-7, NFR-PORT-3)."""

    NONE = "none"
    JSON_SCHEMA = "json_schema"
    PARSE_VALIDATE_RETRY = "parse_validate_retry"
    """Constrained ``json_schema`` decoding, or reliable enough output to parse, validate and
    retry; the weaker of the two is the floor."""


class PurposeStatus(StrEnum):
    DELIVERED = "delivered"
    RESERVED = "reserved"


@dataclass(frozen=True)
class CapabilityFloor:
    """The recommended minimum a model must meet for a purpose (FR-MODEL-10). Declarative in M2;
    the conformance check against real models is #54's."""

    structured_output: StructuredOutput
    min_context_tokens: int = 8_192


class RegistryError(Exception):
    """A purpose declaration breaks the registry's rules (raised at import)."""


class InputSchema(BaseModel):
    """Base of every input schema and nested input model: closed, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# -- input schemas ----------------------------------------------------------------------------

C = ContentCategory


class CharacterDefinition(InputSchema):
    name: Annotated[str, C.CHARACTER_DEFINITION]
    definition: Annotated[str, C.CHARACTER_DEFINITION]


class TranscriptLine(InputSchema):
    """One earlier message as the model sees it: who, what they said, what they did."""

    name: Annotated[str, C.RECENT_MESSAGE]
    say: Annotated[str | None, C.RECENT_MESSAGE] = None
    do: Annotated[str | None, C.RECENT_MESSAGE] = None
    role: Annotated[Literal["user", "assistant"], C.RECENT_MESSAGE] = "assistant"


class PlayerInput(InputSchema):
    name: Annotated[str, C.PLAYER_INPUT]
    say: Annotated[str | None, C.PLAYER_INPUT] = None
    do: Annotated[str | None, C.PLAYER_INPUT] = None


class GenerationInputs(InputSchema):
    """The Lite prompt's sections (FR-LITE-1); M3-only sections join in M3."""

    rating_guidance: Annotated[str, C.RATING_GUIDANCE] = ""
    custom_instructions: Annotated[str, C.CUSTOM_INSTRUCTIONS] = ""
    character_definitions: list[CharacterDefinition] = Field(default_factory=list)
    pinned_memories: Annotated[list[str], C.PINNED_MEMORY] = Field(default_factory=list)
    summary_segments: Annotated[list[str], C.ROLLING_SUMMARY] = Field(default_factory=list)
    recent_messages: list[TranscriptLine] = Field(default_factory=list)
    player_input: PlayerInput


class Subject(InputSchema):
    """A resolved subject of an age-conditioned policy: an opaque label and its true age."""

    label: Annotated[str, C.POLICY_TEXT]
    age: Annotated[int, C.SUBJECT_TRUE_AGE]


class GuardrailCheckInputs(InputSchema):
    """The text being checked, the policy's prompt, and the subjects it is about (FR-IMG-13)."""

    text: Annotated[str, C.CHECKED_TEXT]
    policy_prompt: Annotated[str, C.POLICY_TEXT]
    subjects: list[Subject] = Field(default_factory=list)


class LiteSummaryInputs(InputSchema):
    """Old messages to fold into the rolling summary, and the segments already written."""

    messages: list[TranscriptLine]
    prior_segments: Annotated[list[str], C.ROLLING_SUMMARY] = Field(default_factory=list)
    character_names: Annotated[list[str], C.CHARACTER_DEFINITION] = Field(default_factory=list)


class GuardrailVerdict(BaseModel):
    """The structured result of a model-assisted guardrail check (#53 validates it)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    violation: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


# -- purposes ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Purpose:
    """A delivered model purpose."""

    name: str
    description: str
    default_model_key: str
    capability_floor: CapabilityFloor
    input_schema: type[InputSchema]
    template: str
    hot_path: bool
    never_receives: frozenset[ContentCategory]
    response_schema: type[BaseModel] | None = None
    status: PurposeStatus = field(default=PurposeStatus.DELIVERED, init=False)


@dataclass(frozen=True)
class ReservedPurpose:
    """A purpose (or port) FR-MODEL-2 names that a later milestone delivers."""

    name: str
    description: str
    milestone: str
    kind: Literal["purpose", "port"] = "purpose"
    defaults_to: str | None = None
    status: PurposeStatus = field(default=PurposeStatus.RESERVED, init=False)


DEFAULT_MODEL_KEY = "default"
_EVERYTHING = frozenset(ContentCategory)

GENERATION = Purpose(
    name="generation",
    description="Writes the reply of a Lite turn from the story's prompt sections.",
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.NONE),
    input_schema=GenerationInputs,
    template="generation.j2",
    hot_path=True,
    never_receives=frozenset({C.POLICY_TEXT, C.SUBJECT_TRUE_AGE, C.CHECKED_TEXT}),
)

GUARDRAIL_CHECK = Purpose(
    name="guardrail_check",
    description="Checks one text against one model-assisted policy (FR-GUARD-9).",
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=GuardrailCheckInputs,
    template="guardrail_check.j2",
    hot_path=True,
    never_receives=_EVERYTHING - {C.CHECKED_TEXT, C.POLICY_TEXT, C.SUBJECT_TRUE_AGE},
    response_schema=GuardrailVerdict,
)

LITE_SUMMARY = Purpose(
    name="lite_summary",
    description="Folds old messages into a Lite story's rolling summary (FR-LITE-2).",
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.NONE),
    input_schema=LiteSummaryInputs,
    template="lite_summary.j2",
    hot_path=False,
    never_receives=frozenset({C.POLICY_TEXT, C.SUBJECT_TRUE_AGE, C.CHECKED_TEXT}),
)

RESERVED_PURPOSES: tuple[ReservedPurpose, ...] = (
    ReservedPurpose("regeneration", "Regenerates a span under corrected constraints.", "M4"),
    ReservedPurpose("entailment", "The fuzzy residual of verify.", "M4"),
    ReservedPurpose("consolidation", "Summaries and reflections (worker).", "M4"),
    ReservedPurpose("importance_scoring", "Scores one memory's importance.", "M4"),
    ReservedPurpose("salience_detection", "Detects signals that need commitment.", "M4"),
    ReservedPurpose("extraction_backstop", "Model layer of claim detection.", "M4"),
    ReservedPurpose("quality_judge", "Pairwise prose comparisons (eval only).", "M4"),
    ReservedPurpose(
        "belief_adjudication", "Adjudicated belief transitions.", "M5", defaults_to="entailment"
    ),
    ReservedPurpose(
        "time_estimation",
        "Best-guess duration of a narrated skip.",
        "M6",
        defaults_to="extraction_backstop",
    ),
    ReservedPurpose(
        "rating_suggestion",
        "Per-rating content-policy review.",
        "M3",
        defaults_to="guardrail_check",
    ),
    ReservedPurpose("image_prompt", "Writes image prompts.", "M8", defaults_to="generation"),
    ReservedPurpose("image_check", "Vision checks of image outputs; no default.", "M8"),
    ReservedPurpose("embedding", "The embedding port (FR-EMBED).", "M4", kind="port"),
    ReservedPurpose("image_generation", "The image-generation port (FR-IMG-1).", "M8", kind="port"),
)


# -- validation -------------------------------------------------------------------------------


def _categories_in(metadata: list[object]) -> list[ContentCategory]:
    return [item for item in metadata if isinstance(item, ContentCategory)]


def _models_in(annotation: object) -> Iterator[type[BaseModel]]:
    """Every ``BaseModel`` class reachable through ``annotation``'s type arguments."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        yield from _models_in(typing.get_args(annotation)[0])
        return
    if origin is None and not isinstance(annotation, types.UnionType):
        return
    for argument in typing.get_args(annotation):
        yield from _models_in(argument)


def field_categories(
    schema: type[BaseModel], inherited: ContentCategory | None = None, prefix: str = ""
) -> dict[str, set[ContentCategory]]:
    """Each field path of ``schema`` (nested models included) → its content categories.

    A field's category is its own tag, else the nearest enclosing field's tag. A field whose
    type holds models needs no tag of its own when every field of those models has one. A
    field with no category anywhere raises ``RegistryError``.
    """
    found: dict[str, set[ContentCategory]] = {}
    for name, info in schema.model_fields.items():
        path = f"{prefix}{name}"
        own = _categories_in(list(info.metadata))
        if len(own) > 1:
            raise RegistryError(f"{schema.__name__}.{name}: more than one content category")
        category = own[0] if own else inherited
        nested = list(_models_in(info.annotation))
        if nested:
            for model in nested:
                found.update(field_categories(model, category, f"{path}."))
            if category is not None:
                found[path] = {category}
            continue
        if category is None:
            raise RegistryError(
                f"{schema.__name__}.{name}: input field has no content category "
                "(annotate it with Annotated[..., ContentCategory.<x>])"
            )
        found[path] = {category}
    return found


def declared_paths(schema: type[BaseModel], prefix: str = "") -> frozenset[str]:
    """Every field path ``schema`` declares, nested models included (``recent_messages.say``)."""
    paths: set[str] = set()
    for name, info in schema.model_fields.items():
        path = f"{prefix}{name}"
        paths.add(path)
        for model in _models_in(info.annotation):
            paths |= declared_paths(model, f"{path}.")
    return frozenset(paths)


def schema_categories(schema: type[BaseModel]) -> frozenset[ContentCategory]:
    return frozenset(c for cats in field_categories(schema).values() for c in cats)


def validate_purpose(purpose: Purpose) -> None:
    """Raise ``RegistryError`` unless every input is tagged and none is a forbidden category."""
    forbidden = schema_categories(purpose.input_schema) & purpose.never_receives
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise RegistryError(f"{purpose.name}: declares inputs it never receives: {names}")
    if not purpose.template.endswith(".j2"):
        raise RegistryError(f"{purpose.name}: template must be a .j2 file")


def _build(purposes: tuple[Purpose, ...]) -> Mapping[str, Purpose]:
    built: dict[str, Purpose] = {}
    reserved = {r.name for r in RESERVED_PURPOSES}
    for purpose in purposes:
        validate_purpose(purpose)
        if purpose.name in built or purpose.name in reserved or purpose.name == DEFAULT_MODEL_KEY:
            raise RegistryError(f"{purpose.name}: declared twice")
        built[purpose.name] = purpose
    return types.MappingProxyType(built)


DELIVERED: Mapping[str, Purpose] = _build((GENERATION, GUARDRAIL_CHECK, LITE_SUMMARY))
"""Purposes a model call may use now, by name."""

RESERVED: Mapping[str, ReservedPurpose] = types.MappingProxyType(
    {r.name: r for r in RESERVED_PURPOSES}
)
"""Purposes and ports declared for a later milestone, by name."""


def get_purpose(name: str) -> Purpose:
    """The delivered purpose ``name``; ``KeyError`` for a reserved or unknown name."""
    return DELIVERED[name]


__all__ = [
    "DEFAULT_MODEL_KEY",
    "DELIVERED",
    "RESERVED",
    "CapabilityFloor",
    "CharacterDefinition",
    "ContentCategory",
    "GenerationInputs",
    "GuardrailCheckInputs",
    "GuardrailVerdict",
    "InputSchema",
    "LiteSummaryInputs",
    "PlayerInput",
    "Purpose",
    "PurposeStatus",
    "RegistryError",
    "ReservedPurpose",
    "StructuredOutput",
    "Subject",
    "TranscriptLine",
    "declared_paths",
    "field_categories",
    "get_purpose",
    "schema_categories",
    "validate_purpose",
]
