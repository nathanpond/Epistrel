"""Ingest: record consumer events into authoritative history without generating (FR-RESP-10).

A body carries up to ``MAX_EVENTS`` events, each with a ``client_event_id`` unique within the
story. Three kinds are recorded:

- ``player_turn``: an actor's ``say``/``do``, recorded as a ``player_input`` event at beat 1;
- ``narration``: consumer-authored ``text`` with no actor, recorded as a ``message_rendered``
  event at beat 1 with no character, ``finish_reason`` ``consumer`` and no model or prompt hash;
- ``state_change`` (#186): ``{state, scope_key?, value | delta, reason}``, a consumer set of a
  story State (FR-STATE-9), with the refusals ``PUT /stories/{id}/states/{name}`` has
  (``core.states.api``; ``state_unpermitted`` included).

The first two are narrative turns: each event is its own ``turn`` operation (``origin =
ingested``, provenance ``consumer``, no viewpoint) at the next turn index. A ``state_change`` is
an ``inter_turn`` slot operation (source ``consumer``) in the next slot after the last turn
written before it in the batch, or after the story's last completed turn when none precedes it:
batch order is position order. Each bumps the timeline revision (FR-CONC-1), and none advances
the world clock (#193). The batch is all or nothing (``OperationBatch``).

``outcome`` (#217, FR-EXT-21): ``{effect_id | outbox_id, outcome: confirmed|failed|unknown,
resulting_values?}``, the consumer's report of an external effect's outcome: a #209
consumer-applied effect names its ``effect_id`` (the ``requested_effect``'s), an outbox effect
its row (``outbox_id``, which is also its ``effect_id``). An effect the story does not know is
422 ``effect_not_found``. Outcomes are ingested in a batch of their own (mixed with turns or State
sets, 422 ``validation_error``): one operation with **no narrative position** and no revision
bump records each as an ``outbox_outcomes`` row (``core.outbox``: first terminal answer wins,
FR-EXT-22) and an ``input`` ``outcome_recorded`` event, never refused by a running turn or a
pause (an outcome is recorded on arrival), and its commit asks the Engine's ``settle`` to settle
each deciding one at the next inter-turn slot (the Orchestrator's late settlement). An
outcome's result names that operation and turn ``0``.

**Deduplication.** Every text is NFC-normalised and trimmed, then the event's payload hash is
the SHA-256 of its canonical JSON without ``client_event_id`` (``None`` fields left out, keys
sorted). ``client_events`` keeps ``(story_id, client_event_id)`` unique with the hash and the
first result, for the life of the story. An ID seen before with the same hash is a replay: the
original result comes back and nothing is written. With a different hash it is 409
``client_event_conflict`` and nothing is written. A batch mixing replays and new events records
only the new ones (all or nothing). A concurrent duplicate waits: the commit re-checks
``client_events`` under the story lock, and a duplicate that arrived first turns the retry into
a replay.

**Order of checks** (#44/#56): the kinds (422) → the story (404) → the actors against the cast
(422) → replay or conflict (before anything else may refuse a retry) → archived (409) → the
expected revision (409) → the input gate, each event's text on its own (``gate``, passed in by
the Engine because Core never imports the Orchestrator) → the story's turn slot under
``turn_concurrency`` → one batch commit, which honours ``timeline_changed`` but skips the
read-set decision (an ingest generates nothing that could be stale).

**Turn concurrency** (FR-CONC-5, #63): the batch is one turn of ``core.turn_gate``; its first
operation holds the story's turn slot. Under ``reject`` a generated turn holding the slot is
423 ``story_busy`` with nothing written, while another ingest is only waited for (its commit is
short; a duplicate then replays). Under ``queue`` the batch becomes one ``turn_queue`` row
(``queue_batch``, from the Engine) that the drainer records later (``run_queued``).
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, pause, states, turn_gate
from epistrel.core import outbox as core_outbox
from epistrel.core.concurrency import (
    Abandon,
    Commit,
    Decision,
    RevalidationRequired,
    RevalidationState,
    check_expected_revision,
    hash_member,
)
from epistrel.core.enums import (
    Authority,
    OperationKind,
    Provenance,
    StoryStatus,
)
from epistrel.core.errors import (
    ClientEventConflict,
    EffectNotFound,
    InvalidRequest,
    StoryArchived,
    StoryBusy,
    TimelineChanged,
)
from epistrel.core.events import Operation, OperationBatch, UnregisteredEventType
from epistrel.core.events.operation import Withdrawn
from epistrel.core.events.types import (
    OUTCOME_RECORDED,
    REQUESTED_EFFECT,
    MessageRendered,
    OutcomeRecorded,
    PlayerInput,
    StateSet,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.states import api as state_api
from epistrel.core.states.model import REASON_MAX, Violation
from epistrel.core.tables import client_events, events, stories
from epistrel.core.tables.outbox import VIA_INGEST, external_effect_outbox
from epistrel.core.turn_gate import Concurrency
from epistrel.core.turns import INPUT_BEAT, StoryCast, joined_input, read_story_cast
from epistrel.core.world_clock.slots import slot_position

MAX_EVENTS = 50
MAX_TEXT_CHARS = 8_000
MAX_CLIENT_EVENT_ID_CHARS = 255
CLIENT_EVENT_ID_PATTERN = r"^[A-Za-z0-9._:-]+$"
"""#61's key rule: at most 255 characters of ``[A-Za-z0-9._:-]``."""
MAX_ACTOR_ID_CHARS = 64
MAX_RESULTING_VALUES = 20
"""The most ``resulting_values`` an ingested outcome may report."""

