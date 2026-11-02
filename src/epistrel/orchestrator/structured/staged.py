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
   outcome step's first position; ``turn_stage = outcome_recorded``; ``revision + 1``. The
   settlement runs under the configuration current then (#214, FR-CONC-16:
   ``rebase.revalidate_settlement``): a grant the State's declaration now refuses (its bounds,
   or #188's sources) is not committed, stays pending, and its prerequisite effect is listed
   ``settlement_conflict``; the outcome is preserved either way. A row
   still without an outcome stays ``claimed`` (its send is abandoned here; the lease timer and
   #217 take it from there), its contingent rows pending or held. Effects on ``asynchronous``
   providers in the same turn take #210's ordinary path and never settle here.
4. **completion** (the pipeline): one narrator render narrating every result (``lines``: done,
   a failed attempt, or still pending), verified at its own positions, committed as later beats
   of the same turn with the turn's commit hooks, and the turn's message composed over all its
   committed beats; ``turn_stage = completed``; ``revision + 1``; the slot is given back. When
   the outcome render cannot run, or fails (FR-EXT-17: never undoing the outcome),
   ``deterministic_outcome`` narrates the results instead (#213: one narrator beat per effect,
   in claim order, at the actor's place: "<actor>'s <reason> went through." / "did not go
   through." / "is still pending."; no model call).
5. **response** (``store_response``): the finished response stored under the idempotency key;
   no revision change.

**Resume** (#213, FR-EXT-17): a turn whose process died, or whose stage raised, is left at its
last committed stage, holding its story's slot. ``resume`` continues it from there (taking
the slot over once its lease lapsed): the claim, the re-send under the same provider key of a
row a dead process claimed (or ``unknown`` for a provider that does not deduplicate), the
outcome transaction, and a completion with the deterministic narration (the outcome render's
context lived only in the dead request). It is called by a same-key ``respond`` retry
(``resume_for_response``, which then recomposes the finished response from the committed beats,
``recompose``), by ``outbox_recovery`` and the worker's recovery pass (``resume_stalled``), and
in the background when a request finds the turn holding its slot (``kick``,
``turn_gate.stalled``). The crash points ``staged.*`` (``epistrel.testing.crash``) mark the
stage boundaries.

Log lines carry ids, statuses and counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any

import psycopg
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import outbox as core_outbox
from epistrel.core import providers as core_providers
from epistrel.core import states, turn_gate
from epistrel.core.enums import (
    UNFINISHED_TURN_STAGES,
    OperationStatus,
    Provenance,
    StagedTurnStage,
    StateVisibility,
)
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.epistemic.audience import AudienceBeat
from epistrel.core.epistemic.perception import Qualifiers
from epistrel.core.errors import OriginalInFlightTimeout, TimelineChanged
from epistrel.core.events.operation import Operation, append_stage, stage_entry
from epistrel.core.events.types import REQUESTED_EFFECT, MessageRendered, RequestedEffect
from epistrel.core.history import filtered_turn_beats, staged_message, story_player_view
from epistrel.core.idempotency import IdempotentCall
from epistrel.core.membership import Position
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.states import contingency
from epistrel.core.states import effects as rules
from epistrel.core.tables import characters, events, operations, stories
from epistrel.core.tables.outbox import (
    CANCELLED,
    CLAIMED,
    CONFIRMED,
    FAILED,
    UNKNOWN,
    external_effect_outbox,
    outbox_outcomes,
)
from epistrel.core.turns.beats import TurnBeat, beat_event_ids, stage_beats
from epistrel.core.turns.staged import route_narration
from epistrel.core.viewpoint import is_omniscient
from epistrel.core.world_clock.service import turn_clock_of
from epistrel.orchestrator.jobs.handlers import outbox as dispatcher
from epistrel.orchestrator.jobs.worker import listen_dsn
from epistrel.orchestrator.lite.schemas import FinishedMessage, RespondResult
from epistrel.orchestrator.providers import pinned
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import WriteOutcome
from epistrel.orchestrator.structured import effects as state_effects
from epistrel.orchestrator.structured import rebase
from epistrel.testing import crash

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
SYNCHRONOUS = "synchronous"
"""A provider's ``confirmation`` that makes an effect on its value a staged turn's."""
"""How often the wait re-reads its rows besides the LISTEN wake-up."""

OUTCOME_STEP = "## Outcome step"
"""The first line of the outcome render's constraints (``lines``)."""
SETTLEMENT_CONFLICT = "settlement_conflict"
"""#214: the reconciliation reason of a confirmed effect whose conditional change the outcome
transaction could not commit under the current configuration (#210's full enum)."""


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
    # #213: the claim committed, nothing sent yet.
    crash.hit("staged.after_claim")
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
    *,
    extra: Sequence[Coroutine[Any, Any, Any]] = (),
) -> dict[str, Answer]:
    """Stage 3's wait: send every claimed row (#210's ``send``, which records the answer in its
    own transaction) and wait until each synchronous row has an answer, or ``wait_s`` has passed
    since the claim (LISTEN plus a ``POLL_S`` poll). A send still running then is abandoned:
    its row stays ``claimed`` (the lease timer and #217 take it from there). ``extra`` (#213's
    ``resume``): the re-sends of rows a dead process claimed, run and abandoned the same way."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait_s)
    woke = asyncio.Event()
    listener = asyncio.create_task(_listen(listen_dsn(db), woke))
    sends: set[asyncio.Task[Any]] = {
        *(asyncio.create_task(dispatcher.send(db, c)) for c in claims.claimed),
        *(asyncio.create_task(x) for x in extra),
    }
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

    def conflicts(self) -> list[contingency.Settled]:
        """#214: the changes the settlement could not commit under the current configuration
        (``conflict``): still held pending, their prerequisites listed ``settlement_conflict``."""
        return [s for s in self.settled if s.result == contingency.CONFLICT]


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
        done.settled[:] = await contingency.settle(
            conn, op, outcomes, position, check=rebase.revalidate_settlement
        )
        for found in done.conflicts():
            # #214 (FR-CONC-16): the outcome stands; the grant waits, listed for resolution.
            for outbox_id in found.prerequisites:
                if outbox_id in outcomes:
                    await core_outbox.mark(conn, story_id, outbox_id, SETTLEMENT_CONFLICT)

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
    # #213: the outcome committed, the completion not.
    crash.hit("staged.after_outcome")
    return done


# -- what the outcome step narrates --------------------------------------------------------------


def _holder(request: state_effects.Requested, names: Mapping[str, str]) -> str | None:
    return names.get(str(request.scope_key)) if request.scope_key else None


def _version_value(found: Any) -> Any:
    value = getattr(found, "value", None)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


def all_confirmed(answers: Mapping[str, Answer]) -> bool:
    """#214: every synchronous effect of the turn was confirmed (its exchange went through,
    whatever became of a grant conditional on it)."""
    return bool(answers) and all(a.status == CONFIRMED for a in answers.values())


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
    for held in recorded.conflicts():
        # #214: a grant the current configuration refused at settlement: held pending.
        holder = names.get(str(held.scope_key)) if held.scope_key else None
        what = f"{held.state} for {holder}" if holder else held.state
        found.append(
            f"- the {what}: held pending resolution; narrate it as held pending, never as given."
        )
    return tuple(found)


WENT_THROUGH = "went through"
DID_NOT_GO_THROUGH = "did not go through"
STILL_PENDING = "is still pending"
"""The deterministic outcome narration's three endings (FR-EXT-17; #213)."""


