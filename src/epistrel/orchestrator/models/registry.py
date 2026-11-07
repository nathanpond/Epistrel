"""The purpose registry (FR-MODEL-2, FR-MODEL-10, FR-MODEL-13; docs/02 §5).

Every model purpose is declared here: its name, the ``model_config`` row it defaults to, its
capability floor, and a typed schema of every input its requests may carry, each field tagged
with a ``ContentCategory``. Prompt templates (``prompts.py``) and the payload builder
(``access.ModelAccessLayer.call``) use only declared inputs, so a prompt can carry only what its
purpose declares.

``DELIVERED`` holds the purposes a model call may use today; ``PORTS`` the separate ports
(``embedding``, #138, and the image adapter ``image_generation``, #254: not chat completions, so
they have no template and no response schema, but each its own floor, input schema and
``model_config`` row); ``RESERVED`` names every other purpose (and port) FR-MODEL-2 enumerates,
with the milestone that delivers it. Purposes join ``DELIVERED`` as the capabilities that use
them land (FR-MODEL-13, §22). M8 delivered the last three (``image_prompt``, ``image_check`` and
the ``image_generation`` port), so ``RESERVED`` is empty; the machinery stays for later ones.

A purpose's ``default_model_key`` names the row it falls back to: ``default``, a delivered
purpose declared before it, or its **own name**, meaning its own row only, with no fallback to
any other (``image_check``, #254: a vision checker is never the house chat model; a port is
always its own row). ``OWN_ROW_ONLY`` lists both kinds.

``HOSTED_SAFETY_RETENTION`` is the sentence the model guide prints in both safety purposes'
rows (``SAFETY_PURPOSES``: ``guardrail_check`` and ``image_check``, FR-IMG-13).

The registry validates itself at import: a field with no category (its own, or one on an
enclosing field) or a category the purpose ``never_receives`` raises ``RegistryError``.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from pydantic.json_schema import SkipJsonSchema


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
    BELIEF_RECORD = "belief_record"
    """What ``belief_adjudication`` (#162) is told about one witness: its name, the records the
    deterministic consistency check read for the claim (the witness's own beliefs and the canon
    the witness may read, each rendered like a claim), its trust in the speaker, its prior
    outcomes with that speaker and the rules' default decision. Never another character's
    private record, never canon the witness cannot know."""
    BELIEF_STATE = "belief_state"
    """A structured character render's own belief state (FR-TURN-8; #170,
    ``core.epistemic.context``): its first-order beliefs as "you suspect / believe / know …"
    lines, its nested beliefs ("you think Bobby believes …") and the speech acts it heard ("you
    heard Bobby say …"). The rendering character's partition only, never another's, never a
    scalar, a provenance or a divergence."""
    UTTERANCE_TEXT = "utterance_text"
    AUTHOR_INSTRUCTION = "author_instruction"
    """An author's history-rewrite instruction and free-text answers (#223), and the names it
    is normalized against (the story's entities, predicates and States, and the clock): what
    ``rewrite_parse`` reads. Never a character's private record, a memory or transcript text."""
    TRACKED_VALUES = "tracked_values"
    """A structured render's ``## Tracked values`` (#187, FR-TURN-11): for a character's render,
    the player-visible States it may change (name, type, scope, unit, aliases, allowed values)
    and the effect syntax, never a value; for the narrator's, every State with its current
    value and constraints."""
    EXTERNAL_DATA = "external_data"
    """A value an external-data provider answered (FR-EXT-5, NFR-SEC-2; #208): untrusted content,
    typed and cleaned by ``orchestrator.providers.sanitize`` and rendered only as a fenced,
    labelled data block (``<external name=… tier=…>…</external>``), never as instructions; the
    payload's JSON keys never. The narrator's render takes it (a character learns a value only
    as a belief, #170); never a purpose that judges or compresses the story's own records."""
    """The say a witness heard (#162): the speaker's name, the words as the witness perceived
    them, the visible manner, the claims they carry and the intent as the witness reads it."""
    IMAGE = "image"
    """An image's own bytes (#254, FR-IMG-13): a generated output, or an upload, sent to
    ``image_check`` for the output policies (#258 sends it as a downscaled data URL)."""
    SUBJECT_LABEL = "subject_label"
    """An opaque label of a subject in an image's safety context (``character_1`` …; #257
    widens the shapes): never a name the prompt does not contain."""
    VISUAL_DESCRIPTOR = "visual_descriptor"
    """What an image shows, in visual terms only: a subject's perceivable appearance and
    apparent age band, the visual style, and, in a safety context, the descriptors the prompt
    already uses for a subject. Never a true age, a name the prompt lacks, or story text."""
    IMAGE_PROMPT = "image_prompt"
    """A built image prompt and its negative prompt (#254): what the image adapter generates
    from, and what ``guardrail_check`` and ``image_check`` judge; with its generation
    parameters for the adapter (seed, size or aspect ratio, quality, count)."""
    CAPTION_ALT_TEXT = "caption_alt_text"
    """An image's caption and alt text, as ``image_check`` judges them with the image."""
    REFERENCE_IMAGE = "reference_image"
    """A reference image the image adapter is given: one the audience has perceived (FR-IMG-5),
    as a media asset id the adapter resolves to a signed or data URL (#255)."""


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
    vision: bool = False
    """The model takes image input (``image_check``, #254). Declared, not probed: a ``PUT``
    of ``image_check`` records the floor and sends nothing."""


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
    heard: Annotated[list[str], C.PLAYER_INPUT] = Field(default_factory=list)
    """#572: a character render's perceived input short of its words (an envelope, an
    eavesdropper's part), as working-memory lines; ``say``/``do`` are then what it perceived."""


class AgeClue(InputSchema):
    """A character a structured render introduced without an age (#150): its name and what the
    render said about it."""

    name: Annotated[str, C.SCENE]
    clue: Annotated[str, C.SCENE] = ""


class DirectorCandidate(InputSchema):
    """A primary the director orders (#174, FR-MULTI-6): its id, its name and its public card
    (its definition; never a private record)."""

    id: Annotated[str, C.CHARACTER_DEFINITION]
    name: Annotated[str, C.CHARACTER_DEFINITION]
    card: Annotated[str, C.CHARACTER_DEFINITION] = Field(default="", max_length=2_000)


class TiebreakBeat(InputSchema):
    """One conflicting action the director judges (#175, FR-MULTI-8): the acting primary's id
    and name, and its checked ``say``/``do`` text (never a ``think`` or ``latent``'s)."""

    character_id: Annotated[str, C.VERIFIED_BEAT]
    name: Annotated[str, C.VERIFIED_BEAT]
    kind: Annotated[Literal["say", "do"], C.VERIFIED_BEAT] = "do"
    text: Annotated[str, C.VERIFIED_BEAT] = Field(default="", max_length=2_000)


class TiebreakConflict(InputSchema):
    """One object two or more primaries of a simultaneous turn acted on (#175): its id, its
    name and the conflicting actions, in request order."""

    object: Annotated[str, C.VERIFIED_BEAT]
    name: Annotated[str, C.VERIFIED_BEAT] = ""
    actions: list[TiebreakBeat] = Field(default_factory=list, max_length=6)


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
    own_age: Annotated[str, C.CHARACTER_DEFINITION] = Field(default="", max_length=100)
    """A structured character render under per-character partitions (#170, FR-CAT-12): the
    rendering character's own true age ("You are 34."), part of its own definition; empty in
    every other prompt. Another character's age reaches it only as an apparent band (``scene``)."""
    beliefs: Annotated[list[str], C.BELIEF_STATE] = Field(default_factory=list, max_length=200)
    """A structured character render's ``## What you believe`` (FR-TURN-8; #170): its own belief
    state (``core.epistemic.context.CharacterContext.belief_lines``)."""
    time_belief: Annotated[str, C.BELIEF_STATE] = Field(default="", max_length=4_000)
    """A structured character render's ``## Your sense of time`` (FR-CLOCK-11; #200): its own
    time belief, the time it perceived in the scene and the schedules it holds
    (``CharacterContext.time_belief``); never the objective clock. Empty when the story runs no
    clock and in every other prompt."""
    clock_now: Annotated[str, C.SCENE] = Field(default="", max_length=300)
    """The narrator's objective time (#200, FR-CLOCK-11): the clock as the turn starts, as the
    anchor shows it ("Saturday, 17:30 (evening)"); never a character's (a character knows the
    time only as its own belief, ``time_belief``)."""
    private_memories: Annotated[list[str], C.WORKING_MEMORY] = Field(
        default_factory=list, max_length=20
    )
    """A structured character render's ``## Your private memories`` (#172, FR-VERIFY-9): its
    own ``think`` memories ("you thought: …", ``CharacterContext.private_memories``)."""
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
    length_target: Annotated[str, C.CONSTRAINT] = Field(default="", max_length=300)
    """A structured character render's length target (FR-RESP-3; #464): one fixed line naming
    the requested response length in words; empty in every other prompt."""
    belief_steps: Annotated[bool, C.CONSTRAINT] = False
    """A structured character render's step contract (FR-TURN-12, FR-TURN-8; #168,
    ``steps_contract.j2``): the fixed ``## How to change your mind`` section, present when the
    rendering character keeps its own beliefs (per-character partitions)."""
    clock_contract: Annotated[bool, C.CONSTRAINT] = False
    """A structured render's time contract (FR-CLOCK-6,7,17; #194, ``clock_contract.j2``): the
    fixed ``## Time`` section (time advances, ``time`` steps, ``flashback``), present when the
    story runs the world clock."""
    player_input: PlayerInput
    player_authority: Annotated[Literal["", "own_character", "co_author"], C.CONSTRAINT] = ""
    """A Lite prompt's ``## Player authority`` (FR-RESP-11; #180): the story's setting, rendered
    as one fixed instruction per value, its only Lite effect; empty in a structured prompt (whose
    pipeline applies the setting itself)."""
    player_requests: Annotated[list[str], C.PLAYER_INPUT] = Field(
        default_factory=list, max_length=16
    )
    tracked_values: Annotated[list[str], C.TRACKED_VALUES] = Field(
        default_factory=list, max_length=64
    )
    """A structured render's ``## Tracked values`` (#187): one line per State the render may
    change (a character's: player-visible ones it is authorized for, no value; the narrator's:
    every State with its value at the turn's start and its constraints). A Lite prompt's
    (#203, FR-LITE-5): one line per player-visible State with its value, shown only."""
    lite_time: Annotated[list[str], C.SCENE] = Field(default_factory=list, max_length=65)
    """A Lite prompt's ``## Time`` (#203, FR-LITE-5): the world clock as the turn starts and the
    open canon scheduled events with their planned times (``orchestrator.lite.context``); empty
    with the clock off and in every structured prompt (whose renders take ``clock_now`` and
    ``time_belief``)."""
    tracked_syntax: Annotated[str, C.TRACKED_VALUES] = Field(default="", max_length=1_000)
    """#187: how to declare a State effect, with one example (present with ``tracked_values``)."""
    external_data: Annotated[list[str], C.EXTERNAL_DATA] = Field(
        default_factory=list, max_length=64
    )
    """The narrator's ``## External data`` (#208, FR-EXT-5): one fenced block per value the turn
    pinned from a provider (``sanitize.render_block``); never in a character's render (#170,
    ``check-character-context``)."""
    pending_outcomes: Annotated[list[str], C.TRACKED_VALUES] = Field(
        default_factory=list, max_length=64
    )
    """The narrator's ``## Awaiting the game`` (#209, FR-EXT-9, FR-EXT-21 (e)): the changes the
    last three turns asked the consumer to apply with no outcome reported yet, a hint never to
    narrate one as done; never in a character's render (a character learns an outcome only by
    perception; ``check-character-context``)."""
    """A structured character render's ``## The player asks of you`` (FR-RESP-11; #180): under
    ``player_authority = own_character``, what the player's input described this character
    doing, saying or (never written) thinking, as requests the render complies with or refuses
    in character; never canon."""
    # -- the structured pipeline's sections (#150; empty, and so absent, in a Lite prompt) ------
    scene: Annotated[str, C.SCENE] = ""
    """A structured render's ``## Scene``: the place and who is present."""
    working_memory: Annotated[list[str], C.WORKING_MEMORY] = Field(default_factory=list)
    """A structured render's ``## What you remember``: the latest beats as perceived."""
    just_now: Annotated[list[str], C.WORKING_MEMORY] = Field(default_factory=list, max_length=64)
    """A structured character render's ``## Just now`` (#174): in a ``sequential`` turn, the
    verified beats the earlier primaries rendered, as the character perceived them."""
    others_present: Annotated[list[str], C.SCENE] = Field(default_factory=list, max_length=64)
    """A structured character render's ``## Others present`` (#176, FR-MULTI-9): the reactive
    characters present, ``<name> (id <id>)``, whose brief visible reactions the render may
    narrate in ``reactions``."""
    interjections: Annotated[bool, C.CONSTRAINT] = False
    """#176: whether the ``## Others present`` rule allows a brief interjection."""
    retrieved_memories: Annotated[list[str], C.RETRIEVED_MEMORIES] = Field(default_factory=list)
    """A structured render's ``## Retrieved``: #141's assembled context."""
    canon_facts: Annotated[list[str], C.CANON_FACTS] = Field(default_factory=list)
    """A structured render's ``## Canon``: the facts retrieval found."""
    open_references: Annotated[list[str], C.SCENE] = Field(default_factory=list)
    """A follow-up narrator render (#146's FR-COMMIT-7, #150): the salient references this turn
    left open, which it must settle."""
    director_candidates: list[DirectorCandidate] = Field(default_factory=list, max_length=6)
    """The director's ordering call (#174, FR-MULTI-6; ``director_order.j2``): the primaries of a
    ``sequential`` turn it orders."""
    tiebreak_conflicts: list[TiebreakConflict] = Field(default_factory=list, max_length=12)
    """The director's tie-break (#175, FR-MULTI-8; ``director_tiebreak.j2``): the objects a
    ``simultaneous`` turn's primaries acted on at once, each with the conflicting actions."""
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


SUBJECT_LABEL_PATTERN = r"^(character|subject|figure)_[1-9][0-9]{0,3}$"
"""Positional labels only (``character_1`` … for a text check's cast; ``subject_1`` … and
``figure_1`` … for an image's safety context, #257): a subject's name can never reach the
model."""


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
    present: Annotated[bool, C.SUBJECT_TRUE_AGE] = True
    """Authoritatively co-located with the content (#161); ``False`` only under perception
    routing, for a character placed elsewhere."""


IMAGE_DESCRIPTORS_MAX = 20
IMAGE_TEXT_MAX = 8_000


SubjectClass = Literal["adult", "minor", "indeterminate"]


class ImageSubject(InputSchema):
    """One subject of an image's safety context (FR-IMG-12,13; #254, #257): an opaque label
    (``subject_1`` … for a character, ``figure_1`` … for a figure that resolves to none), its
    true age (``None`` for a figure), its classification (indeterminate counts as a minor) and
    the visual descriptors the prompt already uses for it. No name, no story text."""

    label: Annotated[
        str,
        StringConstraints(pattern=SUBJECT_LABEL_PATTERN),
        Field(examples=["subject_1"]),
        C.SUBJECT_LABEL,
    ]
    true_age: Annotated[int | None, Field(ge=0, le=150), C.SUBJECT_TRUE_AGE] = None
    classification: Annotated[SubjectClass | None, C.SUBJECT_TRUE_AGE] = None
    visual_descriptors: Annotated[
        list[Annotated[str, StringConstraints(max_length=500)]], C.VISUAL_DESCRIPTOR
    ] = Field(default_factory=list, max_length=IMAGE_DESCRIPTORS_MAX)


class CheckedImageSubject(ImageSubject):
    """An ``ImageSubject`` as ``guardrail_check`` receives it (#257). ``mentioned_as`` is how the
    checked text itself names the subject, as written (a span of the prompt, caption or alt
    text, so never a name the prompt lacks). Only the figure classification fills it, so it
    can tell the resolved subjects from the figures; a policy check leaves it empty."""

    mentioned_as: Annotated[
        list[Annotated[str, StringConstraints(max_length=200)]], C.CHECKED_TEXT
    ] = Field(default_factory=list, max_length=IMAGE_DESCRIPTORS_MAX)


class GuardrailCheckInputs(InputSchema):
    """The text being checked, the policy's prompt, and the subjects it is about (FR-IMG-13)."""

    text: Annotated[str, C.CHECKED_TEXT]
    policy_prompt: Annotated[str, C.POLICY_TEXT]
    subjects: list[Subject] = Field(default_factory=list)
    external_data: Annotated[list[str], C.EXTERNAL_DATA] = Field(
        default_factory=list, max_length=64
    )
    """#208: provider values the checked text was written from, as fenced data blocks (data to
    check against, never instructions to the checker)."""
    image_subjects: list[CheckedImageSubject] | None = Field(default=None, max_length=64)
    """#254, #257 (FR-IMG-13): for an image prompt's check, its safety context: each resolved
    subject as an opaque label with its true age (or a figure's classification) and the
    prompt's own visual descriptors; for the figure classification (``figures`` variant), the
    subjects to leave out. ``None`` for a text check."""


class ClassifiedFigure(BaseModel):
    """One depicted person who is none of the listed subjects: the prompt's own words for them
    and their apparent age class (``indeterminate`` when the text does not say)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    descriptor: str = Field(min_length=1, max_length=500)
    classification: SubjectClass


class SubjectDescriptors(BaseModel):
    """The visual descriptors the text uses for one listed subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Annotated[str, StringConstraints(pattern=SUBJECT_LABEL_PATTERN)]
    visual_descriptors: list[Annotated[str, StringConstraints(max_length=500)]] = Field(
        default_factory=list, max_length=IMAGE_DESCRIPTORS_MAX
    )


FIGURES_VARIANT = "figures"
FIGURES_TEMPLATE = "guardrail_figures.j2"


class FigureClassification(BaseModel):
    """The figure classification's answer (``guardrail_check``'s ``figures`` variant, FR-IMG-12;
    #257): every depicted person the text describes who is none of the listed subjects, each
    classified, and the descriptors the text uses for the listed subjects. Scripted on the stub
    by its template (``stub_script``), so no policy check's script answers it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    stub_script: ClassVar[str] = FIGURES_TEMPLATE

    figures: list[ClassifiedFigure] = Field(default_factory=list, max_length=32)
    subjects: list[SubjectDescriptors] = Field(default_factory=list, max_length=64)


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


ADJUDICATION_MAX_RECORDS = 30
"""The most witness records one ``belief_adjudication`` request carries."""
ADJUDICATION_MAX_CLAIMS = 20
"""The most claims of one say one ``belief_adjudication`` request carries."""
AdjudicationDecision = Literal["accept", "reject", "suspend", "speech_act_only"]


class AdjudicationBelief(InputSchema):
    """One of the witness's records the consistency check read for the claim (#162): a belief
    of its own or canon it may read, rendered like a claim, with its level."""

    statement: Annotated[str, Field(max_length=1_000), C.BELIEF_RECORD]
    source: Annotated[Literal["belief", "canon"], C.BELIEF_RECORD] = "belief"
    confidence: Annotated[str | None, Field(max_length=32), C.BELIEF_RECORD] = None


class AdjudicationPriors(InputSchema):
    """What the witness brings to the claim besides its records (#162): its earlier outcomes
    with this speaker, how sure it already is of the claim, and the rules' default decision."""

    outcomes: Annotated[list[AdjudicationDecision], Field(max_length=20), C.BELIEF_RECORD] = Field(
        default_factory=list
    )
    existing_confidence: Annotated[str | None, Field(max_length=32), C.BELIEF_RECORD] = None
    rule_default: Annotated[AdjudicationDecision, C.BELIEF_RECORD] = "suspend"


class AdjudicationUtterance(InputSchema):
    """The say the witness heard (#162): the speaker, the words, the manner and the claims."""

    speaker: Annotated[str, Field(max_length=200), C.UTTERANCE_TEXT]
    text: Annotated[str, Field(max_length=4_000), C.UTTERANCE_TEXT]
    manner: Annotated[str | None, Field(max_length=200), C.UTTERANCE_TEXT] = None
    claims: Annotated[list[str], Field(max_length=ADJUDICATION_MAX_CLAIMS), C.UTTERANCE_TEXT] = (
        Field(default_factory=list)
    )


class BeliefAdjudicationInputs(InputSchema):
    """One salient witness's view of one say (FR-BELIEF-3; #162): what it already holds about
    the claims, its trust in the speaker, its priors, the utterance and the intent it reads."""

    witness: Annotated[str, Field(max_length=200), C.BELIEF_RECORD]
    witness_beliefs: Annotated[
        list[AdjudicationBelief], Field(max_length=ADJUDICATION_MAX_RECORDS)
    ] = Field(default_factory=list)
    trust: Annotated[float, Field(ge=0.0, le=1.0), C.BELIEF_RECORD] = 0.5
    priors: AdjudicationPriors = Field(default_factory=AdjudicationPriors)
    utterance: AdjudicationUtterance
    perceived_intent: Annotated[Literal["sincere", "joking"], C.UTTERANCE_TEXT] = "sincere"


RULE_DEFAULT_MARKER = "Rule default: "
"""The line of the ``belief_adjudication`` prompt that names the rules' default decision; the
stub's default answer echoes it."""


class BeliefAdjudicationResult(BaseModel):
    """The structured result of one ``belief_adjudication`` call: what the witness makes of the
    say's claims, how sure the model is, and short reasons. The defaults (``suspend`` at
    confidence 0) are below the engine's threshold, so the rules' default stands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: AdjudicationDecision = "suspend"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list, max_length=5)

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Any]) -> dict[str, Any]:
        """The stub's default answer: the rules' default decision the prompt names, at 0.9."""
        del digest
        decision = "suspend"
        for message in messages:
            for line in str(getattr(message, "content", "")).splitlines():
                if line.startswith(RULE_DEFAULT_MARKER):
                    decision = line[len(RULE_DEFAULT_MARKER) :].strip()
        if decision not in ("accept", "reject", "suspend", "speech_act_only"):
            decision = "suspend"
        return {"decision": decision, "confidence": 0.9, "reasons": ["stub: the rules' default"]}


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
SALIENCE_MAX_CHARACTERS = 8
"""The most characters one answer may mark for modeling (#163)."""


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
    characters: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=SALIENCE_MAX_CHARACTERS
    )
    """The figures the render singles out as characters to model (FR-EPI-9's explicit salience;
    #163), each by the words of a beat that name it. Empty (the default) models nobody."""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    """How sure the model is of this split (optional, #227). Below the deployment's
    ``uncertain_below`` the answer is uncertain (FR-MODEL-7): no answer under
    ``commitment.salience_bias = balanced``, every span salient under ``recall``."""


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
    external_data: Annotated[list[str], C.EXTERNAL_DATA] = Field(
        default_factory=list, max_length=64
    )
    """#208: provider values the passage was written from, as fenced data blocks (data, never
    instructions; the claims are the passage's own)."""


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


TIME_ESTIMATE_MAX_MINUTES = 525_960 * 10
"""The longest duration one ``time_estimation`` answer may propose (ten years: the render
schema's ``MAX_SKIP_MINUTES``)."""
TIME_ESTIMATION_TEXT_MAX = 2_000
"""The longest narrated text, or activity context, one ``time_estimation`` request carries."""
TIME_ESTIMATION_MAX_TIMES = 20
"""The most established times one ``time_estimation`` request carries."""
CLOCK_TIME_PATTERN = r"^([0-9]{1,2}:[0-5][0-9])?$"
"""A time of day as ``HH:MM`` (empty: none), in a ``time_estimation`` request or answer."""


class TimeEstimationInputs(InputSchema):
    """One undeclared narrated skip (FR-CLOCK-7; #199): the sentence that implies elapsed time,
    what the scene established about the activity, the times the story established ("the movie
    started at 19:10 and runs about two hours") and the clock as the turn starts ("19:40",
    #201). Never a definition, a memory, a policy, an age, a provider's value or eval text."""

    narrated_text: Annotated[str, C.RENDER_TEXT] = Field(max_length=TIME_ESTIMATION_TEXT_MAX)
    activity_context: Annotated[str, C.SCENE] = Field(
        default="", max_length=TIME_ESTIMATION_TEXT_MAX
    )
    established_times: Annotated[list[str], C.SCENE] = Field(
        default_factory=list, max_length=TIME_ESTIMATION_MAX_TIMES
    )
    clock: Annotated[str, C.SCENE] = Field(
        default="", pattern=CLOCK_TIME_PATTERN, examples=["19:40"]
    )
    """The time of day as the turn starts ("19:40"; empty: the clock does not fix it). With it,
    the model may answer with a clock time and the engine does the arithmetic (#201)."""


class TimeEstimate(BaseModel):
    """The structured result of one ``time_estimation`` call, reason first so the model works
    the skip out before it commits to a time (#201). The time is either ``ends_at``, the clock
    time the skip ends at ("the alarm is set for 06:30", or a start plus how long the activity
    runs), from which the engine counts the minutes against the clock across midnight
    (``orchestrator.structured.time_estimation.resolved_minutes``); or ``minutes``, how long the
    skip lasts. The defaults (no time at confidence 0) are the most conservative answer: an
    estimate under ``core.world_clock.skips.MIN_CONFIDENCE`` is no estimate, and the skip is
    removed (NFR-PERF-7 (5))."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(default="", max_length=500)
    ends_at: str | None = Field(default=None, pattern=CLOCK_TIME_PATTERN)
    minutes: int | None = Field(default=None, ge=0, le=TIME_ESTIMATE_MAX_MINUTES)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


REWRITE_INSTRUCTION_MAX = 4_000
REWRITE_SUMMARY_MAX = 200
REWRITE_MAX_CHANGES = 20


class RewriteEntity(InputSchema):
    """One entity the instruction may name: its name, kind and aliases (#223)."""

    name: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=200)
    kind: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=40)
    aliases: Annotated[list[str], C.AUTHOR_INSTRUCTION] = Field(default_factory=list, max_length=16)


class RewriteState(InputSchema):
    """One State the instruction may correct: its name, type, scope, unit, aliases and owner."""

    name: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=80)
    type: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=20)
    scope: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=20)
    unit: Annotated[str | None, C.AUTHOR_INSTRUCTION] = Field(default=None, max_length=40)
    aliases: Annotated[list[str], C.AUTHOR_INSTRUCTION] = Field(default_factory=list, max_length=16)
    owner: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(default="story", max_length=20)