PLAYER_TURN = "player_turn"
NARRATION = "narration"
STATE_CHANGE = "state_change"
OUTCOME = "outcome"
SUPPORTED_KINDS = (PLAYER_TURN, NARRATION, STATE_CHANGE, OUTCOME)
LATER_KINDS: dict[str, str] = {}
"""Kinds docs/03 plans that are not recorded yet, and the milestone that brings them (none
since #217 records ``outcome``)."""
STATE_FIELDS = ("state", "scope_key", "value", "delta", "reason")
"""The fields only a ``state_change`` carries."""
OUTCOME_FIELDS = ("effect_id", "outbox_id", "outcome", "resulting_values")
"""The fields only an ``outcome`` carries."""
OUTCOME_TURN = 0
"""The ``turn`` an outcome's result reports: its record has no narrative position."""

CONSUMER_FINISH_REASON = "consumer"
INGESTED: Literal["ingested"] = "ingested"
LOCATOR = "events[{index}]"
COMMIT_ATTEMPTS = 3
"""How many times a batch is rebuilt after a concurrent duplicate committed first."""


def normalised(value: str | None) -> str | None:
    """NFC-normalised and trimmed; empty after trimming is ``None`` (#60's text rule)."""
    if value is None:
        return None
    value = unicodedata.normalize("NFC", value).strip()
    return value or None


