"""Staged turns: a turn that waits on the game (FR-STORE-5's exception, FR-EXT-15,16,19;
docs/02 §4.8; #212).

A structured turn becomes **staged** when, after every planned render was verified and before
its single commit, a surviving effect is on a ``synchronous`` write-capable provider's State
(``plans_staged``; ``effects.Book.synchronous``). Prose may not outrun confirmation, so the
turn commits as a workflow of atomic stages, each the same operation's work
(``core.events.operation``: ``reopen``), tracked in ``operations.turn_stage`` (``status`` is
``committed`` from intent on) and logged in ``stage_log`` as ``{stage, status, at,
revision}``:

1. **intent** (the pipeline's ordinary commit, ``turn_stage = intent_committed``): the verified
   beats with each action as an attempt (output policies checked, since they are readable from
   then on), #216's committed, ``held`` and ``pending`` rows, one ``intended`` outbox row per
   external effect (#210) and the pinned reads; no message yet (history reads the turn
   ``incomplete``); ``timeline_revision + 1``; the turn slot stays held.
2. **claim** (``claim_all``): #210's ``claim`` of every synchronous row of the turn (a row whose
   provider is no longer write-capable is cancelled and listed, FR-CONC-16); ``turn_stage =
   awaiting_outcome``; no revision change.
3. **outcome** (``await_outcomes``, then ``record_outcomes``): the claimed rows are sent
   (``send``) and the stage waits until every row has an outcome or the longest
   ``confirm_wait_max_seconds`` among the turn's synchronous providers has passed since the
   first claim, woken by ``core.outbox.OUTCOME_CHANNEL`` (LISTEN) and a poll (``POLL_S``): a
   row another worker dispatches is waited on the same way. The turn's ``TurnBudget`` deadline
   clock stands still meanwhile (NFR-PERF-6). Then **one** transaction records one positioned
   ``external_outcome`` event per row with an outcome (``confirmed``, ``failed`` or
   ``unknown``), #215's post-effect version of its value (``pinned.settle_value``), and #216's
   settlement of the conditional effects and reservations (``contingency.settle``), all at the
   outcome step's first position; ``turn_stage = outcome_recorded``; ``revision + 1``. A row
   still without an outcome stays ``claimed`` (its send is abandoned here; the lease timer and
   #217 take it from there), its contingent rows pending or held. Effects on ``asynchronous``
   providers in the same turn take #210's ordinary path and never settle here.
4. **completion** (the pipeline): one narrator render narrating every result (``lines``: done,
   a failed attempt, or still pending), verified at its own positions, committed as later beats
   of the same turn with the turn's commit hooks, and the turn's message composed over all its
   committed beats; ``turn_stage = completed``; ``revision + 1``; the slot is given back. When
   the outcome render cannot run, ``deterministic_outcome`` narrates the results instead (a
   minimal template; #213 completes it).
5. **response** (``store_response``): the finished response stored under the idempotency key;
   no revision change.

Log lines carry ids, statuses and counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import outbox as core_outbox
from epistrel.core import providers as core_providers
from epistrel.core.enums import UNFINISHED_TURN_STAGES, OperationStatus, StagedTurnStage
from epistrel.core.errors import TimelineChanged
from epistrel.core.events.operation import Operation, append_stage, stage_entry
from epistrel.core.idempotency import IdempotentCall
from epistrel.core.membership import Position
from epistrel.core.states import contingency
from epistrel.core.states import effects as rules
from epistrel.core.tables import events, operations
from epistrel.core.tables.outbox import (
    CANCELLED,
    CONFIRMED,
    FAILED,
    UNKNOWN,
    external_effect_outbox,
    outbox_outcomes,
)
from epistrel.core.turns.beats import TurnBeat
from epistrel.orchestrator.jobs.handlers import outbox as dispatcher
from epistrel.orchestrator.jobs.worker import listen_dsn
from epistrel.orchestrator.providers import pinned
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import WriteOutcome
from epistrel.orchestrator.structured import effects as state_effects

log = logging.getLogger(__name__)

INTENT = "intent"
CLAIM = "claim"
OUTCOME = "outcome"
COMPLETION = "completion"
RESPONSE = "response"
STAGES = (INTENT, CLAIM, OUTCOME, COMPLETION, RESPONSE)
"""The stage names ``stage_log`` records (``core.operation_view.TURN_STAGES``)."""

PENDING = "pending"
"""An answer for a row with no recorded outcome when the wait ended (it stays ``claimed``)."""
RECORDED = frozenset({CONFIRMED, FAILED, UNKNOWN})
"""The answers an ``external_outcome`` event records."""
POLL_S = 0.25
"""How often the wait re-reads its rows besides the LISTEN wake-up."""

OUTCOME_STEP = "## Outcome step"
"""The first line of the outcome render's constraints (``lines``)."""