class RewriteParseInputs(InputSchema):
    """A history-rewrite instruction (FR-REWRITE-1,2; #223), the answers given so far and the
    story's names it is normalized against: the entities its mentions may bind, the registered
    predicates, the States and the clock. Never transcript text, a memory or a belief."""

    instruction: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=REWRITE_INSTRUCTION_MAX)
    answers: Annotated[list[str], C.AUTHOR_INSTRUCTION] = Field(default_factory=list, max_length=20)
    entities: Annotated[list[RewriteEntity], C.AUTHOR_INSTRUCTION] = Field(
        default_factory=list, max_length=REWRITE_SUMMARY_MAX
    )
    predicates: Annotated[list[str], C.AUTHOR_INSTRUCTION] = Field(
        default_factory=list, max_length=REWRITE_SUMMARY_MAX
    )
    states: Annotated[list[RewriteState], C.AUTHOR_INSTRUCTION] = Field(
        default_factory=list, max_length=REWRITE_SUMMARY_MAX
    )
    clock: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(default="", max_length=200)


class RewriteValid(BaseModel):
    """When a parsed change holds: ``from``/``to`` as the author put it (``null``: open)."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    from_: str | None = Field(default=None, alias="from", max_length=200)
    to: str | None = Field(default=None, max_length=200)


class RewriteRef(BaseModel):
    """A record an instruction points at: a turn (and beat), or a quote of its text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int | None = Field(default=None, ge=1)
    beat: int | None = Field(default=None, ge=0)
    quote: str | None = Field(default=None, max_length=2_000)
    event_id: SkipJsonSchema[str | None] = Field(default=None, exclude_if=lambda v: v is None)
    """#700: a line edit's own message (never in the model's schema: an author's direct
    target, resolved before the plan)."""