class IngestEvent(BaseModel):
    """One event to record. ``kind`` is free text here so that a kind not recorded yet is
    refused with ``unsupported_event_kind`` rather than a generic validation error."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_event_id: str = Field(
        min_length=1, max_length=MAX_CLIENT_EVENT_ID_CHARS, pattern=CLIENT_EVENT_ID_PATTERN
    )
    kind: str = Field(min_length=1, max_length=64)
    actor_character_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_ACTOR_ID_CHARS
    )
    say: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    do: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    text: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    state: str | None = Field(default=None, min_length=1, max_length=80)
    scope_key: str | None = Field(default=None, min_length=1, max_length=MAX_ACTOR_ID_CHARS)
    value: StrictBool | StrictInt | StrictFloat | StrictStr | None = None
    delta: StrictInt | StrictFloat | None = None
    reason: str | None = Field(default=None, max_length=REASON_MAX)
    effect_id: str | None = Field(default=None, pattern=ULID_PATTERN)
    outbox_id: str | None = Field(default=None, pattern=ULID_PATTERN)
    outcome: Literal["confirmed", "failed", "unknown"] | None = None
    resulting_values: (
        dict[
            Annotated[str, Field(min_length=1, max_length=80)],
            StrictBool | StrictInt | StrictFloat | Annotated[str, Field(max_length=REASON_MAX)],
        ]
        | None
    ) = Field(default=None, max_length=MAX_RESULTING_VALUES)

    @field_validator("say", "do", "text", "reason")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        return normalised(value)

    @model_validator(mode="after")
    def _fields_fit_the_kind(self) -> Self:
        if self.kind == OUTCOME:
            if (self.effect_id is None) == (self.outbox_id is None):
                raise ValueError("an outcome names its effect_id or its outbox_id, not both")
            if self.outcome is None:
                raise ValueError("an outcome gives its outcome: confirmed, failed or unknown")
            if any(getattr(self, name) is not None for name in STATE_FIELDS) or any(
                v is not None for v in (self.actor_character_id, self.say, self.do, self.text)
            ):
                raise ValueError("an outcome carries no actor, text or State fields")
            return self
        if any(getattr(self, name) is not None for name in OUTCOME_FIELDS):
            raise ValueError(f"only an {OUTCOME} carries {', '.join(OUTCOME_FIELDS)}")
        if self.kind == STATE_CHANGE:
            if self.state is None or self.reason is None:
                raise ValueError("a state_change names its state and gives a reason")
            if (self.value is None) == (self.delta is None):
                raise ValueError("a state_change gives a value or a delta, not both")
            if self.actor_character_id is not None or any(
                v is not None for v in (self.say, self.do, self.text)
            ):
                raise ValueError("a state_change carries no actor, say, do or text")
            return self
        if any(getattr(self, name) is not None for name in STATE_FIELDS):
            raise ValueError(f"only a {STATE_CHANGE} carries {', '.join(STATE_FIELDS)}")
        if self.kind == PLAYER_TURN:
            if self.actor_character_id is None:
                raise ValueError("a player_turn needs actor_character_id")
            if self.say is None and self.do is None:
                raise ValueError("a player_turn needs a non-empty say or do")
            if self.text is not None:
                raise ValueError("a player_turn carries say/do, not text")
        elif self.kind == NARRATION:
            if self.text is None:
                raise ValueError("a narration needs a non-empty text")
            if self.actor_character_id is not None:
                raise ValueError("a narration has no actor_character_id")
            if self.say is not None or self.do is not None:
                raise ValueError("a narration carries text, not say/do")
        return self

    @property
    def payload_hash(self) -> str:
        """SHA-256 of the canonical JSON of the event without its ``client_event_id``."""
        return hash_member(self.model_dump(exclude_none=True, exclude={"client_event_id"}))

    @property
    def is_turn(self) -> bool:
        """A narrative turn (``player_turn``, ``narration``), not an inter-turn slot or an
        outcome."""
        return self.kind not in (STATE_CHANGE, OUTCOME)

    @property
    def gated_text(self) -> str:
        """What the input gate checks: ``say``/``do`` joined (as respond does), or the text."""
        if self.kind == PLAYER_TURN:
            return joined_input(self.say, self.do)
        return self.text or ""


class IngestRequest(BaseModel):
    """``POST /stories/{id}/ingest`` and ``Engine.ingest``: 1..50 events, IDs distinct."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "events": [
                        {
                            "client_event_id": "evt-0001",
                            "kind": "player_turn",
                            "actor_character_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
                            "say": "I open the door.",
                        },
                        {
                            "client_event_id": "evt-0002",
                            "kind": "narration",
                            "text": "Rain hammers the roof.",
                        },
                    ]
                }
            ]
        },
    )

    events: list[IngestEvent] = Field(min_length=1, max_length=MAX_EVENTS)

    @model_validator(mode="after")
    def _distinct_ids(self) -> Self:
        seen: set[str] = set()
        for event in self.events:
            if event.client_event_id in seen:
                raise ValueError(f"client_event_id {event.client_event_id!r} appears twice")
            seen.add(event.client_event_id)
        return self


@dataclass(frozen=True, slots=True)
class EventResult:
    """What one ingested event became: its operation, its turn, and whether this call replayed
    an earlier ingest of it."""

    client_event_id: str
    operation_id: str
    turn: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class IngestResult:
    """The per-event results in request order and the story's revision after the call (for a
    call that only replayed: the revision the original ingest left)."""

    results: list[EventResult]
    timeline_revision: int
    key_replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""
    model_calls: tuple[dict[str, Any], ...] = ()
    """The model calls this request's input gate made, each as a turn's ``model_calls`` entry
    reports it (filled by the Engine, #73)."""
    elapsed_ms: int = 0
    """How long the request took, from its arrival (filled by the Engine, #73)."""

    @property
    def replayed(self) -> bool:
        """Nothing was written: a key replay (#61), or every event was ingested before."""
        return self.key_replayed or all(r.replayed for r in self.results)

    @property
    def created(self) -> bool:
        """Every event was new."""
        return not any(r.replayed for r in self.results)


@dataclass(frozen=True, slots=True)
class GateGroup:
    """Texts the input gate checks together: every event of one actor (``None``: narration),
    each text on its own with its locator ``events[i]``."""

    actor: str | None
    texts: tuple[tuple[str, str], ...]


Gate = Callable[[Sequence[GateGroup]], Awaitable[None]]
"""The input gate (#56) over the new events; raises on a violation."""