def is_staged_turn(found: Operation | StagedTurnStage | str | None) -> bool:
    """Whether ``found`` (an operation, or an ``operations.turn_stage`` value) is a staged turn:
    ``turn_stage`` is set. Not ``Operation.staged`` (the events staged for a commit)."""
    if isinstance(found, Operation):
        return found.is_staged_turn
    return found is not None


def unfinished(stage: StagedTurnStage | str | None) -> bool:
    """Whether a staged turn at ``stage`` has not completed (nor been abandoned)."""
    return stage is not None and StagedTurnStage(stage) in UNFINISHED_TURN_STAGES


# -- planning ------------------------------------------------------------------------------------


def synchronous_of(
    book: state_effects.Book, requested: Sequence[state_effects.Requested]
) -> list[state_effects.Requested]:
    """The turn's dispatched effects on a ``synchronous`` provider's State, in order."""
    return [r for r in requested if r.dispatch and r.state in book.synchronous]


def plans_staged(book: state_effects.Book, beats: Sequence[TurnBeat]) -> bool:
    """Whether ``beats``' surviving effects make the turn a staged turn: one is dispatched to a
    ``synchronous`` write-capable provider (decided after every render was verified, before the
    commit; an ordinary turn's commit is untouched otherwise)."""
    if not book.on or not book.synchronous:
        return False
    return bool(synchronous_of(book, state_effects.requested_of(book, beats)))


def wait_of(book: state_effects.Book, synchronous: Sequence[state_effects.Requested]) -> float:
    """The confirmation wait: the longest ``confirm_wait_max_seconds`` among the turn's
    synchronous providers (owner decision, 2026-10-30)."""
    return float(max((book.synchronous.get(r.state, 0) for r in synchronous), default=0))


async def next_beat(db: AsyncEngine, operation_id: str) -> int:
    """The outcome step's first beat: after every event the intent committed (its beats and
    their audit records)."""
    async with db.connect() as conn:
        last = await conn.scalar(
            select(func.max(events.c.beat)).where(events.c.operation_id == operation_id)
        )
    return int(last or 1) + 1


# -- the claim -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claims:
    """What stage 2 did: the rows it claimed, ready to send, and how each other row ended
    (``skipped`` when another worker holds it, ``cancelled``, ``held``)."""

    claimed: tuple[dispatcher.Claimed, ...]
    other: Mapping[str, str]


async def claim_all(
    db: AsyncEngine,
    op: Operation,
    synchronous: Sequence[state_effects.Requested],
    *,
    registry: InProcessRegistry | None,
) -> Claims:
    """Stage 2: claim every synchronous row of the turn (#210's ``claim``, each its own
    transaction, the in-flight point), then move the turn to ``awaiting_outcome`` and log the
    stage (no revision change). A discard that abandoned the turn meanwhile is 409
    ``timeline_changed``."""
    claimed: list[dispatcher.Claimed] = []
    other: dict[str, str] = {}
    for request in synchronous:
        found = await dispatcher.claim(db, request.effect_id, registry=registry)
        if isinstance(found, dispatcher.Claimed):
            claimed.append(found)
        else:
            other[request.effect_id] = found
    async with db.begin() as conn:
        moved = await append_stage(
            conn,
            op.operation_id,
            stage_entry(CLAIM, "done", op.timeline_revision),
            turn_stage=StagedTurnStage.AWAITING_OUTCOME,
        )
    if not moved:
        raise TimelineChanged(
            "a control action discarded the staged turn before its claim",
            {"current_revision": op.timeline_revision, "operation_id": op.operation_id},
        )
    op.turn_stage = StagedTurnStage.AWAITING_OUTCOME
    log.info("staged turn: %d row(s) claimed, %d not", len(claimed), len(other))
    return Claims(tuple(claimed), other)