class RewriteChange(BaseModel):
    """One structured change the instruction asks for: a ``proposition`` (subject, predicate,
    object or literal, polarity), a ``record`` (its ``ref``, ``action`` and ``new_text``), a
    ``state`` (a State's ``value`` or ``delta``), each over ``valid``, or an ``intent`` (what
    the speaker of the line at ``ref`` meant, #225)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["proposition", "record", "state", "intent"]
    subject: str | None = Field(default=None, max_length=200)
    predicate: str | None = Field(default=None, max_length=120)
    object: str | None = Field(default=None, max_length=200)
    literal: str | float | bool | None = None
    polarity: Literal["affirmed", "negated"] = "affirmed"
    valid: RewriteValid = Field(default_factory=RewriteValid)
    ref: RewriteRef | None = None
    action: Literal["replace", "drop"] | None = None
    new_text: str | None = Field(default=None, max_length=4_000)
    state: str | None = Field(default=None, max_length=80)
    scope: str | None = Field(default=None, max_length=200)
    value: str | float | bool | None = None
    delta: float | None = None
    intent: Literal["sincere", "deceptive", "joking", "unspecified"] | None = None
    """An ``intent`` change (#225): what the speaker of the line at ``ref`` meant."""


class RewriteAmbiguity(BaseModel):
    """Something the model could not decide: what it is about, the question, the options and
    the one it recommends (an index into ``options``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    about: str = Field(max_length=200)
    question: str = Field(max_length=500)
    options: list[str] = Field(min_length=1, max_length=8)
    recommended: int = Field(default=0, ge=0)


class RewriteParseResult(BaseModel):
    """What ``rewrite_parse`` answers: the structured changes and the ambiguities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    changes: list[RewriteChange] = Field(default_factory=list, max_length=REWRITE_MAX_CHANGES)
    ambiguities: list[RewriteAmbiguity] = Field(default_factory=list, max_length=8)


REWRITE_RECORD_TEXT_MAX = 4_000


class RewriteRecordInputs(InputSchema):
    """One record a history rewrite must repair (FR-REWRITE-3; #224): its text, its kind, the
    value the rewrite replaces and the new canon, under the constraints (change only the
    conflicting span, keep speaker, voice and intent, length within 20 percent)."""

    record_text: Annotated[str, C.CHECKED_TEXT] = Field(max_length=REWRITE_RECORD_TEXT_MAX)
    record_kind: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=40)
    old_value: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(default="", max_length=500)
    new_canon: Annotated[list[str], C.AUTHOR_INSTRUCTION] = Field(
        default_factory=list, max_length=20
    )
    guidance: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(default="", max_length=2_000)