Settle = Callable[[AsyncConnection, str, str], Awaitable[Any]]
"""``settle(conn, story_id, outcome_id)``: enqueue the late settlement of an ingested outcome
in ``conn``'s transaction (#217; the Engine provides the Orchestrator's, since Core never imports
it)."""


@dataclass(frozen=True, slots=True)
class _Prior:
    payload_hash: str
    result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Prepared:
    story: StoryCast
    replays: dict[int, tuple[EventResult, int]]
    """Request index → (the original result, the revision the original left)."""
    new: tuple[tuple[int, IngestEvent], ...]


class _DuplicateArrived(Withdrawn):
    """A concurrent ingest recorded one of this batch's IDs first: rebuild the batch."""


# -- the checks -----------------------------------------------------------------------------


def check_kinds(request: IngestRequest) -> None:
    """422 ``unsupported_event_kind`` for a kind not recorded yet (naming its milestone)."""
    recorded = ", ".join(SUPPORTED_KINDS)
    for index, event in enumerate(request.events):
        if event.kind in SUPPORTED_KINDS:
            continue
        later = LATER_KINDS.get(event.kind)
        message = (
            f"ingesting {event.kind!r} events arrives in {later}; ingest records {recorded}"
            if later
            else f"{event.kind!r} is not an ingestable kind; ingest records {recorded}"
        )
        raise UnregisteredEventType(
            message,
            {
                "kind": event.kind,
                "locator": LOCATOR.format(index=index),
                "supported": list(SUPPORTED_KINDS),
                **({"milestone": later} if later else {}),
            },
        )


def check_actors(story: StoryCast, request: IngestRequest) -> None:
    """Every ``player_turn`` actor is a cast member (422 ``validation_error`` otherwise)."""
    members = story.by_id()
    unknown = {
        LOCATOR.format(index=index): event.actor_character_id
        for index, event in enumerate(request.events)
        if event.actor_character_id is not None and event.actor_character_id not in members
    }
    if unknown:
        raise InvalidRequest(
            "the request names characters that are not in the story", {"unknown_actor": unknown}
        )


async def _priors(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> dict[str, _Prior]:
    found = await conn.execute(
        select(
            client_events.c.client_event_id,
            client_events.c.payload_hash,
            client_events.c.result,
        ).where(client_events.c.story_id == story_id, client_events.c.client_event_id.in_(ids))
    )
    return {r.client_event_id: _Prior(r.payload_hash, dict(r.result)) for r in found}


def _classify(
    request: IngestRequest, priors: dict[str, _Prior]
) -> tuple[dict[int, tuple[EventResult, int]], tuple[tuple[int, IngestEvent], ...]]:
    replays: dict[int, tuple[EventResult, int]] = {}
    new: list[tuple[int, IngestEvent]] = []
    conflicts: list[dict[str, Any]] = []
    for index, event in enumerate(request.events):
        prior = priors.get(event.client_event_id)
        if prior is None:
            new.append((index, event))
        elif prior.payload_hash != event.payload_hash:
            conflicts.append({"client_event_id": event.client_event_id, "index": index})
        else:
            result = EventResult(
                client_event_id=event.client_event_id,
                operation_id=str(prior.result["operation_id"]),
                turn=int(prior.result["turn"]),
                replayed=True,
            )
            replays[index] = (result, int(prior.result["timeline_revision"]))
    if conflicts:
        raise ClientEventConflict(
            "a client_event_id was already ingested with a different payload",
            {"client_event_ids": conflicts},
        )
    return replays, tuple(new)


async def _prepare(
    db: AsyncEngine, story_id: str, request: IngestRequest, expected_revision: int | None
) -> _Prepared:
    story = await read_story_cast(db, story_id)
    check_actors(story, request)
    async with db.connect() as conn:
        priors = await _priors(conn, story_id, [e.client_event_id for e in request.events])
    replays, new = _classify(request, priors)
    prepared = _Prepared(story=story, replays=replays, new=new)
    if not new:
        return prepared
    if story.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )
    await pause.refuse(db, story_id, story.status)
    await check_expected_revision(db, story_id, expected_revision)
    changes = [event for _, event in new if not event.is_turn]
    if changes:
        async with db.connect() as conn:
            for event in changes:
                assert event.state is not None  # noqa: S101 - the model requires it
                await state_api.check_settable(conn, story_id, event.state)
    return prepared


def gate_groups(new: Sequence[tuple[int, IngestEvent]]) -> list[GateGroup]:
    """The new events' texts grouped by actor (first appearance first), each text its own."""
    groups: dict[str | None, list[tuple[str, str]]] = {}
    for index, event in new:
        if not event.is_turn:
            continue  # a State set is no player input (``PUT /states`` is not gated either)
        groups.setdefault(event.actor_character_id, []).append(
            (LOCATOR.format(index=index), event.gated_text)
        )
    return [GateGroup(actor=actor, texts=tuple(texts)) for actor, texts in groups.items()]


