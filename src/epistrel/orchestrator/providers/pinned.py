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
An adapter error or a timeout goes to ``unavailable``, where #206 applies the provider's
unavailability policy (``unavailability``): ``fail_turn`` fails the turn (503
``provider_unavailable``, nothing committed), ``last_known`` pins a fresh enough recorded value,
otherwise the value is pinned ``Unknown``; ``policies`` lists each application for the report.
Each answer is typed and cleaned before it is pinned (``sanitize``, #208): a malformed one is an
adapter error (``provider_malformed``) that goes to ``unavailable`` too.

**What is recorded** (``stage``): one ``external_read`` event per pinned value, ``input``
category, at its position: the provider, the value's name, the State and scope key, the query
hash, the value, the raw answer's hash (never the body), when, and ``source = live`` (#206:
``last_known`` with its ``origin_event_id``, or ``unknown`` with no value). The
``external_values`` projection folds them in the commit.

**Rebase and regeneration.** A rebase commits the same attempt, so the pins stand (FR-CONC-9: a
rebased turn keeps its reads). A regeneration restarts the read phase, which calls ``reset``:
the next attempt reads live again. Replay, rebuild and fork fold the recorded events and never
reach this module.

**Post-effect versions** (#215, FR-EXT-20). The read stays version 1, never changed. After a
write-capable effect is confirmed, its outcome transaction (#212's stage 3, or #217's late
settlement at an inter-turn slot ``N⁺k``) records the next version of the affected value at
its own position (``settle_value``): the confirmation's resulting value when the provider
``returns_resulting_values`` (``write_confirmation_version``, an ``external_outcome`` event,
source ``confirmation``); else one recorded post-effect read (``post_effect_read``, an
``external_read`` event, source ``post_effect_read``); else ``Unknown`` (``record_unknown``),
so the exact resulting value is unknown for narration. ``pinned`` answers the latest version at
or before a position. No resulting value is ever computed from the read and the effect's delta.

Logs carry the provider's and the value's names only (invariant 3).
"""

from __future__ import annotations

import asyncio
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
from epistrel.core.events.types import (
    EXTERNAL_OUTCOME,
    EXTERNAL_READ,
    ExternalOutcome,
    ExternalRead,
    OutcomeVersion,
)
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition
from epistrel.core.states import external
from epistrel.core.states.external import ExternalPin
from epistrel.core.states.model import StateDefinition, Value, to_json
from epistrel.observability import stages
from epistrel.orchestrator.providers import unavailability
from epistrel.orchestrator.providers.bindings import require_bound
from epistrel.orchestrator.providers.http_manifest import DEFAULT_TIMEOUT_SECONDS
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import (
    Binding,
    ExternalProvider,
    ProviderUnavailable,
    ReadResult,
    WriteOutcome,
)
from epistrel.orchestrator.providers.registry import provider_for
from epistrel.orchestrator.providers.sanitize import Sanitized, in_domain, render_block, sanitize

if TYPE_CHECKING:
    import httpx2

    from epistrel.core.events.operation import Operation

log = logging.getLogger(__name__)

READ_TIMEOUT_SECONDS: float = DEFAULT_TIMEOUT_SECONDS
"""#206: an ``in_process`` read's timeout (the HTTP adapter's default, #204)."""

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
    base_event_id: str | None = None
    """#215: the version-1 read a ``post_effect_read`` versions (``None`` for a turn's read)."""

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
            version=self.pin.version,
            base_event_id=self.base_event_id,
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


@dataclass(frozen=True, slots=True)
class Answer:
    """One live read's outcome (``PinnedReads.answer``): the provider's row, the value's name,
    the story's binding ids, the scope key, and either the adapter's answer with its clean
    value or the adapter error."""

    row: core_providers.ProviderRow
    value_name: str
    external_ids: Mapping[str, str]
    scope_key: str | None
    read: tuple[ReadResult, Sanitized] | ProviderUnavailable

    def pinned(self, pin: ExternalPin, *, base_event_id: str | None = None) -> PinnedValue:
        """The answer pinned as ``pin`` (its value the clean one), with what its
        ``external_read`` event records."""
        assert not isinstance(self.read, ProviderUnavailable)  # noqa: S101 - an answer only
        result, clean = self.read
        post = pin.source is ExternalValueSource.POST_EFFECT_READ
        return PinnedValue(
            pin=pin,
            provider=self.row.name,
            value_name=self.value_name,
            query_hash=query_hash(
                self.row.name, self.value_name, self.external_ids, self.scope_key
            ),
            raw_hash=result.raw_hash,
            read_at=result.at,
            source="post_effect_read" if post else "live",
            tier=self.row.trust_tier,
            truncated=clean.truncated,
            base_event_id=base_event_id,
        )


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
    read_timeout: float = field(default_factory=lambda: READ_TIMEOUT_SECONDS)
    """How long an ``in_process`` read may take (#206)."""
    policies: list[unavailability.Applied] = field(default_factory=list)
    """#206: the attempt's unavailability policy applications, in the order they were read."""
    _pins: dict[ValueRef, PinnedValue] = field(default_factory=dict)
    _versions: dict[ValueRef, list[ExternalPin]] = field(default_factory=dict)
    """#215: the later versions of a pinned value (a confirmation's, a post-effect read's or an
    ``Unknown``), recorded by this module's writers at their own positions."""
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
        self._versions.clear()
        self.policies.clear()
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
        answer = await self.answer(definition, ref[1])
        if isinstance(answer.read, ProviderUnavailable):
            pinned = await self.unavailable(
                definition, ref, at, answer.read, answer.row, answer.external_ids
            )
            self._pins[ref] = pinned
            return pinned
        pinned = answer.pinned(ExternalPin(ref[0], ref[1], answer.read[1].value, at))
        self._pins[ref] = pinned
        log.debug(
            "external read pinned", extra={"provider": answer.row.name, "value": answer.value_name}
        )
        return pinned

    async def answer(self, definition: StateDefinition, scope_key: str | None) -> Answer:
        """One live read of ``definition``'s value under ``scope_key`` (#204's adapter as
        registered now, the story's binding), timed as the ``external_read`` stage and counted
        in ``live_reads``, typed and cleaned (#208); an adapter error (a malformed answer
        included) is answered, not raised, for the caller's policy."""
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
            result = await self.read(adapter, row, value_name, binding, scope_key)
            # #208: typed and cleaned before it is pinned; a malformed answer is an adapter
            # error (provider_malformed), so the unavailability policy applies to it too.
            clean = self.sanitize(definition, result, row)
        except ProviderUnavailable as error:
            stages.observe("external_read", time.perf_counter() - reading)
            return Answer(row, value_name, external_ids, scope_key, error)
        stages.observe("external_read", time.perf_counter() - reading)
        return Answer(row, value_name, external_ids, scope_key, (result, clean))

    async def read(
        self,
        adapter: ExternalProvider,
        row: core_providers.ProviderRow,
        value_name: str,
        binding: Binding,
        scope_key: str | None,
    ) -> ReadResult:
        """One attempt at the adapter (#206: no retry inside a turn; the policy is the retry).
        An ``in_process`` callable gets ``READ_TIMEOUT_SECONDS`` (the HTTP adapter applies the
        manifest's ``read.timeout_seconds`` itself); running past it is ``timeout``."""
        if row.transport != "in_process":
            return await adapter.read(value_name, binding, scope_key)
        try:
            return await asyncio.wait_for(
                adapter.read(value_name, binding, scope_key), self.read_timeout
            )
        except TimeoutError:
            raise ProviderUnavailable(row.name, value_name, "timeout") from None

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
        row: core_providers.ProviderRow,
        external_ids: Mapping[str, str],
    ) -> PinnedValue:
        """#206: the provider's unavailability policy (``unavailability.apply_policy``):
        ``fail_turn`` raises 503 ``provider_unavailable`` (nothing is committed); otherwise the
        ``last_known`` or ``Unknown`` value is pinned like a read (its ``external_read`` event
        records the source, and a ``last_known`` value its origin event) and the application
        is listed for the turn report (``policies``)."""
        degraded = await unavailability.apply_policy(
            self.db, self.story_id, row, definition, ref, position, error
        )
        self.policies.append(degraded.applied)
        value_name = degraded.applied.value
        return PinnedValue(
            pin=degraded.pin,
            provider=row.name,
            value_name=value_name,
            query_hash=query_hash(row.name, value_name, external_ids, ref[1]),
            raw_hash="",
            read_at=degraded.read_at,
            source=degraded.source,
            origin_event_id=degraded.origin_event_id,
            tier=row.trust_tier,
        )

    def policy_report(self) -> list[dict[str, Any]]:
        """#206: the turn report's ``provider_policy_applied`` entries (empty when every read
        answered)."""
        return [applied.report() for applied in self.policies]

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
        """Core's read of the pin (``core.states.external.PinSource``): the latest version of
        ``(state_id, scope_key)`` pinned at or before ``through`` (the read, or a later version
        #215's writers recorded), by position then version; else ``None``."""
        ref = (state_id, scope_key)
        found = self._pins.get(ref)
        versions = [*([found.pin] if found is not None else []), *self._versions.get(ref, ())]
        candidates = [
            pin for pin in versions if through is None or pin.position.at_or_before(through)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda pin: (pin.position.key(), pin.version))

    def add_version(self, pin: ExternalPin) -> None:
        """#215: record a later version of a pinned value (``pinned`` answers it from its
        position on). Never replaces the read: version 1 is immutable."""
        self._versions.setdefault((pin.state_id, pin.scope_key), []).append(pin)

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