class RewrittenRecord(BaseModel):
    """What ``rewrite_record`` answers: the repaired text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(default="", max_length=REWRITE_RECORD_TEXT_MAX)


class DirectiveChange(BaseModel):
    """One change a durable directive makes (#226): a ``proposition`` (as a rewrite's), a
    ``state`` set (a value or a delta), a ``clock`` advance (``minutes`` or ``to``, a time of
    day "HH:MM"), a ``belief`` a character comes to hold (``holder`` and the proposition), or
    (#696) a scheduled ``event`` (``name``, ``planned``: minutes from the story's start or
    "HH:MM", ``participants``, ``place``), a ``reschedule`` of the ``event`` named to
    ``planned``, or a ``cancel_event``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["proposition", "state", "clock", "belief", "event", "reschedule", "cancel_event"]
    subject: str | None = Field(default=None, max_length=200)
    predicate: str | None = Field(default=None, max_length=120)
    object: str | None = Field(default=None, max_length=200)
    literal: str | float | bool | None = None
    polarity: Literal["affirmed", "negated"] = "affirmed"
    valid: RewriteValid = Field(default_factory=RewriteValid)
    state: str | None = Field(default=None, max_length=80)
    scope: str | None = Field(default=None, max_length=200)
    value: str | float | bool | None = None
    delta: float | None = None
    minutes: int | None = Field(default=None, ge=0)
    to: str | None = Field(default=None, pattern=CLOCK_TIME_PATTERN)
    holder: str | None = Field(default=None, max_length=200)
    confidence: Literal["suspected", "believed", "known"] | None = None
    qualifiers: dict[str, str | int | float | bool] | None = None
    """The proposition's key qualifiers (``{"degree_id": "bobby_ba"}``), when it has any."""
    name: str | None = Field(default=None, max_length=200)
    """#696: a new scheduled event's name."""
    event: str | None = Field(default=None, max_length=200)
    """#696: the scheduled event a ``reschedule`` or ``cancel_event`` names."""
    planned: int | str | None = None
    """#696: minutes from the story's start, or a time of day "HH:MM"."""
    participants: list[str] = Field(default_factory=list, max_length=20)
    place: str | None = Field(default=None, max_length=200)


class DirectiveBinding(BaseModel):
    """#241: who or what an open question of the scene is, as the author directs it ("the
    knocker is George"): the entity's name, and what it answers when the direction says."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity: str = Field(min_length=1, max_length=200)
    about: str | None = Field(default=None, max_length=200)


class DirectiveParseResult(BaseModel):
    """What ``directive_parse`` answers: ``ephemeral`` guidance (``steer_text``) or a
    ``durable`` change, about ``now`` or the ``past``, and the changes; on a directed
    regeneration (#241), the ``binding`` of an open question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: Literal["ephemeral", "durable"] = "ephemeral"
    temporal_scope: Literal["now", "past"] = "now"
    changes: list[DirectiveChange] = Field(default_factory=list, max_length=REWRITE_MAX_CHANGES)
    steer_text: str = Field(default="", max_length=2_000)
    binding: list[DirectiveBinding] = Field(default_factory=list, max_length=4)
    standing: bool = False
    """#696: the direction is standing guidance ("always describe the weather"): a steer for
    the next reply, with the hint to set it as custom instructions instead."""


class ImageCheckPolicy(InputSchema):
    """One image-output policy an ``image_check`` call judges: its id and its text."""

    policy_id: Annotated[str, StringConstraints(max_length=200), C.POLICY_TEXT]
    text: Annotated[str, StringConstraints(max_length=IMAGE_TEXT_MAX), C.POLICY_TEXT]


class ImageCheckInputs(InputSchema):
    """One ``image_check`` call (FR-IMG-3,13; #254, sent by #258): the image, its prompt,
    caption and alt text, the safety context's subjects and the image-output policies to judge.
    No story text, no beliefs, no name the prompt lacks."""

    image: Annotated[str, C.IMAGE]
    """The image as a data URL (#258: downscaled to at most 1024 px)."""
    prompt: Annotated[str, StringConstraints(max_length=IMAGE_TEXT_MAX), C.IMAGE_PROMPT] = ""
    caption: Annotated[str, StringConstraints(max_length=2_000), C.CAPTION_ALT_TEXT] = ""
    alt_text: Annotated[str, StringConstraints(max_length=2_000), C.CAPTION_ALT_TEXT] = ""
    subjects: list[ImageSubject] = Field(default_factory=list, max_length=64)
    policies: list[ImageCheckPolicy] = Field(default_factory=list, max_length=64)


type ImageVerdict = Literal["pass", "violation", "uncertain"]


class ImageCheckVerdict(BaseModel):
    """``image_check``'s answer: a verdict per policy id. An omitted id, or a value outside
    the three, counts as ``uncertain`` (#258). The free-form map has no strict wire form on
    api.anthropic.com, so that endpoint is sent this call as parse-validate-retry (#761)."""

    model_config = ConfigDict(extra="forbid")

    verdicts: dict[str, ImageVerdict] = Field(default_factory=dict)


class ImagePromptSubject(InputSchema):
    """A subject as the image's audience perceives it (FR-IMG-5,12): its visual identity, its
    apparent age band and its catalog base appearance. Never a true age or a restricted
    reference."""

    description: Annotated[str, StringConstraints(max_length=4_000), C.VISUAL_DESCRIPTOR]
    age_band: Annotated[str, StringConstraints(max_length=64), C.VISUAL_DESCRIPTOR] = ""
    base_appearance_prompt: Annotated[
        str, StringConstraints(max_length=2_000), C.VISUAL_DESCRIPTOR
    ] = ""


class ImagePromptInputs(InputSchema):
    """One ``image_prompt`` call (FR-IMG-2; #254, built by #259 and #263): what the audience
    perceived of the subjects and the scene, the visual style, the rating's image guidance and
    what the image is for (#259's ``orchestrator.images.prompt_builder`` builds it). The
    ``base_appearance`` variant (``image_prompt_base_appearance.j2``) drafts a character's base
    appearance prompt from ``subjects[0].description`` alone (FR-CAT-4)."""

    subjects: list[ImagePromptSubject] = Field(default_factory=list, max_length=16)
    scene: Annotated[str, StringConstraints(max_length=IMAGE_TEXT_MAX), C.SCENE] = ""
    visual_style: Annotated[str, StringConstraints(max_length=2_000), C.VISUAL_DESCRIPTOR] = ""
    rating_image_guidance: Annotated[
        str, StringConstraints(max_length=4_000), C.RATING_GUIDANCE
    ] = ""
    purpose: Annotated[str, StringConstraints(max_length=200), C.CONSTRAINT] = ""
    """What the image is for (a catalog portrait, a story image at a position)."""


IMAGE_PROMPT_MAX = 2_000
"""The longest prompt ``image_prompt`` may answer (#259)."""


def _not_blank_prompt(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class ImagePromptResult(BaseModel):
    """``image_prompt``'s answer: the prompt the adapter generates from (1 to 2,000
    characters, never blank). The negative prompt, seed and references never pass through the
    model (#259)."""

    model_config = ConfigDict(extra="forbid")

    prompt: Annotated[
        str, Field(min_length=1, max_length=IMAGE_PROMPT_MAX), AfterValidator(_not_blank_prompt)
    ]

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Any]) -> dict[str, Any]:
        """The stub's default answer: a prompt named by the request's hash (#254)."""
        return {"prompt": f"stub image {digest[:12]}"}


class ImageGenerationInputs(InputSchema):
    """What the image adapter receives (FR-IMG-1,13; the ``image_generation`` port): the built
    prompt, its parameters and the perceivable reference images. Never a true age or the safety
    context (FR-IMG-12)."""

    prompt: Annotated[str, C.IMAGE_PROMPT]
    negative_prompt: Annotated[str | None, C.IMAGE_PROMPT] = None
    seed: Annotated[int | None, C.IMAGE_PROMPT] = None
    size: Annotated[str | None, C.IMAGE_PROMPT] = None
    aspect_ratio: Annotated[str | None, C.IMAGE_PROMPT] = None
    quality: Annotated[str | None, C.IMAGE_PROMPT] = None
    n: Annotated[int, C.IMAGE_PROMPT] = 1
    reference_images: Annotated[list[str], C.REFERENCE_IMAGE] = Field(
        default_factory=list, max_length=4
    )


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
class ImageGenerationFloor:
    """The recommended minimum an image adapter's model must meet (FR-MODEL-10 for the port,
    #254): it generates images from a text prompt. What else it supports (seeds, reference
    images, sizes) is discovered per endpoint, never assumed (FR-IMG-1, #255)."""

    generates_images: bool = True


type PortFloor = EmbeddingFloor | ImageGenerationFloor


@dataclass(frozen=True)
class Port[F: PortFloor]:
    """A delivered port (FR-MODEL-2's separately ported capability): configured through its
    own ``model_config`` row (``default_model_key``, its own name, with no fallback to the
    ``default`` row) and called through its own adapter, never as a chat completion."""

    name: str
    description: str
    default_model_key: str
    floor: F
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
        "`response_schema`: constrained decoding or parse-validate-retry), the age repair, "
        "the composition of the turn's verified beats into its message (#152), the director's "
        "ordering of a sequential turn's primaries (#174) and its judgment of a simultaneous "
        "turn's conflicting actions (#175)."
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
            "director_order": "director_order.j2",
            "director_tiebreak": "director_tiebreak.j2",
        }
    ),
)
"""``generation``: the Lite reply (``lite_generation.j2``) and, under ``structured`` (#150), the
character render (``structured_generation.j2``), the narrator render
(``narrator_generation.j2``), the age repair (``age_repair.j2``), the composition of the
turn's verified beats into its message (``composition.j2``, #152), the director's ordering of
a sequential turn's primaries (``director_order.j2``, #174) and its tie-break over a
simultaneous turn's conflicting actions (``director_tiebreak.j2``, #175). The structured path
needs
``json_schema`` or ``parse_validate_retry`` (its beats are a ``response_schema``)."""