# -- recording -------------------------------------------------------------------------------


def stage_event(op: Operation, event: IngestEvent) -> None:
    """Stage one ingested event on its own operation: a turn's at beat 1; a ``state_change``'s
    ``state_set`` event, and the change itself as a commit step at the slot's position."""
    if event.kind == STATE_CHANGE:
        _stage_state_change(op, event)
        return
    if event.kind == PLAYER_TURN:
        op.append(
            "player_input",
            PlayerInput(text=joined_input(event.say, event.do), say=event.say, do=event.do),
            provenance=Provenance.CONSUMER,
            character_id=event.actor_character_id,
            beat=INPUT_BEAT,
        )
        return
    op.append(
        "message_rendered",
        MessageRendered(
            text=event.text or "",
            render_character_ids=(),
            model=None,
            prompt_hash=None,
            finish_reason=CONSUMER_FINISH_REASON,
        ),
        provenance=Provenance.CONSUMER,
        authority=Authority.AUTHORITATIVE,
        character_id=None,
        beat=INPUT_BEAT,
    )


def _stage_state_change(op: Operation, event: IngestEvent) -> None:
    assert event.state is not None  # noqa: S101 - the model requires it
    assert event.reason is not None  # noqa: S101 - the model requires it
    name = event.state
    change = state_api.consumer_change(
        name,
        value=event.value,
        delta=event.delta,
        scope_key=event.scope_key,
        reason=event.reason,
    )
    op.append(
        "state_set",
        StateSet(source=state_api.CONSUMER, kind=change.kind.value),
        provenance=Provenance.CONSUMER,
    )

    async def write(conn: AsyncConnection) -> None:
        story_id = op.story_id
        assert story_id is not None  # noqa: S101 - an ingest's operations are its story's
        await state_api.check_settable(conn, story_id, name)
        applied = await states.apply_change(
            conn, op, state_api.consumer_change_at(change, slot_position(op))
        )
        if isinstance(applied, Violation):
            raise state_api.refused(name, applied)

    op.add_commit_step(write)


def _operation(
    db: AsyncEngine,
    story_id: str,
    event: IngestEvent,
    *,
    principal: str,
    idempotency_key: str | None,
    operation_id: str | None,
    queued: bool,
) -> Operation:
    """The operation one new event is recorded by: a ``turn`` for a narrative event, an
    ``inter_turn`` slot for a ``state_change`` (positioned at commit after the turn written
    before it in the batch, the batch's operations being written in order)."""
    if event.is_turn:
        return Operation(
            db,
            story_id=story_id,
            kind=OperationKind.TURN,
            principal=principal,
            provenance=Provenance.CONSUMER,
            idempotency_key=idempotency_key,
            origin=INGESTED,
            operation_id=operation_id,
            queued=queued,
        )
    if queued:
        # A queued batch's first operation is the queued row the drainer starts (a turn).
        return Operation(
            db,
            story_id=story_id,
            kind=OperationKind.INTER_TURN,
            principal=principal,
            provenance=Provenance.CONSUMER,
            bumps_revision=True,
            idempotency_key=idempotency_key,
            origin=INGESTED,
            operation_id=operation_id,
            queued=True,
            slot=True,
        )
    return Operation(
        db,
        story_id=story_id,
        kind=OperationKind.INTER_TURN,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
        origin=INGESTED,
        operation_id=operation_id,
        slot=True,
    )


def _timeline_only(state: RevalidationState) -> Decision:
    """Ingest's commit decision: abandon after a control action, otherwise commit (an ingest
    generated nothing, so a moved revision or a touched read set changes nothing)."""
    if state.control_since_base:
        return Abandon(control_operation_id=state.control_operation_id)
    return Commit()


