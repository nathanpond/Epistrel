"""Fork a story at a checkpoint (FR-STORE-15, FR-CONC-10; docs/02 §4.6; #67).

``fork`` copies the source story's corrected history through turn N into a new story: a full,
independent copy under new ids, isolated from the source from the moment it exists.

- **What is copied.** The source's committed operations that the history through N holds:
  every position-less active operation (genesis, pinned memories, custom instructions, settings)
  and every operation positioned at or before N on the selected chain (turn N's own inter-turn
  slots excluded, as ``views.corrected_history(through_turn=N)`` reads it), with their events
  in ``seq`` order, renumbered dense. ``variant_chain`` selects another chain (#66,
  ``variants.fork_selection``): the active history up to where it parts, then the chain's own
  operations, all ``active`` in the fork, variant numbers kept. Left out: ``audit`` events,
  ``maintenance`` operations, and the timeline's own control events (``SKIPPED_EVENT_TYPES``:
  truncate, untruncate, swap, fork, archive, unarchive), whose operations go with them.
- **Every story-scoped table** in ``stories.STORY_TABLES`` is copied with one ``INSERT …
  SELECT`` unless ``stories.STORY_TABLES_NOT_COPIED`` names it, so a new table is copied by
  default: a row follows its operation when it has an ``operation_id`` column (``client_events``,
  #58), and otherwise belongs to the story (``characters``, ``policies``, ``story_texts``).
  ``story_heads`` is rebuilt from the copied turns and ``lite_summary_segments`` by folding the
  copied events (#72: segments positioned at or before N come along with their operations, and
  the post-commit ``fork`` hook schedules the fork's summary job for the boundary); story-scoped
  ``config_changes`` rows are
  copied too. Ids are remapped through a per-fork temp table: every column named ``*_id`` (and
  ``id``) and every string in a ``jsonb`` column that is a mapped id, alone or after a
  ``prefix:`` (``character:<id>``), takes the new id; unmapped values (ratings, client event
  ids) are kept. New ULIDs keep the source's relative order. ``tx_time``, ``valid_time``,
  ``base_revision``, ``config_revisions`` and ``parent_variant`` are kept; ``membership_tx``
  is the fork's time on rows that were not active.
- **One snapshot.** The copy runs on one connection whose transaction is ``REPEATABLE READ``
  from its first statement, so every read sees the source at one ``timeline_revision`` and a
  turn committing meanwhile is wholly in or wholly out (FR-CONC-10). A serialization failure is
  retried (``SNAPSHOT_ATTEMPTS``), then 503 ``database_unavailable``.
- **The new story.** Status ``active`` (an archived source can be forked), its settings,
  rating, engine profile and ``config_revision`` copied (a retired rating or a deleted profile
  too: a fork continues its story, so it is not a new assignment; #106), owned by the requester
  or the ``owner`` the request names (FR-AUTH-5: a fork is a creation, so it never inherits the
  source's owner, and the source's relations stay with the source), no turn in flight,
  ``next_turn_index`` N + 1 and ``timeline_revision`` 1: its own ``genesis`` operation (after
  the copied one) records a ``story_created`` event with ``forked_from {story_id, revision,
  at_turn, variant_chain}``.
  Under an idempotent call the deployment-scoped key is bound to that operation.
- **The source** records a ``fork`` control event (no revision bump) in a separate short
  transaction after the copy commits; a crash in between leaves a fork the source does not
  mention (accepted). Then the ``post_commit`` membership hooks run on the fork with cause
  ``fork`` (#65's registry): one change per source membership of the copied operations, each
  now ``active``.

``at_turn`` defaults to the head (the chain's tip); above it, or below zero, is 422. An unknown
or ambiguous chain is 409 ``unknown_chain`` (#66). The expected revision, when given, is the
source's, checked inside the snapshot.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    Table,
    and_,
    exists,
    func,
    insert,
    not_,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import idempotency, pause, projections, timeline
from epistrel.core.enums import (
    Authority,
    EventCategory,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
    StoryStatus,
)
from epistrel.core.erasure.index import reindex_story
from epistrel.core.erasure.ledger import LEDGER_LOCK_KEY
from epistrel.core.errors import (
    DatabaseUnavailable,
    InvalidRequest,
    RevisionConflict,
    StoryNotFound,
)
from epistrel.core.events.operation import Operation
from epistrel.core.events.registry import validate
from epistrel.core.events.types import CastMember, Fork, ForkedFrom, StoryCreated
from epistrel.core.ids import new_ulid
from epistrel.core.ownership import Owner
from epistrel.core.projections.heads import head_row
from epistrel.core.stories import (
    CONFIG_SCOPE_STORY,
    STORY_NAME_MAX,
    STORY_TABLES,
    STORY_TABLES_NOT_COPIED,
    settings_of,
)
from epistrel.core.tables import (
    characters,
    config_changes,
    content_ratings,
    erasure_ledger,
    events,
    lite_summary_segments,
    operations,
    stories,
    story_heads,
    transcript_messages,
)
from epistrel.core.variants import ChainRef, ChainSelection, ChainSelector, fork_selection
from epistrel.testing import crash

logger = logging.getLogger(__name__)

CRASH_KIND = "fork"
"""What the fork's crash points (#75) count its hits as."""
SNAPSHOT_ATTEMPTS = 3
"""How many times the snapshot copy is tried before 503 ``database_unavailable``."""

