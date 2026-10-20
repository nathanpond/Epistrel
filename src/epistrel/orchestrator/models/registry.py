"""The purpose registry (FR-MODEL-2, FR-MODEL-10, FR-MODEL-13; docs/02 §5).

Every model purpose is declared here: its name, the ``model_config`` row it defaults to, its
capability floor, and a typed schema of every input its requests may carry, each field tagged
with a ``ContentCategory``. Prompt templates (``prompts.py``) and the payload builder
(``access.ModelAccessLayer.call``) use only declared inputs, so a prompt can carry only what its
purpose declares.

``DELIVERED`` holds the purposes a model call may use today; ``PORTS`` the separate ports
delivered so far (``embedding``, #138: not a chat completion, so it has no template and no
response schema, but its own capability floor, input schema and ``model_config`` row);
``RESERVED`` names every other purpose (and port) FR-MODEL-2 enumerates, with the milestone
that delivers it. Purposes join ``DELIVERED`` as the capabilities that use them land
(FR-MODEL-13, §22).

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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


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
    CATALOG_TEXT = "catalog_text"
    RATING_DESCRIPTION = "rating_description"
    SCENARIO_TEXT = "scenario_text"
    DIRECTOR_NOTES = "director_notes"
    EVAL_TEXT = "eval_text"
    """Text an eval case supplies or an eval's scratch story produced (#134): what the
    ``quality_judge`` compares. Never a live story's content."""
    MEMORY_TEXT = "memory_text"
    """A memory record's text and kind (#135): what ``importance_scoring`` rates, the
    ``embedding`` port embeds (#138; a retrieval query is embedded as one) and ``consolidation``
    compresses (#143: with the canon facts positioned in its window, rendered as one line)."""
    RECORD_META = "record_meta"
    """What ``consolidation`` is told about each record it compresses (#143): its index, kind,
    holder, visibility, position and speaker. Labels and positions only, never other text."""
    SCOPE_META = "scope_meta"
    """The viewpoint scope, level and turns one ``consolidation`` call covers (#143)."""
    CLAIM_TEXT = "claim_text"
    """A normalized claim rendered as ``<subject> <predicate> <object> [T+minutes]`` (#142): what
    the ``entailment`` purpose judges. Names and ids only, never the beat's prose."""
    CANON_RECORD = "canon_record"
    """One of the claim holder's records (#142): a canon fact rendered like a claim, its holder
    and its valid-time. Only the records the deterministic check fetched for the claim and their
    neighborhood, never unrelated story content."""
    RENDER_TEXT = "render_text"
    """The beats of one render (#146): each beat's index, kind, speaker's name and text.
    ``salience_detection`` reads the observable beats only (never a ``think``/``latent`` beat or
    the player's input); ``extraction_backstop`` (#148) reads the beats being detected, its own
    render's ``think``/``latent`` beats included, or the composed message. Never another
    render's text."""
    SCENE = "scene"
    """What a render's scene holds (#146): the names of the entities present and the text of the
    last few observable beats before it (``scene_context``). A structured render (#150) gets its
    ``## Scene`` section (the place and who is present) and, for the age repair, the introduced
    characters' names and the clues the render gave about them."""
    WORKING_MEMORY = "working_memory"
    """A structured render's working memory (FR-MEM-5; #150): the latest beats as the rendering
    character perceived them (never another character's ``think`` or any ``latent``)."""
    RETRIEVED_MEMORIES = "retrieved_memories"
    """What #141's retrieval assembled for a structured render (#150): the ledger's open
    threads, pins, occasions, profiles and ranked memories the rendering character may know."""
    CANON_FACTS = "canon_facts"
    """The canon facts retrieval found for a structured render (#150), as the rendering
    character may read them: never its own true age or a fact hidden from it."""
    CONSTRAINT = "constraint"
    """A repair's constraint on the span it regenerates (#151): its kind (``do_not_assert``,
    ``avoid_binding``, ``policy``), its text ("do not assert that Bobby enrolled at Florida")
    and the ids of the records it cites."""
    VERIFIED_BEAT = "verified_beat"
    """What a structured turn's composition (#152, FR-RESP-8) is given: the turn's verified
    beats as the output viewpoint receives them (each beat's index, kind, actor's name and
    text; never a ``think`` or ``latent`` beat for a character viewpoint) and their
    deterministic assembly, which the polish may only order and join."""


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
    """A character's author notes: its name, its definition and, when it has one, how it
    refuses or changes the subject (``deflection_style``, FR-GUARD-7; #115)."""

    name: Annotated[str, C.CHARACTER_DEFINITION]
    definition: Annotated[str, C.CHARACTER_DEFINITION]
    deflection_style: Annotated[str | None, C.CHARACTER_DEFINITION] = None


class TranscriptLine(InputSchema):
    """One earlier message as the model sees it: who, what they said, what they did; or, for a
    reply, its ``text`` verbatim. Always the story's own recorded transcript (FR-RESP-4)."""

    name: Annotated[str, C.RECENT_MESSAGE]
    say: Annotated[str | None, C.RECENT_MESSAGE] = None
    do: Annotated[str | None, C.RECENT_MESSAGE] = None
    text: Annotated[str | None, C.RECENT_MESSAGE] = None
    role: Annotated[Literal["user", "assistant"], C.RECENT_MESSAGE] = "assistant"


class PlayerInput(InputSchema):
    name: Annotated[str, C.PLAYER_INPUT]
    say: Annotated[str | None, C.PLAYER_INPUT] = None
    do: Annotated[str | None, C.PLAYER_INPUT] = None


class AgeClue(InputSchema):
    """A character a structured render introduced without an age (#150): its name and what the
    render said about it."""

    name: Annotated[str, C.SCENE]
    clue: Annotated[str, C.SCENE] = ""