async def _record(
    db: AsyncEngine,
    story_id: str,
    new: Sequence[tuple[int, IngestEvent]],
    *,
    principal: str,
    idempotency_key: str | None,
    first_operation_id: str,
    queued: bool = False,
) -> tuple[dict[int, EventResult], int]:
    """Record the new events as one all-or-nothing batch of ``turn`` operations. The first
    operation holds the story's turn slot (``first_operation_id``; a queued batch's first
    operation is its ``queued`` row) and answers for the batch (idempotency, lease, 202)."""
    ops = [
        _operation(
            db,
            story_id,
            event,
            principal=principal,
            idempotency_key=idempotency_key if position == 0 else None,
            operation_id=first_operation_id if position == 0 else None,
            queued=queued and position == 0,
        )
        for position, (_, event) in enumerate(new)
    ]
    ops[0].holds_turn_slot = True
    batch = OperationBatch(db, story_id, ops)
    await batch.begin()
    try:
        for op, (_, event) in zip(ops, new, strict=True):
            stage_event(op, event)
    except BaseException as exc:
        await batch.abandon(exc)
        raise
    ids = [event.client_event_id for _, event in new]

    async def before(conn: AsyncConnection) -> None:
        if await _priors(conn, story_id, ids):
            raise _DuplicateArrived

    async def after(conn: AsyncConnection) -> None:
        revision = batch.last.timeline_revision
        await conn.execute(
            insert(client_events),
            [
                {
                    "story_id": story_id,
                    "client_event_id": event.client_event_id,
                    "payload_hash": event.payload_hash,
                    "operation_id": op.operation_id,
                    "result": {
                        "operation_id": op.operation_id,
                        "turn": op.narrative_turn,
                        "timeline_revision": revision,
                    },
                }
                for op, (_, event) in zip(ops, new, strict=True)
            ],
        )

    try:
        await batch.commit(decide=_timeline_only, before=[before], after=[after])
    except RevalidationRequired as signal:
        raise TimelineChanged(
            "a control action changed the timeline while the ingest was recorded",
            {
                "current_revision": signal.state.current_revision,
                "control_operation_id": getattr(signal.outcome, "control_operation_id", None),
            },
        ) from None
    results: dict[int, EventResult] = {}
    for op, (index, event) in zip(ops, new, strict=True):
        assert op.narrative_turn is not None  # noqa: S101 - a committed turn has a position
        results[index] = EventResult(
            client_event_id=event.client_event_id,
            operation_id=op.operation_id,
            turn=op.narrative_turn,
            replayed=False,
        )
    assert batch.last.timeline_revision is not None  # noqa: S101 - set by the commit
    return results, batch.last.timeline_revision


# -- outcomes (#217) ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Effect:
    """What an ingested outcome answers: an outbox row, or a consumer-applied effect."""

    outbox_id: str | None
    effect_id: str | None


async def _effect_of(
    conn: AsyncConnection, story_id: str, index: int, event: IngestEvent
) -> _Effect:
    """The effect ``event`` names (an outbox row's id is also its effect id), or 422
    ``effect_not_found``."""
    wanted = event.outbox_id or event.effect_id
    o = external_effect_outbox.c
    row = await conn.scalar(select(o.id).where(o.story_id == story_id, o.id == wanted))
    if row is not None:
        return _Effect(str(row), None)
    if event.effect_id is not None:
        e = events.c
        found = await conn.scalar(
            select(e.id)
            .where(
                e.story_id == story_id,
                e.type == REQUESTED_EFFECT,
                e.payload["effect_id"].astext == event.effect_id,
            )
            .limit(1)
        )
        if found is not None:
            return _Effect(None, event.effect_id)
    field = "outbox_id" if event.outbox_id is not None else "effect_id"
    raise EffectNotFound(
        f"the story has no external effect {wanted}",
        {"locator": LOCATOR.format(index=index), field: wanted},
    )


async def _prepare_outcomes(
    db: AsyncEngine, story_id: str, request: IngestRequest
) -> tuple[_Prepared, list[_Effect]]:
    story = await read_story_cast(db, story_id)
    async with db.connect() as conn:
        priors = await _priors(conn, story_id, [e.client_event_id for e in request.events])
        replays, new = _classify(request, priors)
        effects = [await _effect_of(conn, story_id, i, e) for i, e in new]
    return _Prepared(story=story, replays=replays, new=new), effects