def outcome_sentence(actor: str, answer: Answer) -> str:
    """One effect's deterministic narration (FR-EXT-17, #213), its action the effect's
    ``reason``: ``"<actor>'s <action> went through."`` when ``confirmed``, ``"... did not go
    through."`` when ``failed`` (or never sent: ``cancelled``, which settles as failed), and
    ``"... is still pending."`` otherwise (no answer when the wait ended, or ``unknown``)."""
    action = answer.request.reason.strip().rstrip(".") or (
        answer.request.state.replace("_", " ") or "action"
    )
    if answer.status == CONFIRMED:
        ending = WENT_THROUGH
    elif answer.status in (FAILED, CANCELLED):
        ending = DID_NOT_GO_THROUGH
    else:
        ending = STILL_PENDING
    return f"{actor}'s {action} {ending}."


def deterministic_texts(
    answers: Mapping[str, Answer], actor: str, recorded: Recorded | None = None
) -> list[str]:
    """``deterministic_outcome``'s narration: one sentence per effect (``outcome_sentence``),
    in claim order (``answers``' order), stating no value; #214: then one per change
    ``recorded`` holds pending (a settlement conflict), narrated as held, never as given."""
    found = [outcome_sentence(actor, a) for a in answers.values()]
    for held in [] if recorded is None else recorded.conflicts():
        found.append(f"The {held.state.replace('_', ' ')} is held pending resolution.")
    return found