class ComposeBeat(InputSchema):
    """One verified beat a composition (#152) is given: its position, kind, actor's name
    (empty for narration) and text."""

    index: Annotated[int, C.VERIFIED_BEAT]
    kind: Annotated[Literal["say", "think", "do", "latent", "narration"], C.VERIFIED_BEAT]
    name: Annotated[str, C.VERIFIED_BEAT] = ""
    text: Annotated[str, C.VERIFIED_BEAT] = Field(max_length=8_000)


class GenerationInputs(InputSchema):
    """The Lite prompt's sections (FR-LITE-1), in prompt order; M3-only sections join in M3.

    ``recent_messages`` is the story's own verbatim window, sent as alternating ``user`` /
    ``assistant`` chat messages; ``voiced_characters`` names the render characters the reply
    voices, in request order (FR-LITE-5); ``policy_constraints`` is empty except when the turn
    regenerates after an output-policy violation (#68)."""

    rating_guidance: Annotated[str, C.RATING_GUIDANCE] = ""
    custom_instructions: Annotated[str, C.CUSTOM_INSTRUCTIONS] = ""
    scenario_text: Annotated[str, C.SCENARIO_TEXT] = ""
    """A story created from a scenario (#122): its ``## Scenario`` block, rendered from the
    story's snapshot (the world, the seeds as text, the relationships, the starting places, the
    player's persona), closed by the instruction that scenario facts prevail."""
    character_definitions: list[CharacterDefinition] = Field(default_factory=list)
    director_notes: Annotated[str, C.DIRECTOR_NOTES] = ""
    """A story created from a scenario (#122): its ``## Author notes (not facts)`` block (the
    plot hooks, objectives, guidance, planned reveals, milestones and endings)."""
    pinned_memories: Annotated[list[str], C.PINNED_MEMORY] = Field(default_factory=list)
    summary_segments: Annotated[list[str], C.ROLLING_SUMMARY] = Field(default_factory=list)
    recent_messages: list[TranscriptLine] = Field(default_factory=list)
    voiced_characters: Annotated[list[str], C.CHARACTER_DEFINITION] = Field(default_factory=list)
    policy_constraints: Annotated[list[str], C.POLICY_TEXT] = Field(default_factory=list)
    """On a guardrail regeneration (FR-GUARD-6; #68): one line per output policy an earlier
    draft of this turn broke, naming the policy's message — never a subject or an age."""
    resolution_steering: Annotated[str, C.CONSTRAINT] = Field(default="", max_length=1_000)
    """A structured render's deferral bias (FR-COMMIT-11; #154, ``steering_resolution.j2``): a
    fixed line, present when an open thunk an earlier turn introduced is in the context."""
    player_input: PlayerInput
    # -- the structured pipeline's sections (#150; empty, and so absent, in a Lite prompt) ------
    scene: Annotated[str, C.SCENE] = ""
    """A structured render's ``## Scene``: the place and who is present."""
    working_memory: Annotated[list[str], C.WORKING_MEMORY] = Field(default_factory=list)
    """A structured render's ``## What you remember``: the latest beats as perceived."""
    retrieved_memories: Annotated[list[str], C.RETRIEVED_MEMORIES] = Field(default_factory=list)
    """A structured render's ``## Retrieved``: #141's assembled context."""
    canon_facts: Annotated[list[str], C.CANON_FACTS] = Field(default_factory=list)
    """A structured render's ``## Canon``: the facts retrieval found."""
    open_references: Annotated[list[str], C.SCENE] = Field(default_factory=list)
    """A follow-up narrator render (#146's FR-COMMIT-7, #150): the salient references this turn
    left open, which it must settle."""
    introductions: list[AgeClue] = Field(default_factory=list)
    """The age repair (#150): each character a render introduced without an age, with the
    render's clue."""
    composition_beats: list[ComposeBeat] = Field(default_factory=list, max_length=100)
    """The composition (#152): the verified beats the output viewpoint receives, in turn
    order."""
    composition_draft: Annotated[str, C.VERIFIED_BEAT] = Field(default="", max_length=40_000)
    """The composition (#152): those beats' deterministic assembly, the draft it polishes."""
    composition_omniscient: Annotated[bool, C.VERIFIED_BEAT] = False
    """The composition (#152): the message is for an ``omniscient`` read, so labelled thoughts
    may appear."""


ConstraintKind = Literal["do_not_assert", "avoid_binding", "policy"]
"""What a repair constraint forbids (#151; shared with #154's reconciliation and #68's policy
regeneration): asserting a claim, binding an open reference a certain way, or breaking an
output policy."""


class RepairConstraint(InputSchema):
    """One constraint a regeneration must keep (#151): its kind, its text and the records it
    cites (fact and closed-set ids; never rendered)."""

    kind: Annotated[ConstraintKind, C.CONSTRAINT] = "do_not_assert"
    text: Annotated[str, C.CONSTRAINT] = Field(max_length=1_000)
    cites: Annotated[list[str], C.CONSTRAINT] = Field(default_factory=list, max_length=50)


REGENERATION_MAX_RECORDS = 50
"""The most contradicted facts, and the most neighborhood facts, one regeneration carries."""


class RegenerationInputs(GenerationInputs):
    """A repair's regeneration of one beat (FR-REPAIR-1; #151; docs/02 §5's Regeneration row):
    the generation inputs of the render the beat came from, plus the rejected beat's text and
    kind, the constraints it must keep, the canon facts it contradicted and their neighborhood
    (each rendered like a claim). Edit in place: the beat is rewritten, not the turn."""

    rejected_text: Annotated[str, C.RENDER_TEXT] = Field(default="", max_length=8_000)
    beat_kind: Annotated[Literal["say", "think", "do", "latent", "narration"], C.RENDER_TEXT] = (
        "say"
    )
    constraints: list[RepairConstraint] = Field(default_factory=list, max_length=50)
    contradicted_facts: Annotated[list[str], C.CANON_RECORD] = Field(
        default_factory=list, max_length=REGENERATION_MAX_RECORDS
    )
    neighborhood: Annotated[list[str], C.CANON_RECORD] = Field(
        default_factory=list, max_length=REGENERATION_MAX_RECORDS
    )


