"""Degrading honestly when a provider cannot answer (FR-EXT-7,11, FR-STORE-17; #206).

``PinnedReads.get`` (#205) reads each external value once per turn. When the adapter cannot
answer (an adapter error or a timeout, #204's missing secret or unregistered callable, #208's
``provider_malformed``), ``apply_policy`` applies the provider's declared ``unavailability``
policy (#204's ``external_providers`` columns; a change is an interpretive configuration change,
``external_provider:<name>.unavailability`` / ``.freshness_minutes``, FR-CONC-15):

- **``fail_turn``**: 503 ``provider_unavailable`` ``{provider, value}``. The turn fails before its
  commit, so nothing is recorded: no event, no ``external_read``.
- **``last_known``** (the default): the most recent live read of that value on the story's
  active timeline (``core.states.external.last_read``) stands in, if the provider answered it
  within ``freshness_minutes`` of now (``DEFAULT_FRESHNESS_MINUTES`` when the provider sets no
  window). It is pinned with ``source = last_known`` and recorded as an ``external_read`` event
  carrying the recorded value and ``origin_event_id`` (the live read's event). Past the window,
  or with no recorded read, the value falls to ``Unknown``.
- **``unknown``**: the value is pinned ``Unknown`` (``value = null``, ``source = unknown``) and
  recorded the same way, so a rebuild, a replay and a fork reproduce ``Unknown`` without calling
  the provider (FR-STORE-17).

Each application is one ``Applied`` entry: the turn report lists them as
``provider_policy_applied`` (``{provider, value, policy, age_minutes, reason}``; ``age_minutes``
the recorded value's age for ``last_known``, else ``null``). A turn whose reads all answered
carries no entry.

One attempt per read and no retry inside a turn: the policy is the retry. "Now" is the wall
clock at the failed read (``now``); a replay uses the recorded event and never recomputes it.

What ``Unknown`` means downstream (FR-EXT-11) lives in Core: an objective claim of the value is
``uncertain(provider_unknown)`` (``core.states.verify``, the safe disposition), an observation of
it is never written (``core.epistemic.observation``), and an ``observe`` step of it is rejected
(``provider_unknown``, ``orchestrator.structured.steps``); testimony and a belief holder's own
claims keep their semantics (ADR-170; #207's belief-only lookup).

Logs carry the provider's and the value's names and the policy only (invariant 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import ExternalValueSource
from epistrel.core.errors import ProviderDown
from epistrel.core.membership import Position
from epistrel.core.providers import ProviderRow
from epistrel.core.states import external
from epistrel.core.states.external import ExternalPin
from epistrel.core.states.model import StateDefinition, from_json
from epistrel.orchestrator.providers.port import ProviderUnavailable

log = logging.getLogger(__name__)

FAIL_TURN, LAST_KNOWN, UNKNOWN = "fail_turn", "last_known", "unknown"
POLICIES: tuple[str, ...] = (FAIL_TURN, LAST_KNOWN, UNKNOWN)
DEFAULT_FRESHNESS_MINUTES = 60
"""``last_known``'s window when the provider sets no ``freshness_minutes`` (#206's default)."""
REPORT_KEY = "provider_policy_applied"
"""The turn report's list of applied policies."""


def now() -> datetime:
    """The wall clock a failed read is judged at (patched by tests)."""
    return datetime.now(UTC)


def freshness(row: ProviderRow) -> int:
    """The provider's ``last_known`` window, in minutes."""
    return row.freshness_minutes or DEFAULT_FRESHNESS_MINUTES


@dataclass(frozen=True, slots=True)
class Applied:
    """One policy application: the provider, its value's name, the policy that took effect
    (``last_known`` or ``unknown``; a ``last_known`` provider past its window is ``unknown``),
    the recorded value's age in whole minutes (``last_known``) and the adapter's reason code."""

    provider: str
    value: str
    policy: str
    age_minutes: int | None
    reason: str

    def report(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "value": self.value,
            "policy": self.policy,
            "age_minutes": self.age_minutes,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Degraded:
    """What a policy pinned instead of a live answer: Core's pin, the source the
    ``external_read`` event records (``last_known`` or ``unknown``), the origin event of a
    ``last_known`` value, when the read failed, and the report entry."""

    pin: ExternalPin
    source: str
    origin_event_id: str | None
    read_at: datetime
    applied: Applied


async def apply_policy(
    db: AsyncEngine,
    story_id: str,
    row: ProviderRow,
    definition: StateDefinition,
    ref: tuple[str, str | None],
    position: Position,
    error: ProviderUnavailable,
) -> Degraded:
    """The provider's unavailability policy for ``ref`` (see the module docstring): raises
    ``ProviderDown`` under ``fail_turn``, else the ``last_known`` or ``Unknown`` value to pin."""
    value_name = str(definition.external_value_name or definition.name)
    policy = row.unavailability if row.unavailability in POLICIES else LAST_KNOWN
    if policy == FAIL_TURN:
        log.info(
            "provider unavailable: failing the turn",
            extra={"provider": row.name, "value": value_name, "reason": error.reason},
        )
        raise ProviderDown(
            f"provider {row.name} could not answer {value_name}",
            {"provider": row.name, "value": value_name},
        ) from error
    at = now()
    if policy == LAST_KNOWN:
        async with db.connect() as conn:
            recorded = await external.last_read(conn, story_id, ref[0], ref[1])
        if recorded is not None:
            age = at - recorded.read_at
            if age.total_seconds() <= freshness(row) * 60:
                applied = Applied(
                    row.name, value_name, LAST_KNOWN, int(age.total_seconds() // 60), error.reason
                )
                log.info("provider unavailable: last known value", extra=applied.report())
                return Degraded(
                    pin=ExternalPin(
                        state_id=ref[0],
                        scope_key=ref[1],
                        value=from_json(definition.type, recorded.value),
                        position=position,
                        source=ExternalValueSource.LAST_KNOWN,
                    ),
                    source=LAST_KNOWN,
                    origin_event_id=recorded.event_id,
                    read_at=at,
                    applied=applied,
                )
    applied = Applied(row.name, value_name, UNKNOWN, None, error.reason)
    log.info("provider unavailable: value unknown", extra=applied.report())
    return Degraded(
        pin=ExternalPin(
            state_id=ref[0],
            scope_key=ref[1],
            value=None,
            position=position,
            source=ExternalValueSource.UNKNOWN,
        ),
        source=UNKNOWN,
        origin_event_id=None,
        read_at=at,
        applied=applied,
    )


__all__ = [
    "DEFAULT_FRESHNESS_MINUTES",
    "FAIL_TURN",
    "LAST_KNOWN",
    "POLICIES",
    "REPORT_KEY",
    "UNKNOWN",
    "Applied",
    "Degraded",
    "apply_policy",
    "freshness",
    "now",
]
