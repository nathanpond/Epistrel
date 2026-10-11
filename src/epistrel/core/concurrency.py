"""Optimistic concurrency: revisions, the Lite read set, and the commit decision (FR-CONC-1,2,4).

Every story-scoped ``Operation`` captures the story's ``timeline_revision`` as its
``base_revision`` when it begins. Work that generates (a turn) also captures a **read set**: a
dict of named SHA-256 hashes of its prompt inputs (``LiteReadSet``). Model calls run with no
lock held. At commit, under the story row lock, ``Operation.commit(decide=...)`` builds a
``RevalidationState`` and the synchronous ``decide`` returns the FR-CONC-4 outcome:

- revision unchanged → ``Commit``;
- a control action (truncate, untruncate, swap, regenerate) committed since the base →
  ``Abandon``: nothing is written and the caller gets 409 ``timeline_changed``;
- the read set was touched → ``Regenerate``: nothing is written, and the same operation
  (same id, new base) generates again, up to the story's ``regenerate_retry_budget``, then
  409 ``revision_conflict``;
- otherwise → ``Rebase``: the deterministic re-checks run and the operation commits.

``run_with_revalidation`` drives that loop; the respond pipeline (#57) calls it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.errors import RevisionConflict, StoryNotFound, TimelineChanged
from epistrel.core.tables import (
    characters,
    content_rating_versions,
    content_ratings,
    events,
    operations,
    stories,
)

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation

ReadSet = dict[str, str]
"""Read-set member name → SHA-256 hex digest of the member's canonical JSON."""

READ_SET_MEMBERS: tuple[str, ...] = (
    "window",
    "segments",
    "pins",
    "instructions",
    "definitions",
    "guidance_text",
    "generation_settings",
)
"""The Lite read set (FR-CONC-4: the assembled prompt's inputs). Guardrail and policy sets are
not members: they belong to #64's configuration dependency set, which rebases."""


# -- outcomes ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Commit:
    """The revision is the one the operation read: commit as is."""

    name: str = "commit"


@dataclass(frozen=True, slots=True)
class Rebase:
    """The story moved but nothing the operation read changed; the re-checks passed."""

    name: str = "rebase"


@dataclass(frozen=True, slots=True)
class Regenerate:
    """Something the operation read changed (or a re-check failed): generate again."""

    reason: str
    touched: tuple[str, ...] = ()
    name: str = "regenerate"


@dataclass(frozen=True, slots=True)
class Abandon:
    """A control action changed the timeline since the base: the result is no longer wanted."""

    control_operation_id: str | None
    name: str = "abandon"


Decision = Commit | Rebase | Regenerate | Abandon


@dataclass(frozen=True, slots=True)
class RevalidationState:
    """What ``decide`` sees, read under the story lock inside the commit transaction."""

    attempt: int
    base_revision: int
    current_revision: int
    control_since_base: bool
    control_operation_id: str | None
    touched: tuple[str, ...]

    @property
    def moved(self) -> bool:
        return self.current_revision != self.base_revision


Decide = Callable[[RevalidationState], Decision]
"""Synchronous by construction: it runs under the story row lock."""

Recheck = Callable[[], bool]
"""A deterministic re-check run at rebase (policy checks, #64/#68); ``False`` regenerates."""


def decide(state: RevalidationState, *, recheck: Recheck | None = None) -> Decision:
    """The FR-CONC-4 decision. Bind ``recheck`` with ``functools.partial`` to add re-checks."""
    if state.control_since_base:
        return Abandon(control_operation_id=state.control_operation_id)
    if not state.moved:
        return Commit()
    if state.touched:
        return Regenerate(reason="read_set_touched", touched=state.touched)
    if recheck is not None and not recheck():
        return Regenerate(reason="rebase_failed")
    return Rebase()


class RevalidationRequired(Exception):
    """Raised by ``Operation.commit(decide=...)`` for ``Regenerate`` and ``Abandon``.

    The commit transaction has rolled back (no event exists) and the attempt is recorded on
    ``operations.attempts``; the operation is still ``in_flight``.
    """

    def __init__(self, outcome: Regenerate | Abandon, state: RevalidationState) -> None:
        super().__init__(outcome.name)
        self.outcome = outcome
        self.state = state


