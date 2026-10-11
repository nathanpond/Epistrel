"""The M2 event types (#45). Most are minimal shells that the story owning them extends.

Categories follow FR-STORE-25: what the player and the characters say, do and think is
``narrative_state``; the player's turn input is ``input``; summaries are ``derived``; rejected
outputs are ``audit``; truncate, untruncate, swap, fork, settings and story/character setup are
``control``.
"""

from __future__ import annotations

from pydantic import JsonValue

from epistrel.core.enums import EventCategory
from epistrel.core.events.registry import EventPayload, register


@register("story_created", category=EventCategory.CONTROL)
class StoryCreated(EventPayload):
    """The genesis event of every story (shell; #50 extends)."""

    title: str | None = None


@register("character_defined", category=EventCategory.CONTROL)
class CharacterDefined(EventPayload):
    """A character joins the story (shell; #50 extends)."""

    character_id: str
    name: str


@register("player_input", category=EventCategory.INPUT)
class PlayerInput(EventPayload):
    """What the player sent this turn (shell; #57 extends)."""

    text: str


@register("message_rendered", category=EventCategory.NARRATIVE_STATE)
class MessageRendered(EventPayload):
    """The reply the turn produced (shell; #57 extends)."""

    text: str


@register("think", category=EventCategory.NARRATIVE_STATE)
class Think(EventPayload):
    """A character's private thought (shell)."""

    text: str


@register("latent", category=EventCategory.NARRATIVE_STATE)
class Latent(EventPayload):
    """Unrendered narrative state (shell)."""

    text: str


@register("truncate", category=EventCategory.CONTROL)
class Truncate(EventPayload):
    """Discard every turn after ``after_turn`` (shell; #65 extends)."""

    after_turn: int


@register("untruncate", category=EventCategory.CONTROL)
class Untruncate(EventPayload):
    """Reactivate the turns a truncation discarded (shell; #65 extends)."""

    after_turn: int


@register("swap", category=EventCategory.CONTROL)
class Swap(EventPayload):
    """Make another continuation active from ``turn`` on (shell; #66 extends)."""

    turn: int


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