SERIALIZATION_FAILURE = "40001"

SKIPPED_EVENT_TYPES: frozenset[str] = frozenset(
    {"truncate", "untruncate", "swap", "fork", "story_archived", "story_unarchived"}
)
"""Control events that edit the source's own timeline or lifecycle; the fork starts without
them (its membership is already flattened) and their operations are not copied."""

STORY_TABLES_REBUILT: frozenset[str] = frozenset({story_heads.name, lite_summary_segments.name})
"""Story tables the fork rebuilds from the copied rows instead of copying: ``story_heads`` from
the copied turns, and ``lite_summary_segments`` (#72) by folding the copied events, so a copied
segment's ``superseded`` state is the fork's own (a merged parent past the fork point is not
copied, and its children are active in the fork)."""

FORK_SUFFIX = " (fork)"

_NOT_COPIED = STORY_TABLES_NOT_COPIED | STORY_TABLES_REBUILT
_CONFIG_FILTER = f"t.scope = '{CONFIG_SCOPE_STORY}' AND t.scope_id = :source"

_MAP = "pg_temp.epistrel_fork_ids"
_ID = "pg_temp.epistrel_fork_id"
_JSON = "pg_temp.epistrel_fork_json"

_SETUP = (
    "CREATE TEMP TABLE epistrel_fork_ids (old_id text PRIMARY KEY, new_id text NOT NULL, "
    "kind text NOT NULL, new_seq bigint) ON COMMIT DROP",
    f"""
CREATE OR REPLACE FUNCTION {_ID}(value text) RETURNS text
LANGUAGE plpgsql STABLE AS $fork$
DECLARE
    parts text[];
    mapped text;
BEGIN
    IF value IS NULL THEN
        RETURN NULL;
    END IF;
    parts := regexp_match(value, '^([a-z_]+:)?([0-9A-HJKMNP-TV-Z]{{26}})$');
    IF parts IS NULL THEN
        RETURN value;
    END IF;
    SELECT m.new_id INTO mapped FROM {_MAP} m WHERE m.old_id = parts[2];
    IF mapped IS NULL THEN
        RETURN value;
    END IF;
    RETURN coalesce(parts[1], '') || mapped;
END
$fork$""",  # noqa: S608 - fixed names, no input
    f"""
CREATE OR REPLACE FUNCTION {_JSON}(value jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $fork$
BEGIN
    CASE jsonb_typeof(value)
        WHEN 'object' THEN
            RETURN (
                SELECT coalesce(jsonb_object_agg({_ID}(e.key), {_JSON}(e.value)), '{{}}'::jsonb)
                FROM jsonb_each(value) AS e
            );
        WHEN 'array' THEN
            RETURN (
                SELECT coalesce(jsonb_agg({_JSON}(a.value) ORDER BY a.n), '[]'::jsonb)
                FROM jsonb_array_elements(value) WITH ORDINALITY AS a(value, n)
            );
        WHEN 'string' THEN
            RETURN to_jsonb({_ID}(value #>> '{{}}'));
        ELSE
            RETURN value;
    END CASE;
END
$fork$""",  # noqa: S608 - fixed names, no input
)
_TEARDOWN = (f"DROP FUNCTION {_JSON}(jsonb)", f"DROP FUNCTION {_ID}(text)")