# -- post-effect versions (#215, FR-EXT-20) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeTarget:
    """The external value a write's outcome may version: the State (``definition``) under
    ``scope_key``, the position the outcome is recorded at (the outcome step, #212, or a late
    settlement's inter-turn slot ``N⁺k``, #217), and the read chain the new version continues
    (``core.states.external.version_base``)."""

    definition: StateDefinition
    scope_key: str | None
    position: Position
    base: external.VersionBase

    @property
    def state_id(self) -> str:
        return str(self.definition.id)

    @property
    def value_name(self) -> str:
        return str(self.definition.external_value_name or self.definition.name)

    def pin(self, value: Value | None, source: ExternalValueSource) -> ExternalPin:
        """The new version as Core reads it."""
        return ExternalPin(
            state_id=self.state_id,
            scope_key=self.scope_key,
            value=value,
            position=self.position,
            version=self.base.next_version,
            source=source,
        )


async def outcome_target(
    db: AsyncEngine,
    story_id: str,
    definition: StateDefinition,
    scope_key: str | None,
    position: Position,
) -> OutcomeTarget | None:
    """The ``OutcomeTarget`` of ``definition``'s value at ``position``: the committed version it
    continues; ``None`` when no read of it was ever recorded (there is nothing to version, and
    the outcome records none)."""
    async with db.connect() as conn:
        base = await external.version_base(conn, story_id, str(definition.id), scope_key, position)
    return None if base is None else OutcomeTarget(definition, scope_key, position, base)