# -- the wait ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the game answered for one of the turn's synchronous effects when the wait ended:
    ``confirmed``, ``failed``, ``unknown`` (an ambiguous answer), ``pending`` (none yet) or
    ``cancelled`` (never sent: its provider is no longer write-capable, or its turn was
    discarded); the adapter's answer (its resulting values) and when it was recorded."""

    request: state_effects.Requested
    status: str
    answer: WriteOutcome | None = None
    answered_at: datetime | None = None

    @property
    def outbox_id(self) -> str:
        return self.request.effect_id

    @property
    def recorded(self) -> bool:
        """An outcome the outcome event records."""
        return self.status in RECORDED and self.answer is not None and self.answered_at is not None


async def read_answers(
    db: AsyncEngine, synchronous: Sequence[state_effects.Requested]
) -> dict[str, Answer]:
    """Each row's answer as committed now (its status, and its latest outcome record)."""
    ids = [r.effect_id for r in synchronous]
    o, x = external_effect_outbox.c, outbox_outcomes.c
    async with db.connect() as conn:
        statuses = {
            str(r.id): str(r.status)
            for r in await conn.execute(select(o.id, o.status).where(o.id.in_(ids)))
        }
        latest: dict[str, Any] = {}
        for row in await conn.execute(
            select(x.outbox_id, x.kind, x.payload, x.raw_hash, x.received_at)
            .where(x.outbox_id.in_(ids), x.kind.in_(sorted(RECORDED)))
            .order_by(x.received_at, x.id)
        ):
            latest[str(row.outbox_id)] = row
    found: dict[str, Answer] = {}
    for request in synchronous:
        status = statuses.get(request.effect_id, PENDING)
        record = latest.get(request.effect_id)
        if status in RECORDED and record is not None:
            payload = dict(record.payload or {})
            found[request.effect_id] = Answer(
                request,
                status,
                WriteOutcome(
                    status=status,  # type: ignore[arg-type]
                    raw_hash=str(record.raw_hash or ""),
                    resulting_values=payload.get("resulting_values"),
                ),
                record.received_at,
            )
        elif status == CANCELLED:
            found[request.effect_id] = Answer(request, CANCELLED)
        else:
            found[request.effect_id] = Answer(request, PENDING)
    return found


async def _listen(dsn: str, woke: asyncio.Event) -> None:
    """LISTEN on ``core.outbox.OUTCOME_CHANNEL``; every notification wakes the wait. Any
    failure leaves the wait to its poll."""
    try:
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        async with conn:
            await conn.execute(f"LISTEN {core_outbox.OUTCOME_CHANNEL}")
            async for _ in conn.notifies():
                woke.set()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("staged turn: outcome LISTEN unavailable: %s", type(exc).__name__)