# -- request and result -----------------------------------------------------------------------


class ForkRequest(BaseModel):
    """``POST /stories/{id}/fork``: copy the history through ``at_turn`` (default the head, or
    the chain's tip) along ``variant_chain`` (default the active chain) into a new story."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"at_turn": 4, "name": "What if she stayed"}]},
    )

    at_turn: int | None = Field(
        default=None, ge=0, description="The last turn to copy (0: only the story's setup)."
    )
    variant_chain: ChainSelector | None = Field(
        default=None,
        description='The chain to copy, `{turn, variant}` or `"turn:variant"` (default the '
        "active one); a dormant continuation becomes the fork's active history.",
    )
    name: str | None = Field(
        default=None, max_length=STORY_NAME_MAX, description='Default "<source name> (fork)".'
    )
    owner: Owner | None = Field(
        default=None,
        description="The fork's owner principal (default: the requesting principal, not the "
        "source's owner; never `system`). Recorded, never interpreted.",
    )


@dataclass(frozen=True, slots=True)
class ForkCharacter:
    """A character of the fork and the source character it copies."""

    id: str
    name: str
    source_id: str


@dataclass(frozen=True, slots=True)
class ForkOrigin:
    story_id: str
    revision: int
    at_turn: int
    variant_chain: ChainRef | None = None


@dataclass(frozen=True, slots=True)
class Forked:
    """A committed fork: the new story, its revision (1), where it came from, its cast, and
    how many rows were copied per table (its own genesis not counted)."""

    story_id: str
    operation_id: str
    """The fork's own ``genesis`` operation."""
    timeline_revision: int
    next_turn_index: int
    name: str | None
    forked_from: ForkOrigin
    characters: tuple[ForkCharacter, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class _Genesis:
    """What ``IdempotentCall.bind`` needs of the fork's genesis operation."""

    operation_id: str


# -- the fork ---------------------------------------------------------------------------------


async def fork(
    db: AsyncEngine,
    story_id: str,
    request: ForkRequest,
    *,
    principal: str,
    expected_revision: int | None = None,
) -> Forked:
    """Copy ``story_id`` into a new story; see the module docstring."""
    forked: Forked | None = None
    moved: dict[Membership, list[str]] = {}
    for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
        moved = {}
        try:
            async with db.connect() as conn, conn.begin():
                forked = await _copy(
                    conn, story_id, request, principal, expected_revision, moved=moved
                )
                await _erasure_fence(db, conn, story_id, forked.story_id)
            break
        except ErasedMeanwhile:
            forked = None
            logger.warning(
                "fork of %s: an erasure committed meanwhile (attempt %d)", story_id, attempt
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != SERIALIZATION_FAILURE:
                raise
            logger.warning("fork of %s: serialization failure (attempt %d)", story_id, attempt)
    if forked is None:
        raise DatabaseUnavailable(
            "the fork's snapshot could not be read consistently; retry",
            {"attempts": SNAPSHOT_ATTEMPTS},
        )
    crash.hit("after_commit_before_response", CRASH_KIND)
    await _record_on_source(db, story_id, forked, principal)
    for old, ids in moved.items():
        await timeline.run_post_commit_hooks(
            db,
            timeline.MembershipChange(
                story_id=forked.story_id,
                operation_ids=tuple(ids),
                old=old,
                new=Membership.ACTIVE,
                cause="fork",
                control_operation_id=forked.operation_id,
            ),
        )
    return forked


async def _copy(
    conn: AsyncConnection,
    source: str,
    request: ForkRequest,
    principal: str,
    expected_revision: int | None,
    *,
    moved: dict[Membership, list[str]],
) -> Forked:
    """The whole copy, inside ``conn``'s transaction (REPEATABLE READ from its first read)."""
    await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    row = (await conn.execute(select(stories).where(stories.c.id == source))).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {source} does not exist", {"story_id": source})
    await pause.refuse(conn, source, row.status)
    revision = int(row.timeline_revision)
    if expected_revision is not None and expected_revision != revision:
        raise RevisionConflict(
            f"expected timeline revision {expected_revision}, the story is at {revision}",
            {"current_revision": revision, "attempts": 0, "reason": "expected_revision"},
        )
    selection, through = await fork_selection(conn, source, request.variant_chain, request.at_turn)
    head = int(row.next_turn_index) - 1
    if selection is None and not 0 <= through <= head:
        raise InvalidRequest(
            "at_turn must be a turn of the story, from 0 to its head",
            {"field": "body.at_turn", "head": head},
        )
    fork_id = new_ulid()
    for statement in _SETUP:
        await conn.exec_driver_sql(statement)
    await _map(conn, [(source, fork_id)], "story")

    copied = await _select_operations(conn, source, selection, through)
    for operation_id, membership in copied:
        moved.setdefault(membership, []).append(operation_id)
    await _map(conn, _remapped([op for op, _ in copied]), "operation")
    await _map_events(conn)

    # Every id is mapped before any row is copied, so a row naming another (the settings'
    # viewpoint, a policy's character) finds it.
    copying = [t for t in reversed(STORY_TABLES) if t.name not in _NOT_COPIED]
    for table in copying:
        await _map_primary_keys(conn, table, _row_filter(table), source)
    await _map_primary_keys(conn, config_changes, _CONFIG_FILTER, source)

    counts: dict[str, int] = {}
    name = request.name if request.name is not None else _fork_name(row.name)
    await _copy_story_row(conn, source, fork_id, name, through, request.owner or principal)
    for table in copying:
        counts[table.name] = await _copy_table(conn, table, source, fork_id)
    counts[config_changes.name] = await _copy_config_changes(conn, source, fork_id)
    counts[story_heads.name] = await _rebuild_heads(conn, fork_id)
    counts[lite_summary_segments.name] = await _rebuild_segments(conn, fork_id)

    cast = await _cast(conn, fork_id)
    origin = ForkOrigin(
        story_id=source,
        revision=revision,
        at_turn=through,
        variant_chain=request.variant_chain,
    )
    genesis = await _genesis(conn, fork_id, name, origin, principal)
    # A fork copies no subject-reference rows: its copied records are indexed again, under
    # their new ids (FR-ADMIN-6; #123).
    await reindex_story(conn, fork_id)
    for statement in _TEARDOWN:
        await conn.exec_driver_sql(statement)
    call = idempotency.current_call()
    if call is not None:
        await call.bind(conn, _Genesis(genesis))
    crash.hit("before_commit", CRASH_KIND)
    return Forked(
        story_id=fork_id,
        operation_id=genesis,
        timeline_revision=1,
        next_turn_index=through + 1,
        name=name,
        forked_from=origin,
        characters=cast,
        counts=counts,
    )


class ErasedMeanwhile(Exception):
    """An erasure of the source committed after the fork's snapshot was taken: the copy would
    hold what it erased, so the fork is retried on a new snapshot (FR-ADMIN-9; #127)."""


_LEDGER_COPY = text(
    "INSERT INTO erasure_ledger (id, subject_kind, subject_id, scope, story_ref, principal, "
    "operation_id, counts, scan_term_hashes, applied_at, applied_note, copied_from, at) "
    "SELECT m.new_id, t.subject_kind, t.subject_id, t.scope, :fork, t.principal, "
    "t.operation_id, t.counts, t.scan_term_hashes, t.applied_at, t.applied_note, t.id, t.at "
    "FROM erasure_ledger t JOIN (SELECT unnest(CAST(:old AS text[])) AS old_id, "
    "unnest(CAST(:new AS text[])) AS new_id) m ON m.old_id = t.id::text"
)


async def _erasure_fence(db: AsyncEngine, conn: AsyncConnection, source: str, fork_id: str) -> None:
    """The fork's resurrection guard (FR-ADMIN-9; #127), last in its transaction.

    The shared ledger lock waits for an erasure between its ledger write and its commit (it
    holds the lock exclusively, #125) and keeps one from starting before the fork commits. The
    source's story-scope ledger rows are then read twice: in the fork's snapshot and as
    committed now; a row the snapshot misses is an erasure the copy predates
    (``ErasedMeanwhile``: retried). The rows the snapshot holds are copied to the fork with
    ``copied_from`` set (the reconciler ignores them, #125), so the guard keeps refusing the
    erased subject in the fork too."""
    await conn.execute(select(func.pg_advisory_xact_lock_shared(LEDGER_LOCK_KEY)))
    rows = select(erasure_ledger.c.id).where(
        erasure_ledger.c.scope == "story", erasure_ledger.c.story_ref == source
    )
    seen = {str(i) for i in await conn.scalars(rows)}
    async with db.connect() as fresh:
        committed = {str(i) for i in await fresh.scalars(rows)}
    if committed - seen:
        raise ErasedMeanwhile(f"{len(committed - seen)} erasure(s) of {source} committed")
    if seen:
        old = sorted(seen)
        await conn.execute(
            _LEDGER_COPY,
            {"fork": fork_id, "old": old, "new": [new for _, new in _remapped(old)]},
        )


def _fork_name(source_name: str | None) -> str | None:
    if source_name is None:
        return None
    return source_name[: STORY_NAME_MAX - len(FORK_SUFFIX)] + FORK_SUFFIX


def _remapped(old_ids: Sequence[str]) -> list[tuple[str, str]]:
    """New ULIDs for ``old_ids``, in the same relative order."""
    fresh = sorted(new_ulid() for _ in old_ids)
    return list(zip(sorted(old_ids), fresh, strict=True))


async def _map(conn: AsyncConnection, pairs: Sequence[tuple[str, str]], kind: str) -> None:
    if pairs:
        await conn.execute(
            text(f"INSERT INTO {_MAP} (old_id, new_id, kind) VALUES (:o, :n, :k)"),  # noqa: S608
            [{"o": old, "n": new, "k": kind} for old, new in pairs],
        )


def _through(turn: Any, slot: Any, through: int) -> Any:
    """Positioned at or before ``through``, without its inter-turn slots (#65's view)."""
    return or_(turn < through, and_(turn == through, slot.is_(None)))


async def _select_operations(
    conn: AsyncConnection, source: str, selection: ChainSelection | None, through: int
) -> list[tuple[str, Membership]]:
    """The operations the fork copies, with their membership in the source."""
    o = operations.alias("o")
    e = events.alias("e")
    if selection is None:
        visible = o.c.membership == Membership.ACTIVE.value
    else:
        visible = selection.visible(o.c.membership, o.c.narrative_turn, o.c.operation_id)
    copyable = exists().where(
        e.c.operation_id == o.c.operation_id,
        e.c.category != EventCategory.AUDIT.value,
    )
    skipped = exists().where(
        e.c.operation_id == o.c.operation_id, e.c.type.in_(sorted(SKIPPED_EVENT_TYPES))
    )
    found = await conn.execute(
        select(o.c.operation_id, o.c.membership)
        .where(
            o.c.story_id == source,
            o.c.status == OperationStatus.COMMITTED.value,
            o.c.kind != OperationKind.MAINTENANCE.value,
            or_(
                and_(
                    o.c.narrative_turn.is_(None),
                    o.c.membership == Membership.ACTIVE.value,
                ),
                and_(_through(o.c.narrative_turn, o.c.slot_k, through), visible),
            ),
            copyable,
            not_(skipped),
        )
        .order_by(o.c.operation_id)
    )
    return [(r.operation_id, Membership(r.membership)) for r in found]


async def _map_events(conn: AsyncConnection) -> None:
    """Map the copied operations' events (no ``audit``, no skipped type) to new ids and dense
    ``seq`` values in the source's order."""
    found = await conn.execute(
        text(
            f"SELECT e.id FROM events e JOIN {_MAP} m ON m.old_id = e.operation_id "  # noqa: S608
            "WHERE m.kind = 'operation' AND e.category <> 'audit' AND e.type <> ALL(:skipped) "
            "ORDER BY e.seq"
        ),
        {"skipped": sorted(SKIPPED_EVENT_TYPES)},
    )
    old = [r.id for r in found]
    fresh = sorted(new_ulid() for _ in old)
    if old:
        await conn.execute(
            text(
                f"INSERT INTO {_MAP} (old_id, new_id, kind, new_seq) "  # noqa: S608
                "VALUES (:o, :n, 'event', :s)"
            ),
            [
                {"o": o, "n": n, "s": seq}
                for seq, (o, n) in enumerate(zip(old, fresh, strict=True), start=1)
            ],
        )


# -- the generic copy -------------------------------------------------------------------------


def _is_id(column: Column[Any]) -> bool:
    return column.name == "id" or column.name.endswith("_id")


def _expression(column: Column[Any], overrides: dict[str, str]) -> str:
    """The SELECT expression that copies ``column`` of source row ``t`` into the fork."""
    if column.name in overrides:
        return overrides[column.name]
    if column.name == "story_id":
        return ":fork"
    if _is_id(column):
        return f'{_ID}(t."{column.name}"::text)'
    if isinstance(column.type, JSONB):
        return f'{_JSON}(t."{column.name}")'
    return f't."{column.name}"'


def _insert_select(table: Table, overrides: dict[str, str], where: str) -> str:
    names = [f'"{c.name}"' for c in table.columns]
    values = [_expression(c, overrides) for c in table.columns]
    return (
        f'INSERT INTO "{table.name}" ({", ".join(names)}) '  # noqa: S608 - names from metadata
        f'SELECT {", ".join(values)} FROM "{table.name}" t WHERE {where}'
    )


def _seq_of(event_id: str) -> str:
    return f"(SELECT m.new_seq FROM {_MAP} m WHERE m.old_id = {event_id})"  # noqa: S608


_ACTIVE = f"'{Membership.ACTIVE.value}'"

OVERRIDES: dict[str, dict[str, str]] = {
    operations.name: {
        "membership": _ACTIVE,
        "membership_tx": (
            f"CASE WHEN t.membership = {_ACTIVE} THEN t.membership_tx "
            "ELSE transaction_timestamp() END"
        ),
        "idempotency_key": "NULL",
    },
    events.name: {"seq": _seq_of("t.id")},
    transcript_messages.name: {"membership": _ACTIVE, "seq": _seq_of("t.message_id")},
    "policies": {"idempotency_key": "NULL"},
}
"""Per-table columns that are not a plain remapped copy: memberships flatten to ``active``,
events and transcript rows take the new ``seq``, and idempotency keys stay with the source."""

_EVENT_IDS = f"(SELECT old_id FROM {_MAP} WHERE kind = 'event')"  # noqa: S608
_OPERATION_IDS = f"(SELECT old_id FROM {_MAP} WHERE kind = 'operation')"  # noqa: S608

ROW_FILTERS: dict[str, str] = {
    events.name: f"t.id IN {_EVENT_IDS}",
    transcript_messages.name: f"t.message_id IN {_EVENT_IDS}",
}
"""Which source rows a table copies beyond ``story_id``: by default the rows of the copied
operations when the table has an ``operation_id`` column, else every row of the story."""


def _row_filter(table: Table) -> str:
    where = "t.story_id = :source"
    if table.name in ROW_FILTERS:
        return f"{where} AND {ROW_FILTERS[table.name]}"
    if "operation_id" in table.columns:
        return f"{where} AND t.operation_id IN {_OPERATION_IDS}"
    return where


async def _map_primary_keys(conn: AsyncConnection, table: Table, where: str, source: str) -> None:
    """New ids for the rows' own single-column id key (characters, policies, …) when no
    earlier step mapped them."""
    keys = list(table.primary_key.columns)
    if len(keys) != 1 or keys[0].name == "story_id" or not _is_id(keys[0]):
        return
    key = keys[0].name
    found = await conn.scalars(
        text(
            f'SELECT t.{key} FROM "{table.name}" t WHERE {where} '  # noqa: S608
            f"AND t.{key}::text NOT IN (SELECT old_id FROM {_MAP})"
        ),
        {"source": source},
    )
    await _map(conn, _remapped([str(k) for k in found]), table.name)


async def _copy_table(conn: AsyncConnection, table: Table, source: str, fork_id: str) -> int:
    where = _row_filter(table)
    result = await conn.execute(
        text(_insert_select(table, OVERRIDES.get(table.name, {}), where)),
        {"source": source, "fork": fork_id},
    )
    return int(result.rowcount)


async def _copy_story_row(
    conn: AsyncConnection, source: str, fork_id: str, name: str | None, through: int, owner: str
) -> None:
    overrides = {
        "id": ":fork",
        "name": ":name",
        "owner_principal": ":owner",
        "status": f"'{StoryStatus.ACTIVE.value}'",
        "archived_at": "NULL",
        "timeline_revision": "1",
        "next_turn_index": ":next",
        "last_control_revision": "0",
        "last_control_operation_id": "NULL",
        "in_flight_turn": "NULL",
        "in_flight_lease_until": "NULL",
        "paused_by": "NULL",
        "created_at": "transaction_timestamp()",
        # The copied chain is checked in full at the fork's first chain check (#281); the
        # chain triggers would set it for the copied turns anyway.
        "chain_unchecked": "true",
    }
    await conn.execute(
        text(_insert_select(stories, overrides, "t.id = :source")),
        {"source": source, "fork": fork_id, "name": name, "next": through + 1, "owner": owner},
    )


async def _copy_config_changes(conn: AsyncConnection, source: str, fork_id: str) -> int:
    result = await conn.execute(
        text(_insert_select(config_changes, {"setting": f"{_ID}(t.setting)"}, _CONFIG_FILTER)),
        {"source": source, "fork": fork_id},
    )
    return int(result.rowcount)


async def _rebuild_heads(conn: AsyncConnection, fork_id: str) -> int:
    """The fork's ``story_heads`` row from its (all active) turns."""
    found = await conn.execute(
        select(operations.c.narrative_turn, operations.c.turn_variant).where(
            operations.c.story_id == fork_id,
            operations.c.kind == OperationKind.TURN.value,
        )
    )
    variants = {str(r.narrative_turn): int(r.turn_variant) for r in found}
    await conn.execute(insert(story_heads).values(**head_row(fork_id, variants)))
    return 1


async def _rebuild_segments(conn: AsyncConnection, fork_id: str) -> int:
    """The fork's ``lite_summary_segments`` rows, folded from its copied events (#72)."""
    folded = (await projections.fold(conn, fork_id, only=(projections.LITE_SUMMARY,)))[
        lite_summary_segments.name
    ]
    if len(folded):
        await conn.execute(insert(lite_summary_segments), list(folded))
    return len(folded)


async def _cast(conn: AsyncConnection, fork_id: str) -> tuple[ForkCharacter, ...]:
    found = await conn.execute(
        text(
            f"SELECT c.id, c.name, m.old_id AS source_id FROM characters c "  # noqa: S608
            f"JOIN {_MAP} m ON m.new_id = c.id::text "
            "WHERE c.story_id = :fork ORDER BY c.ordinal"
        ),
        {"fork": fork_id},
    )
    return tuple(ForkCharacter(id=r.id, name=r.name, source_id=r.source_id) for r in found)


async def _genesis(
    conn: AsyncConnection, fork_id: str, name: str | None, origin: ForkOrigin, principal: str
) -> str:
    """The fork's own ``genesis`` operation and ``story_created`` event (revision 1)."""
    row = (
        await conn.execute(
            select(stories, content_ratings.c.current_version_id)
            .join(content_ratings, content_ratings.c.id == stories.c.content_rating_id)
            .where(stories.c.id == fork_id)
        )
    ).one()
    cast = await conn.execute(
        select(characters.c.id, characters.c.name, characters.c.is_player)
        .where(characters.c.story_id == fork_id)
        .order_by(characters.c.ordinal)
    )
    payload = StoryCreated(
        name=name,
        characters=[CastMember(id=c.id, name=c.name, is_player=bool(c.is_player)) for c in cast],
        settings=settings_of(row),
        content_rating_id=row.content_rating_id,
        rating_version_id=row.current_version_id,
        forked_from=ForkedFrom(
            story_id=origin.story_id,
            revision=origin.revision,
            at_turn=origin.at_turn,
            variant_chain=None if origin.variant_chain is None else origin.variant_chain.wire(),
        ),
    )
    operation_id = new_ulid()
    now = func.transaction_timestamp()
    await conn.execute(
        insert(operations).values(
            operation_id=operation_id,
            story_id=fork_id,
            kind=OperationKind.GENESIS.value,
            status=OperationStatus.COMMITTED.value,
            principal=principal,
            base_revision=0,
            membership=Membership.ACTIVE.value,
            membership_tx=now,
            committed_at=now,
            timeline_revision=1,
        )
    )
    seq = (
        int(
            await conn.scalar(
                select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == fork_id)
            )
            or 0
        )
        + 1
    )
    event_id = new_ulid()
    await conn.execute(
        insert(events).values(
            id=event_id,
            story_id=fork_id,
            seq=seq,
            operation_id=operation_id,
            category=EventCategory.CONTROL.value,
            principal=principal,
            type="story_created",
            payload=payload.model_dump(mode="json"),
            provenance=Provenance.CONSUMER.value,
            authority=Authority.AUTHORITATIVE.value,
        )
    )
    await projections.apply_events(
        conn,
        fork_id,
        kind=CRASH_KIND,
        new_events=[
            projections.LogEvent(
                id=event_id,
                story_id=fork_id,
                seq=seq,
                operation_id=operation_id,
                kind=OperationKind.GENESIS,
                membership=Membership.ACTIVE,
                category=EventCategory.CONTROL,
                narrative_turn=None,
                turn_variant=None,
                beat=None,
                character_id=None,
                provenance=Provenance.CONSUMER,
                type="story_created",
                payload=validate("story_created", payload),
            )
        ],
    )
    return operation_id


async def _record_on_source(db: AsyncEngine, source: str, forked: Forked, principal: str) -> None:
    """The source's ``fork`` control event (no revision bump), after the copy committed."""
    origin = forked.forked_from
    try:
        async with Operation(
            db,
            story_id=source,
            kind=OperationKind.CONTROL,
            principal=principal,
            provenance=Provenance.CONSUMER,
            bumps_revision=False,
        ) as op:
            op.append(
                "fork",
                Fork(
                    fork_story_id=forked.story_id,
                    at_turn=origin.at_turn,
                    revision=origin.revision,
                    variant_chain=(
                        None if origin.variant_chain is None else origin.variant_chain.wire()
                    ),
                ),
            )
            await op.commit()
    except Exception as exc:  # the fork exists; only the source's record of it is missing
        logger.warning(
            "fork %s of %s: the source's fork event was not recorded: %s",
            forked.story_id,
            source,
            type(exc).__name__,
        )


__all__ = [
    "OVERRIDES",
    "SKIPPED_EVENT_TYPES",
    "SNAPSHOT_ATTEMPTS",
    "STORY_TABLES_REBUILT",
    "ForkCharacter",
    "ForkOrigin",
    "ForkRequest",
    "Forked",
    "fork",
]