@dataclass(frozen=True, slots=True)
class Outcome:
    """A write's outcome as the outcome event records it: the outbox row and its effect, the
    adapter's ``WriteOutcome`` and when it was answered."""

    outbox_id: str
    effect_id: str
    answer: WriteOutcome
    answered_at: datetime

    def event(self, version: OutcomeVersion | None = None) -> ExternalOutcome:
        """The ``external_outcome`` payload, recording ``version`` if any."""
        resulting = self.answer.resulting_values
        return ExternalOutcome(
            outbox_id=self.outbox_id,
            effect_id=self.effect_id,
            status=self.answer.status,
            raw_hash=self.answer.raw_hash,
            answered_at=self.answered_at,
            resulting_values=(
                json.loads(json.dumps(dict(resulting), default=str)) if resulting else None
            ),
            version=version,
        )


def stage_outcome(
    op: Operation, outcome: Outcome, position: Position, version: OutcomeVersion | None = None
) -> ExternalOutcome:
    """Stage the ``external_outcome`` event of ``outcome`` at ``position``'s beat and step (the
    operation is the outcome step's, #212, or the settlement slot's, #217), recording the
    post-effect ``version`` it decides, if any."""
    payload = outcome.event(version)
    op.append(
        EXTERNAL_OUTCOME,
        payload,
        provenance=Provenance.SYSTEM,
        character_id=None,
        beat=position.beat,
        step=position.step,
    )
    return payload


def _version(target: OutcomeTarget, value: Value | None, *, known: bool) -> OutcomeVersion:
    return OutcomeVersion(
        state_id=target.state_id,
        scope_key=target.scope_key,
        value=None if value is None else to_json(value),
        version=target.base.next_version,
        base_event_id=target.base.base_event_id,
        source="confirmation" if known else "unknown",
    )


def write_confirmation_version(
    op: Operation,
    target: OutcomeTarget,
    outcome: Outcome,
    provider: core_providers.ProviderRow,
    *,
    pins: PinnedReads | None = None,
) -> ExternalPin | None:
    """A confirmation's resulting value as the next version of ``target`` (FR-EXT-20): when the
    outcome is ``confirmed``, the provider declares ``returns_resulting_values`` and its
    ``resulting_values`` (the manifest's ``resulting_values_path``) carry the value, typed and
    cleaned like a read (#208), the ``external_outcome`` event is staged recording it (source
    ``confirmation``) and the new version is answered (and added to ``pins``). Otherwise
    nothing is staged and ``None`` is answered. The value is the provider's, as answered."""
    if outcome.answer.status != "confirmed" or not provider.returns_resulting_values:
        return None
    resulting = outcome.answer.resulting_values or {}
    if target.value_name not in resulting:
        return None
    declared = provider.values().get(target.value_name, {}).get("type")
    try:
        clean = sanitize(
            resulting[target.value_name],
            str(declared),
            provider=provider.name,
            name=target.value_name,
            state_type=target.definition.type,
            allowed_values=target.definition.constraints.allowed_values,
        )
    except ProviderUnavailable:
        return None
    stage_outcome(op, outcome, target.position, _version(target, clean.value, known=True))
    pin = target.pin(clean.value, ExternalValueSource.CONFIRMATION)
    if pins is not None:
        pins.add_version(pin)
    return pin


