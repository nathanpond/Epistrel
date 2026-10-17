"""``stories``: the story row whose lock serialises every commit to a story (FR-CONC-2).

Minimal here (#45); #50 extends it with the rest of the story's columns. #47 adds the
control-action marker (``last_control_revision``/``last_control_operation_id``: the revision and
operation of the latest truncate, untruncate, swap or regenerate, so an in-flight turn can tell
"history was edited" from "someone else played") and the story ``settings`` (``StorySettings``).
#48 adds ``content_rating_id``. #50 adds ``name``, ``archived_at`` and the status vocabulary
(``StoryStatus``: ``active``, ``archived``, ``paused``). #63 adds the turn-concurrency columns and
the in-flight turn marker. #104 adds ``owner_principal`` (FR-AUTH-5): the story's one owner, set at
creation and changed only by a transfer (``core.ownership``); it decides nothing (FR-AUTH-8).
#106 adds ``engine_profile_id`` (FR-PROF-1): the engine profile the story runs under, the
deployment's default when it was created (a fork keeps its source's). It references
``engine_profiles``, so a profile is never purged while a story uses it. #109 adds
``engine_profile_version_pin`` (FR-PROF-7,8's interim rule until M7): ``NULL`` while the story
follows its profile's latest version; the version it keeps when a confirmed data-producing edit
could not apply to it (``upgrade_unavailable``: it has history and M3 has no backfill). The pair
references ``engine_profile_versions``, so a pinned version always exists. #119 adds
``scenario_id``, ``scenario_version`` and ``scenario_snapshot`` (FR-SCN-9): the scenario version a
story was created from, by identifiers only (the catalog never participates in a running story),
and that version's whole document as genesis copied it; all three or none
(``ck_stories_scenario_snapshot``). #125 adds the ``blocked_by_erasure`` status and
``status_before_block``: the status a story had when the erasure reconciler blocked it (a restored
backup that predates an erasure the ledger holds), restored when its reapply completes; set
exactly while the story is blocked (``ck_stories_status_before_block``). #281 adds
``chain_unchecked``: the story's active turn chain changed in a way a commit's head-only chain
check cannot see, so the next check is the full one (``Operation._check_chain``).

Nothing references ``stories`` from another story, and story deletion enumerates every table
with a ``story_id`` (``epistrel.core.stories.STORY_TABLES``) instead of cascading.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.enums import StoryStatus
from epistrel.core.seeds.ratings import DEFAULT_RATING_ID
from epistrel.core.tables.base import metadata, ulid_type

INITIAL_TURN_INDEX = 1
"""``stories.next_turn_index`` of a new story: the first turn is turn 1."""

PIN_FOREIGN_KEY = "fk_stories_engine_profile_version_pin"
"""The pinned version references its profile's versions (#109)."""

STATUS_CHECK = "status IN ({})".format(", ".join(f"'{s.value}'" for s in StoryStatus))
TURN_CONCURRENCY_CHECK = "turn_concurrency IN ('reject', 'queue')"
TURN_QUEUE_MAX_CHECK = "turn_queue_max IS NULL OR turn_queue_max BETWEEN 1 AND 50"
STATUS_BEFORE_BLOCK_CHECK = (
    "(status = '{blocked}') = (status_before_block IS NOT NULL) AND "
    "(status_before_block IS NULL OR status_before_block IN ({before}))"
).format(
    blocked=StoryStatus.BLOCKED_BY_ERASURE.value,
    before=", ".join(
        f"'{s.value}'" for s in StoryStatus if s is not StoryStatus.BLOCKED_BY_ERASURE
    ),
)
SCENARIO_SNAPSHOT_CHECK = (
    "(scenario_id IS NULL AND scenario_version IS NULL AND scenario_snapshot IS NULL) "
    "OR (scenario_id IS NOT NULL AND scenario_version IS NOT NULL "
    "AND scenario_snapshot IS NOT NULL)"
)