async def await_outcomes(
    db: AsyncEngine,
    synchronous: Sequence[state_effects.Requested],
    claims: Claims,
    wait_s: float,
) -> dict[str, Answer]:
    """Stage 3's wait: send every claimed row (#210's ``send``, which records the answer in its
    own transaction) and wait until each synchronous row has an answer, or ``wait_s`` has passed
    since the claim (LISTEN plus a ``POLL_S`` poll). A send still running then is abandoned:
    its row stays ``claimed`` (the lease timer and #217 take it from there)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait_s)
    woke = asyncio.Event()
    listener = asyncio.create_task(_listen(listen_dsn(db), woke))
    sends = {asyncio.create_task(dispatcher.send(db, c)) for c in claims.claimed}
    try:
        while True:
            answers = await read_answers(db, synchronous)
            if all(a.status != PENDING for a in answers.values()):
                return answers
            remaining = deadline - loop.time()
            if remaining <= 0:
                return answers
            woke.clear()
            waiter = asyncio.create_task(woke.wait())
            running = {t for t in sends if not t.done()}
            done, _ = await asyncio.wait(
                {waiter, *running},
                timeout=min(POLL_S, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            for task in done & sends:
                if not task.cancelled() and task.exception() is not None:
                    log.warning("staged turn: a send raised %s", type(task.exception()).__name__)
    finally:
        for task in [*sends, listener]:
            task.cancel()
        for task in [*sends, listener]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


# -- the outcome transaction ---------------------------------------------------------------------


@dataclass(slots=True)
class Recorded:
    """What stage 3 committed: each recorded answer's post-effect version (a pin, ``Unknown``
    or ``None``) and what the settlement did."""

    versions: dict[str, Any] = field(default_factory=dict)
    settled: list[contingency.Settled] = field(default_factory=list)

    def granted(self) -> set[tuple[str, str | None]]:
        """The conditional and held changes the settlement committed (State, scope key)."""
        return {(s.state, s.scope_key) for s in self.settled if s.result == contingency.CONVERTED}


def settlement_outcomes(answers: Mapping[str, Answer]) -> dict[str, str]:
    """``contingency.settle``'s ``{effect_id: confirmed | failed | unknown}``: a cancelled
    effect (never sent) settles as ``failed``; one still pending decides nothing."""
    found: dict[str, str] = {}
    for outbox_id, a in answers.items():
        if a.status == CANCELLED:
            found[outbox_id] = FAILED
        elif a.status in RECORDED:
            found[outbox_id] = a.status
    return found


async def record_outcomes(
    db: AsyncEngine,
    op: Operation,
    book: state_effects.Book,
    reads: pinned.PinnedReads,
    answers: Mapping[str, Answer],
    position: Position,
) -> Recorded:
    """Stage 3's one transaction (see the module docstring), at ``position`` (the outcome
    step's first beat): an ``external_outcome`` event per recorded answer with its post-effect
    version (#215), the settlement of the turn's contingent rows (#216), ``turn_stage =
    outcome_recorded``, ``revision + 1``. Committed even when no row answered, so the turn's
    revision moves once per stage."""
    story_id = op.story_id
    assert story_id is not None  # noqa: S101 - a turn belongs to a story
    done = Recorded()
    op.reopen()
    for outbox_id, a in answers.items():
        if not a.recorded:
            continue
        assert a.answer is not None  # noqa: S101 - recorded
        assert a.answered_at is not None  # noqa: S101 - recorded
        outcome = pinned.Outcome(outbox_id, outbox_id, a.answer, a.answered_at)
        definition = book.definitions.get(a.request.state)
        target = (
            None
            if definition is None
            else await pinned.outcome_target(
                db, story_id, definition, a.request.scope_key, position
            )
        )
        provider = None
        if definition is not None and definition.provider_id is not None:
            async with db.connect() as conn:
                provider = await core_providers.find_provider(conn, definition.provider_id)
        if target is None or provider is None:
            pinned.stage_outcome(op, outcome, position)
            continue
        done.versions[outbox_id] = await pinned.settle_value(op, reads, target, outcome, provider)
    outcomes = settlement_outcomes(answers)

    async def settle(conn: Any) -> None:
        done.settled[:] = await contingency.settle(conn, op, outcomes, position)

    if outcomes:
        op.add_commit_step(settle)
    op.turn_stage = StagedTurnStage.OUTCOME_RECORDED
    op.stage_turn(OUTCOME)
    await op.commit()
    log.info(
        "staged turn: outcome recorded (%d answered, %d settled)",
        sum(1 for a in answers.values() if a.recorded),
        len(done.settled),
    )
    return done


# -- what the outcome step narrates --------------------------------------------------------------


def _holder(request: state_effects.Requested, names: Mapping[str, str]) -> str | None:
    return names.get(str(request.scope_key)) if request.scope_key else None


def _version_value(found: Any) -> Any:
    value = getattr(found, "value", None)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


def lines(
    answers: Mapping[str, Answer], recorded: Recorded, names: Mapping[str, str]
) -> tuple[str, ...]:
    """The outcome render's constraints: ``OUTCOME_STEP``, then one line per effect with what
    the game answered and how to narrate it (done; a failed attempt; still pending; not carried
    out), and the value the game reported after it when a version recorded one."""
    found = [
        OUTCOME_STEP,
        "Narrate, in narration beats only, what the game answered for this turn's actions. "
        "Declare no effects. State an outcome as done only where it is confirmed below.",
    ]
    for outbox_id, a in answers.items():
        change = rules.pending_line(
            a.request.payload().model_dump(mode="json"), _holder(a.request, names)
        )
        if a.status == CONFIRMED:
            value = _version_value(recorded.versions.get(outbox_id))
            reported = f" The game now reports {a.request.state} at {value}." if value else ""
            found.append(f"- {change}: confirmed; narrate it as done.{reported}")
        elif a.status == FAILED:
            found.append(f"- {change}: refused; narrate it as a failed attempt.")
        elif a.status == CANCELLED:
            found.append(f"- {change}: not carried out; narrate the attempt as not carried out.")
        else:
            found.append(f"- {change}: no answer yet; narrate it as still pending, never as done.")
    return tuple(found)


def deterministic_texts(answers: Mapping[str, Answer]) -> list[str]:
    """``deterministic_outcome``'s narration, one sentence per effect: a minimal template that
    states no value (#213 completes it)."""
    found: list[str] = []
    for a in answers.values():
        reason = a.request.reason.strip().rstrip(".") or a.request.state.replace("_", " ")
        if a.status == CONFIRMED:
            found.append(f"The game confirms it: {reason}.")
        elif a.status == FAILED:
            found.append(f"The attempt fails: {reason}.")
        elif a.status == CANCELLED:
            found.append(f"The attempt is not carried out: {reason}.")
        else:
            found.append(f"There is no word yet on whether it went through: {reason}.")
    return found


def deterministic_outcome(answers: Mapping[str, Answer], start: int) -> list[TurnBeat]:
    """#212's stub of #213's deterministic outcome narration: one narrator ``narration`` beat
    per effect from ``start`` (``deterministic_texts``), committed through the same completion
    path as a rendered outcome step."""
    return [
        TurnBeat(kind="narration", beat=start + n, text=text, character_id=None, narrator=True)
        for n, text in enumerate(deterministic_texts(answers))
    ]


async def write_report(conn: AsyncConnection, *, operation_id: str, report: dict[str, Any]) -> None:
    """The completion's ``turn_report.staged`` (ids, statuses, counts and repair codes only),
    beside the intent's report, in the completion's commit."""
    found = await conn.scalar(
        select(operations.c.turn_report).where(operations.c.operation_id == operation_id)
    )
    merged = {**(found if isinstance(found, dict) else {}), "staged": report}
    await conn.execute(
        update(operations)
        .where(operations.c.operation_id == operation_id)
        .values(turn_report=merged)
    )


# -- the response --------------------------------------------------------------------------------


async def store_response(
    db: AsyncEngine, op: Operation, call: IdempotentCall | None, value: Any
) -> None:
    """Stage 5: the finished response stored under the idempotency key (when the call has one)
    and the stage logged, in one transaction; no revision change."""
    async with db.begin() as conn:
        if call is not None:
            await call.store(conn, value)
        await append_stage(
            conn, op.operation_id, stage_entry(RESPONSE, "done", op.timeline_revision)
        )


def completed(op: Operation) -> bool:
    """Whether ``op`` is a committed staged turn whose completion committed."""
    return op.status is OperationStatus.COMMITTED and op.turn_stage is StagedTurnStage.COMPLETED


__all__ = [
    "CLAIM",
    "COMPLETION",
    "CONFIRMED",
    "INTENT",
    "OUTCOME",
    "OUTCOME_STEP",
    "PENDING",
    "POLL_S",
    "RECORDED",
    "RESPONSE",
    "STAGES",
    "Answer",
    "Claims",
    "Recorded",
    "StagedTurnStage",
    "await_outcomes",
    "claim_all",
    "completed",
    "deterministic_outcome",
    "deterministic_texts",
    "is_staged_turn",
    "lines",
    "next_beat",
    "plans_staged",
    "read_answers",
    "record_outcomes",
    "settlement_outcomes",
    "store_response",
    "synchronous_of",
    "unfinished",
    "wait_of",
    "write_report",
]
