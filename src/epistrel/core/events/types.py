"""The M2 event types (#45). Most are minimal shells that the story owning them extends.

Categories follow FR-STORE-25: what the player and the characters say, do and think is
``narrative_state``; the player's turn input is ``input``; summaries are ``derived``; rejected
outputs are ``audit``; truncate, untruncate, swap, fork, settings and story/character setup are
``control``.
"""

from __future__ import annotations

from pydantic import Field, JsonValue

from epistrel.core.enums import EventCategory
from epistrel.core.events.registry import EventPayload, register


class CastMember(EventPayload):
    """A character as the genesis event records it: never its age (FR-CAT-12)."""

    id: str
    name: str
    is_player: bool


@register("story_created", category=EventCategory.CONTROL)
class StoryCreated(EventPayload):
    """The genesis event of every story (#50): its name, cast, settings and rating version.

    The cast carries ids, names and ``is_player`` only. True ages live only in
    ``characters.true_age``, so no view built from the log can disclose them.
    """

    name: str | None = None
    characters: list[CastMember] = Field(default_factory=list)
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    content_rating_id: str | None = None
    rating_version_id: str | None = None


@register("story_archived", category=EventCategory.CONTROL)
class StoryArchivedEvent(EventPayload):
    """The story became read-only (#50); a control event with no revision bump."""


@register("story_unarchived", category=EventCategory.CONTROL)
class StoryUnarchivedEvent(EventPayload):
    """The story is writable again (#50)."""


@register("story_deleted", category=EventCategory.AUDIT)
class StoryDeletedEvent(EventPayload):
    """A story was deleted (#50): written by a story-less operation, so it outlives the story;
    the operation carries the acting principal. Holds no story content. #56's deployment
    ``audit_log`` becomes the home for this record.
    """

    story_id: str


@register("character_defined", category=EventCategory.CONTROL)
class CharacterDefined(EventPayload):
    """A character joins the story (shell; #50 extends)."""

    character_id: str
    name: str


@register("player_input", category=EventCategory.INPUT)
class PlayerInput(EventPayload):
    """What the acting character said and did this turn: the turn's first beat (FR-RESP-9).

    ``text`` is ``say`` and ``do`` joined with a newline (what the gate checked and the
    transcript shows); ``say``/``do`` keep the two apart for the prompt (#57). The event's
    ``character_id`` is the actor."""

    text: str
    say: str | None = None
    do: str | None = None


@register("message_rendered", category=EventCategory.NARRATIVE_STATE)
class MessageRendered(EventPayload):
    """The finished reply of a turn (#57): stored verbatim, with the characters it voices, the
    model that wrote it, the SHA-256 of the prompt it answered (the rendered chat messages'
    canonical JSON) and the provider's finish reason. ``character_id`` is null for a combined
    reply of several characters."""

    text: str
    render_character_ids: tuple[str, ...] = ()
    model: str | None = None
    prompt_hash: str | None = None
    finish_reason: str | None = None


@register("think", category=EventCategory.NARRATIVE_STATE)
class Think(EventPayload):
    """A character's private thought (shell)."""

    text: str


@register("latent", category=EventCategory.NARRATIVE_STATE)
class Latent(EventPayload):
    """Unrendered narrative state (shell)."""

    text: str


class Head(EventPayload):
    """A story's head: the next turn index and the active variant of every turn.

    The same shape as the ``story_heads`` projection row; membership-changing control events
    carry the head they leave behind, so the projection follows them without re-reading.
    """

    next_turn_index: int = Field(ge=1)
    active_variants: dict[str, int] = Field(default_factory=dict)


@register("truncate", category=EventCategory.CONTROL)
class Truncate(EventPayload):
    """Discard every operation positioned after ``to_turn`` (#65 owns the operation).

    ``operation_ids`` are the operations whose membership became ``discarded``; ``prior_head``
    is the head before the truncation.
    """

    to_turn: int = Field(ge=0)
    prior_head: Head
    operation_ids: list[str]


@register("untruncate", category=EventCategory.CONTROL)
class Untruncate(EventPayload):
    """Reactivate the operations the truncation ``truncate_operation_id`` discarded (#65)."""

    truncate_operation_id: str
    operation_ids: list[str]
    restored_head: Head


@register("swap", category=EventCategory.CONTROL)
class Swap(EventPayload):
    """Make another continuation active from ``turn`` on (#66 owns the operation and may extend).

    ``activated_operation_ids`` become ``active``, ``dormant_operation_ids`` become ``dormant``,
    and ``active_head`` is the head afterwards.
    """

    turn: int = Field(ge=1)
    activated_operation_ids: list[str]
    dormant_operation_ids: list[str]
    active_head: Head


@register("fork", category=EventCategory.CONTROL)
class Fork(EventPayload):
    """Copy the history through ``through_turn`` into a new story (shell; #67 extends)."""

    new_story_id: str
    through_turn: int


@register("pinned_memories_set", category=EventCategory.CONTROL)
class PinnedMemoriesSet(EventPayload):
    """The story's pinned memories (shell; #60 extends)."""

    memories: list[str]


@register("custom_instructions_set", category=EventCategory.CONTROL)
class CustomInstructionsSet(EventPayload):
    """The story's custom instructions (shell; #60 extends)."""

    text: str


@register("config_changed", category=EventCategory.CONTROL)
class ConfigChanged(EventPayload):
    """One setting changed (shell; #64 extends)."""

    setting: str
    old: JsonValue = None
    new: JsonValue = None


@register("guardrail_output_rejected", category=EventCategory.AUDIT)
class GuardrailOutputRejected(EventPayload):
    """A generated output a guardrail rejected; audit only (shell; #68 extends)."""

    text: str
    reason: str


@register("lite_summary_segment", category=EventCategory.DERIVED)
class LiteSummarySegment(EventPayload):
    """A rolling-summary segment over earlier turns (shell; #72 extends)."""

    text: str
    through_turn: int
