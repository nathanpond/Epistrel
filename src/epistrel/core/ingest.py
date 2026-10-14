"""Ingest: record consumer events into authoritative history without generating (FR-RESP-10).

A body carries up to ``MAX_EVENTS`` events, each with a ``client_event_id`` unique within the
story. Two kinds exist in M2:

- ``player_turn``: an actor's ``say``/``do``, recorded as a ``player_input`` event at beat 1;
- ``narration``: consumer-authored ``text`` with no actor, recorded as a ``message_rendered``
  event at beat 1 with no character, ``finish_reason`` ``consumer`` and no model or prompt hash.

Both are narrative turns: each event is its own ``turn`` operation (``origin = ingested``,
provenance ``consumer``, no viewpoint) at the next turn index, and each bumps the timeline
revision (FR-CONC-1). The batch is all or nothing (``OperationBatch``). ``outcome`` and
``state_change`` arrive in M6 and are refused 422 ``unsupported_event_kind``.

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
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import pause, turn_gate
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
    InvalidRequest,
    StoryArchived,
    StoryBusy,
    TimelineChanged,
)
from epistrel.core.events import Operation, OperationBatch, UnregisteredEventType
from epistrel.core.events.operation import Withdrawn
from epistrel.core.events.types import MessageRendered, PlayerInput
from epistrel.core.ids import new_ulid
from epistrel.core.tables import client_events
from epistrel.core.turn_gate import Concurrency
from epistrel.core.turns import INPUT_BEAT, StoryCast, joined_input, read_story_cast

MAX_EVENTS = 50
MAX_TEXT_CHARS = 8_000
MAX_CLIENT_EVENT_ID_CHARS = 255
CLIENT_EVENT_ID_PATTERN = r"^[A-Za-z0-9._:-]+$"
"""#61's key rule: at most 255 characters of ``[A-Za-z0-9._:-]``."""
MAX_ACTOR_ID_CHARS = 64

PLAYER_TURN = "player_turn"
NARRATION = "narration"
SUPPORTED_KINDS = (PLAYER_TURN, NARRATION)
LATER_KINDS = {"outcome": "M6", "state_change": "M6"}
"""Kinds docs/03 plans that M2 does not record yet, and the milestone that brings them."""

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
    """One event to record. ``kind`` is free text here so that a kind M2 does not record is
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

    @field_validator("say", "do", "text")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        return normalised(value)

    @model_validator(mode="after")
    def _fields_fit_the_kind(self) -> Self:
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
    """422 ``unsupported_event_kind`` for a kind M2 does not record (naming its milestone)."""
    for index, event in enumerate(request.events):
        if event.kind in SUPPORTED_KINDS:
            continue
        later = LATER_KINDS.get(event.kind)
        message = (
            f"ingesting {event.kind!r} events arrives in {later}; M2 records "
            f"{' and '.join(SUPPORTED_KINDS)}"
            if later
            else f"{event.kind!r} is not an ingestable kind; M2 records "
            f"{' and '.join(SUPPORTED_KINDS)}"
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
    return prepared


def gate_groups(new: Sequence[tuple[int, IngestEvent]]) -> list[GateGroup]:
    """The new events' texts grouped by actor (first appearance first), each text its own."""
    groups: dict[str | None, list[tuple[str, str]]] = {}
    for index, event in new:
        groups.setdefault(event.actor_character_id, []).append(
            (LOCATOR.format(index=index), event.gated_text)
        )
    return [GateGroup(actor=actor, texts=tuple(texts)) for actor, texts in groups.items()]


# -- recording -------------------------------------------------------------------------------


def stage_event(op: Operation, event: IngestEvent) -> None:
    """Stage one ingested event on its own turn operation, at beat 1."""
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
        Operation(
            db,
            story_id=story_id,
            kind=OperationKind.TURN,
            principal=principal,
            provenance=Provenance.CONSUMER,
            idempotency_key=idempotency_key if position == 0 else None,
            origin=INGESTED,
            operation_id=first_operation_id if position == 0 else None,
            queued=queued and position == 0,
        )
        for position in range(len(new))
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


QueueBatch = Callable[[str, dict[str, Any], Concurrency], Awaitable["IngestResult"]]
"""``queue_batch(operation_id, request, concurrency)``: queue the batch as one turn (#63) and
wait for its result (or raise ``TurnAccepted``); the Engine provides it."""


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
) -> IngestResult:
    """Record ``request``'s events; see the module docstring for the rules and their order."""
    check_kinds(request)
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
                return await queue_batch(
                    first, {"body": request.model_dump(mode="json")}, concurrency
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
    "PLAYER_TURN",
    "SUPPORTED_KINDS",
    "EventResult",
    "GateGroup",
    "IngestEvent",
    "IngestRequest",
    "IngestResult",
    "check_kinds",
    "gate_groups",
    "ingest",
    "run_queued",
    "stage_event",
]
