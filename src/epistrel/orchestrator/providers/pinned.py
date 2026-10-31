"""One value per turn: the pinned external reads of a structured turn (FR-EXT-2,4, FR-CONC-9,
FR-STORE-17,31; #205).

``PinnedReads`` is held by the structured pipeline for one turn (``orchestrator.structured
.pipeline.play``) and shared by every primary of a sequential or simultaneous turn; Core's
``TurnStage`` stays pure. It is the turn's ``core.states.external.PinSource`` for the whole turn
(``external.pinning``), its commit included, so Core's ``state_value`` owner lookup (#191) reads
the pinned version and never a provider.

**What a turn reads.** At the start of each attempt (``start``, from the attempt's ``generate``,
never inside a database transaction): every player-visible externally owned State in the
actor's scope (a story-scoped one, and a per-character one under the actor's key), positioned at
the turn's input (beat 0, step 0). During verification (``ensure``, before any claim of the
render is checked): each externally owned State a detected claim resolves to (hidden ones
included) that is not pinned yet, positioned at the claim's beat and step. Nothing else is read:
no speculative reads.

**How it reads** (``get``). The first ``get`` of a ``(State, scope key)`` in an attempt resolves
the provider's adapter as registered now (#204's ``provider_for``) with the story's binding,
reads it once (timed as the ``external_read`` stage; no model budget is debited), and pins the
answer: later ``get``s, every primary's included, return the pinned value with no adapter call.
An adapter error goes to ``unavailable`` (#206 applies the provider's policy there; until then
it is raised). Each answer is typed and cleaned before it is pinned (``sanitize``, #208): a
malformed one is an adapter error (``provider_malformed``) that goes to ``unavailable`` too.

**What is recorded** (``stage``): one ``external_read`` event per pinned value, ``input``
category, at its position: the provider, the value's name, the State and scope key, the query
hash, the value, the raw answer's hash (never the body), when, and ``source = live``. The
``external_values`` projection folds them in the commit.

**Rebase and regeneration.** A rebase commits the same attempt, so the pins stand (FR-CONC-9: a
rebased turn keeps its reads). A regeneration restarts the read phase, which calls ``reset``:
the next attempt reads live again. Replay, rebuild and fork fold the recorded events and never
reach this module.

Logs carry the provider's and the value's names only (invariant 3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import providers as core_providers
from epistrel.core import states
from epistrel.core.enums import ExternalValueSource, Provenance, StateScope, StateVisibility
from epistrel.core.events.types import EXTERNAL_READ, ExternalRead
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition
from epistrel.core.states import external
from epistrel.core.states.external import ExternalPin
from epistrel.core.states.model import StateDefinition, Value, to_json
from epistrel.observability import stages
from epistrel.orchestrator.providers.bindings import require_bound
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import Binding, ProviderUnavailable, ReadResult
from epistrel.orchestrator.providers.registry import provider_for
from epistrel.orchestrator.providers.sanitize import Sanitized, in_domain, render_block, sanitize

if TYPE_CHECKING:
    import httpx2

    from epistrel.core.events.operation import Operation

log = logging.getLogger(__name__)

ValueRef = tuple[str, str | None]
"""An external value of the story: ``(State id, scope key)`` (``None``: story-scoped)."""


@dataclass(frozen=True, slots=True)
class PinnedValue:
    """A value pinned for the turn: Core's ``ExternalPin`` (the State, scope key, typed value
    and position) and what its ``external_read`` event records about the read."""

    pin: ExternalPin
    provider: str
    value_name: str
    query_hash: str
    raw_hash: str
    read_at: datetime
    source: str = "live"
    origin_event_id: str | None = None
    tier: str = "authoritative"
    """The provider's ``trust_tier`` as registered when it was read (the block's ``tier``)."""
    truncated: bool = False
    """#208: a ``text`` value was capped at ``sanitize.TEXT_MAX`` characters."""

    @property
    def value(self) -> Value | None:
        return self.pin.value

    @property
    def ref(self) -> ValueRef:
        return (self.pin.state_id, self.pin.scope_key)

    def event(self) -> ExternalRead:
        """The ``external_read`` payload recording this read."""
        return ExternalRead(
            provider=self.provider,
            value_name=self.value_name,
            state_id=self.pin.state_id,
            scope_key=self.pin.scope_key,
            query_hash=self.query_hash,
            value=None if self.pin.value is None else to_json(self.pin.value),
            raw_hash=self.raw_hash,
            read_at=self.read_at,
            source=self.source,
            origin_event_id=self.origin_event_id,
            truncated=self.truncated,
        )

    def block(self) -> str:
        """#208: this value as the narrator's fenced data block (``sanitize.render_block``)."""
        return render_block(self.value_name, self.tier, self.value, holder=self.pin.scope_key)


