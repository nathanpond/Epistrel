"""The respond contract (FR-RESP-1,3,4,9; #57): the request a consumer sends and the result.

The request carries only what a consumer knows: who acts, what they say and do, who answers,
the viewpoint, the prompt parameters and the scene context. There is no field for history,
memories, guidance or anything else a consumer would have to retrieve (FR-RESP-2), and every
model is ``extra="forbid"``, so a request that tries to send one is refused.

``scene_context`` is validated and then dropped: it is never persisted, gated, or placed in a
prompt (FR-RESP-4). The result's ``message`` is the **finished** reply: under ``lite`` no claim
in it has been checked (FR-RESP-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from epistrel.core.history import HistoryItem
from epistrel.core.ids import Ulid
from epistrel.core.profiles.effective import ProfileVersionRef
from epistrel.core.profiles.fingerprint import QualityStatus
from epistrel.core.settings_schema import (
    MAX_CALLS_PER_TURN,
    MAX_TURN_DEADLINE_S,
    MIN_CALLS_PER_TURN,
    MIN_DEADLINE_S,
    TIE_BREAK_PATTERN,
    GenerationSettings,
    InteractionMode,
    SequenceOrder,
)
from epistrel.orchestrator.models.metrics import ModelCall

MAX_INPUT_CHARS = 8_000
MAX_RECENT_MESSAGES = 20
MAX_RECENT_MESSAGE_CHARS = 2_000
MAX_ID_CHARS = 64
MAX_RENDER_CHARACTERS = 64


class PlayerInputBody(BaseModel):
    """The actor's ``say`` and/or ``do``; at least one, and whitespace alone counts as none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    say: str | None = Field(default=None, max_length=MAX_INPUT_CHARS)
    do: str | None = Field(default=None, max_length=MAX_INPUT_CHARS)

    @model_validator(mode="after")
    def _something_said_or_done(self) -> Self:
        if not ((self.say or "").strip() or (self.do or "").strip()):
            raise ValueError("player_input needs a non-empty say or do")
        return self

    @property
    def say_text(self) -> str | None:
        return self.say if self.say and self.say.strip() else None

    @property
    def do_text(self) -> str | None:
        return self.do if self.do and self.do.strip() else None


class RecentMessage(BaseModel):
    """One message of the consumer's recent scene: non-authoritative, never stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    character_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    text: str = Field(max_length=MAX_RECENT_MESSAGE_CHARS)


class SceneContext(BaseModel):
    """What the consumer knows about the scene (FR-RESP-4): validated, then dropped.

    ``characters_present`` must name cast members; in M2 it narrows nothing (M5 uses it)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    characters_present: list[Ulid] = Field(default_factory=list, max_length=MAX_RENDER_CHARACTERS)
    recent_messages: list[RecentMessage] = Field(
        default_factory=list, max_length=MAX_RECENT_MESSAGES
    )


class BudgetOverride(BaseModel):
    """A request's own turn budget (NFR-PERF-6; #69), within the story's: ``max_calls`` (1 to
    50, at most the story's call cap, its effective ``budgets.model_calls``) and ``deadline_s``
    (seconds, at least 0.5 and at most its turn deadline, ``budgets.deadline_ms``; #113); above
    either is 422 ``validation_error``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_calls: StrictInt | None = Field(default=None, ge=MIN_CALLS_PER_TURN, le=MAX_CALLS_PER_TURN)
    deadline_s: StrictFloat | None = Field(default=None, ge=MIN_DEADLINE_S, le=MAX_TURN_DEADLINE_S)


class RespondRequest(BaseModel):
    """``POST /stories/{id}/respond`` and ``Engine.respond``.

    - ``actor_character_id``: whose input this is. Any cast member may act, a player's own
      character or not: who acts is the consumer's call (FR-AUTH-3).
    - ``player_input``: the actor's ``say``/``do``; the turn's first beat. It is only ever the
      actor's own speech and action: there is no field for another character's words, and a
      ``say`` that quotes a render character is still the actor speaking (FR-AUTH-3 b).
    - ``render_character_ids``: who answers, voiced in this order in one combined reply
      (FR-RESP-5, FR-LITE-5). Omitted: every cast member except the actor, or the actor when
      no one else is in the cast. The actor may be listed. An explicit empty list is refused,
      and duplicates are dropped.
    - ``output_viewpoint``: ``character:<id>`` or ``omniscient``; default the actor.
    - ``prompt_params``: ``length`` (``short``/``medium``/``long`` or a token cap),
      ``temperature`` and ``top_p``, for this request only (FR-RESP-3).
    - ``scene_context``: non-authoritative; never stored or placed in a prompt.
    - ``budget``: this turn's ``max_calls``/``deadline_s``, within the story's (#69).
    - ``interaction_mode``/``sequence_order``: a structured story with per-character renders
      (#174): several ``render_character_ids`` render ``sequential``ly, in request order or the
      director's, or ``simultaneous``ly (#175), their conflicts settled by
      ``simultaneous_tie_break``.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "actor_character_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
                    "player_input": {"say": "Who goes there?", "do": "raises the lantern"},
                    "render_character_ids": ["01J9Z8Q4N5V6W7X8Y9Z0A1B2C4"],
                    "prompt_params": {"length": "short", "temperature": 0.7},
                    "budget": {"max_calls": 9, "deadline_s": 30},
                }
            ]
        },
    )

    actor_character_id: Ulid
    player_input: PlayerInputBody
    render_character_ids: list[Ulid] | None = Field(
        default=None, min_length=1, max_length=MAX_RENDER_CHARACTERS
    )
    output_viewpoint: str | None = Field(default=None, max_length=MAX_ID_CHARS + 16)
    prompt_params: GenerationSettings | None = None
    scene_context: SceneContext | None = None
    budget: BudgetOverride | None = None
    interaction_mode: InteractionMode | None = None
    """#174: this turn's interaction mode over the story's (``sequential`` renders each
    primary seeing what the earlier ones did); needs per-character renders (409 ``module_off``
    otherwise)."""
    sequence_order: SequenceOrder | None = None
    """#174: who orders this turn's primaries over the story's setting (``request`` or
    ``director``); needs per-character renders (409 ``module_off`` otherwise)."""
    simultaneous_tie_break: str | None = Field(default=None, pattern=TIE_BREAK_PATTERN)
    """#175: how this ``simultaneous`` turn settles conflicting actions, over the story's
    setting (``request_order``, ``director``; ``state:<name>`` is 422 ``not_yet`` until M6);
    needs per-character renders (409 ``module_off`` otherwise)."""