async def post_effect_read(
    op: Operation, reads: PinnedReads, target: OutcomeTarget
) -> ExternalPin | None:
    """One recorded post-effect read of ``target`` (FR-EXT-20): when the provider's manifest
    declares the value, read it once (``PinnedReads.answer``: #204's adapter, #208's
    sanitizing) and stage it as an ``external_read`` event with ``source = post_effect_read``,
    the next version of the read it extends, at the target's position; answers the new version
    (added to ``reads``). An adapter error (an unreachable provider, a malformed answer) or an
    undeclared value answers ``None`` and stages nothing: #206's unavailability policy does not
    apply mid-stage."""
    definition = target.definition
    assert definition.provider_id is not None  # noqa: S101 - an external State names one
    async with reads.db.connect() as conn:
        row = await core_providers.find_provider(conn, definition.provider_id)
    if row is None or target.value_name not in row.values():
        return None
    answer = await reads.answer(definition, target.scope_key)
    if isinstance(answer.read, ProviderUnavailable):
        log.info(
            "post-effect read unavailable",
            extra={"provider": row.name, "value": target.value_name, "reason": answer.read.reason},
        )
        return None
    pin = target.pin(answer.read[1].value, ExternalValueSource.POST_EFFECT_READ)
    pinned = answer.pinned(pin, base_event_id=target.base.base_event_id)
    op.append(
        EXTERNAL_READ,
        pinned.event(),
        provenance=Provenance.SYSTEM,
        character_id=None,
        beat=target.position.beat,
        step=target.position.step,
    )
    reads.add_version(pin)
    return pin


def record_unknown(
    op: Operation, target: OutcomeTarget, outcome: Outcome, *, pins: PinnedReads | None = None
) -> external.Unknown:
    """Neither the confirmation nor a post-effect read gave the resulting value (FR-EXT-20,11):
    the ``external_outcome`` event is staged recording an ``Unknown`` next version of
    ``target``, so a claim of the exact resulting value from the target's position on is
    ``uncertain(provider_unknown)``, while the effect itself ("you paid 50") is unaffected."""
    stage_outcome(op, outcome, target.position, _version(target, None, known=False))
    pin = target.pin(None, ExternalValueSource.UNKNOWN)
    if pins is not None:
        pins.add_version(pin)
    return external.Unknown(target.state_id, target.scope_key, pin)


async def settle_value(
    op: Operation,
    reads: PinnedReads,
    target: OutcomeTarget,
    outcome: Outcome,
    provider: core_providers.ProviderRow,
) -> ExternalPin | external.Unknown | None:
    """Record a write's outcome with the post-effect version of its value it decides
    (FR-EXT-20, docs/02 §4.8), staging the one ``external_outcome`` event:

    1. a confirmation carrying the resulting value: its version (``write_confirmation_version``);
    2. else one recorded ``post_effect_read``, the outcome recording no version;
    3. else an ``Unknown`` version (``record_unknown``).

    A ``failed`` or ``unknown`` outcome versions nothing (the value is as last read): the
    outcome is recorded alone and ``None`` answered. The resulting value is never computed
    from the read and the effect's delta."""
    if outcome.answer.status != "confirmed":
        stage_outcome(op, outcome, target.position)
        return None
    confirmed = write_confirmation_version(op, target, outcome, provider, pins=reads)
    if confirmed is not None:
        return confirmed
    read = await post_effect_read(op, reads, target)
    if read is not None:
        stage_outcome(op, outcome, target.position)
        return read
    return record_unknown(op, target, outcome, pins=reads)


__all__ = [
    "Answer",
    "Outcome",
    "OutcomeTarget",
    "PinnedReads",
    "PinnedValue",
    "ValueRef",
    "outcome_target",
    "post_effect_read",
    "query_hash",
    "record_unknown",
    "scope_of",
    "settle_value",
    "stage_outcome",
    "write_confirmation_version",
]