def deterministic_outcome(
    answers: Mapping[str, Answer],
    start: int,
    *,
    actor: str,
    location: str | None = None,
    recorded: Recorded | None = None,
) -> list[TurnBeat]:
    """The deterministic outcome narration (FR-EXT-17, #213): one narrator ``narration`` beat
    per effect from ``start`` (``deterministic_texts``), located at the actor's ``location``,
    with no model call. The completion commits it when the outcome render cannot run or fails
    (``pipeline``), and a resumed turn always (``resume``)."""
    qualifiers = Qualifiers(location=location) if location is not None else Qualifiers()
    return [
        TurnBeat(
            kind="narration",
            beat=start + n,
            text=text,
            character_id=None,
            narrator=True,
            qualifiers=qualifiers,
        )
        for n, text in enumerate(deterministic_texts(answers, actor, recorded))
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


# -- resume (#213) -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resumed:
    """What ``resume`` did: the stages it committed (``claim``, ``outcome``, ``completion``)
    and where the turn is now (``None``: no committed staged turn). ``taken``: whether this
    call ran the turn (``False``: it was not unfinished, or another process holds its slot with
    a live lease)."""

    operation_id: str
    stage: StagedTurnStage | None
    ran: tuple[str, ...] = ()
    taken: bool = False


async def _requested(db: AsyncEngine, story_id: str, operation_id: str) -> list[_Row]:
    """The turn's effects as committed: its outbox rows (dispatched, #210) and its
    ``requested_effect`` events (read-only providers, #209), in beat order."""
    o = external_effect_outbox.c
    async with db.connect() as conn:
        rows = list(
            await conn.execute(
                select(
                    o.id,
                    o.provider_name,
                    o.scope_key,
                    o.effect,
                    o.beat,
                    o.step,
                    o.status,
                )
                .where(o.operation_id == operation_id, o.executable.is_(True))
                .order_by(o.beat, o.step, o.created_at, o.id)
            )
        )
        found = [
            _Row(
                state_effects.Requested(
                    effect_id=str(r.id),
                    kind=str((r.effect or {}).get("kind") or "change"),
                    state=str((r.effect or {}).get("state") or ""),
                    scope_key=r.scope_key,
                    reason=str((r.effect or {}).get("reason") or ""),
                    beat=int(r.beat),
                    delta=(r.effect or {}).get("delta"),
                    value=(r.effect or {}).get("value"),
                    step=int(r.step),
                    current=(r.effect or {}).get("pinned"),  # #657
                    dispatch=True,
                ),
                str(r.provider_name),
                str(r.status),
            )
            for r in rows
        ]
        requested = await conn.execute(
            select(events.c.payload, events.c.beat, events.c.step)
            .where(events.c.operation_id == operation_id, events.c.type == REQUESTED_EFFECT)
            .order_by(events.c.seq)
        )
        for e in requested:
            payload = RequestedEffect.model_validate(e.payload)
            found.append(
                _Row(
                    state_effects.Requested(
                        effect_id=payload.effect_id,
                        kind=payload.kind,
                        state=payload.state,
                        scope_key=payload.scope_key,
                        reason=payload.reason,
                        beat=int(e.beat or 0),
                        delta=payload.delta,
                        value=payload.value,
                        minutes=payload.minutes,
                        step=int(e.step or 0),
                    ),
                    None,
                    None,
                )
            )
        definitions = {d.name: d for d in await states.definitions(conn, story_id)}
    for row in found:
        definition = definitions.get(row.request.state)
        if definition is not None and definition.visibility is not StateVisibility.PLAYER_VISIBLE:
            row.request = dataclass_replace(row.request, hidden=True)
    return sorted(found, key=lambda r: (r.request.beat, r.request.step))


@dataclass(slots=True)
class _Row:
    request: state_effects.Requested
    provider: str | None
    """The outbox row's provider (``None``: a read-only provider's requested effect)."""
    status: str | None


async def synchronous_rows(
    db: AsyncEngine, story_id: str, operation_id: str
) -> tuple[list[state_effects.Requested], float, dict[str, str]]:
    """The turn's synchronous effects in claim order, the confirmation wait (the longest
    ``confirm_wait_max_seconds`` of their providers) and each row's status, as committed."""
    found: list[state_effects.Requested] = []
    statuses: dict[str, str] = {}
    wait = 0
    async with db.connect() as conn:
        for row in await _requested(db, story_id, operation_id):
            if row.provider is None:
                continue
            provider = await core_providers.find_provider(conn, row.provider)
            if provider is None or provider.confirmation != SYNCHRONOUS:
                continue
            found.append(row.request)
            statuses[row.request.effect_id] = str(row.status)
            wait = max(wait, int(provider.confirm_wait_max_seconds))
    return found, float(wait), statuses


async def _book(db: AsyncEngine, story_id: str) -> state_effects.Book:
    """The States an outcome transaction reads (``record_outcomes``: the definitions)."""
    async with db.connect() as conn:
        found = await states.definitions(conn, story_id)
    return state_effects.Book(story_id, definitions={d.name: d for d in found})


async def _actor(db: AsyncEngine, story_id: str, operation_id: str) -> tuple[str | None, str]:
    """The turn's acting character (its ``player_input``'s) and the name the narration uses."""
    async with db.connect() as conn:
        actor = await conn.scalar(
            select(events.c.character_id)
            .where(events.c.operation_id == operation_id, events.c.type == "player_input")
            .limit(1)
        )
        name = (
            None
            if actor is None
            else await conn.scalar(select(characters.c.name).where(characters.c.id == actor))
        )
    return (None if actor is None else str(actor)), str(name or "The player")


async def _place(db: AsyncEngine, story_id: str, turn: int, character_id: str | None) -> str | None:
    """Where ``character_id`` is once the turn's committed moves are made."""
    if character_id is None:
        return None
    async with db.connect() as conn:
        state, moves = await scene_loader.load_scene(conn, story_id, turn)
    for _, step in sorted(moves.items()):
        state = state.moved(step)
    return state.at(character_id)


async def complete_deterministically(
    db: AsyncEngine,
    op: Operation,
    answers: Mapping[str, Answer],
    *,
    cause: str,
    recorded: Recorded | None = None,
) -> list[TurnBeat]:
    """Stage 4 without the request's render context (#213): the deterministic outcome
    narration at the actor's place, committed as later beats of the turn and routed per
    perceiver (``core.turns.staged.route_narration``), and the turn's message, the
    deterministic assembly of its committed beats as the stored request's output viewpoint
    receives them (``core.history.staged_message``, #177's audience filter); ``turn_stage =
    completed``, ``revision + 1``, the slot given back. ``recorded`` is what this process's
    outcome transaction did; without it (the outcome committed by an earlier process) the
    grants its settlement held pending (#214's ``conflict``s) are read back
    (``contingency.conflicted``), so both narrate them as held pending."""
    story_id = op.story_id
    turn = op.narrative_turn
    assert story_id is not None  # noqa: S101 - a committed staged turn
    assert turn is not None  # noqa: S101
    actor_id, actor = await _actor(db, story_id, op.operation_id)
    start = await next_beat(db, op.operation_id)
    place = await _place(db, story_id, turn, actor_id)
    if recorded is None:
        async with db.connect() as conn:
            held = await contingency.conflicted(conn, story_id, settlement_outcomes(answers))
        recorded = Recorded(settled=held)
    beats = deterministic_outcome(answers, start, actor=actor, location=place, recorded=recorded)
    viewpoint = op.viewpoint or OMNISCIENT
    async with db.connect() as conn:
        view = await story_player_view(conn, story_id)
        message = await staged_message(
            conn,
            story_id,
            op.operation_id,
            viewpoint,
            player_view=view,
            extra=[
                AudienceBeat("narration", b.beat, None, b.text, qualifiers=b.qualifiers)
                for b in beats
            ],
        )
    op.reopen()
    stage_beats(op, beats)
    last = max((b.beat for b in beats), default=start - 1)
    op.append(
        "message_rendered",
        MessageRendered(
            text=message.text,
            finish_reason="deterministic",
            composed=True,
            perception_level=message.perception_level,
            player_view=message.player_view,
        ),
        provenance=Provenance.ENGINE,
        character_id=None,
        beat=last + 1,
    )

    async def route(conn: AsyncConnection) -> None:
        ids = await beat_event_ids(conn, op.operation_id)
        await route_narration(
            conn, op, story_id, turn, [(b, ids[b.beat]) for b in beats if b.beat in ids]
        )

    op.add_commit_step(route)
    report = {
        "effects": [{"outbox_id": k, "status": a.status} for k, a in answers.items()],
        "settled": [{"state": x.state, "result": x.result} for x in recorded.settled],
        "outcome_step": {
            "beats": [b.beat for b in beats],
            "deterministic": True,
            "cause": cause,
            "repairs": [],
            "verify": None,
        },
        "composition": None,
        "resumed": True,
    }
    op.add_commit_step(partial(write_report, operation_id=op.operation_id, report=report))
    op.turn_stage = StagedTurnStage.COMPLETED
    op.stage_turn(COMPLETION)
    await op.commit()
    log.info("staged turn: completed on resume (%d effect(s))", len(answers))
    return beats


async def resume(
    db: AsyncEngine, operation_id: str, *, registry: InProcessRegistry | None = None
) -> Resumed:
    """Continue the staged turn ``operation_id`` from its last committed stage (FR-EXT-17;
    docs/02 §4.8; #213). Committed stages are never redone or rolled back:

    - ``intent_committed``: claim every synchronous row (#210's ``claim``; a row the dispatcher
      already claimed or answered is left to it and waited on), then as below;
    - ``awaiting_outcome``: a row a dead process claimed and never answered is re-sent under
      the same provider key when the provider deduplicates, else moved to ``unknown`` and
      listed, never re-sent (``dispatcher.expire(force=True)``, FR-EXT-18); the rows are waited
      on up to the confirmation wait, then the outcome transaction commits
      (``record_outcomes``);
    - ``outcome_recorded``: the turn completes with the deterministic outcome narration
      (``complete_deterministically``: the request's render context lived only in the process
      that began the turn);
    - ``completed`` or ``abandoned``: nothing to do (the caller recomposes the response).

    The resume takes the story's turn slot over first (``turn_gate.take_over_staged``: only
    when its lease lapsed) and renews it while it runs (#63), so one process resumes a turn at
    a time; the completion gives it back. A discard that abandoned the turn meanwhile ends the
    resume (the slot given back). Any other failure leaves the turn at its last committed
    stage, for the next resume."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(operations.c.story_id, operations.c.turn_stage, operations.c.status).where(
                    operations.c.operation_id == operation_id
                )
            )
        ).one_or_none()
    if (
        row is None
        or row.turn_stage is None
        or row.story_id is None
        or str(row.status) != OperationStatus.COMMITTED.value
    ):
        return Resumed(operation_id, None)
    stage = StagedTurnStage(row.turn_stage)
    if stage not in UNFINISHED_TURN_STAGES:
        return Resumed(operation_id, stage)
    story_id = str(row.story_id)
    lease_s = (await turn_gate.read_concurrency(db, story_id)).lease_s
    if not await turn_gate.take_over_staged(db, story_id, operation_id, lease_s):
        return Resumed(operation_id, stage)
    ran: list[str] = []
    found: Operation | None = None
    try:
        async with turn_gate.renewing(db, story_id, operation_id, lease_s):
            found = await Operation.resumed(db, operation_id)
            if found is None:  # pragma: no cover - read just above
                return Resumed(operation_id, stage, taken=True)
            op = found
            log.info("staged turn: resuming from %s", op.turn_stage)
            synchronous, wait_s, statuses = await synchronous_rows(db, story_id, operation_id)
            claims = Claims((), {})
            recorded: Recorded | None = None
            if op.turn_stage is StagedTurnStage.INTENT_COMMITTED:
                claims = await claim_all(db, op, synchronous, registry=registry)
                ran.append(CLAIM)
                _, _, statuses = await synchronous_rows(db, story_id, operation_id)
            if op.turn_stage is StagedTurnStage.AWAITING_OUTCOME:
                mine = {c.outbox_id for c in claims.claimed}
                stale = [
                    r.effect_id
                    for r in synchronous
                    if statuses.get(r.effect_id) == CLAIMED and r.effect_id not in mine
                ]
                answers = await await_outcomes(
                    db,
                    synchronous,
                    claims,
                    wait_s,
                    extra=[dispatcher.expire(db, x, registry=registry, force=True) for x in stale],
                )
                start = await next_beat(db, operation_id)
                assert op.narrative_turn is not None  # noqa: S101 - committed
                position = Position(op.narrative_turn, op.turn_variant or 0, 0, start, 0)
                reads = pinned.PinnedReads(
                    db, story_id, in_process=registry or dispatcher.bound() or InProcessRegistry()
                )
                recorded = await record_outcomes(
                    db, op, await _book(db, story_id), reads, answers, position
                )
                ran.append(OUTCOME)
            if op.turn_stage is StagedTurnStage.OUTCOME_RECORDED:
                answers = await read_answers(db, synchronous)
                await complete_deterministically(
                    db, op, answers, cause="resumed", recorded=recorded
                )
                ran.append(COMPLETION)
                # #213: the completion committed, the response not stored.
                crash.hit("staged.after_completion")
    except TimelineChanged:
        # A discard abandoned the turn meanwhile: its remaining stages never commit.
        await turn_gate.release_now(db, story_id, operation_id)
        return Resumed(operation_id, StagedTurnStage.ABANDONED, tuple(ran), True)
    return Resumed(operation_id, None if found is None else found.turn_stage, tuple(ran), True)


async def stalled(db: AsyncEngine) -> list[str]:
    """The unfinished staged turns whose story's turn slot lease lapsed (the process running
    them is gone), oldest first."""
    o, st = operations.c, stories.c
    async with db.connect() as conn:
        found = await conn.scalars(
            select(o.operation_id)
            .join(stories, st.id == o.story_id)
            .where(
                o.status == OperationStatus.COMMITTED.value,
                o.turn_stage.in_(sorted(x.value for x in UNFINISHED_TURN_STAGES)),
                or_(
                    st.in_flight_turn.is_(None),
                    and_(
                        st.in_flight_turn == o.operation_id,
                        st.in_flight_lease_until <= func.clock_timestamp(),
                    ),
                ),
            )
            .order_by(o.committed_at, o.operation_id)
        )
        return [str(i) for i in found]


async def resume_stalled(db: AsyncEngine, *, registry: InProcessRegistry | None = None) -> int:
    """Resume every stalled staged turn (``stalled``), one at a time; answers how many this
    call ran (#213: ``outbox_recovery`` and the worker's recovery pass call it). A failure is
    logged and leaves the turn for the next pass."""
    ran = 0
    for operation_id in await stalled(db):
        try:
            done = await resume(db, operation_id, registry=registry)
        except Exception as exc:
            log.warning("staged turn: resume failed: %s", type(exc).__name__)
            continue
        ran += int(done.taken)
    return ran


_kicked: dict[str, asyncio.Task[Resumed]] = {}


def kick(db: AsyncEngine, story_id: str, operation_id: str) -> None:
    """``turn_gate``'s stalled hook (#213): resume ``operation_id`` in the background, once at a
    time in this process (a request that found it holding its story's slot gets 423 meanwhile)."""
    running = _kicked.get(operation_id)
    if running is not None and not running.done():
        return

    async def run() -> Resumed:
        try:
            return await resume(db, operation_id, registry=dispatcher.bound())
        except Exception as exc:
            log.warning("staged turn: background resume failed: %s", type(exc).__name__)
            return Resumed(operation_id, None)
        finally:
            _kicked.pop(operation_id, None)

    _kicked[operation_id] = asyncio.get_running_loop().create_task(run())


async def settle_kicked() -> None:
    """Wait for every background resume this process started (tests; shutdown)."""
    while _kicked:
        await asyncio.gather(*list(_kicked.values()), return_exceptions=True)


async def drain_kicked(timeout_s: float) -> int:
    """#658: shutdown's wait for the background resumes this process started, up to
    ``timeout_s``; any still running then is cancelled (it is at a stage boundary: the next
    recovery sweep resumes it). Answers how many were cancelled."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while (pending := [t for t in list(_kicked.values()) if not t.done()]) and (
        left := deadline - loop.time()
    ) > 0:
        # ``asyncio.wait`` (not ``wait_for``): reaching the bound leaves the resumes running.
        await asyncio.wait(pending, timeout=left)
    running = [t for t in list(_kicked.values()) if not t.done()]
    for task in running:
        task.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
        log.warning("staged turn: %d background resume(s) cut short at shutdown", len(running))
    return len(running)


turn_gate.bind_stalled(kick)


async def resume_for_response(
    db: AsyncEngine,
    operation_id: str,
    *,
    registry: InProcessRegistry | None = None,
    wait_s: float,
) -> RespondResult:
    """A same-key ``respond`` retry of a staged turn (#213, FR-EXT-17, FR-CONC-6): resume the
    turn (or wait while another process does, up to ``wait_s``: then 503
    ``original_in_flight_timeout``), then its finished response, recomposed from the committed
    beats (``recompose``). A turn a discard abandoned is 409 ``timeline_changed``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while True:
        done = await resume(db, operation_id, registry=registry)
        if done.stage is StagedTurnStage.ABANDONED:
            raise TimelineChanged(
                "a control action discarded the staged turn before its completion",
                {"operation_id": operation_id},
            )
        if done.stage is StagedTurnStage.COMPLETED:
            return await recompose(db, operation_id)
        if done.stage is None:
            raise RuntimeError(f"operation {operation_id} is no committed staged turn")
        if loop.time() >= deadline:
            raise OriginalInFlightTimeout(
                "the original request under this Idempotency-Key is still running",
                {"operation_id": operation_id},
            )
        await asyncio.sleep(POLL_S)


async def recompose(db: AsyncEngine, operation_id: str) -> RespondResult:
    """The finished response of the completed staged turn ``operation_id``, recomposed from its
    committed record for the stored request's output viewpoint (FR-EXT-17, FR-RESP-8,13): its
    message, its beats through #177's audience filter, its position and revision, its State
    effects (the outbox rows and requested effects it committed), the reconciliation count and
    the world clock. What only the original request's process held (its model calls, budget
    and timings) is not part of it. Deterministic: the same committed record gives the same
    response."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(
                    operations.c.story_id,
                    operations.c.narrative_turn,
                    operations.c.turn_variant,
                    operations.c.timeline_revision,
                    operations.c.viewpoint,
                    operations.c.config_revisions,
                ).where(operations.c.operation_id == operation_id)
            )
        ).one()
        story_id = str(row.story_id)
        viewpoint = str(row.viewpoint or OMNISCIENT)
        view = await story_player_view(conn, story_id)
        filtered = await filtered_turn_beats(
            conn, story_id, operation_id, viewpoint, player_view=view
        )
        reply = next(item for item in filtered.items if item.kind == "message_rendered")
        omniscient = is_omniscient(viewpoint)
        requested = [r.request for r in await _requested(db, story_id, operation_id)]
        changed = await state_effects.committed(
            conn, story_id, operation_id, omniscient=omniscient, requested=requested
        )
        reconciliation = await core_outbox.summary(conn, story_id)
        world = await turn_clock_of(
            conn, story_id, int(row.narrative_turn), int(row.turn_variant or 0)
        )
    return RespondResult(
        message=FinishedMessage(message_id=reply.message_id, text=reply.text, mode="deterministic"),
        beats=filtered.items,
        turn=int(row.narrative_turn),
        variant=int(row.turn_variant or 0),
        timeline_revision=int(row.timeline_revision),
        output_viewpoint=viewpoint,
        audience_filter=filtered.audience_filter,
        player_view=view,
        model_calls=[],
        elapsed_ms=0,
        state_effects=changed,
        reconciliation=reconciliation,
        clock=world,
    )


async def store_response_of(
    db: AsyncEngine, operation_id: str, call: IdempotentCall | None, value: Any
) -> None:
    """``store_response`` for a resumed turn (stage 5): the response stored under the retry's
    key and the stage logged, in one transaction; no revision change."""
    async with db.begin() as conn:
        revision = await conn.scalar(
            select(operations.c.timeline_revision).where(operations.c.operation_id == operation_id)
        )
        if call is not None:
            await call.store(conn, value)
        await append_stage(conn, operation_id, stage_entry(RESPONSE, "done", revision))


__all__ = [
    "CLAIM",
    "COMPLETION",
    "CONFIRMED",
    "DID_NOT_GO_THROUGH",
    "INTENT",
    "OUTCOME",
    "OUTCOME_STEP",
    "PENDING",
    "POLL_S",
    "RECORDED",
    "RESPONSE",
    "SETTLEMENT_CONFLICT",
    "STAGES",
    "STILL_PENDING",
    "SYNCHRONOUS",
    "WENT_THROUGH",
    "Answer",
    "Claims",
    "Recorded",
    "Resumed",
    "StagedTurnStage",
    "all_confirmed",
    "await_outcomes",
    "claim_all",
    "complete_deterministically",
    "completed",
    "deterministic_outcome",
    "deterministic_texts",
    "drain_kicked",
    "is_staged_turn",
    "kick",
    "lines",
    "next_beat",
    "outcome_sentence",
    "plans_staged",
    "read_answers",
    "recompose",
    "record_outcomes",
    "resume",
    "resume_for_response",
    "resume_stalled",
    "settle_kicked",
    "settlement_outcomes",
    "stalled",
    "store_response",
    "store_response_of",
    "synchronous_of",
    "synchronous_rows",
    "unfinished",
    "wait_of",
    "write_report",
]