GUARDRAIL_CHECK = Purpose(
    name="guardrail_check",
    description="Checks one text against one model-assisted policy (FR-GUARD-9).",
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=GuardrailCheckInputs,
    template="guardrail_check.j2",
    variants=types.MappingProxyType({FIGURES_VARIANT: FIGURES_TEMPLATE}),
    hot_path=True,
    never_receives=_EVERYTHING
    - {
        C.CHECKED_TEXT,
        C.POLICY_TEXT,
        C.SUBJECT_TRUE_AGE,
        C.EXTERNAL_DATA,
        C.SUBJECT_LABEL,
        C.VISUAL_DESCRIPTOR,
    },
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

BELIEF_ADJUDICATION = Purpose(
    name="belief_adjudication",
    description=(
        "Decides what one salient witness makes of a say it heard: accept, reject, suspend or "
        "only note that it was said, from its own records, its trust in the speaker and the "
        "intent it reads; optional work within the turn budget, defaulted by the rules "
        "otherwise (FR-BELIEF-3)."
    ),
    # FR-MODEL-2: defaults to the entailment model (its own row, then entailment's, then the
    # default's).
    default_model_key=ENTAILMENT.name,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=BeliefAdjudicationInputs,
    template="belief_adjudication.j2",
    hot_path=True,
    # The witness's own view and the say it heard only: never another character's private
    # record, canon the witness cannot know, a definition, a memory, a policy, an age, a
    # transcript or eval text.
    never_receives=_EVERYTHING - {C.BELIEF_RECORD, C.UTTERANCE_TEXT},
    response_schema=BeliefAdjudicationResult,
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
    # The render's own beats (or the composed message), its scene and the provider values it
    # was written from (#208, fenced data) only: never another story's text, a belief, a
    # definition, a memory, a policy, an age or eval text.
    never_receives=_EVERYTHING - {C.RENDER_TEXT, C.SCENE, C.EXTERNAL_DATA},
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

TIME_ESTIMATION = Purpose(
    name="time_estimation",
    description=(
        "Proposes a best-guess duration, with its reason and a confidence, for a narrated skip "
        "a beat implies but does not declare, from what the scene established; optional work "
        "within the turn budget (FR-CLOCK-7)."
    ),
    # FR-MODEL-2: defaults to the extraction backstop's model (its own row, then the
    # backstop's, then the default's).
    default_model_key=EXTRACTION_BACKSTOP.name,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=TimeEstimationInputs,
    template="time_estimation.j2",
    hot_path=True,
    # The skip's sentence and what the scene established only: never a definition, a belief, a
    # memory, a policy, an age, a provider's value (#208: ``external_data`` stays out) or eval
    # text.
    never_receives=_EVERYTHING - {C.RENDER_TEXT, C.SCENE},
    response_schema=TimeEstimate,
)
"""``time_estimation`` (#199): one call per undeclared narrated skip
(``orchestrator.structured.time_estimation.estimate``)."""

REWRITE_PARSE = Purpose(
    name="rewrite_parse",
    description=(
        "Parses an author's history-rewrite instruction into structured proposition, record "
        "and State changes, and names what it cannot decide, on the author's side-channel, "
        "never in a turn (FR-REWRITE-1,2)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=RewriteParseInputs,
    template="rewrite_parse.j2",
    hot_path=False,
    # The instruction, the answers and the names it is normalized against only: never
    # transcript text, a memory, a belief, a definition, a policy, an age or eval text.
    never_receives=_EVERYTHING - {C.AUTHOR_INSTRUCTION},
    response_schema=RewriteParseResult,
    # The author side-channel: not routed by a story's profile, so a story's runtime facts and
    # certifications are unchanged by it (#223's deviation, logged).
    story_scoped=True,
)
"""``rewrite_parse`` (#223): one call per opened session and per free-text answer
(``orchestrator.rewrite.parse``)."""

REWRITE_RECORD = Purpose(
    name="rewrite_record",
    description=(
        "Repairs one record a history rewrite contradicts: the least change that makes its "
        "text agree with the new canon, keeping its speaker, voice and intent, on the author's "
        "side-channel, never in a turn (FR-REWRITE-3)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=RewriteRecordInputs,
    template="rewrite_record.j2",
    hot_path=False,
    # The record being repaired and the change: never another record, a memory, a belief, a
    # definition, a policy, an age or eval text.
    never_receives=_EVERYTHING - {C.AUTHOR_INSTRUCTION, C.CHECKED_TEXT},
    response_schema=RewrittenRecord,
    story_scoped=True,
)
"""``rewrite_record`` (#224): one call per conflicting record the apply rewrites
(``orchestrator.rewrite.apply``)."""

DIRECTIVE_PARSE = Purpose(
    name="directive_parse",
    description=(
        "Classifies an author's directive as guidance for the next turn or a change to the "
        "world, now or in the past, and parses its changes, on the author's side-channel, "
        "never in a turn (FR-DIRECT-1,2)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=RewriteParseInputs,
    template="directive_parse.j2",
    hot_path=False,
    never_receives=_EVERYTHING - {C.AUTHOR_INSTRUCTION},
    response_schema=DirectiveParseResult,
    story_scoped=True,
)
"""``directive_parse`` (#226): one call per directive whose kind or changes the request does
not settle (``orchestrator.directives``)."""


class LiteReachLine(BaseModel):
    """One transcript line a Lite reach pass reads: its turn, speaker and text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int
    speaker: str = Field(max_length=200)
    text: str = Field(max_length=REWRITE_RECORD_TEXT_MAX)


LITE_REACH_LINES_MAX = 200


class LiteRewriteReachInputs(InputSchema):
    """A Lite history rewrite's correction (FR-LITE-8; #245) and the transcript lines it is
    searched against (the active chain, newest first, at most ``LITE_REACH_LINES_MAX``)."""

    instruction: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=REWRITE_INSTRUCTION_MAX)
    lines: Annotated[list[LiteReachLine], C.RECENT_MESSAGE] = Field(
        default_factory=list, max_length=LITE_REACH_LINES_MAX
    )


class LiteReachHit(BaseModel):
    """A line the correction touches: where it is, how, and its fix (``null``: none clean)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int = Field(ge=1)
    quote: str = Field(max_length=2_000)
    relation: Literal["conflicts", "mentions"]
    fix_text: str | None = Field(default=None, max_length=REWRITE_RECORD_TEXT_MAX)
    reason: str = Field(default="", max_length=300)


class LiteRewriteReachResult(BaseModel):
    """What ``lite_rewrite_reach`` answers: the lines the correction touches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hits: list[LiteReachHit] = Field(default_factory=list, max_length=50)


LITE_REWRITE_REACH = Purpose(
    name="lite_rewrite_reach",
    description=(
        "Finds the lines of a Lite story's transcript a correction to its past contradicts or "
        "mentions and proposes the least fix of each, on the author's side-channel, never in a "
        "turn (FR-LITE-8): the Lite reach is model-searched, not structurally complete."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=LiteRewriteReachInputs,
    template="lite_rewrite_reach.j2",
    hot_path=False,
    never_receives=_EVERYTHING - {C.AUTHOR_INSTRUCTION, C.RECENT_MESSAGE},
    response_schema=LiteRewriteReachResult,
    story_scoped=True,
)
"""``lite_rewrite_reach`` (#245): one call per Lite rewrite session the author opens with an
instruction (``orchestrator.rewrite.lite``)."""

LITE_PARSE_RECENT_MAX = 10
"""#700: the latest transcript lines ``lite_rewrite_parse`` reads with the instruction."""
LITE_PARSE_FACTS_MAX = 50


class LiteRewriteParseInputs(InputSchema):
    """A Lite history rewrite's instruction (FR-LITE-8; #245, #700), the answers so far, the
    latest lines (newest first, at most ``LITE_PARSE_RECENT_MAX``) and the story's lasting
    author facts: what the change statement is read from."""

    instruction: Annotated[str, C.AUTHOR_INSTRUCTION] = Field(max_length=REWRITE_INSTRUCTION_MAX)
    answers: Annotated[list[str], C.AUTHOR_INSTRUCTION] = Field(default_factory=list, max_length=20)
    recent: Annotated[list[LiteReachLine], C.RECENT_MESSAGE] = Field(
        default_factory=list, max_length=LITE_PARSE_RECENT_MAX
    )
    author_facts: Annotated[list[str], C.AUTHOR_INSTRUCTION] = Field(
        default_factory=list, max_length=LITE_PARSE_FACTS_MAX
    )


class LiteRewriteParseResult(BaseModel):
    """What ``lite_rewrite_parse`` answers: the correction as one plain statement (``""``: the
    instruction stands as given), whether it changes the story's world (a ``lite_note`` part,
    "Author correction: ..."), and what it could not decide (each a clarification question
    with a recommended option)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_statement: str = Field(default="", max_length=REWRITE_INSTRUCTION_MAX)
    world_change: bool = False
    ambiguities: list[RewriteAmbiguity] = Field(default_factory=list, max_length=8)


LITE_REWRITE_PARSE = Purpose(
    name="lite_rewrite_parse",
    description=(
        "Reads a Lite story's history-rewrite instruction as one change statement, says whether "
        "it changes the world, and asks what it cannot decide, on the author's side-channel, "
        "never in a turn (FR-LITE-8)."
    ),
    default_model_key=DEFAULT_MODEL_KEY,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=LiteRewriteParseInputs,
    template="lite_rewrite_parse.j2",
    hot_path=False,
    never_receives=_EVERYTHING - {C.AUTHOR_INSTRUCTION, C.RECENT_MESSAGE},
    response_schema=LiteRewriteParseResult,
    story_scoped=True,
)
"""``lite_rewrite_parse`` (#700): one call per Lite rewrite session the author opens with an
instruction, and one per answer that re-reads it (``orchestrator.rewrite.lite``)."""

IMAGE_PROMPT = Purpose(
    name="image_prompt",
    description=(
        "Writes an image prompt from what the image's audience has perceived: the subjects' "
        "visual identities and apparent age bands, the scene, the visual style and the rating's "
        "image guidance (FR-IMG-2). Catalog candidates (#259) and story images (#263)."
    ),
    default_model_key=GENERATION.name,
    capability_floor=CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY),
    input_schema=ImagePromptInputs,
    template="image_prompt.j2",
    variants=types.MappingProxyType({"base_appearance": "image_prompt_base_appearance.j2"}),
    hot_path=False,
    # What the audience perceived, visually: never a true age, a policy, a belief or story text.
    never_receives=_EVERYTHING - {C.VISUAL_DESCRIPTOR, C.SCENE, C.RATING_GUIDANCE, C.CONSTRAINT},
    response_schema=ImagePromptResult,
    story_scoped=False,
)
"""``image_prompt`` (#254): defaults to ``generation``'s row (then the ``default`` row). It runs
for catalog entries outside any story, so no engine profile routes it (#121's rule). Its
``base_appearance`` variant drafts a character's base appearance prompt (#259)."""

IMAGE_CHECK = Purpose(
    name="image_check",
    description=(
        "Checks one image against the image-output policies, with its prompt, caption, alt "
        "text and the safety context's subject labels, true ages and visual descriptors "
        "(FR-IMG-3,13). A vision-capable model; no default: unset, every image-output policy "
        "evaluation fails closed (`image_check_unconfigured`)."
    ),
    default_model_key="image_check",
    capability_floor=CapabilityFloor(
        structured_output=StructuredOutput.PARSE_VALIDATE_RETRY, vision=True
    ),
    input_schema=ImageCheckInputs,
    template="image_check.j2",
    hot_path=False,
    never_receives=_EVERYTHING
    - {
        C.IMAGE,
        C.IMAGE_PROMPT,
        C.CAPTION_ALT_TEXT,
        C.POLICY_TEXT,
        C.SUBJECT_LABEL,
        C.SUBJECT_TRUE_AGE,
        C.VISUAL_DESCRIPTOR,
    },
    response_schema=ImageCheckVerdict,
    story_scoped=False,
)
"""``image_check`` (#254; called by #258): its own row only, never the ``default`` row (a
vision checker is not the house chat model), so an unconfigured deployment reports it
``unset``. It checks catalog candidates and uploads outside any story, so no engine profile
routes it."""

RESERVED_PURPOSES: tuple[ReservedPurpose, ...] = ()
"""Empty since M8 (#254 delivered the last three); tests register one to keep the refusal of a
reserved purpose covered (``tests/model_support.py::reserved_purpose``)."""


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

IMAGE_GENERATION = Port(
    name="image_generation",
    description=(
        "The image adapter (FR-IMG-1): generates images through the `ImageGenerator` port "
        "(`orchestrator.images.port`) with the adapter its row names (`params.adapter`; "
        "OpenRouter is bundled, #255). With no adapter, model or key, every generation entry "
        "point answers 409 `not_configured` and uploads keep working."
    ),
    default_model_key="image_generation",
    floor=ImageGenerationFloor(),
    input_schema=ImageGenerationInputs,
    hot_path=False,
    # The built prompt, its parameters and perceivable references: never the safety context.
    never_receives=_EVERYTHING - {C.IMAGE_PROMPT, C.REFERENCE_IMAGE},
)

PORTS: Mapping[str, Port[PortFloor]] = types.MappingProxyType(
    {EMBEDDING.name: EMBEDDING, IMAGE_GENERATION.name: IMAGE_GENERATION}
)
"""The ports delivered so far, by name (configurable like purposes, never chat-completed)."""


def validate_port(port: Port[PortFloor]) -> None:
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
        if parent not in (DEFAULT_MODEL_KEY, purpose.name) and (
            parent not in built or built[parent].default_model_key != DEFAULT_MODEL_KEY
        ):
            raise RegistryError(
                f"{purpose.name}: defaults to {parent!r}, which is not its own row or a "
                "delivered purpose declared before it that defaults to the default row"
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
        BELIEF_ADJUDICATION,
        SALIENCE_DETECTION,
        EXTRACTION_BACKSTOP,
        REGENERATION,
        TIME_ESTIMATION,
        REWRITE_PARSE,
        REWRITE_RECORD,
        DIRECTIVE_PARSE,
        LITE_REWRITE_REACH,
        LITE_REWRITE_PARSE,
        IMAGE_PROMPT,
        IMAGE_CHECK,
    )
)
"""Purposes a model call may use now, by name."""

OWN_ROW_ONLY: frozenset[str] = frozenset(
    {*PORTS, *(name for name, purpose in DELIVERED.items() if purpose.default_model_key == name)}
)
"""The ports and the purposes that read their own ``model_config`` row only, never the
``default`` row (``image_check``, #254): unset until an operator configures them."""

SAFETY_PURPOSES: tuple[str, ...] = (GUARDRAIL_CHECK.name, IMAGE_CHECK.name)
"""The image safety path's purposes (FR-IMG-13): their guide rows carry
``HOSTED_SAFETY_RETENTION``."""

HOSTED_SAFETY_RETENTION = (
    "When this purpose is routed to a hosted provider, the images and true ages it receives "
    "leave the deployment, are kept under that provider's terms and are outside Epistrel's "
    "erasure boundary; to keep them in-house, route both `guardrail_check` and `image_check` "
    "to self-hosted models."
)
"""FR-IMG-13's retention sentence, verbatim in both safety purposes' guide rows."""

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
    "ADJUDICATION_MAX_CLAIMS",
    "ADJUDICATION_MAX_RECORDS",
    "BELIEF_ADJUDICATION",
    "CLOCK_TIME_PATTERN",
    "CONSOLIDATION",
    "CONSOLIDATION_MAX_RECORDS",
    "CONSOLIDATION_STATEMENTS_MAX",
    "DEFAULT_MODEL_KEY",
    "DELIVERED",
    "DIRECTIVE_PARSE",
    "EMBEDDING",
    "ENTAILMENT",
    "ENTAILMENT_MAX_RECORDS",
    "EXTRACTION_BACKSTOP",
    "EXTRACTION_MAX_BEATS",
    "EXTRACTION_MAX_CLAIMS",
    "EXTRACTION_MIN_CONFIDENCE",
    "FIGURES_TEMPLATE",
    "FIGURES_VARIANT",
    "HOSTED_SAFETY_RETENTION",
    "IMAGE_CHECK",
    "IMAGE_GENERATION",
    "IMAGE_PROMPT",
    "IMAGE_PROMPT_MAX",
    "IMPORTANCE_SCORING",
    "LITE_PARSE_FACTS_MAX",
    "LITE_PARSE_RECENT_MAX",
    "LITE_REWRITE_PARSE",
    "LITE_REWRITE_REACH",
    "OWN_ROW_ONLY",
    "PORTS",
    "QUALITY_JUDGE",
    "RATING_SUGGESTION",
    "REGENERATION",
    "REGENERATION_MAX_RECORDS",
    "RESERVED",
    "REWRITE_PARSE",
    "REWRITE_RECORD",
    "RULE_DEFAULT_MARKER",
    "SAFETY_PURPOSES",
    "SALIENCE_DETECTION",
    "SALIENCE_LAST_BEATS",
    "SALIENCE_MAX_BEATS",
    "SALIENCE_MAX_SPANS",
    "SALIENCE_QUOTE_MAX",
    "STORY_PURPOSES",
    "SUBJECT_LABEL_PATTERN",
    "TIME_ESTIMATE_MAX_MINUTES",
    "TIME_ESTIMATION",
    "TIME_ESTIMATION_MAX_TIMES",
    "TIME_ESTIMATION_TEXT_MAX",
    "AdjudicationBelief",
    "AdjudicationDecision",
    "AdjudicationPriors",
    "AdjudicationUtterance",
    "AgeClue",
    "BeliefAdjudicationInputs",
    "BeliefAdjudicationResult",
    "CapabilityFloor",
    "CharacterDefinition",
    "CheckedImageSubject",
    "ClassifiedFigure",
    "ConsolidationInputs",
    "ConsolidationRecord",
    "ConsolidationResult",
    "ConsolidationStatement",
    "ConstraintKind",
    "ContentCategory",
    "DirectiveChange",
    "DirectiveParseResult",
    "DirectorCandidate",
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
    "FigureClassification",
    "GenerationInputs",
    "GuardrailCheckInputs",
    "GuardrailVerdict",
    "ImageCheckInputs",
    "ImageCheckPolicy",
    "ImageCheckVerdict",
    "ImageGenerationFloor",
    "ImageGenerationInputs",
    "ImagePromptInputs",
    "ImagePromptResult",
    "ImagePromptSubject",
    "ImageSubject",
    "ImageVerdict",
    "ImportanceScore",
    "ImportanceScoringInputs",
    "InputSchema",
    "JudgeCriteria",
    "JudgePair",
    "KnownHit",
    "LiteReachHit",
    "LiteReachLine",
    "LiteRewriteParseInputs",
    "LiteRewriteParseResult",
    "LiteRewriteReachInputs",
    "LiteRewriteReachResult",
    "LiteSummaryInputs",
    "Modality",
    "PlayerInput",
    "Port",
    "PortFloor",
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
    "RewriteChange",
    "RewriteParseInputs",
    "RewriteParseResult",
    "RewriteRecordInputs",
    "RewrittenRecord",
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
    "SubjectClass",
    "SubjectDescriptors",
    "SuggestedConcern",
    "SuggestedElement",
    "SuggestedLocation",
    "SuggestionElement",
    "SuggestionPolicy",
    "TiebreakBeat",
    "TiebreakConflict",
    "TimeEstimate",
    "TimeEstimationInputs",
    "TranscriptLine",
    "declared_paths",
    "field_categories",
    "get_purpose",
    "schema_categories",
    "validate_port",
    "validate_purpose",
]
