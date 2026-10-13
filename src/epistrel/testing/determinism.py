"""The DETERMINISM comparator (NFR-DETERM-1; #75).

With the stub adapter, the same inputs must produce byte-identical Engine Core state. Two runs
of a script on two fresh databases differ only in what is generated per run: ids (ULIDs carry
the clock and random bits) and timestamps. ``state`` reads a database's Engine Core state in a
stable order and maps those away. ``differences`` lists every place where two such states (or
two runs' response bodies) still differ.

**What is compared.** Every table with a ``story_id`` column, plus ``stories``,
``deployment_state`` and the story-scoped ``config_changes``, except the operational tables
(``OPERATIONAL_TABLES``: work in progress and request bookkeeping, not story state). Each table
is read in ``ORDER``, an order built from natural keys (a story's creation order, then ``seq``,
turn, ordinal and the like), never from an id. ``state`` refuses a story table ``ORDER`` does
not know, so a new table is compared from the day it exists.

**What is mapped away.** Every ULID becomes ``«n»``, numbered in the order first met, alone or
inside a string such as ``character:<id>`` (the ``ID_PATTERN`` sweep covers ids nested in
``jsonb``). Every timestamp becomes ``«ts»``, whether a ``timestamptz`` column or an ISO string
in ``jsonb`` (``valid_time == tx_time`` in M2 maps the same way). Hashes are excluded: columns
and keys named ``*_hash`` are dropped, and a whole SHA-256 hex digest anywhere (the hashes an
operation's ``read_set`` records, #47) becomes ``«hash»``, because each digests content that
holds ids. Every key in ``VOLATILE_KEYS`` (durations, the worker and attempt of a job) is
dropped at any depth. Anything else that differs is a difference.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import CROCKFORD, ULID_LENGTH

OPERATIONAL_TABLES = frozenset(
    {"jobs", "timers", "recurring_tasks", "worker_heartbeats", "turn_queue", "idempotency_keys"}
)
"""Excluded: background work, timers, heartbeats, the turn queue and idempotency bookkeeping."""

VOLATILE_KEYS = frozenset(
    {"elapsed_ms", "latency_ms", "queued_ms", "first_call_offset_ms", "worker_id", "attempt"}
)
"""Keys dropped wherever they appear (columns, ``jsonb`` and response bodies): wall-clock
durations (a turn's queue wait and first-call delay too, #76) and which worker or attempt ran a
job."""

ID_PATTERN = re.compile(rf"(?<![{CROCKFORD}])[{CROCKFORD}]{{{ULID_LENGTH}}}(?![{CROCKFORD}])")
TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""A whole SHA-256 hex digest (a hash of content that holds ids)."""
ID_MARK = "«{n}»"
TIMESTAMP_MARK = "«ts»"
HASH_MARK = "«hash»"

_STORY_RANK = "(SELECT s.created_at FROM stories s WHERE s.id = t.story_id)"

ORDER: dict[str, str] = {
    "deployment_state": "t.id",
    "stories": "t.created_at",
    "characters": f"{_STORY_RANK}, t.ordinal",
    "operations": (
        f"{_STORY_RANK}, t.started_at, t.narrative_turn NULLS FIRST, t.turn_variant NULLS FIRST,"
        " t.kind"
    ),
    "events": f"{_STORY_RANK}, t.seq",
    "transcript_messages": (
        f"{_STORY_RANK}, t.seq, t.narrative_turn, t.turn_variant, t.beat, t.kind, t.channel, t.role"
    ),
    "lite_summary_segments": (
        f"{_STORY_RANK}, t.covers_from, t.covers_to, t.level, t.turn_variant, t.seq"
    ),
    "story_heads": _STORY_RANK,
    "story_texts": _STORY_RANK,
    "policies": f"{_STORY_RANK}, t.created_at, t.name",
    "client_events": f"{_STORY_RANK}, t.client_event_id",
    "audit_log": f"{_STORY_RANK} NULLS FIRST, t.at, t.kind",
    "config_changes": "t.at, t.config_revision, t.setting",
    # #105 creates the index; #123 writes its story-scoped rows and may refine this order.
    "subject_references": (
        f"{_STORY_RANK}, t.record_table, t.field NULLS FIRST, t.ref_kind, t.subject_kind"
    ),
}
"""Each compared table's ``ORDER BY`` (``t`` is the table): natural keys, never an id."""

WHERE: dict[str, str] = {
    "config_changes": "t.scope = 'story'",
    "subject_references": "t.scope = 'story'",
}
"""Rows of a compared table that are not story state are left out."""


class Normalizer:
    """Maps ids to ``«n»`` (first met, first numbered), timestamps to ``«ts»`` and digests to
    ``«hash»``, and drops ``*_hash`` and ``VOLATILE_KEYS`` keys; see the module docstring."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def _id(self, match: re.Match[str]) -> str:
        return self.ids.setdefault(match.group(0), ID_MARK.format(n=len(self.ids)))

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            if HASH_PATTERN.match(value):
                return HASH_MARK
            return TIMESTAMP_PATTERN.sub(TIMESTAMP_MARK, ID_PATTERN.sub(self._id, value))
        if isinstance(value, dt.datetime | dt.date | dt.time):
            return TIMESTAMP_MARK
        if isinstance(value, dt.timedelta | Decimal):
            return str(value)
        if isinstance(value, Mapping):
            return {
                self(k): self(v)
                for k, v in value.items()
                if not (isinstance(k, str) and (k in VOLATILE_KEYS or k.endswith("_hash")))
            }
        if isinstance(value, Sequence | set | frozenset) and not isinstance(value, bytes):
            return [self(v) for v in value]
        return value


async def compared_tables(db: AsyncEngine) -> list[str]:
    """The tables ``state`` reads: see the module docstring. Raises for a story table
    ``ORDER`` does not know."""
    async with db.connect() as conn:
        found = set(
            await conn.scalars(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'story_id'"
                )
            )
        )
    tables = (found | {"stories", "deployment_state", "config_changes"}) - OPERATIONAL_TABLES
    unknown = sorted(tables - ORDER.keys())
    if unknown:
        raise ValueError(f"no comparison order for {unknown}; add them to determinism.ORDER")
    return [name for name in ORDER if name in tables]


async def state(db: AsyncEngine, normalizer: Normalizer | None = None) -> dict[str, list[Any]]:
    """The database's Engine Core state, ordered and normalised (see the module docstring)."""
    normalize = normalizer or Normalizer()
    found: dict[str, list[Any]] = {}
    async with db.connect() as conn:
        for table in await compared_tables(db):
            where = f" WHERE {WHERE[table]}" if table in WHERE else ""
            result = await conn.execute(
                text(f'SELECT t.* FROM "{table}" t{where} ORDER BY {ORDER[table]}')  # noqa: S608
            )
            found[table] = [normalize(dict(row)) for row in result.mappings()]
    return found


def differences(left: Any, right: Any, path: str = "$", limit: int = 50) -> list[str]:
    """Every path where two normalised values differ (at most ``limit``)."""
    out: list[str] = []
    _diff(left, right, path, out, limit)
    return out


def _diff(left: Any, right: Any, path: str, out: list[str], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys(), key=str):
            if key not in left or key not in right:
                out.append(f"{path}.{key}: only on the {'right' if key in right else 'left'}")
            else:
                _diff(left[key], right[key], f"{path}.{key}", out, limit)
        return
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            out.append(f"{path}: {len(left)} items != {len(right)} items")
        for index, (a, b) in enumerate(zip(left, right, strict=False)):
            _diff(a, b, f"{path}[{index}]", out, limit)
        return
    if left != right:
        out.append(f"{path}: {left!r} != {right!r}")


__all__ = [
    "HASH_PATTERN",
    "ID_PATTERN",
    "OPERATIONAL_TABLES",
    "ORDER",
    "TIMESTAMP_PATTERN",
    "VOLATILE_KEYS",
    "Normalizer",
    "compared_tables",
    "differences",
    "state",
]
