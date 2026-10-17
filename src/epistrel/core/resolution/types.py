"""The resolver's result types (FR-PROP-6,9, FR-RET-2; #139).

Every resolver answers either a resolution or an ``Uncertain`` carrying a ``Reason``: a ``code``
(``ReasonCode``) and a ``detail``. #140's ``uncertain`` verdicts and #155's gate-reason field
read ``Reason.gate``: ``unresolved_entity`` for every mention code, ``unresolved_time`` for every
time code, ``unresolved_event_type`` for an occasion word the vocabulary lacks. ``Uncertain`` is
shared with #140 (an uncertain verdict wraps one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from epistrel.core.propositions.model import Instant, Interval

APOSTROPHE = chr(0x2019)
"""The typographic apostrophe, matched like ``'``."""


class ReasonCode(StrEnum):
    UNRESOLVED_ENTITY = "unresolved_entity"
    AMBIGUOUS = "ambiguous"
    UNKNOWN_NAME = "unknown_name"
    NO_ANTECEDENT = "no_antecedent"
    GENDER_MISMATCH = "gender_mismatch"
    UNRESOLVED_TIME = "unresolved_time"
    NO_CALENDAR = "no_calendar"
    NO_ANCHOR = "no_anchor"
    UNRESOLVED_EVENT_TYPE = "unresolved_event_type"


ENTITY_CODES = frozenset(
    {
        ReasonCode.UNRESOLVED_ENTITY,
        ReasonCode.AMBIGUOUS,
        ReasonCode.UNKNOWN_NAME,
        ReasonCode.NO_ANTECEDENT,
        ReasonCode.GENDER_MISMATCH,
    }
)
TIME_CODES = frozenset({ReasonCode.UNRESOLVED_TIME, ReasonCode.NO_CALENDAR, ReasonCode.NO_ANCHOR})

GateReason = Literal["unresolved_entity", "unresolved_time", "unresolved_event_type"]


@dataclass(frozen=True, slots=True)
class Reason:
    """Why a resolver could not resolve: its ``code`` and a short ``detail`` (no story text: a
    rule name, a count)."""

    code: ReasonCode
    detail: str = ""

    @property
    def gate(self) -> GateReason:
        """The gate-reason family (#155): ``unresolved_entity``, ``unresolved_time`` or
        ``unresolved_event_type``."""
        if self.code in ENTITY_CODES:
            return "unresolved_entity"
        if self.code in TIME_CODES:
            return "unresolved_time"
        return "unresolved_event_type"

    def as_payload(self) -> dict[str, str]:
        return {"code": self.code.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Uncertain:
    """An unresolved result: never a guess. ``candidates`` lists the entity ids an ambiguous
    mention could be (empty otherwise)."""

    reason: Reason
    candidates: tuple[str, ...] = ()


class MentionKind(StrEnum):
    NAME = "name"
    ALIAS = "alias"
    PRONOUN = "pronoun"
    FIRST_PERSON = "first_person"
    SECOND_PERSON = "second_person"
    DESCRIPTION = "description"
    GROUP = "group"


@dataclass(frozen=True, slots=True)
class Mention:
    """One mention in a text: its character ``span`` (``[start, end)``), what kind, the entity
    it resolves to (or ``None``), the ``candidates`` considered, the ``confidence`` (1.0 an exact
    alias, 0.8 a pronoun, 0.6 a description; no floor here, #142's gate owns thresholds) and,
    when unresolved, the ``reason``. A group mention (``those guys``) names no entity and has no
    reason: #141's occasion lookup resolves its members."""

    span: tuple[int, int]
    kind: MentionKind
    entity_id: str | None
    candidates: tuple[str, ...] = ()
    confidence: float = 0.0
    reason: Reason | None = None
    rule: str = ""
    """Which rule resolved it (``alias``, ``implicit_token``, ``pronoun``, ``speaker``,
    ``addressee``, ``descriptor``, ``relation``): audit and Q-02 detail."""

    @property
    def resolved(self) -> bool:
        return self.entity_id is not None

    def uncertain(self) -> Uncertain | None:
        """The ``Uncertain`` an unresolved, non-group mention stands for."""
        if self.entity_id is not None or self.kind is MentionKind.GROUP:
            return None
        reason = self.reason or Reason(ReasonCode.UNRESOLVED_ENTITY)
        return Uncertain(reason, self.candidates)

    def as_payload(self) -> dict[str, Any]:
        """The JSON form persisted on a beat event's ``mentions`` (spans and ids, no text)."""
        return {
            "span": list(self.span),
            "kind": self.kind.value,
            "entity_id": self.entity_id,
            "candidates": list(self.candidates),
            "confidence": self.confidence,
            "reason": None if self.reason is None else self.reason.as_payload(),
            "rule": self.rule,
        }


Direction = Literal["past", "present", "future"]


@dataclass(frozen=True, slots=True)
class ResolvedTime:
    """A resolved temporal expression: its valid-time in story-minutes (an ``Instant`` with a
    granularity, or an ``Interval``, approximate where the expression is coarse), its
    ``direction`` relative to the position's "now" and the grammar ``rule`` that matched."""

    valid_time: Interval | Instant
    direction: Direction
    rule: str
    approximate: bool = False


@dataclass(frozen=True, slots=True)
class TimeExpression:
    """A temporal expression found in a text: its ``span``, the grammar ``rule`` it matched and
    the matched groups (numbers, units, names)."""

    span: tuple[int, int]
    rule: str
    groups: dict[str, str | None] = field(default_factory=dict)
    text: str = ""


TimeResolution = ResolvedTime | Uncertain

__all__ = [
    "APOSTROPHE",
    "ENTITY_CODES",
    "TIME_CODES",
    "Direction",
    "GateReason",
    "Mention",
    "MentionKind",
    "Reason",
    "ReasonCode",
    "ResolvedTime",
    "TimeExpression",
    "TimeResolution",
    "Uncertain",
]