stories = Table(
    "stories",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("name", Text, nullable=True),
    # FR-AUTH-5 (#104): set at creation (the requester, or the ``owner`` it names) and changed
    # only by a transfer. No server default: every insert names it.
    Column("owner_principal", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text(f"'{StoryStatus.ACTIVE.value}'")),
    Column("archived_at", TIMESTAMP(timezone=True), nullable=True),
    Column("timeline_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("config_revision", BigInteger, nullable=False, server_default=text("0")),
    Column(
        "next_turn_index",
        Integer,
        nullable=False,
        server_default=text(str(INITIAL_TURN_INDEX)),
    ),
    Column("last_control_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("last_control_operation_id", ulid_type(), nullable=True),
    Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    # FR-STORE-28 (#281): set by the ``operations_chain_changed_*`` triggers (migration 0045)
    # when the story's active turn set changed outside the commit that checks only the chain's
    # head; the next commit that checks the chain runs the full check and clears it.
    Column("chain_unchecked", Boolean, nullable=False, server_default=text("false")),
    # FR-CONC-5 (#63): same-story turn concurrency and the queue's bound (null: the
    # deployment's ``turn_queue_max``), mirrored in ``StorySettings``; and the durable in-flight
    # marker: the turn holding the story's turn slot and its lease (never a lock held across
    # model calls). An expired lease is reclaimed by the next request.
    Column("turn_concurrency", Text, nullable=False, server_default=text("'reject'")),
    Column("turn_queue_max", SmallInteger, nullable=True),
    Column("in_flight_turn", ulid_type(), nullable=True),
    Column("in_flight_lease_until", TIMESTAMP(timezone=True), nullable=True),
    # FR-RATE-7 (#118): the ``rating_check_jobs`` row that paused the story (``status =
    # 'paused'``); ``NULL`` otherwise.
    Column("paused_by", ulid_type(), nullable=True),
    # FR-ADMIN-7 (#125): the status to restore once the story's pending erasures are reapplied.
    Column("status_before_block", Text, nullable=True),
    # FR-RATE-1: every story has exactly one rating (#48). The server default is the seeded
    # default; story creation resolves the deployment's current default explicitly.
    Column(
        "content_rating_id",
        ulid_type(),
        nullable=False,
        server_default=text(f"'{DEFAULT_RATING_ID}'"),
    ),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    # FR-PROF-1 (#106): every story runs under exactly one profile. No server default: story
    # creation names the deployment's default, and a fork copies its source's.
    Column("engine_profile_id", ulid_type(), ForeignKey("engine_profiles.id"), nullable=False),
    # FR-PROF-7,8 interim (#109): the version an ``upgrade_unavailable`` story keeps; ``NULL``:
    # the story follows its profile's latest version.
    Column("engine_profile_version_pin", Integer, nullable=True),
    ForeignKeyConstraint(
        ["engine_profile_id", "engine_profile_version_pin"],
        ["engine_profile_versions.profile_id", "engine_profile_versions.version"],
        name=PIN_FOREIGN_KEY,
    ),
    Column("scenario_id", ulid_type(), nullable=True),
    Column("scenario_version", Integer, nullable=True),
    Column("scenario_snapshot", JSONB, nullable=True),
    CheckConstraint(STATUS_CHECK, name="status"),
    CheckConstraint(STATUS_BEFORE_BLOCK_CHECK, name="status_before_block"),
    CheckConstraint(SCENARIO_SNAPSHOT_CHECK, name="scenario_snapshot"),
    CheckConstraint(TURN_CONCURRENCY_CHECK, name="turn_concurrency"),
    CheckConstraint(TURN_QUEUE_MAX_CHECK, name="turn_queue_max"),
)

Index("ix_stories_owner_principal", stories.c.owner_principal)
# A profile's delete warnings and its purge find the stories that use it (#106).
Index("ix_stories_engine_profile_id", stories.c.engine_profile_id)