async def _ingest_outcomes(
    db: AsyncEngine,
    story_id: str,
    request: IngestRequest,
    *,
    principal: str,
    idempotency_key: str | None,
    settle: Settle | None,
) -> IngestResult:
    """Record a batch of outcomes (see the module docstring): one position-less operation, no
    turn slot, no revision bump."""
    for _ in range(COMMIT_ATTEMPTS):
        prepared, effects = await _prepare_outcomes(db, story_id, request)
        if not prepared.new:
            return _replayed(request, prepared)
        ids = [new_ulid() for _ in prepared.new]
        op = Operation(
            db,
            story_id=story_id,
            kind=OperationKind.INTER_TURN,
            principal=principal,
            provenance=Provenance.CONSUMER,
            bumps_revision=False,
            idempotency_key=idempotency_key,
            origin=INGESTED,
        )
        revision: list[int] = []

        async def step(
            conn: AsyncConnection,
            ids: list[str] = ids,
            prepared: _Prepared = prepared,
            effects: list[_Effect] = effects,
            op: Operation = op,
            revision: list[int] = revision,
        ) -> None:
            if await _priors(conn, story_id, [e.client_event_id for _, e in prepared.new]):
                raise _DuplicateArrived
            now = clock.now()
            for outcome_id, (_, event), effect in zip(ids, prepared.new, effects, strict=True):
                assert event.outcome is not None  # noqa: S101 - the model requires it
                payload = (
                    {"resulting_values": dict(event.resulting_values)}
                    if event.resulting_values
                    else {}
                )
                if effect.outbox_id is not None:
                    recorded = await core_outbox.record_outcome(
                        conn,
                        effect.outbox_id,
                        event.outcome,
                        payload=payload,
                        via=VIA_INGEST,
                        now=now,
                        outcome_id=outcome_id,
                    )
                else:
                    assert effect.effect_id is not None  # noqa: S101 - one of the two
                    recorded = await core_outbox.record_effect_outcome(
                        conn,
                        story_id,
                        effect.effect_id,
                        event.outcome,
                        payload=payload,
                        via=VIA_INGEST,
                        now=now,
                        outcome_id=outcome_id,
                    )
                if settle is not None and recorded.kind in core_outbox.TERMINAL:
                    await settle(conn, story_id, outcome_id)
            current = await conn.scalar(
                select(stories.c.timeline_revision).where(stories.c.id == story_id)
            )
            revision.append(int(current or 0))
            await conn.execute(
                insert(client_events),
                [
                    {
                        "story_id": story_id,
                        "client_event_id": event.client_event_id,
                        "payload_hash": event.payload_hash,
                        "operation_id": op.operation_id,
                        "result": {
                            "operation_id": op.operation_id,
                            "turn": OUTCOME_TURN,
                            "timeline_revision": revision[0],
                        },
                    }
                    for _, event in prepared.new
                ],
            )

        try:
            async with op:
                for outcome_id, (_, event), effect in zip(ids, prepared.new, effects, strict=True):
                    assert event.outcome is not None  # noqa: S101 - the model requires it
                    op.append(
                        OUTCOME_RECORDED,
                        OutcomeRecorded(
                            outcome_id=outcome_id,
                            outbox_id=effect.outbox_id,
                            effect_id=effect.effect_id,
                            status=event.outcome,
                            received_via="ingest",
                        ),
                        provenance=Provenance.CONSUMER,
                        character_id=None,
                    )
                op.add_commit_step(step)
                await op.commit()
        except _DuplicateArrived:
            continue
        recorded_results = {
            index: EventResult(
                client_event_id=event.client_event_id,
                operation_id=op.operation_id,
                turn=OUTCOME_TURN,
                replayed=False,
            )
            for index, event in prepared.new
        }
        merged = {**{i: r for i, (r, _) in prepared.replays.items()}, **recorded_results}
        return IngestResult(
            results=[merged[i] for i in range(len(request.events))],
            timeline_revision=revision[0],
        )
    raise StoryBusy(  # pragma: no cover - three collisions in a row
        "concurrent ingests of the same outcomes kept colliding; retry",
        {"reason": "contended", "story_id": story_id},
    )


def _outcomes_alone(request: IngestRequest) -> bool:
    """Whether the batch is outcomes (422 when it mixes them with turns or State sets)."""
    found = [e.kind == OUTCOME for e in request.events]
    if any(found) and not all(found):
        raise InvalidRequest(
            "outcomes are ingested in a batch of their own",
            {"field": "events", "reason": "mixed_outcomes"},
        )
    return all(found)


class QueueBatch(Protocol):
    """``queue_batch(operation_id, request, concurrency, operation_kind)``: queue the batch as
    one turn (#63), its first operation of ``operation_kind``, and wait for its result (or raise
    ``TurnAccepted``); the Engine provides it."""

    def __call__(
        self,
        operation_id: str,
        queued: dict[str, Any],
        concurrency: Concurrency,
        operation_kind: OperationKind = ...,
    ) -> Awaitable[IngestResult]: ...