TURN_OVERRIDES = ("interaction_mode", "sequence_order", "simultaneous_tie_break")
"""#174's per-request overrides of a story's multi-character settings."""


def body_of(request: RespondRequest, *exclude: str) -> dict[str, Any]:
    """``request`` as JSON, #174's overrides left out when unset (a turn that names none hashes,
    and is stored, as it did before them)."""
    unset = {k for k in TURN_OVERRIDES if getattr(request, k) is None}
    return request.model_dump(mode="json", exclude={*exclude, *unset})


class RegenerateRequest(BaseModel):
    """``POST /stories/{id}/regenerate`` and ``Engine.regenerate`` (#66, FR-STORE-12).

    The recorded ``player_input`` of the head turn is reused, never re-sent. ``turn``, when
    given, must be the head turn (422 otherwise). ``render_character_ids``,
    ``output_viewpoint`` and ``prompt_params`` override the values the turn was played with;
    omitted, the recorded (resolved) ones are reused. ``budget`` is this call's own (#69).
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "output_viewpoint": "character:01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
                    "prompt_params": {"temperature": 0.9},
                }
            ]
        },
    )

    turn: StrictInt | None = Field(default=None, ge=1)
    render_character_ids: list[Ulid] | None = Field(
        default=None, min_length=1, max_length=MAX_RENDER_CHARACTERS
    )
    output_viewpoint: str | None = Field(default=None, max_length=MAX_ID_CHARS + 16)
    prompt_params: GenerationSettings | None = None
    budget: BudgetOverride | None = None


@dataclass(frozen=True, slots=True)
class FinishedMessage:
    """The finished reply: the ``message_rendered`` event's id and its text; for a structured
    turn (#152), how it was composed (``polished`` or ``deterministic``). ``mode`` is left out
    of the stored result when unset, so a Lite turn's stored and replayed message has exactly
    its pre-#152 shape."""

    message_id: str
    text: str
    mode: Annotated[
        Literal["polished", "deterministic"] | None, Field(exclude_if=lambda v: v is None)
    ] = None


AudienceFilter = Literal["none", "co_location", "full"]
SAFE_FALLBACK = "safe_fallback"
"""The ``degradation`` entry of a turn whose reply is the safe fallback (#68)."""
QUARANTINED_CONTEXT = "quarantined_context"
"""The ``degradation`` entry of a turn whose rejected renders quarantined context records
(FR-GUARD-5; #145): the regeneration ran without them."""
PIN_QUARANTINED = "pin_quarantined"
"""The warning naming an author pin the convergence rule quarantined (#145)."""


@dataclass(frozen=True, slots=True)
class TurnWarning:
    """Something the turn did that the consumer should know (#145: ``pin_quarantined``, an
    author pin the output gate's convergence rule quarantined, with its ``memory_id``; #174:
    ``primary_not_present``, a named primary not in the scene, with its ``character_id``)."""

    kind: str
    memory_id: str | None = None
    character_id: str | None = None


@dataclass(frozen=True, slots=True)
class GuardrailViolationItem:
    """An output policy the released reply breaks (#68)."""

    policy_id: str
    name: str
    message: str
    kind: str


@dataclass(frozen=True, slots=True)
class GuardrailReport:
    """The output gate's outcome for the released reply (#68): its violations (empty unless the
    last-resort fallback line broke a policy) and whether it is the safe fallback."""

    violations: list[GuardrailViolationItem] = field(default_factory=list)
    fallback_used: bool = False


@dataclass(frozen=True, slots=True)
class BudgetReport:
    """What the turn's budget allowed and spent (#69): the cap, the guardrail calls G it
    reserved (``guardrail_input``, ``guardrail_output``) and the calls it counted, which equal
    the entries of ``model_calls``."""

    max_calls: int | None
    reserved: dict[str, int]
    used: int


@dataclass(frozen=True, slots=True)
class DegradationStep:
    """A degradation step the turn took (NFR-PERF-7) and why: ``safe_fallback`` because the
    polish budget was spent (``retry_budget``), the call budget could not afford
    the regeneration (``call_budget``), or the deadline passed (``deadline``)."""

    step: str
    cause: str


@dataclass(frozen=True, slots=True)
class RespondResult:
    """What a Lite turn returns (FR-RESP-1): the finished message, the turn's beats for the
    resolved viewpoint, its position, the new revision, what it cost, and the configuration it
    ran under (its fingerprint, capabilities and quality status, FR-PROF-12,16)."""

    message: FinishedMessage
    beats: list[HistoryItem]
    turn: int
    variant: int
    timeline_revision: int
    output_viewpoint: str
    model_calls: list[ModelCall]
    elapsed_ms: int
    audience_filter: AudienceFilter = "none"
    degradation: list[str] = field(default_factory=list)
    guardrail: GuardrailReport = field(default_factory=GuardrailReport)
    """The output gate's outcome (#68): always present."""
    call_counts: dict[str, int] = field(default_factory=dict)
    """``model_calls`` counted by purpose (#69)."""
    budget: BudgetReport | None = None
    """The turn's budget and what it spent (#69)."""
    degradation_details: list[DegradationStep] = field(default_factory=list)
    """Each ``degradation`` step with its cause (#69)."""
    queued_ms: int | None = None
    """How long the turn waited in its story's queue before it started (#76); ``None`` when it
    was never queued."""
    first_call_offset_ms: int | None = None
    """From the turn reaching the front (a queued turn starting, else the request arriving) to
    its first model call starting (#76); ``elapsed_ms`` starts at the same moment."""
    fingerprint: str | None = None
    """The fingerprint of the configuration the turn ran under (FR-PROF-16; #109); ``None``
    only in a result stored before fingerprints existed."""
    capabilities: list[str] = field(default_factory=list)
    """The capabilities that configuration enables (#109)."""
    quality_status: QualityStatus | None = None
    """That configuration's quality status: ``uncertified`` in M3 (#109)."""
    warnings: list[TurnWarning] = field(default_factory=list)
    """What the turn did that the consumer should know (#145: ``pin_quarantined``)."""
    profile: ProfileVersionRef | None = None
    """The engine profile and the version the turn ran under (C-16, FR-PROF-6; #113): the
    configuration it captured at its start. ``None`` only in a result stored before #113."""
    config_revision: int | None = None
    """The story's configuration revision when the turn started (#113): it moves with every
    change of the story's settings, profile or overrides. ``None`` only in a result stored
    before #113."""
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""
    order: list[str] = field(default_factory=list)
    """#174: a structured turn with several primaries, their ids in the order they rendered
    (the request's, or the director's); empty otherwise."""
    interaction_mode: str | None = None
    """#175: a structured turn under per-character renders, the mode it ran in (``sequential``
    or ``simultaneous``, as requested); ``None`` otherwise."""
    adjudications: list[dict[str, Any]] = field(default_factory=list)
    """#175: a simultaneous turn's settled conflicts, ``{object, winner, loser, rule}``."""


@dataclass(frozen=True, slots=True)
class RegenerateResult(RespondResult):
    """What a regenerate returns: the respond result of the new variant, and the variant it
    replaced (now ``dormant``, swappable back)."""

    previous_variant: int = 0


__all__ = [
    "MAX_INPUT_CHARS",
    "PIN_QUARANTINED",
    "QUARANTINED_CONTEXT",
    "SAFE_FALLBACK",
    "TURN_OVERRIDES",
    "BudgetOverride",
    "BudgetReport",
    "DegradationStep",
    "FinishedMessage",
    "GuardrailReport",
    "GuardrailViolationItem",
    "PlayerInputBody",
    "RecentMessage",
    "RegenerateRequest",
    "RegenerateResult",
    "RespondRequest",
    "RespondResult",
    "SceneContext",
    "TurnWarning",
    "body_of",
]
