"""The M2 event types (#45). Most are minimal shells that the story owning them extends.

Categories follow FR-STORE-25: what the player and the characters say, do and think is
``narrative_state``; the player's turn input is ``input``; summaries are ``derived``; rejected
outputs are ``audit``; truncate, untruncate, swap, fork, settings and story/character setup are
``control``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, JsonValue

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


class BeatProposition(EventPayload):
    """One proposition a beat asserts (#150): the normalized proposition (#140's
    ``Proposition`` document: ids, a literal, the polarity and the valid-time) and the verdict
    it ended with (#142: ``supported``, ``contradicted``, ``novel``, ``uncertain`` or
    ``denied``; ``None`` with verification off)."""

    proposition: dict[str, JsonValue]
    verdict: str | None = None
    claim_id: str | None = Field(default=None, exclude_if=lambda v: v is None)


class BeatVisibility(EventPayload):
    """A ``latent`` beat's declared scope (FR-TURN-4): ``objective`` (canon only),
    ``public`` (every character may know it) or ``characters`` (only those listed)."""

    scope: str = "objective"
    characters: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)


class BeatLabels(EventPayload):
    """An utterance's three independent labels (FR-TURN-6): the objective relation of what it
    asserts (#142's verdicts), the speaker's belief relation (``unknown`` until M5's partitions)
    and its communicative intent as declared and checked; under partitions (#166) also the
    verifier's ``classification`` and whether a declared lie was relabelled sincere
    (``intent_corrected``). Omniscient reads only, once classified."""

    objective_relation: str
    belief_relation: str = "unknown"
    intent: str = "unspecified"
    classification: str | None = None
    intent_corrected: bool = False


class DetectedRef(EventPayload):
    """A claim detection found in a beat that the beat did not declare (#148): its character
    span, category and verdict. Spans and codes only."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    category: str
    verdict: str | None = None


@register("player_input", category=EventCategory.INPUT)
class PlayerInput(EventPayload):
    """What the acting character said and did this turn: the turn's first beat (FR-RESP-9).

    ``text`` is ``say`` and ``do`` joined with a newline (what the gate checked and the
    transcript shows); ``say``/``do`` keep the two apart for the prompt (#57). The event's
    ``character_id`` is the actor."""

    text: str
    say: str | None = None
    do: str | None = None
    labels: BeatLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    """A structured turn (#150): the labels of the player's own ``say`` as an utterance
    (``intent`` ``unspecified``); absent from every Lite input, whose payload is unchanged."""
    propositions: tuple[BeatProposition, ...] = Field(default=(), exclude_if=lambda v: not v)
    """A structured turn (#150): the propositions detected in the input, with their verdicts."""
    mentions: tuple[dict[str, JsonValue], ...] = Field(default=(), exclude_if=lambda v: not v)
    """A structured turn (#150): the input's resolved mentions (#139)."""
    perception: dict[str, JsonValue] | None = Field(default=None, exclude_if=lambda v: not v)
    """The player's ``say``'s perception qualifiers (#177, from #161): ``whisper_to`` as the
    request named it, the targets not in the room dropped (``Qualifiers.payload``), so a history
    read re-routes the input like any beat. Absent when the request named none."""


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
    composed: bool = Field(default=False, exclude_if=lambda v: not v)
    """A structured turn's composed message (#150, #152): its verified beats as the turn's
    output viewpoint receives them, polished under the composition check or deterministically
    assembled. The beats are the record; working memory and the episodic writer read them,
    never this message, and a history read for another viewpoint recomposes it from them.
    Absent from every Lite reply."""
    present_character_ids: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)
    """A scenario's opening narration (turn 1 of a story created from a scenario, #119): the
    characters of the roles present when the story opens (stored; the Lite pipeline does not
    use it)."""
    interaction_mode: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """#175: ``simultaneous`` for a structured turn whose primaries rendered against one
    starting state (a history read routes its beats against the starting placements); absent
    otherwise."""
    perception_level: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """#177: the perception level (``co_location`` or ``full``) a structured turn's message was
    audience-filtered at; absent when it was not (perception ``none``, and every turn before
    #177: its beats are read as perceived by all). A history read recomposes the message when
    the story's level or player view is no longer the one it was composed under."""
    player_view: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """#177: the player view (``character`` or ``story``) the message was composed under, beside
    ``perception_level``; absent with it."""


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


class RecordRef(EventPayload):
    """A structured record a render's context held (FR-GUARD-5; #145): its ``table``
    (``memories`` or ``facts``) and id."""

    table: str
    id: str


@register("guardrail_output_rejected", category=EventCategory.AUDIT)
class GuardrailOutputRejected(EventPayload):
    """A text an output policy rejected (FR-GUARD-6, FR-STORE-25; #68): a generated draft
    (``reason`` ``output_policy``, ``attempt`` 0 for the first generation) or a safe fallback
    line that itself broke a policy (``reason`` ``fallback_policy``). Audit only: it holds the
    text, but no story view and no prompt reads it; it is written with its turn, so it follows
    the turn's membership (discarded with it on truncation). ``confidence`` is the guardrail
    model's, when a model-assisted check decided.

    Payload version 2 (#145, FR-GUARD-5) adds ``implicated_record_ids``: the memories and facts
    the rejected draft's context held. It is left out of the stored payload when empty (a Lite
    reply and a fallback line hold none), so a Lite turn's event is byte-identical to version
    1's, and a version-1 payload reads as empty."""

    text: str
    reason: str
    attempt: int | None = Field(default=None, ge=0)
    policy_ids: tuple[str, ...] = ()
    violations: tuple[RejectedViolation, ...] = ()
    confidence: float | None = None
    implicated_record_ids: tuple[RecordRef, ...] = Field(
        default=(), exclude_if=lambda refs: not refs
    )


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


@register("consolidation_output", category=EventCategory.DERIVED)
class ConsolidationOutput(EventPayload):
    """A consolidation window's outputs (FR-MEM-2,3,6, FR-STORE-27; #143): the semantic
    memories and reflections it wrote (``memory_ids``, possibly none) over turns
    ``from_turn``..``to_turn`` of ``scope`` at ``level``, and the earlier outputs it replaced
    (``superseded_ids``: a rerun's). Its operation (``consolidation``) is positioned at
    ``to_turn``, so the outputs follow that turn's membership. Identifiers and counts only."""

    memory_ids: tuple[str, ...] = ()
    superseded_ids: tuple[str, ...] = ()
    from_turn: int = Field(ge=1)
    to_turn: int = Field(ge=1)
    scope: str = "shared"
    level: int = Field(default=1, ge=1, le=2)


@register("erasure_marker", category=EventCategory.CONTROL)
class ErasureMarker(EventPayload):
    """Where erasure (FR-ADMIN-2,6; #124) deleted a structured record of the story: the
    ledger entry (``erasure_id``), the subject (``subject_kind``, ``subject_id``), the
    ``scope`` (``story``), and the record it removed (``record_table``, ``record_id``).
    Identifiers only, never content. Written by the erasure's ``control`` operation, which has
    no turn position, so a truncation never discards it; the audit view shows it as
    ``[erasure marker: <subject id>, <scope>]``."""

    erasure_id: str
    subject_kind: str
    subject_id: str
    scope: str
    record_table: str
    record_id: str


@register("fact_committed", category=EventCategory.DERIVED)
class FactCommitted(EventPayload):
    """A fact version written to ``facts`` (FR-BITEMP-1, FR-STORE-9; #133): the new version
    (``fact_id``, its ``lineage_id`` and ``proposition_id``), the versions it closed
    (``supersedes``) and the remainders it kept of them (``remainders``: what is left of a
    trimmed version on either side of the new valid-time). Identifiers only; the fact follows
    its operation's membership, so this event does too."""

    fact_id: str
    lineage_id: str
    proposition_id: str
    supersedes: tuple[str, ...] = ()
    remainders: tuple[str, ...] = ()


@register("fact_superseded", category=EventCategory.DERIVED)
class FactSuperseded(EventPayload):
    """An explicit supersede (FR-BITEMP-2,4; #133): version ``superseded_id`` of a fact was
    closed in transaction-time by ``fact_id`` (same ``lineage_id``), never deleted; ``authority``
    is the new version's (``author`` for a retcon). Identifiers only."""

    fact_id: str
    superseded_id: str
    lineage_id: str
    proposition_id: str
    authority: str
    supersedes: tuple[str, ...] = ()
    remainders: tuple[str, ...] = ()


@register("belief_committed", category=EventCategory.DERIVED)
class BeliefCommitted(EventPayload):
    """A belief record written to a holder's partition (FR-EPI-1,3,6; #159): the new row
    (``belief_id``) and its proposition, and the open belief of the same holder and proposition it
    closed (``closes``, at the new row's ``held_from``). Identifiers only, and no character's (the
    row names its holder); the
    belief follows its operation's membership, so this event does too. #160's
    ``belief_transition`` event joins it with the acquisition's cause."""

    belief_id: str
    proposition_id: str
    closes: str | None = None


@register("belief_transition", category=EventCategory.DERIVED)
class BeliefTransitioned(EventPayload):
    """A belief-set change with its typed cause (FR-EPI-7,13, FR-BELIEF-2; #160): the transition
    (``transition_id``), the belief it produced or changed, its ``kind``, its ``antecedent``
    (``{kind, ref}``) and the beliefs a correction closed (``prior_belief_ids``). Identifiers
    only; the transition follows its operation's membership, so this event does too."""

    transition_id: str
    belief_id: str
    kind: str
    antecedent: dict[str, Any]
    prior_belief_ids: tuple[str, ...] = ()


@register("exposure_recorded", category=EventCategory.DERIVED)
class ExposureRecorded(EventPayload):
    """A non-salient witness's exposure records (FR-BELIEF-5; #164): the exposures one beat
    wrote, by id. Identifiers only; each row names its witness, proposition, source and
    position."""

    exposure_ids: tuple[str, ...]


@register("belief_resolved", category=EventCategory.DERIVED)
class BeliefResolved(EventPayload):
    """An exposure resolved lazily inside the narrative turn that needed it (FR-BELIEF-7,12;
    #164): the exposure, the transition that resolved it (the first-order belief's, else the
    speech act's) and how (``defaulted`` or ``adjudicated``). Identifiers only; the transition
    names the belief, dated at the exposure position."""

    exposure_id: str
    transition_id: str
    resolution: str = "defaulted"


@register("character_weight_set", category=EventCategory.CONTROL)
class CharacterWeightSet(EventPayload):
    """The author set a character's narrative weight (FR-EPI-10; #163's
    ``POST /stories/{id}/characters/{cid}/weight``): ``minor`` or ``major``, and the weight it
    had. Prospective: what the character already holds is not rewritten."""

    character_id: str
    weight: str
    previous: str


@register("character_modeled", category=EventCategory.DERIVED)
class CharacterModeled(EventPayload):
    """A story entity became a modeled character (FR-EPI-9; #163): the trigger
    (``interaction`` or ``salience``), the entity (a story character's entity id is its
    character id) and the position it was modeled at (``at_turn``, ``at_variant``,
    ``at_slot_k``, ``at_beat``, ``at_step``). Identifiers only; the ``character_defined`` event
    beside it names the character."""

    trigger: str
    entity_id: str
    position: dict[str, int]


@register("belief_authored", category=EventCategory.CONTROL)
class BeliefAuthored(EventPayload):
    """An author directed a character's belief (FR-EPI-7's author direction; #160's
    ``POST /admin/stories/{id}/beliefs``): the author act an ``author`` transition cites. The
    holder is ``holder_character_id``; the belief and its transition carry the rest."""

    holder_character_id: str
    disclosure: str | None = None


@register("seed_materialized", category=EventCategory.CONTROL)
class SeedMaterialized(EventPayload):
    """A structured genesis seeded what characters start out believing (FR-SCN-8; #173): the
    author act each ``author`` transition of the seed cites. ``field_ref`` names the item (a
    seed, a relationship, the persona), ``seed_id`` the seed, ``holder_character_id`` its holder
    (``None`` for the world's), ``audience`` its tokens as written and ``disclosure`` how the
    holder treats it (what #166's lie rule reads). Identifiers and tokens only."""

    field_ref: str
    seed_id: str | None = None
    holder_character_id: str | None = None
    audience: tuple[str, ...] = ()
    disclosure: str | None = None


# -- commitments (FR-COMMIT-1..3,5..7,10,13; #146) ---------------------------------------------


@register("thunk_introduced", category=EventCategory.DERIVED)
class ThunkIntroduced(EventPayload):
    """A thunk was introduced (#146): ``thunk_id``, which slot of its proposition is open
    (``open_slot``) and whether a salient reference introduced it. Identifiers only; the sketch
    lives in the ``thunks`` row."""

    thunk_id: str
    open_slot: str
    salient: bool = False


@register("thunk_resolved", category=EventCategory.DERIVED)
class ThunkResolved(EventPayload):
    """A dependence resolved a thunk, once, globally (FR-COMMIT-2,10; #146): the committed
    ``fact_id``. Identifiers only."""

    thunk_id: str
    fact_id: str


@register("ledger_entry", category=EventCategory.DERIVED)
class LedgerEntryCreated(EventPayload):
    """A salient reference created a promissory-ledger entry (FR-COMMIT-7,13; #146): on a
    thunk (``thunk_id``) or on a hidden fact's proposition (``proposition_id``), exactly one.
    Identifiers only."""

    entry_id: str
    thunk_id: str | None = None
    proposition_id: str | None = None


@register("ledger_discharged", category=EventCategory.DERIVED)
class LedgerDischarged(EventPayload):
    """A ledger entry was discharged (#146): how (``mode``: ``dependence``, ``render`` or
    ``minimal``) and, for a minimal commit, the fact it wrote. Identifiers only."""

    entry_id: str
    mode: str
    fact_id: str | None = None


# -- the mutation primitives (FR-GUARD-4,5; #145) --------------------------------------------------


@register("memory_quarantined", category=EventCategory.CONTROL)
class MemoryQuarantined(EventPayload):
    """A memory was quarantined (FR-GUARD-4; #145): excluded from retrieval and from every
    generation context from this operation on. ``reason`` is the author's (at most 500
    characters) or ``repeated_implication`` when the output gate's convergence rule
    quarantined it (``automatic``, FR-GUARD-5; ``pinned`` when it was an author pin). Written by
    a position-less ``control`` operation, so a truncation never discards it."""

    memory_id: str
    reason: str
    automatic: bool = False
    pinned: bool = False


@register("memory_unquarantined", category=EventCategory.CONTROL)
class MemoryUnquarantined(EventPayload):
    """A quarantine was reversed (#145): the memory is retrievable again."""

    memory_id: str


# -- the hot set (FR-MEM-8; #147) -----------------------------------------------------------------


@register("memory_pinned", category=EventCategory.CONTROL)
class MemoryPinned(EventPayload):
    """An author pinned a memory (FR-MEM-8; #147): outside the hot-set budgets and never retired
    from this operation on (a retired memory is rehydrated to the hot set). Written by a
    position-less ``control`` operation, so a truncation never discards it. Identifiers only."""

    memory_id: str


@register("memory_unpinned", category=EventCategory.CONTROL)
class MemoryUnpinned(EventPayload):
    """A pin was removed (#147): the memory counts toward the hot-set budgets again."""

    memory_id: str


@register("sanitized", category=EventCategory.NARRATIVE_STATE)
class Sanitized(EventPayload):
    """A memory's text had every occurrence of a span replaced by ``surrogate`` (FR-GUARD-4;
    #145): the replacement as narrative state. The span itself is only in the paired
    ``sanitize_original`` audit event. History views are unchanged (sanitize rewrites the memory
    record, never the beat)."""

    memory_id: str
    surrogate: str
    occurrences: int = Field(ge=1)


@register("sanitize_original", category=EventCategory.AUDIT)
class SanitizeOriginal(EventPayload):
    """What a sanitize replaced (#145): the span and the memory's text before the rewrite.
    Audit only: no story view, retrieval or prompt reads it."""

    memory_id: str
    span: str
    text: str


@register("memories_implicated", category=EventCategory.CONTROL)
class MemoriesImplicated(EventPayload):
    """A rejected render's context implicated these records (FR-GUARD-5; #145): each memory's
    ``implication_count`` went up by one; ``fact_ids`` are reported, never changed. ``turn``
    is the operation whose render was rejected, and ``attempt`` the render. Identifiers
    only; written by a ``system`` control operation before the regeneration."""

    turn: str
    attempt: int = Field(ge=0)
    memory_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()


# -- the repair ladder (FR-REPAIR-3, FR-STORE-25; #151) ------------------------------------------


@register("render_rejected", category=EventCategory.AUDIT)
class RenderRejected(EventPayload):
    """A beat's text the repair ladder rejected (FR-REPAIR-3; #151): the original beat when a
    rung corrected it, and every regeneration that failed. ``beat`` is the beat it was (the
    accepted correction keeps that position), ``attempt`` 0 for the original render and ``n``
    for the n-th regeneration, ``rung`` the rung that rejected it (the one that replaced the
    original, or the regeneration that failed). ``constraints`` are the typed constraints the
    rung worked under, ``verdicts`` the claims' verdicts (ids and codes), ``detected_claims``
    their spans and categories. Audit only: it holds the text, but no story view, retrieval or
    prompt reads it; written with its turn, so a truncation discards it with the turn, and
    nothing in it is ever canonized."""

    beat: int = Field(ge=1)
    attempt: int = Field(ge=0)
    rung: int = Field(ge=1, le=4)
    text: str
    constraints: tuple[dict[str, JsonValue], ...] = ()
    verdicts: tuple[dict[str, JsonValue], ...] = ()
    detected_claims: tuple[dict[str, JsonValue], ...] = ()
    prompt_hash: str | None = None


@register("verify_verdict", category=EventCategory.AUDIT)
class VerifyVerdict(EventPayload):
    """The verdicts that sent one beat to the repair ladder (FR-STORE-25; #151): each claim's
    verdict entry (``turn_report.verify``'s: ids and codes, no text) for the beat as rendered.
    Audit only."""

    beat: int = Field(ge=1)
    verdicts: tuple[dict[str, JsonValue], ...] = ()


@register("composition_rejected", category=EventCategory.AUDIT)
class CompositionRejected(EventPayload):
    """A polished composition the turn did not release (FR-RESP-8; #152): its text, the check's
    failures (``{kind, span, beat}``: codes and offsets into ``text``) and why it was rejected
    (``check``, ``length`` or ``output_policy``). The turn's message is the deterministic
    assembly of its beats instead. Audit only: no story view, retrieval or prompt reads it;
    written with its turn, so a truncation discards it with the turn."""

    text: str
    failures: tuple[dict[str, JsonValue], ...] = ()
    cause: str | None = None


@register("turn_reconciled", category=EventCategory.AUDIT)
class TurnReconciled(EventPayload):
    """The concurrent commitments a structured turn reconciled on one timeline before its
    composition (FR-MULTI-3; #177, ``orchestrator.structured.reconcile``): each contradictory
    pair's proposition key, the beat whose commitment stood, the beat rewritten as an attempt
    (its ``do``) or whose ``latent`` was dropped, and the rule (``author``, ``earlier_beat``,
    ``request_order``); each duplicate commitment kept once (``dedupe``). Ids, beat numbers and
    codes only. Audit only: no story view reads it."""

    pairs: tuple[dict[str, JsonValue], ...] = ()


# -- the structured turn (FR-TURN-1,4,5,6; #150) -------------------------------------------------


class RepairedFrom(EventPayload):
    """How a beat the repair ladder corrected was made (#151): the rung that was accepted
    (1 narrowing, 2 substitution, 3 regeneration, 4 the safe fallback) and its FR-REPAIR-4
    class (``cosmetic``, ``substitution``, ``cascade``), stored as ``class``."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_by_name=True, serialize_by_alias=True
    )

    rung: int = Field(ge=1, le=4)
    repair_class: str = Field(alias="class")


class StructuredBeat(EventPayload):
    """The payload every ``beat_*`` event shares (#150): the beat's ``text``, the propositions
    it declared with their verdicts, the claims detection found beyond them (``detected``) and
    its resolved mentions (#139's ``Mention.as_payload``: spans and entity ids). The event's
    ``character_id`` is the acting character (``None`` for narration) and its ``beat`` the
    position in the turn. Never promoted to canon by being stored (FR-TURN-5). A beat the
    repair ladder corrected (#151) carries ``repaired_from``; the rejected text is only in the
    turn's ``render_rejected`` audit events (left out when absent, so an unrepaired beat's
    payload is #150's)."""

    text: str
    propositions: tuple[BeatProposition, ...] = ()
    detected: tuple[DetectedRef, ...] = Field(default=(), exclude_if=lambda v: not v)
    mentions: tuple[dict[str, JsonValue], ...] = Field(default=(), exclude_if=lambda v: not v)
    repaired_from: RepairedFrom | None = Field(default=None, exclude_if=lambda v: v is None)
    perception: dict[str, JsonValue] | None = Field(default=None, exclude_if=lambda v: not v)
    """The beat's explicit perception qualifiers as validated before staging (#161;
    ``core.epistemic.perception.Qualifiers.payload``): ``addressee``, ``manner``, ``whisper_to``,
    ``aside``, ``eavesdrop_by``, ``remote_channel``, ``language``, ``partial_text``,
    ``location``; absent when none was declared. A history read re-routes the beat from them."""
    narrated_by: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """#176 (FR-MULTI-9): a reactive character's reaction another render narrated (a
    ``beat_do``, or a brief interjection's ``beat_say``): the narrating render's character id;
    absent for a beat the character's own render gave."""


@register("beat_say", category=EventCategory.NARRATIVE_STATE)
class BeatSay(StructuredBeat):
    """A character's speech (FR-TURN-5): a speech act, stored with its labels and declared
    ``intent`` (``sincere``, ``deceptive``, ``joking`` or ``unspecified``); its ``utterances``
    row carries the same labels."""

    intent: str = "unspecified"
    labels: BeatLabels | None = None


@register("beat_think", category=EventCategory.NARRATIVE_STATE)
class BeatThink(StructuredBeat):
    """A character's private thought: never in a character viewpoint's read (FR-TURN-3)."""


@register("beat_do", category=EventCategory.NARRATIVE_STATE)
class BeatDo(StructuredBeat):
    """What a character does: an observable action."""


@register("beat_latent", category=EventCategory.NARRATIVE_STATE)
class BeatLatent(StructuredBeat):
    """Unrendered narrative state (FR-TURN-4): it routes to canon with its declared
    ``visibility`` and never appears in a character viewpoint's read."""

    visibility: BeatVisibility = Field(default_factory=BeatVisibility)


@register("beat_narration", category=EventCategory.NARRATIVE_STATE)
class BeatNarration(StructuredBeat):
    """The narrator's or director's narration (``character_id`` null): scene changes and
    arrivals."""


BEAT_TYPES: dict[str, type[StructuredBeat]] = {
    "say": BeatSay,
    "think": BeatThink,
    "do": BeatDo,
    "latent": BeatLatent,
    "narration": BeatNarration,
}
"""Each structured beat kind's event model (its event type is ``beat_<kind>``)."""

BEAT_EVENT_PREFIX = "beat_"