async def ingest(
    db: AsyncEngine,
    story_id: str,
    request: IngestRequest,
    *,
    principal: str,
    gate: Gate,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
    queue_batch: QueueBatch | None = None,
    settle: Settle | None = None,
) -> IngestResult:
    """Record ``request``'s events; see the module docstring for the rules and their order."""
    check_kinds(request)
    if _outcomes_alone(request):
        return await _ingest_outcomes(
            db,
            story_id,
            request,
            principal=principal,
            idempotency_key=idempotency_key,
            settle=settle,
        )
    gated: set[int] = set()
    duplicates = 0
    deadline: float | None = None
    while True:
        prepared = await _prepare(db, story_id, request, expected_revision)
        if not prepared.new:
            return _replayed(request, prepared)
        ungated = [(i, e) for i, e in prepared.new if i not in gated]
        if ungated:
            await gate(gate_groups(ungated))
            gated.update(i for i, _ in ungated)
        concurrency = await turn_gate.read_concurrency(db, story_id)
        if deadline is None:
            deadline = time.monotonic() + concurrency.turn_deadline
        first = new_ulid()
        held = await turn_gate.acquire(db, story_id, first, lease_s=concurrency.lease_s)
        if held is not None:
            if concurrency.mode == "queue" and queue_batch is not None:
                first_event = prepared.new[0][1]
                return await queue_batch(
                    first,
                    {"body": request.model_dump(mode="json")},
                    concurrency,
                    OperationKind.TURN if first_event.is_turn else OperationKind.INTER_TURN,
                )
            starting = held.operation_id is not None and held.origin in (INGESTED, None)
            if starting and time.monotonic() < deadline:
                # Another ingest's commit is short (a holder with no operation row yet is just
                # starting): wait for it, then look again (a duplicate becomes a replay).
                await turn_gate.wakeups.wait(story_id, turn_gate.POLL_S)
                continue
            raise held.busy(story_id)
        try:
            async with turn_gate.renewing(db, story_id, first, concurrency.lease_s):
                recorded, revision = await _record(
                    db,
                    story_id,
                    prepared.new,
                    principal=principal,
                    idempotency_key=idempotency_key,
                    first_operation_id=first,
                )
        except BaseException as exc:
            await turn_gate.release_now(db, story_id, first)
            if not isinstance(exc, _DuplicateArrived):
                raise
            duplicates += 1
            if duplicates >= COMMIT_ATTEMPTS:  # pragma: no cover - three collisions in a row
                raise StoryBusy(
                    "concurrent ingests of the same events kept colliding; retry",
                    {"reason": "contended", "story_id": story_id},
                ) from None
            continue
        merged = {**{i: r for i, (r, _) in prepared.replays.items()}, **recorded}
        return IngestResult(
            results=[merged[i] for i in range(len(request.events))], timeline_revision=revision
        )


async def run_queued(
    db: AsyncEngine,
    story_id: str,
    request: IngestRequest,
    *,
    principal: str,
    idempotency_key: str | None,
    operation_id: str,
) -> IngestResult:
    """Record a queued ingest (#63): the drainer took the story's turn slot for it. The batch
    passed its prechecks and the input gate when it was queued; events another ingest recorded
    meanwhile replay. The batch's first operation is the queued one."""
    prepared = await _prepare(db, story_id, request, None)
    if not prepared.new:
        return _replayed(request, prepared)
    try:
        recorded, revision = await _record(
            db,
            story_id,
            prepared.new,
            principal=principal,
            idempotency_key=idempotency_key,
            first_operation_id=operation_id,
            queued=True,
        )
    except _DuplicateArrived:  # pragma: no cover - only past an expired lease
        raise StoryBusy(
            "another ingest of the same events committed meanwhile; retry",
            {"reason": "contended", "story_id": story_id},
        ) from None
    merged = {**{i: r for i, (r, _) in prepared.replays.items()}, **recorded}
    return IngestResult(
        results=[merged[i] for i in range(len(request.events))], timeline_revision=revision
    )


def _replayed(request: IngestRequest, prepared: _Prepared) -> IngestResult:
    return IngestResult(
        results=[prepared.replays[i][0] for i in range(len(request.events))],
        timeline_revision=max(revision for _, revision in prepared.replays.values()),
    )


__all__ = [
    "LATER_KINDS",
    "MAX_EVENTS",
    "NARRATION",
    "OUTCOME",
    "PLAYER_TURN",
    "STATE_CHANGE",
    "SUPPORTED_KINDS",
    "EventResult",
    "GateGroup",
    "IngestEvent",
    "IngestRequest",
    "IngestResult",
    "Settle",
    "check_kinds",
    "gate_groups",
    "ingest",
    "run_queued",
    "stage_event",
]