def query_hash(
    provider: str, value: str, external_ids: Mapping[str, str], scope: str | None
) -> str:
    """The SHA-256 of a read's request: the provider, its value, the story's binding and the
    scope key (what the adapter was asked, without a secret)."""
    request = {"provider": provider, "value": value, "binding": dict(external_ids), "scope": scope}
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def scope_of(definition: StateDefinition, subject: str) -> str | None:
    """A ``state_value`` subject as the State's scope key (``None`` for a story State)."""
    return None if definition.scope is StateScope.STORY else subject


@dataclass
class PinnedReads:
    """A structured turn's pinned external reads (see the module docstring)."""

    db: AsyncEngine
    story_id: str
    in_process: InProcessRegistry = field(default_factory=InProcessRegistry)
    transport: httpx2.AsyncBaseTransport | None = None
    environ: Mapping[str, str] | None = None
    live_reads: int = 0
    """Live adapter reads this turn made, every attempt included."""
    hits: int = 0
    """``get``s answered from a pin (no adapter call)."""
    _pins: dict[ValueRef, PinnedValue] = field(default_factory=dict)
    _definitions: dict[str, StateDefinition] | None = None
    _turn: int = 0
    _started: bool = False

    @property
    def turn(self) -> int:
        """The turn the pins are read for (``start``; Core's ``PinSource.turn``)."""
        return self._turn

    # -- the attempt --------------------------------------------------------------------------

    def reset(self) -> None:
        """A new attempt (the read phase ran again): every pin is dropped, so a regenerated
        turn reads live again (FR-CONC-9)."""
        self._pins.clear()
        self._definitions = None
        self._started = False

    async def definitions(self) -> dict[str, StateDefinition]:
        """The story's active externally owned States, by id (read once per attempt)."""
        if self._definitions is None:
            async with self.db.connect() as conn:
                found = external.external(await states.definitions(conn, self.story_id))
            self._definitions = {str(d.id): d for d in found}
        return self._definitions

    def known(self) -> dict[str, StateDefinition]:
        """The externally owned States the attempt has read (empty before ``start``)."""
        return dict(self._definitions or {})

    async def start(self, turn: int, actor_id: str | None) -> list[PinnedValue]:
        """Pin every player-visible externally owned State in the actor's scope at the turn's
        input (once per attempt); answers the values pinned."""
        self._turn = turn
        if self._started:
            return list(self._pins.values())
        self._started = True
        defined = await self.definitions()
        if not defined:
            return []
        await require_bound(self.db, self.story_id)
        at = Position(turn)
        found: list[PinnedValue] = []
        for state in sorted(defined.values(), key=lambda d: d.name):
            if state.visibility is not StateVisibility.PLAYER_VISIBLE:
                continue
            if state.scope is StateScope.STORY:
                found.append(await self.get((str(state.id), None), at))
            elif actor_id is not None:
                found.append(await self.get((str(state.id), actor_id), at))
        return found

    # -- reading ----------------------------------------------------------------------------

    async def get(self, ref: ValueRef, position: Position | None = None) -> PinnedValue:
        """The value of ``ref`` pinned for the turn: read live on its first ``get`` (at
        ``position``, default the turn's input), from the pin afterwards (see the module
        docstring). An unknown State is ``KeyError``."""
        found = self._pins.get(ref)
        if found is not None:
            self.hits += 1
            return found
        definition = (await self.definitions())[ref[0]]
        at = position or Position(self._turn)
        assert definition.provider_id is not None  # noqa: S101 - an external State names one
        value_name = str(definition.external_value_name or definition.name)
        provider_name = definition.provider_id
        async with self.db.connect() as conn:
            adapter = await provider_for(
                conn,
                provider_name,
                in_process=self.in_process,
                transport=self.transport,
                environ=self.environ,
            )
            bound = await core_providers.find_binding(conn, self.story_id, provider_name)
            row = await core_providers.get_provider(conn, provider_name)
        external_ids = dict(bound.external_ids) if bound is not None else {}
        binding = Binding(self.story_id, provider_name, external_ids)
        reading = time.perf_counter()
        self.live_reads += 1
        try:
            result = await adapter.read(value_name, binding, ref[1])
            # #208: typed and cleaned before it is pinned; a malformed answer is an adapter
            # error (provider_malformed), so the unavailability policy applies to it too.
            clean = self.sanitize(definition, result, row)
        except ProviderUnavailable as error:
            pinned = await self.unavailable(definition, ref, at, error)
            self._pins[ref] = pinned
            return pinned
        finally:
            stages.observe("external_read", time.perf_counter() - reading)
        pinned = PinnedValue(
            pin=ExternalPin(
                state_id=ref[0],
                scope_key=ref[1],
                value=clean.value,
                position=at,
                source=ExternalValueSource.READ,
            ),
            provider=provider_name,
            value_name=value_name,
            query_hash=query_hash(provider_name, value_name, external_ids, ref[1]),
            raw_hash=result.raw_hash,
            read_at=result.at,
            tier=row.trust_tier,
            truncated=clean.truncated,
        )
        self._pins[ref] = pinned
        log.debug("external read pinned", extra={"provider": provider_name, "value": value_name})
        return pinned

    def sanitize(
        self, definition: StateDefinition, result: ReadResult, row: core_providers.ProviderRow
    ) -> Sanitized:
        """#208: an answer typed against the manifest's declared type and the State, and a
        text value cleaned, before it is pinned (``providers.sanitize``); ``ProviderMalformed``
        otherwise."""
        value_name = str(definition.external_value_name or definition.name)
        declared = row.values().get(value_name, {}).get("type")
        return sanitize(
            result.value,
            str(declared),
            provider=row.name,
            name=value_name,
            state_type=definition.type,
            allowed_values=definition.constraints.allowed_values,
        )

    async def unavailable(
        self,
        definition: StateDefinition,
        ref: ValueRef,
        position: Position,
        error: ProviderUnavailable,
    ) -> PinnedValue:
        """The seam #206's unavailability policy fills (``fail_turn``, ``last_known``,
        ``unknown``): until then the adapter's error is raised and nothing is committed."""
        del definition, ref, position
        raise error

    async def ensure(self, claims: Iterable[tuple[Proposition, int, int]]) -> list[PinnedValue]:
        """The pinned value of each externally owned State a claim resolves to (``(proposition,
        beat, step)``; a ``state_value`` claim names its State): one the turn has pinned comes
        from its pin (no adapter call: verification never re-reads), one it has not is read
        now, on demand, at that claim's beat and step."""
        defined = await self.definitions()
        found: list[PinnedValue] = []
        for proposition, beat, step in claims:
            # #208's domain gating: a value is read only for a claim on the State it owns.
            definition = next((d for d in defined.values() if in_domain(d, proposition)), None)
            if definition is None:
                continue
            ref = (str(definition.id), scope_of(definition, proposition.subject_id))
            found.append(await self.get(ref, Position(self._turn, 0, 0, beat, step)))
        return found

    # -- Core's PinSource -------------------------------------------------------------------

    def pinned(self, state_id: str, scope_key: str | None, through: Position | None) -> Any:
        """Core's read of the pin (``core.states.external.PinSource``): the version of
        ``(state_id, scope_key)`` pinned at or before ``through``, else ``None``."""
        found = self._pins.get((state_id, scope_key))
        if found is None:
            return None
        if through is not None and not found.pin.position.at_or_before(through):
            return None
        return found.pin

    # -- what the turn records --------------------------------------------------------------

    def values(self) -> list[PinnedValue]:
        """The attempt's pinned values, in the order they were read."""
        return list(self._pins.values())

    def by_name(self) -> dict[tuple[str, str | None], Value | None]:
        """The pinned values by ``(State name, scope key)`` (the narrator's tracked values)."""
        names = self._definitions or {}
        return {
            (names[p.pin.state_id].name, p.pin.scope_key): p.value
            for p in self._pins.values()
            if p.pin.state_id in names
        }

    def blocks(self) -> tuple[str, ...]:
        """#208: the narrator's ``external_data``: one fenced block per pinned value, in the
        order they were read (never a character's: ``check-character-context``)."""
        return tuple(p.block() for p in self._pins.values())

    def stage(self, op: Operation) -> None:
        """Stage one ``external_read`` event per pinned value, at its position."""
        for pinned in self._pins.values():
            op.append(
                EXTERNAL_READ,
                pinned.event(),
                provenance=Provenance.SYSTEM,
                character_id=None,
                beat=pinned.pin.position.beat,
                step=pinned.pin.position.step,
            )


__all__ = [
    "PinnedReads",
    "PinnedValue",
    "ValueRef",
    "query_hash",
    "scope_of",
]