def attempt_record(state: RevalidationState, outcome: Decision) -> dict[str, Any]:
    """One entry of ``operations.attempts`` (exposed by ``GET /operations/{id}``, #61)."""
    record: dict[str, Any] = {
        "attempt": state.attempt,
        "base_revision": state.base_revision,
        "current_revision": state.current_revision,
        "outcome": outcome.name,
    }
    if isinstance(outcome, Regenerate):
        record["reason"] = outcome.reason
        record["touched"] = list(outcome.touched)
    elif isinstance(outcome, Abandon):
        record["control_operation_id"] = outcome.control_operation_id
    return record


# -- the read set -----------------------------------------------------------------------------


def hash_member(value: Any) -> str:
    """SHA-256 of ``value``'s canonical JSON (sorted keys, no insignificant whitespace)."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def touched_members(base: Mapping[str, str], current: Mapping[str, str]) -> tuple[str, ...]:
    """The members whose hash differs (a member missing on either side counts as touched)."""
    return tuple(
        sorted(name for name in base.keys() | current.keys() if base.get(name) != current.get(name))
    )


class ReadSetSource(Protocol):
    """Computes an operation's read set from the database (at start and again under the lock)."""

    async def hashes(self, conn: AsyncConnection, story_id: str) -> ReadSet: ...


MemberLoader = Callable[[AsyncConnection, str, "LiteReadSet"], Awaitable[Any]]
"""Loads one member's JSON value for a story; hashed by ``hash_member``."""

_MEMBER_LOADERS: dict[str, MemberLoader] = {}


def register_read_set_member(name: str, loader: MemberLoader) -> None:
    """Set the loader of a Lite read-set member (#57/#60/#72 replace the M2 defaults)."""
    if name not in READ_SET_MEMBERS:
        raise ValueError(f"{name!r} is not a Lite read-set member")
    _MEMBER_LOADERS[name] = loader


def member_loader(name: str) -> MemberLoader:
    return _MEMBER_LOADERS[name]


@dataclass(frozen=True, slots=True)
class LiteReadSet:
    """The Lite read set of a story: one hash per member of ``READ_SET_MEMBERS``.

    ``window_turns`` bounds the verbatim window to the latest N active turns (``None``: all).
    The same object computes the base read set at start and the current one under the lock,
    so the rebase decision is a hash compare, not a re-read of the inputs.
    """

    window_turns: int | None = None

    async def values(self, conn: AsyncConnection, story_id: str) -> dict[str, Any]:
        return {
            name: await _MEMBER_LOADERS[name](conn, story_id, self) for name in READ_SET_MEMBERS
        }

    async def hashes(self, conn: AsyncConnection, story_id: str) -> ReadSet:
        return {
            name: hash_member(value) for name, value in (await self.values(conn, story_id)).items()
        }


def _committed_active(story_id: str) -> Any:
    return and_(
        operations.c.story_id == story_id,
        operations.c.status == OperationStatus.COMMITTED.value,
        operations.c.membership == Membership.ACTIVE.value,
    )