SUBJECT_LABEL_PATTERN = r"^character_[1-9][0-9]{0,3}$"
"""Positional labels only (``character_1`` …): a subject's name can never reach the model."""


class Subject(InputSchema):
    """A resolved subject of an age-conditioned policy: an opaque positional label and its true
    age (FR-GUARD-11). The label pattern refuses anything else, a character's name included."""

    label: Annotated[
        str,
        StringConstraints(pattern=SUBJECT_LABEL_PATTERN),
        Field(examples=["character_1"]),
        C.POLICY_TEXT,
    ]
    age: Annotated[int, Field(ge=0, le=150), C.SUBJECT_TRUE_AGE]


class GuardrailCheckInputs(InputSchema):
    """The text being checked, the policy's prompt, and the subjects it is about (FR-IMG-13)."""

    text: Annotated[str, C.CHECKED_TEXT]
    policy_prompt: Annotated[str, C.POLICY_TEXT]
    subjects: list[Subject] = Field(default_factory=list)


class LiteSummaryInputs(InputSchema):
    """One rolling-summary call (FR-LITE-2; #72). ``mode`` ``compress``: fold ``messages`` (the
    chunk's transcript rows) into a segment; ``merge``: fold ``merge_segments`` (adjacent
    segments, oldest first) into one coarser segment. ``prior_segments`` is the segment just
    before, as context only; ``covers`` names the turns (``Turns a-b``); ``target_tokens`` is
    the length asked for."""

    messages: list[TranscriptLine] = Field(default_factory=list)
    prior_segments: Annotated[list[str], C.ROLLING_SUMMARY] = Field(default_factory=list)
    character_names: Annotated[list[str], C.CHARACTER_DEFINITION] = Field(default_factory=list)
    mode: Annotated[Literal["compress", "merge"], C.ROLLING_SUMMARY] = "compress"
    merge_segments: Annotated[list[str], C.ROLLING_SUMMARY] = Field(default_factory=list)
    covers: Annotated[str, C.ROLLING_SUMMARY] = ""
    target_tokens: Annotated[int, C.ROLLING_SUMMARY] = 400


