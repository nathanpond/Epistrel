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


class ForkedFrom(EventPayload):
    """Where a fork came from (#67): the source story, the source revision the copy is equal
    to, the turn it runs through and the chain it followed (``None``: the active one). The one
    place a story names another, as provenance only: nothing resolves it."""

    story_id: str
    revision: int = Field(ge=0)
    at_turn: int = Field(ge=0)
    variant_chain: dict[str, int] | None = None


class ScenarioOrigin(EventPayload):
    """The scenario version a story was created from (#119): identifiers only. The version's
    document is the story's ``scenario_snapshot``, never copied into the log."""

    id: str
    version: int = Field(ge=1)


class CatalogSource(EventPayload):
    """The catalog character version a story character was created from (#119): its
    ``character_snapshots`` row holds the content, which the log never duplicates."""

    catalog_id: str
    version: int = Field(ge=1)


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
    forked_from: ForkedFrom | None = None
    """Set on a fork's own genesis (#67): the story, revision and turn it was copied from."""
    scenario: ScenarioOrigin | None = Field(default=None, exclude_if=lambda v: v is None)
    """Set on the genesis of a story created from a scenario (#119)."""


@register("story_archived", category=EventCategory.CONTROL)
class StoryArchivedEvent(EventPayload):
    """The story became read-only (#50); a control event with no revision bump."""


@register("story_unarchived", category=EventCategory.CONTROL)
class StoryUnarchivedEvent(EventPayload):
    """The story is writable again (#50)."""


@register("story_paused", category=EventCategory.CONTROL)
class StoryPausedEvent(EventPayload):
    """The story is paused while a check runs (FR-RATE-7; #118): ``job_id`` names the check,
    ``cause`` why (``rating_check``), and ``target_rating_id`` the rating a tightening asks
    for. A control event with no revision bump; truth-changing calls answer ``story_paused``
    until the matching ``story_resumed``."""

    job_id: str
    cause: str
    target_rating_id: str | None = None


@register("story_resumed", category=EventCategory.CONTROL)
class StoryResumedEvent(EventPayload):
    """The check that paused the story ended (#118): ``outcome`` is ``applied``, ``refused``
    or ``failed``. Never names a violating text."""

    job_id: str
    outcome: str


@register("story_deleted", category=EventCategory.AUDIT)
class StoryDeletedEvent(EventPayload):
    """A story was deleted (#50): written by a story-less operation, so it outlives the story;
    the operation carries the acting principal. Holds no story content. #56's deployment
    ``audit_log`` becomes the home for this record.
    """

    story_id: str


@register("character_defined", category=EventCategory.CONTROL)
class CharacterDefined(EventPayload):
    """A character joins a running story (FR-CAT-7; #114): an ad-hoc character the author adds,
    named as the genesis event names its cast (id, name, ``is_player``), never with its age or
    its definition, which live only in ``characters`` (FR-CAT-12). A character added in a story
    is never a player character. A story created from a scenario (#119) records one at genesis
    for every cast member, the player's included, with its role and catalog source."""

    character_id: str
    name: str
    is_player: bool = False
    role_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """The scenario role the character fills (#119's genesis records one event per cast
    member); ``None`` for a character added later."""
    source: CatalogSource | None = Field(default=None, exclude_if=lambda v: v is None)
    """The catalog version a genesis character was created from (its snapshot row)."""


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
    reply of several characters. ``fallback_used`` marks the safe fallback (#68)."""

    text: str
    render_character_ids: tuple[str, ...] = ()
    model: str | None = None
    prompt_hash: str | None = None
    finish_reason: str | None = None
    fallback_used: bool = False
    """The reply is the safe fallback line (#68): every generated draft broke an output policy
    (``model``/``prompt_hash`` null, ``finish_reason`` ``fallback``)."""
    present_character_ids: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)
    """A scenario's opening narration (turn 1 of a story created from a scenario, #119): the
    characters of the roles present when the story opens (stored; the Lite pipeline does not
    use it)."""