async def _window(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """Operation id, position and membership of the active turns in the verbatim window."""
    query = (
        select(
            operations.c.operation_id,
            operations.c.narrative_turn,
            operations.c.turn_variant,
            operations.c.membership,
        )
        .where(_committed_active(story_id), operations.c.kind == OperationKind.TURN.value)
        .order_by(operations.c.narrative_turn.desc())
    )
    if spec.window_turns is not None:
        query = query.limit(spec.window_turns)
    return [list(row) for row in (await conn.execute(query)).all()]


async def _segments(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """Operation id and membership of every committed summary segment (#72 refines)."""
    found = await conn.execute(
        select(events.c.operation_id, operations.c.membership)
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(
            events.c.story_id == story_id,
            events.c.type == "lite_summary_segment",
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        .order_by(events.c.seq)
    )
    return [list(row) for row in found.all()]


async def _payloads(conn: AsyncConnection, story_id: str, event_type: str) -> list[Any]:
    found = await conn.scalars(
        select(events.c.payload)
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(
            events.c.story_id == story_id, events.c.type == event_type, _committed_active(story_id)
        )
        .order_by(events.c.seq)
    )
    return list(found)


async def _latest(conn: AsyncConnection, story_id: str, event_type: str) -> Any:
    found = await _payloads(conn, story_id, event_type)
    return found[-1] if found else None


async def _pins(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    return await _latest(conn, story_id, "pinned_memories_set")


async def _instructions(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    return await _latest(conn, story_id, "custom_instructions_set")


async def _definitions(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """The cast's definitions as the prompt renders them: ``[id, name, definition]`` in cast
    order, read from ``characters`` (#50; the cast is fixed at creation in M2)."""
    found = await conn.execute(
        select(characters.c.id, characters.c.name, characters.c.definition)
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    return [list(row) for row in found.all()]


async def _guidance_text(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """The prompt guidance of the story's rating at its current version (FR-RATE-4, #48).

    The rating's own guidance only: guidance is never inherited (FR-RATE-2).
    """
    return await conn.scalar(
        select(content_rating_versions.c.prompt_guidance)
        .join(
            content_ratings,
            content_ratings.c.current_version_id == content_rating_versions.c.version_id,
        )
        .join(stories, stories.c.content_rating_id == content_ratings.c.id)
        .where(stories.c.id == story_id)
    )


async def _not_yet_recorded(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """Generation-settings versions arrive with #52/#57."""
    return None


for _name, _loader in (
    ("window", _window),
    ("segments", _segments),
    ("pins", _pins),
    ("instructions", _instructions),
    ("definitions", _definitions),
    ("guidance_text", _guidance_text),
    ("generation_settings", _not_yet_recorded),
):
    register_read_set_member(_name, _loader)


# -- the expected-revision precondition (FR-RESP-6) -------------------------------------------


async def check_expected_revision(
    db: AsyncEngine | AsyncConnection, story_id: str, expected: int | None
) -> int:
    """The story's current revision; 409 ``revision_conflict`` when ``expected`` differs.

    A start-time precondition only, checked before anything is written; ``None`` checks
    nothing but the story's existence.
    """
    query = select(stories.c.timeline_revision).where(stories.c.id == story_id)
    if isinstance(db, AsyncConnection):
        current = await db.scalar(query)
        if db.in_transaction():
            await db.rollback()
    else:
        async with db.connect() as conn:
            current = await conn.scalar(query)
    if current is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    if expected is not None and expected != current:
        raise RevisionConflict(
            f"expected timeline revision {expected}, the story is at {current}",
            {"current_revision": int(current), "attempts": 0, "reason": "expected_revision"},
        )
    return int(current)


# -- the regenerate loop ----------------------------------------------------------------------

Begin = Callable[[AsyncConnection], Awaitable[Any]]
"""The read phase: reads the prompt inputs inside the operation's REPEATABLE READ snapshot."""

Generate = Callable[["Operation", Any], Awaitable[None]]
"""The model call(s): stages the operation's events from the inputs. Holds no lock."""


@dataclass
class RevalidationResult:
    """How ``run_with_revalidation`` ended: the committed operation and its attempts."""

    operation: Operation
    attempts: list[dict[str, Any]] = field(default_factory=list)


async def run_with_revalidation(
    op: Operation,
    *,
    begin: Begin,
    generate: Generate,
    decide: Decide = decide,
    budget: int | None = None,
    read_set: ReadSetSource | None = None,
) -> RevalidationResult:
    """Begin ``op``, generate, and commit under FR-CONC-4, regenerating within ``budget``.

    ``budget`` defaults to the story's ``regenerate_retry_budget`` (read at start). Every
    attempt is the same operation (same id) with a new base. Raises ``TimelineChanged`` on
    ``Abandon`` and ``RevisionConflict`` once the budget is spent; either way the operation
    ends ``abandoned`` with no events. Any other failure marks it ``failed``.
    """
    op.read_set_source = read_set if read_set is not None else LiteReadSet()
    inputs = await op.begin(read=begin)
    limit = budget if budget is not None else op.story_settings.regenerate_retry_budget
    regenerations = 0
    try:
        while True:
            await generate(op, inputs)
            try:
                await op.commit(decide=decide)
            except RevalidationRequired as signal:
                outcome, state = signal.outcome, signal.state
                if isinstance(outcome, Abandon):
                    await op.abandon()
                    raise TimelineChanged(
                        "a control action changed the timeline while the turn was generating",
                        {
                            "current_revision": state.current_revision,
                            "control_operation_id": outcome.control_operation_id,
                        },
                    ) from None
                if regenerations >= limit:
                    await op.abandon()
                    raise RevisionConflict(
                        "the story kept changing under the turn; regeneration budget spent",
                        {
                            "current_revision": state.current_revision,
                            "attempts": len(op.attempts),
                            "reason": outcome.reason,
                        },
                    ) from None
                regenerations += 1
                inputs = await op.restart(read=begin)
                continue
            return RevalidationResult(operation=op, attempts=list(op.attempts))
    except BaseException as exc:
        if op.in_flight:
            await op.fail(exc)
        raise
