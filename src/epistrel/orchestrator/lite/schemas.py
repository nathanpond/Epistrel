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
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from epistrel.core.history import HistoryItem
from epistrel.core.ids import Ulid
from epistrel.core.settings_schema import GenerationSettings
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
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_character_id: Ulid
    player_input: PlayerInputBody
    render_character_ids: list[Ulid] | None = Field(
        default=None, min_length=1, max_length=MAX_RENDER_CHARACTERS
    )
    output_viewpoint: str | None = Field(default=None, max_length=MAX_ID_CHARS + 16)
    prompt_params: GenerationSettings | None = None
    scene_context: SceneContext | None = None


class RegenerateRequest(BaseModel):
    """``POST /stories/{id}/regenerate`` and ``Engine.regenerate`` (#66, FR-STORE-12).

    The recorded ``player_input`` of the head turn is reused, never re-sent. ``turn``, when
    given, must be the head turn (422 otherwise). ``render_character_ids``,
    ``output_viewpoint`` and ``prompt_params`` override the values the turn was played with;
    omitted, the recorded (resolved) ones are reused.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int | None = Field(default=None, ge=1)
    render_character_ids: list[Ulid] | None = Field(
        default=None, min_length=1, max_length=MAX_RENDER_CHARACTERS
    )
    output_viewpoint: str | None = Field(default=None, max_length=MAX_ID_CHARS + 16)
    prompt_params: GenerationSettings | None = None


@dataclass(frozen=True, slots=True)
class FinishedMessage:
    """The finished reply: the ``message_rendered`` event's id and its text."""

    message_id: str
    text: str


AudienceFilter = Literal["none", "co_location", "full"]


@dataclass(frozen=True, slots=True)
class RespondResult:
    """What a Lite turn returns (FR-RESP-1): the finished message, the turn's beats for the
    resolved viewpoint, its position, the new revision, and what it cost."""

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
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class RegenerateResult(RespondResult):
    """What a regenerate returns: the respond result of the new variant, and the variant it
    replaced (now ``dormant``, swappable back)."""

    previous_variant: int = 0


__all__ = [
    "MAX_INPUT_CHARS",
    "FinishedMessage",
    "PlayerInputBody",
    "RecentMessage",
    "RegenerateRequest",
    "RegenerateResult",
    "RespondRequest",
    "RespondResult",
    "SceneContext",
]