class GuardrailVerdict(BaseModel):
    """The structured result of a model-assisted guardrail check, validated strictly by
    ``structured.structured_call``. No free-text field: a verdict carries nothing a model could
    smuggle story content through."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    violation: bool
    confidence: float = Field(ge=0.0, le=1.0)


class SuggestionElement(InputSchema):
    """One part of a catalog entry a rating suggestion reviews (FR-RATE-8; #121): its kind, its
    location as canonical JSON (what a concern must name to be kept), and its text."""

    kind: Annotated[str, C.CATALOG_TEXT]
    location: Annotated[str, C.CATALOG_TEXT]
    text: Annotated[str, C.CATALOG_TEXT]


class SuggestionPolicy(InputSchema):
    """A model-assisted policy of the rating under review: its id (what a concern names), its
    name and its prompt (the live threshold already in place)."""

    policy_id: Annotated[str, C.POLICY_TEXT]
    name: Annotated[str, C.POLICY_TEXT]
    prompt: Annotated[str, C.POLICY_TEXT]


class KnownHit(InputSchema):
    """A deterministic policy hit already found, given as context so the model does not repeat
    it: the policy's id and the element's kind and location."""

    policy_id: Annotated[str, C.POLICY_TEXT]
    kind: Annotated[str, C.CATALOG_TEXT]
    location: Annotated[str, C.CATALOG_TEXT]


class RatingSuggestionInputs(InputSchema):
    """One rating's review of one catalog entry (FR-RATE-8; #121): the rating's name,
    description and prompt guidance, its model-assisted policies, the deterministic hits found
    already, the entry's characters as opaque labels with true ages (for an age-conditioned
    policy, #55), and the entry's elements."""

    rating_name: Annotated[str, C.RATING_DESCRIPTION]
    rating_description: Annotated[str, C.RATING_DESCRIPTION] = ""
    rating_guidance: Annotated[str, C.RATING_GUIDANCE] = ""
    policies: list[SuggestionPolicy] = Field(default_factory=list)
    deterministic_hits: list[KnownHit] = Field(default_factory=list)
    subjects: list[Subject] = Field(default_factory=list)
    elements: list[SuggestionElement] = Field(default_factory=list)


ElementKind = Literal["field", "seed", "role", "reveal", "cast_character", "media_caption"]
"""What part of a catalog entry a rating-suggestion concern names (FR-RATE-8)."""


class SuggestedLocation(BaseModel):
    """Where a concern points, as the element list gave it (#111's location: ``field``,
    ``role``, ``seed_id``; a cast character's ``entry`` and ``version``; a medium's
    ``media_id``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: str | None = None
    version: int | Literal["draft"] | None = None
    role: str | None = None
    field: str | None = None
    seed_id: str | None = None
    media_id: str | None = None


class SuggestedElement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ElementKind
    location: SuggestedLocation


class SuggestedConcern(BaseModel):
    """One conflict a model reports: the policy (``policy_id``) or the rating-description clause
    (``clause``, quoted) it breaks, the element that breaks it, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str | None = None
    clause: str | None = Field(default=None, max_length=2_000)
    element: SuggestedElement
    explanation: str = Field(min_length=1, max_length=2_000)


class RatingSuggestionVerdict(BaseModel):
    """The structured result of one ``rating_suggestion`` call: the concerns only (no verdict,
    no confidence: a rating is appropriate exactly when no concern is kept)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    concerns: list[SuggestedConcern] = Field(default_factory=list, max_length=200)


JudgeCriteria = Literal["voice_readability", "voice_fits_definition", "story_driver_adherence"]
"""What a ``quality_judge`` call judges (#158): ``voice_readability`` (E-12's pairs: voice
consistency and readability, factual differences excluded), ``voice_fits_definition`` (E-13: the
speech fits the character's definition) and ``story_driver_adherence`` (E-13: the messages offer
the story's hooks and move toward its planned reveals)."""
JUDGE_TEXT_MAX = 12_000
JUDGE_REASONS_MAX = 1_000


class JudgePair(InputSchema):
    """The two passages a pairwise judgement compares, in the order shown (#158)."""

    a: Annotated[str, C.EVAL_TEXT] = Field(min_length=1, max_length=JUDGE_TEXT_MAX)
    b: Annotated[str, C.EVAL_TEXT] = Field(min_length=1, max_length=JUDGE_TEXT_MAX)


class QualityJudgeInputs(InputSchema):
    """One ``quality_judge`` call (NFR-QUAL-14,15; #158), of one of two shapes: a **pairwise**
    comparison (``pair``, the default template, answered by ``QualityJudgeVerdict``) or a
    **score** of one ``message`` against a ``reference`` (the ``score`` template variant,
    answered by ``QualityJudgeScore``). Eval text only: masked passages from an eval's scratch
    stories or its labeled calibration set, never a live story's content."""

    pair: JudgePair | None = None
    message: Annotated[str | None, C.EVAL_TEXT] = Field(default=None, max_length=JUDGE_TEXT_MAX)
    reference: Annotated[str | None, C.EVAL_TEXT] = Field(default=None, max_length=JUDGE_TEXT_MAX)
    criteria: Annotated[JudgeCriteria, C.EVAL_TEXT] = "voice_readability"


class QualityJudgeVerdict(BaseModel):
    """Which passage a pairwise ``quality_judge`` call prefers (``a`` as shown first, ``b``
    second), or a tie, with its short reasons (#158)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preferred: Literal["a", "b", "tie"] = "tie"
    reasons: str = Field(default="", max_length=JUDGE_REASONS_MAX)


class QualityJudgeScore(BaseModel):
    """A scored ``quality_judge`` call (``score`` variant, #158): 1 (poor) to 5 (excellent)
    on its criteria, with its short reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: int = Field(default=3, ge=1, le=5)
    reasons: str = Field(default="", max_length=JUDGE_REASONS_MAX)


class ImportanceScoringInputs(InputSchema):
    """One memory to score (FR-MEM-7; #135): its text and its kind."""

    text: Annotated[str, C.MEMORY_TEXT]
    kind: Annotated[Literal["episodic", "semantic", "reflection"], C.MEMORY_TEXT] = "episodic"


class ImportanceScore(BaseModel):
    """The structured result of one ``importance_scoring`` call: a number, no free text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    importance: float = Field(ge=0.0, le=1.0)


class EntailmentRecord(InputSchema):
    """One holder record the ``entailment`` model judges a claim against (#142)."""

    text: Annotated[str, C.CANON_RECORD] = Field(max_length=1_000)
    holder: Annotated[str, C.CANON_RECORD] = Field(default="canon", max_length=200)
    valid_time: Annotated[str, C.CANON_RECORD] = Field(default="always", max_length=200)


ENTAILMENT_MAX_RECORDS = 200
"""The most records one ``entailment`` request may carry (the deployment's
``entailment_max_records`` is at most this)."""


class EntailmentInputs(InputSchema):
    """The fuzzy residual of one ``verify`` (FR-VERIFY-3; #142): the normalized claim and the
    holder's records it rests on, nothing else."""

    claim: Annotated[str, C.CLAIM_TEXT] = Field(max_length=1_000)
    records: Annotated[list[EntailmentRecord], C.CANON_RECORD] = Field(
        default_factory=list, max_length=ENTAILMENT_MAX_RECORDS
    )


class EntailmentVerdict(BaseModel):
    """The structured result of one ``entailment`` call: a verdict from the enum and a
    confidence, no free text. The defaults (``novel`` at confidence 0) are the most conservative
    answer: validated, they take the safe disposition (FR-VERIFY-8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["supported", "contradicted", "novel"] = "novel"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


CONSOLIDATION_MAX_RECORDS = 200
"""The most records one ``consolidation`` request carries (a window is cut to it)."""
CONSOLIDATION_STATEMENTS_MAX = 50
"""The most facts, and the most reflections, one ``consolidation`` answer may hold (the engine
keeps the first ``consolidation`` targets: 10 and 5)."""

RecordKind = Literal["episodic", "semantic", "reflection", "fact"]
Modality = Literal["objective", "attributed_claim", "observation"]
"""A consolidation statement's epistemic type (FR-MEM-9): ``objective`` (a world fact),
``attributed_claim`` (someone said it: "Bobby said P") or ``observation`` (something seen
done or narrated)."""


class ConsolidationRecord(InputSchema):
    """One record a consolidation compresses (#143): its index (what a statement's ``supports``
    name), its text, and the labels that keep its epistemic scope and type."""

    index: Annotated[int, Field(ge=0), C.RECORD_META]
    text: Annotated[str, Field(max_length=2_000), C.MEMORY_TEXT]
    kind: Annotated[RecordKind, C.RECORD_META] = "episodic"
    holder: Annotated[str, Field(max_length=200), C.RECORD_META] = "shared"
    visibility: Annotated[str, Field(max_length=200), C.RECORD_META] = "public"
    position: Annotated[str, Field(max_length=64), C.RECORD_META] = ""
    speaker: Annotated[str | None, Field(max_length=200), C.RECORD_META] = None
    """The character a ``say`` record quotes (``None`` for narration, a deed or a fact)."""


class ConsolidationInputs(InputSchema):
    """One window of one viewpoint scope (FR-MEM-3,6,9; #143): its records, oldest first, the
    scope's label, the level (1: episodic memories and canon; 2: level-1 outputs) and the turns
    it covers."""

    records: Annotated[list[ConsolidationRecord], Field(max_length=CONSOLIDATION_MAX_RECORDS)] = (
        Field(default_factory=list)
    )
    scope: Annotated[str, Field(max_length=500), C.SCOPE_META] = "shared"
    level: Annotated[int, Field(ge=1, le=2), C.SCOPE_META] = 1
    covers: Annotated[str, Field(max_length=64), C.SCOPE_META] = ""


class ConsolidationStatement(BaseModel):
    """One standing fact or reflection a consolidation proposes: its text, its epistemic type,
    whose claim it is (``attributed_claim``) and the records it rests on. The engine recomputes
    the type and the speaker from ``supports`` and never widens either (FR-MEM-9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=2_000)
    modality: Modality = "observation"
    attributed_to: str | None = Field(default=None, max_length=200)
    supports: list[int] = Field(default_factory=list, max_length=CONSOLIDATION_MAX_RECORDS)


class ConsolidationResult(BaseModel):
    """The structured result of one ``consolidation`` call: standing facts and reflections. The
    defaults (nothing) are the conservative answer: no output, the window stays as it is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: list[ConsolidationStatement] = Field(
        default_factory=list, max_length=CONSOLIDATION_STATEMENTS_MAX
    )
    reflections: list[ConsolidationStatement] = Field(
        default_factory=list, max_length=CONSOLIDATION_STATEMENTS_MAX
    )


SALIENCE_MAX_BEATS = 50
"""The most beats one ``salience_detection`` request carries (one render's)."""
SALIENCE_LAST_BEATS = 6
"""How many earlier beats ``scene_context.last_beats`` holds."""
SALIENCE_MAX_SPANS = 50
"""The most salient spans, and texture spans, one answer may hold."""


class SalienceBeat(InputSchema):
    """One observable beat of the render being read (#146)."""

    index: Annotated[int, Field(ge=0), C.RENDER_TEXT]
    kind: Annotated[Literal["say", "do", "narration"], C.RENDER_TEXT] = "narration"
    speaker: Annotated[str | None, Field(max_length=200), C.RENDER_TEXT] = None
    text: Annotated[str, Field(max_length=4_000), C.RENDER_TEXT]


class SceneContext(InputSchema):
    """What the scene holds around the render (#146): who and what is present, by name, and
    the last observable beats before it."""

    present_entities: Annotated[list[str], Field(max_length=100), C.SCENE] = Field(
        default_factory=list
    )
    last_beats: Annotated[list[str], Field(max_length=SALIENCE_LAST_BEATS), C.SCENE] = Field(
        default_factory=list
    )


class SalienceInputs(InputSchema):
    """One render's observable beats and its scene (FR-COMMIT-3; #146)."""

    beats: Annotated[list[SalienceBeat], Field(max_length=SALIENCE_MAX_BEATS)] = Field(
        default_factory=list
    )
    scene_context: SceneContext = Field(default_factory=SceneContext)


SALIENCE_QUOTE_MAX = 2_000
"""The longest quote one salience span may carry."""


class SalienceQuote(BaseModel):
    """A span of one beat as the model names it (#461): the beat's index and the span's exact
    words, copied from the beat (``span``). The engine locates the quote in the beat's text, as
    the extraction backstop does; small models place character offsets poorly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    beat: int = Field(ge=0)
    span: str = Field(min_length=1, max_length=SALIENCE_QUOTE_MAX)


class SalienceSpan(BaseModel):
    """A located span of one beat: its index and ``[start, end)`` in the beat's text (the
    engine's location of a ``SalienceQuote``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    beat: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class SalienceSketch(BaseModel):
    """The proposition a salient span makes salient, as text (#140 normalizes it): its
    subject, relation and object, any of them an indefinite ("someone") when unknown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(default="", max_length=500)
    relation: str = Field(default="", max_length=500)
    object: str | None = Field(default=None, max_length=500)
    time: str | None = Field(default=None, max_length=200)


class SalientSpan(BaseModel):
    """Content placed to be noticed (FR-COMMIT-3), located: where, and the proposition it
    raises."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span: SalienceSpan
    proposition_sketch: SalienceSketch


class SalientQuote(BaseModel):
    """Content placed to be noticed (FR-COMMIT-3) as the model names it: the beat, the span's
    exact words (``span``, located by search, #461) and the proposition it raises."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    beat: int = Field(ge=0)
    span: str = Field(min_length=1, max_length=SALIENCE_QUOTE_MAX)
    proposition_sketch: SalienceSketch


class SalienceResult(BaseModel):
    """The structured result of one ``salience_detection`` call: salient and texture spans as
    quotes. The defaults (nothing salient) are the safe answer: every beat is texture, and
    nothing is owed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    salient: list[SalientQuote] = Field(default_factory=list, max_length=SALIENCE_MAX_SPANS)
    texture: list[SalienceQuote] = Field(default_factory=list, max_length=SALIENCE_MAX_SPANS)


EXTRACTION_MAX_BEATS = 50
"""The most beats one ``extraction_backstop`` request carries (one render's, or the composed
message as one)."""
EXTRACTION_MAX_CLAIMS = 50
"""The most claims one answer may hold."""
EXTRACTION_MIN_CONFIDENCE = 0.3
"""An extracted claim below this confidence is dropped (#148)."""


class ExtractionBeat(InputSchema):
    """One beat of the text being read for claims (#148): its index, kind, speaker's name and
    text. The composed message is one beat of kind ``message``."""

    index: Annotated[int, Field(ge=0), C.RENDER_TEXT]
    kind: Annotated[
        Literal["say", "do", "think", "latent", "narration", "message"], C.RENDER_TEXT
    ] = "narration"
    speaker: Annotated[str | None, Field(max_length=200), C.RENDER_TEXT] = None
    text: Annotated[str, Field(max_length=8_000), C.RENDER_TEXT]


class ExtractionInputs(InputSchema):
    """One render's beats (or the composed message) and its scene (FR-VERIFY-13 (c); #148)."""

    beats: Annotated[list[ExtractionBeat], Field(max_length=EXTRACTION_MAX_BEATS)] = Field(
        default_factory=list
    )
    scene_context: SceneContext = Field(default_factory=SceneContext)


class ExtractedClaim(BaseModel):
    """One factual claim the text makes: its beat, the exact substring that says it
    (``span``, located by search), and the claim as subject, predicate and object (#140
    normalizes it), its time expression, polarity and the model's confidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    beat: int = Field(default=0, ge=0)
    span: str = Field(min_length=1, max_length=2_000)
    subject: str = Field(default="", max_length=500)
    predicate: str = Field(default="", max_length=500)
    object: str | None = Field(default=None, max_length=500)
    time: str | None = Field(default=None, max_length=200)
    polarity: Literal["affirmed", "negated"] = "affirmed"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    """The structured result of one ``extraction_backstop`` call. The default (no claims) is
    what the stub answers: the deterministic layers stand alone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=EXTRACTION_MAX_CLAIMS)


class EmbeddingInputs(InputSchema):
    """One text to embed (FR-EMBED-1; #138): a memory's text, or a retrieval query's."""

    text: Annotated[str, C.MEMORY_TEXT]


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
    history_field: str | None = None
    """An input field whose ``TranscriptLine``s render as chat messages between the ``system``
    and ``user`` blocks, each with its own role, through the template's ``line`` macro."""
    story_scoped: bool = True
    """Whether the purpose runs inside a story (its turns, its background work), so an engine
    profile may route it and a story's runtime facts name its model. ``rating_suggestion``
    reviews catalog entries and is deployment-level (#121)."""
    variants: Mapping[str, str] = field(default_factory=lambda: types.MappingProxyType({}))
    """Further templates over the same input schema, by name (#150: ``generation``'s
    structured, narrator and age-repair renders). A call names one (``variant=``); the
    ``template`` is the default."""
    status: PurposeStatus = field(default=PurposeStatus.DELIVERED, init=False)

    def template_of(self, variant: str | None = None) -> str:
        """The template a call renders: ``variant``'s, else the purpose's own."""
        if variant is None:
            return self.template
        try:
            return self.variants[variant]
        except KeyError:
            raise RegistryError(f"{self.name}: no template variant {variant!r}") from None


@dataclass(frozen=True)
class ReservedPurpose:
    """A purpose (or port) FR-MODEL-2 names that a later milestone delivers."""

    name: str
    description: str
    milestone: str
    kind: Literal["purpose", "port"] = "purpose"
    defaults_to: str | None = None
    status: PurposeStatus = field(default=PurposeStatus.RESERVED, init=False)


@dataclass(frozen=True)
class EmbeddingFloor:
    """The recommended minimum an embedding model must meet (FR-MODEL-10 for the port): it
    accepts inputs of ``min_input_tokens``, its vectors have ``min_dimension`` to
    ``max_dimension`` dimensions, and one request may carry up to ``max_batch`` texts."""

    min_input_tokens: int = 512
    min_dimension: int = 64
    max_dimension: int = 4_096
    max_batch: int = 64


@dataclass(frozen=True)
class Port:
    """A delivered port (FR-MODEL-2's separately ported capability): configured through its
    own ``model_config`` row (``default_model_key``, its own name, with no fallback to the
    ``default`` row) and called through its own adapter, never as a chat completion."""

    name: str
    description: str
    default_model_key: str
    floor: EmbeddingFloor
    input_schema: type[InputSchema]
    hot_path: bool
    never_receives: frozenset[ContentCategory]
    story_scoped: bool = False
    """A port has one deployment-wide configuration; an engine profile cannot route it."""
    kind: Literal["port"] = "port"
    status: PurposeStatus = field(default=PurposeStatus.DELIVERED, init=False)


DEFAULT_MODEL_KEY = "default"
_EVERYTHING = frozenset(ContentCategory)

GENERATION = Purpose(
    name="generation",
    description=(
        "Writes the reply of a Lite turn from the story's prompt sections; in a structured "
        "story (#150) the typed beats of one character's or the narrator's render (a "
        "`response_schema`: constrained decoding or parse-validate-retry), the age repair and "
        "the composition of the turn's verified beats into its message (#152)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.NONE),
    input_schema=GenerationInputs,
    template="lite_generation.j2",
    hot_path=True,
    # Policy text arrives only as a regeneration's constraints (#68): the violated policies'
    # messages, never a policy prompt, subject label or age.
    never_receives=frozenset({C.SUBJECT_TRUE_AGE, C.CHECKED_TEXT, C.EVAL_TEXT}),
    history_field="recent_messages",
    variants=types.MappingProxyType(
        {
            "structured": "structured_generation.j2",
            "narrator": "narrator_generation.j2",
            "age_repair": "age_repair.j2",
            "composition": "composition.j2",
        }
    ),
)
"""``generation``: the Lite reply (``lite_generation.j2``) and, under ``structured`` (#150), the
character render (``structured_generation.j2``), the narrator render
(``narrator_generation.j2``), the age repair (``age_repair.j2``) and the composition of the
turn's verified beats into its message (``composition.j2``, #152). The structured path needs
``json_schema`` or ``parse_validate_retry`` (its beats are a ``response_schema``)."""

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

CONSOLIDATION = Purpose(
    name="consolidation",
    description=(
        "Compresses one window of episodic memories (and the canon in it) into standing facts "
        "and reflections, in the background (FR-MEM-3,6,9,10)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=ConsolidationInputs,
    template="consolidation.j2",
    hot_path=False,
    # The window's records and their labels only: never a definition, a policy, an age, a
    # transcript or eval text, and never a record outside the window or the scope.
    never_receives=_EVERYTHING - {C.MEMORY_TEXT, C.RECORD_META, C.SCOPE_META},
    response_schema=ConsolidationResult,
)

LITE_SUMMARY = Purpose(
    name="lite_summary",
    description="Folds old messages into a Lite story's rolling summary (FR-LITE-2).",
    # #51's note, applied by #143: the rolling summary is a consolidation, so a deployment that
    # configures a consolidation model summarises with it unless lite_summary has its own row.
    default_model_key=CONSOLIDATION.name,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.NONE),
    input_schema=LiteSummaryInputs,
    template="lite_summary.j2",
    hot_path=False,
    never_receives=frozenset({C.POLICY_TEXT, C.SUBJECT_TRUE_AGE, C.CHECKED_TEXT, C.EVAL_TEXT}),
)

RATING_SUGGESTION = Purpose(
    name="rating_suggestion",
    description=(
        "Reviews one catalog entry against one rating's content policy: its description, "
        "guidance and model-assisted policies (FR-RATE-8)."
    ),
    default_model_key=GUARDRAIL_CHECK.name,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=RatingSuggestionInputs,
    template="rating_suggestion.j2",
    hot_path=False,
    never_receives=_EVERYTHING
    - {
        C.CATALOG_TEXT,
        C.RATING_DESCRIPTION,
        C.RATING_GUIDANCE,
        C.POLICY_TEXT,
        C.SUBJECT_TRUE_AGE,
    },
    response_schema=RatingSuggestionVerdict,
    story_scoped=False,
)

QUALITY_JUDGE = Purpose(
    name="quality_judge",
    description=(
        "Pairwise prose comparisons and single-message scores for the eval harness "
        "(NFR-QUAL-14,15; #158): never in a turn, never routed by a profile."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=QualityJudgeInputs,
    template="quality_judge.j2",
    hot_path=False,
    # Eval text only: every story-content category is refused, so the judge can never be handed
    # a live story's transcript, memories or definitions (the harness also refuses any story
    # that is not one of its scratch stories, #134).
    never_receives=_EVERYTHING - {C.EVAL_TEXT},
    response_schema=QualityJudgeVerdict,
    story_scoped=False,
    # The score of one message against a reference (E-13's voice and story-driver signals),
    # answered by ``QualityJudgeScore``.
    variants=types.MappingProxyType({"score": "quality_judge_score.j2"}),
)

IMPORTANCE_SCORING = Purpose(
    name="importance_scoring",
    description="Scores one memory's importance, 0 (trivia) to 1 (pivotal) (FR-MEM-7).",
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=ImportanceScoringInputs,
    template="importance_scoring.j2",
    hot_path=False,
    # The memory's own text and kind only: never a definition, a policy, an age or eval text.
    never_receives=_EVERYTHING - {C.MEMORY_TEXT},
    response_schema=ImportanceScore,
)

ENTAILMENT = Purpose(
    name="entailment",
    description=(
        "Judges the fuzzy residual of verify: one claim the deterministic check left uncertain, "
        "against the holder's records only (FR-VERIFY-3,8)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=EntailmentInputs,
    template="entailment.j2",
    hot_path=True,
    # The claim and the holder's records only: never a transcript, a definition, a memory, a
    # policy, an age or eval text.
    never_receives=_EVERYTHING - {C.CLAIM_TEXT, C.CANON_RECORD},
    response_schema=EntailmentVerdict,
)

SALIENCE_DETECTION = Purpose(
    name="salience_detection",
    description=(
        "Tells content placed to be noticed from background texture in one render's beats, "
        "naming the proposition each salient span raises (FR-COMMIT-3)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=SalienceInputs,
    template="salience.j2",
    hot_path=True,
    # The render's own observable beats and its scene only: never a definition, a memory, a
    # policy, an age, a think/latent beat or eval text.
    never_receives=_EVERYTHING - {C.RENDER_TEXT, C.SCENE},
    response_schema=SalienceResult,
)

EXTRACTION_BACKSTOP = Purpose(
    name="extraction_backstop",
    description=(
        "The model layer of claim detection: lists every factual claim one render's beats, or "
        "the composed message, make, so a claim the declared structure and the deterministic "
        "detectors missed is still checked (FR-VERIFY-13,14)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=ExtractionInputs,
    template="extraction.j2",
    hot_path=True,
    # The render's own beats (or the composed message) and its scene only: never another
    # story's text, a belief, a definition, a memory, a policy, an age or eval text.
    never_receives=_EVERYTHING - {C.RENDER_TEXT, C.SCENE},
    response_schema=ExtractionResult,
)

REGENERATION = Purpose(
    name="regeneration",
    description=(
        "Rewrites one beat of a structured render that contradicted canon or asserted what its "
        "speaker may not introduce, under the repair's constraints, with the facts it "
        "contradicted and their neighborhood (FR-REPAIR-1,2; the repair ladder's rung 3)."
    ),
    # docs/02 §5: usually the same model as generation, so it shares generation's row.
    default_model_key=GENERATION.name,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=RegenerationInputs,
    template="regeneration.j2",
    hot_path=True,
    never_receives=frozenset({C.SUBJECT_TRUE_AGE, C.CHECKED_TEXT, C.EVAL_TEXT}),
)
"""``regeneration`` (#151): the repair ladder's constrained rewrite of one beat. Its answer is a
``response_schema`` the ladder passes (``orchestrator.repair.ladder.RegeneratedBeat``)."""

RESERVED_PURPOSES: tuple[ReservedPurpose, ...] = (
    ReservedPurpose(
        "belief_adjudication", "Adjudicated belief transitions.", "M5", defaults_to="entailment"
    ),
    ReservedPurpose(
        "time_estimation",
        "Best-guess duration of a narrated skip.",
        "M6",
        defaults_to="extraction_backstop",
    ),
    ReservedPurpose("image_prompt", "Writes image prompts.", "M8", defaults_to="generation"),
    ReservedPurpose("image_check", "Vision checks of image outputs; no default.", "M8"),
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
    if not all(t.endswith(".j2") for t in (purpose.template, *purpose.variants.values())):
        raise RegistryError(f"{purpose.name}: template must be a .j2 file")
    if (
        purpose.history_field is not None
        and purpose.history_field not in purpose.input_schema.model_fields
    ):
        raise RegistryError(f"{purpose.name}: history field {purpose.history_field!r} undeclared")


EMBEDDING = Port(
    name="embedding",
    description=(
        "Embeds memory texts and retrieval queries over the OpenAI-compatible `/embeddings` "
        "surface (FR-EMBED-1). One deployment-wide model: its dimension is pinned at the first "
        "embedding, and changing the model re-embeds every hot vector in the background "
        "(FR-EMBED-3,4)."
    ),
    default_model_key="embedding",
    floor=EmbeddingFloor(),
    input_schema=EmbeddingInputs,
    hot_path=True,
    # Texts only: a memory's or a query's, of the story being searched. Never a definition, a
    # policy, an age or eval text.
    never_receives=_EVERYTHING - {C.MEMORY_TEXT},
)

PORTS: Mapping[str, Port] = types.MappingProxyType({EMBEDDING.name: EMBEDDING})
"""The ports delivered so far, by name (configurable like purposes, never chat-completed)."""


def validate_port(port: Port) -> None:
    """Raise ``RegistryError`` unless every input is tagged and none is a forbidden category."""
    forbidden = schema_categories(port.input_schema) & port.never_receives
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise RegistryError(f"{port.name}: declares inputs it never receives: {names}")
    if port.default_model_key != port.name:
        raise RegistryError(f"{port.name}: a port has its own model_config row")


for _port in PORTS.values():
    validate_port(_port)


def _build(purposes: tuple[Purpose, ...]) -> Mapping[str, Purpose]:
    built: dict[str, Purpose] = {}
    reserved = {r.name for r in RESERVED_PURPOSES}
    for purpose in purposes:
        validate_purpose(purpose)
        if (
            purpose.name in built
            or purpose.name in reserved
            or purpose.name in PORTS
            or purpose.name == DEFAULT_MODEL_KEY
        ):
            raise RegistryError(f"{purpose.name}: declared twice")
        parent = purpose.default_model_key
        if parent != DEFAULT_MODEL_KEY and (
            parent not in built or built[parent].default_model_key != DEFAULT_MODEL_KEY
        ):
            raise RegistryError(
                f"{purpose.name}: defaults to {parent!r}, which is not a delivered purpose "
                "declared before it that defaults to the default row"
            )
        built[purpose.name] = purpose
    return types.MappingProxyType(built)


DELIVERED: Mapping[str, Purpose] = _build(
    (
        GENERATION,
        GUARDRAIL_CHECK,
        CONSOLIDATION,
        LITE_SUMMARY,
        RATING_SUGGESTION,
        QUALITY_JUDGE,
        IMPORTANCE_SCORING,
        ENTAILMENT,
        SALIENCE_DETECTION,
        EXTRACTION_BACKSTOP,
        REGENERATION,
    )
)
"""Purposes a model call may use now, by name."""

STORY_PURPOSES: Mapping[str, Purpose] = types.MappingProxyType(
    {name: purpose for name, purpose in DELIVERED.items() if purpose.story_scoped}
)
"""The delivered purposes that run inside a story: what an engine profile routes, a story's
runtime facts fingerprint and the model matrix measures (not ``rating_suggestion``, #121, nor
``quality_judge``, #134)."""

RESERVED: Mapping[str, ReservedPurpose] = types.MappingProxyType(
    {r.name: r for r in RESERVED_PURPOSES}
)
"""Purposes and ports declared for a later milestone, by name."""


def get_purpose(name: str) -> Purpose:
    """The delivered purpose ``name``; ``KeyError`` for a reserved or unknown name."""
    return DELIVERED[name]


__all__ = [
    "CONSOLIDATION",
    "CONSOLIDATION_MAX_RECORDS",
    "CONSOLIDATION_STATEMENTS_MAX",
    "DEFAULT_MODEL_KEY",
    "DELIVERED",
    "EMBEDDING",
    "ENTAILMENT",
    "ENTAILMENT_MAX_RECORDS",
    "EXTRACTION_BACKSTOP",
    "EXTRACTION_MAX_BEATS",
    "EXTRACTION_MAX_CLAIMS",
    "EXTRACTION_MIN_CONFIDENCE",
    "IMPORTANCE_SCORING",
    "PORTS",
    "QUALITY_JUDGE",
    "RATING_SUGGESTION",
    "REGENERATION",
    "REGENERATION_MAX_RECORDS",
    "RESERVED",
    "SALIENCE_DETECTION",
    "SALIENCE_LAST_BEATS",
    "SALIENCE_MAX_BEATS",
    "SALIENCE_MAX_SPANS",
    "SALIENCE_QUOTE_MAX",
    "STORY_PURPOSES",
    "SUBJECT_LABEL_PATTERN",
    "AgeClue",
    "CapabilityFloor",
    "CharacterDefinition",
    "ConsolidationInputs",
    "ConsolidationRecord",
    "ConsolidationResult",
    "ConsolidationStatement",
    "ConstraintKind",
    "ContentCategory",
    "ElementKind",
    "EmbeddingFloor",
    "EmbeddingInputs",
    "EntailmentInputs",
    "EntailmentRecord",
    "EntailmentVerdict",
    "ExtractedClaim",
    "ExtractionBeat",
    "ExtractionInputs",
    "ExtractionResult",
    "GenerationInputs",
    "GuardrailCheckInputs",
    "GuardrailVerdict",
    "ImportanceScore",
    "ImportanceScoringInputs",
    "InputSchema",
    "JudgeCriteria",
    "JudgePair",
    "KnownHit",
    "LiteSummaryInputs",
    "Modality",
    "PlayerInput",
    "Port",
    "Purpose",
    "PurposeStatus",
    "QualityJudgeInputs",
    "QualityJudgeScore",
    "QualityJudgeVerdict",
    "RatingSuggestionInputs",
    "RatingSuggestionVerdict",
    "RecordKind",
    "RegenerationInputs",
    "RegistryError",
    "RepairConstraint",
    "ReservedPurpose",
    "SalienceBeat",
    "SalienceInputs",
    "SalienceQuote",
    "SalienceResult",
    "SalienceSketch",
    "SalienceSpan",
    "SalientQuote",
    "SalientSpan",
    "SceneContext",
    "StructuredOutput",
    "Subject",
    "SuggestedConcern",
    "SuggestedElement",
    "SuggestedLocation",
    "SuggestionElement",
    "SuggestionPolicy",
    "TranscriptLine",
    "declared_paths",
    "field_categories",
    "get_purpose",
    "schema_categories",
    "validate_port",
    "validate_purpose",
]
