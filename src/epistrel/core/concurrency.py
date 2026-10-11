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
- an interpretive setting the operation depends on changed since its capture (FR-CONC-16,
  #64; ``core.config_dependencies``) → ``ConfigRecheck``: nothing is written; the loop
  re-captures the configuration, re-runs the policy checks on the staged output outside the
  lock (``config_recheck``), and commits again if they pass, else regenerates — each round
  within the same ``regenerate_retry_budget``, then 409 ``revision_conflict``;
- otherwise → ``Rebase``: the deterministic re-checks run and the operation commits.

The order is abandon > regenerate > configuration re-check: a prospective change moves no
dependency, so it never reaches the re-check.

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

from epistrel.core.config_changes import read_lite_defaults
from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.errors import RevisionConflict, StoryNotFound, TimelineChanged
from epistrel.core.settings_schema import StorySettings, resolve_generation
from epistrel.core.tables import (
    characters,
    content_rating_versions,
    content_ratings,
    events,
    operations,
    stories,
    story_texts,
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
not members: they belong to #64's configuration dependency set, which is re-checked."""


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


@dataclass(frozen=True, slots=True)
class ConfigRecheck:
    """An interpretive setting the operation depends on changed (FR-CONC-16): re-check the
    staged output under the new configuration before committing."""

    changed: tuple[str, ...]
    name: str = "recheck"


Decision = Commit | Rebase | Regenerate | Abandon | ConfigRecheck


@dataclass(frozen=True, slots=True)
class RevalidationState:
    """What ``decide`` sees, read under the story lock inside the commit transaction."""

    attempt: int
    base_revision: int
    current_revision: int
    control_since_base: bool
    control_operation_id: str | None
    touched: tuple[str, ...]
    config_changed: tuple[str, ...] = ()
    """Dependency members whose value changed since the operation's capture (#64)."""

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
    if not state.moved and not state.config_changed:
        return Commit()
    if state.touched:
        return Regenerate(reason="read_set_touched", touched=state.touched)
    if state.config_changed:
        return ConfigRecheck(changed=state.config_changed)
    if recheck is not None and not recheck():
        return Regenerate(reason="rebase_failed")
    return Rebase()


class RevalidationRequired(Exception):
    """Raised by ``Operation.commit(decide=...)`` for ``Regenerate``, ``Abandon`` and
    ``ConfigRecheck``.

    The commit transaction has rolled back (no event exists) and the attempt is recorded on
    ``operations.attempts``; the operation is still ``in_flight``.
    """

    def __init__(
        self, outcome: Regenerate | Abandon | ConfigRecheck, state: RevalidationState
    ) -> None:
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
    elif isinstance(outcome, ConfigRecheck):
        record["config_changed"] = list(outcome.changed)
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


async def _pins(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """The pinned memories' texts in order, from the ``story_texts`` projection (#60). Texts
    only: an item's id is not a prompt input, so only a text or order change touches it."""
    pinned = await conn.scalar(
        select(story_texts.c.pinned).where(story_texts.c.story_id == story_id)
    )
    return [item["text"] for item in pinned or ()]


async def _instructions(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """The custom instructions' text from the ``story_texts`` projection (#60)."""
    found = await conn.scalar(
        select(story_texts.c.custom_instructions).where(story_texts.c.story_id == story_id)
    )
    return found or ""


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


async def _generation_settings(conn: AsyncConnection, story_id: str, spec: LiteReadSet) -> Any:
    """The generation settings and window size a turn of the story starts from: the
    deployment's Lite defaults with the story's overrides applied (FR-LITE-4, #57). A request's
    own ``prompt_params`` are fixed for the turn, so they are not part of the read set."""
    defaults = await read_lite_defaults(conn)
    raw = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
    story = StorySettings.from_row(raw)
    resolved = resolve_generation(defaults.lite_generation_defaults, story.generation_settings)
    return {
        "generation": resolved.model_dump(mode="json"),
        "window_messages": story.lite_window_messages or defaults.lite_window_messages,
    }


for _name, _loader in (
    ("window", _window),
    ("segments", _segments),
    ("pins", _pins),
    ("instructions", _instructions),
    ("definitions", _definitions),
    ("guidance_text", _guidance_text),
    ("generation_settings", _generation_settings),
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

ConfigRecheckFn = Callable[["Operation", tuple[str, ...]], Awaitable[bool]]
"""``config_recheck(op, changed)``: the policy checks on ``op``'s staged output under the
configuration just re-captured (FR-CONC-16); ``False`` regenerates. Runs with no lock held."""


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
    config_recheck: ConfigRecheckFn | None = None,
) -> RevalidationResult:
    """Begin ``op``, generate, and commit under FR-CONC-4/16, regenerating within ``budget``.

    ``budget`` defaults to the story's ``regenerate_retry_budget`` (read at start). Every
    attempt is the same operation (same id) with a new base. Raises ``TimelineChanged`` on
    ``Abandon`` and ``RevisionConflict`` once the budget is spent; either way the operation
    ends ``abandoned`` with no events. Any other failure marks it ``failed``.

    A ``ConfigRecheck`` (FR-CONC-16) re-captures the configuration and runs ``config_recheck``
    on the staged output with no lock held (none given: nothing to re-check, it passes); a
    pass commits again without generating, a failure regenerates. The regenerated output is
    checked by ``generate`` itself, which reads the configuration as it then is (the Lite turn's
    output gate, #68), so no reply produced after a tightening commits unchecked and none is
    checked twice. Up to ``budget + 1`` re-check rounds run (one always may) and regenerations
    spend ``budget``; past either bound the call is 409 ``revision_conflict``.
    """
    op.read_set_source = read_set if read_set is not None else LiteReadSet()
    inputs = await op.begin(read=begin)
    limit = budget if budget is not None else op.story_settings.regenerate_retry_budget
    regenerations = 0
    rechecks = 0
    generate_next = True

    async def conflict(current_revision: int, reason: str) -> RevisionConflict:
        await op.abandon()
        return RevisionConflict(
            "the story kept changing under the turn; regeneration budget spent",
            {"current_revision": current_revision, "attempts": len(op.attempts), "reason": reason},
        )

    try:
        while True:
            if generate_next:
                await generate(op, inputs)
            generate_next = True
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
                if isinstance(outcome, ConfigRecheck):
                    if rechecks > limit:
                        raise await conflict(state.current_revision, "config_changed") from None
                    rechecks += 1
                    await op.recapture_config()
                    passed = (
                        True
                        if config_recheck is None
                        else await config_recheck(op, outcome.changed)
                    )
                    op.attempts[-1]["recheck"] = "passed" if passed else "failed"
                    if passed:
                        generate_next = False
                        continue
                    reason = "rebase_failed"
                else:
                    reason = outcome.reason
                if regenerations >= limit:
                    raise await conflict(state.current_revision, reason) from None
                regenerations += 1
                inputs = await op.restart(read=begin)
                continue
            return RevalidationResult(operation=op, attempts=list(op.attempts))
    except BaseException as exc:
        if op.in_flight:
            await op.fail(exc)
        raise