@register("think", category=EventCategory.NARRATIVE_STATE)
class Think(EventPayload):
    """A character's private thought (shell). ``character_id`` names whose it is (#59); a
    character viewpoint never receives it (FR-RESP-12)."""

    character_id: str | None = None
    text: str


@register("latent", category=EventCategory.NARRATIVE_STATE)
class Latent(EventPayload):
    """Unrendered narrative state (shell). ``character_id`` names whose it is (#59); a
    character viewpoint never receives it (FR-RESP-12)."""

    character_id: str | None = None
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
    """The source side of a fork (#67): the history through ``at_turn`` (along
    ``variant_chain``, ``None`` for the active chain) at source revision ``revision`` was copied
    into ``fork_story_id``. Written after the copy commits; it bumps no revision."""

    fork_story_id: str
    at_turn: int = Field(ge=0)
    revision: int = Field(ge=0)
    variant_chain: dict[str, int] | None = None


class PinnedItem(EventPayload):
    """One pinned memory: its server-minted id and its normalised text (#60)."""

    id: str
    text: str


@register("pinned_memories_set", category=EventCategory.CONTROL)
class PinnedMemoriesSet(EventPayload):
    """The story's pinned memories after an edit (FR-LITE-3, #60): the full ordered snapshot,
    plus the snapshot it replaced (``previous``) for audit."""

    items: tuple[PinnedItem, ...] = ()
    previous: tuple[PinnedItem, ...] = ()


@register("custom_instructions_set", category=EventCategory.CONTROL)
class CustomInstructionsSet(EventPayload):
    """The story's custom instructions after an edit (FR-LITE-3, #60); ``""`` clears them.
    ``previous`` is the text it replaced, for audit. Instructions are guidance, never facts."""

    text: str
    previous: str = ""


@register("config_changed", category=EventCategory.CONTROL)
class ConfigChanged(EventPayload):
    """One setting changed (shell; #64 extends)."""

    setting: str
    old: JsonValue = None
    new: JsonValue = None


class RejectedViolation(EventPayload):
    """One output policy a rejected text broke (#68)."""

    policy_id: str
    name: str
    message: str
    kind: str


@register("guardrail_output_rejected", category=EventCategory.AUDIT)
class GuardrailOutputRejected(EventPayload):
    """A text an output policy rejected (FR-GUARD-6, FR-STORE-25; #68): a generated draft
    (``reason`` ``output_policy``, ``attempt`` 0 for the first generation) or a safe fallback
    line that itself broke a policy (``reason`` ``fallback_policy``). Audit only: it holds the
    text, but no story view and no prompt reads it; it is written with its turn, so it follows
    the turn's membership (discarded with it on truncation). ``confidence`` is the guardrail
    model's, when a model-assisted check decided."""

    text: str
    reason: str
    attempt: int | None = Field(default=None, ge=0)
    policy_ids: tuple[str, ...] = ()
    violations: tuple[RejectedViolation, ...] = ()
    confidence: float | None = None


class SummarySource(EventPayload):
    """One input a summary segment was made from (FR-STORE-27; #72): a turn operation (level 0)
    or a child segment's operation (a merge), with the membership it had when it was read."""

    operation_id: str
    membership: str


@register("lite_summary_segment", category=EventCategory.DERIVED)
class LiteSummarySegment(EventPayload):
    """A Lite rolling-summary segment (FR-LITE-2, FR-STORE-27; #72): the summary of turns
    ``covers_from``..``covers_to``. Level 0 summarises transcript rows; level ``n + 1`` merges
    adjacent segments. Its operation (``consolidation``) is positioned at ``covers_to``, so the
    segment follows that turn's membership. ``source_versions`` are its inputs as read;
    ``model`` and ``prompt_hash`` say what produced it (FR-MEM-10)."""

    text: str
    covers_from: int = Field(ge=1)
    covers_to: int = Field(ge=1)
    level: int = Field(default=0, ge=0)
    source_versions: tuple[SummarySource, ...] = ()
    model: str | None = None
    prompt_hash: str | None = None
