"""The residue scan (I-09, NFR-PRIV-2; #124): after an erasure, does any text in its scope still
name the subject?

``verify`` reads every text-bearing column in the erasure's scope and counts the values in which
the subject is still certainly named (``Redactor.hits``: the erasure matcher's certain mentions,
ADR-154; a candidate left unconfirmed is not residue). A JSON or array value is read as JSON and
its strings (keys and values) matched one by one, as erasure redacts them.
The columns are enumerated from ``information_schema``, not listed by hand, so a table a later
story adds is scanned without a change here:

- **story scope**: every ``public`` table with a ``story_id`` column, over its ``text``,
  ``character varying``, ``jsonb`` and array columns, for the story's rows; the story's own row
  (``stories.id``); its configuration changes (``config_changes``, ``scope = 'story'``); and its
  idempotency keys, its own (``scope = 'story'``) and the deployment-scoped ones bound to one of
  its operations (its creation's).
- **catalog scope**: every table of the ``catalog`` schema, ``personas`` and ``media_assets``,
  and the deployment-scoped records the catalog index covers: stored results of
  deployment-scoped keys, story-less events, policies and operation reports, and the
  deployment's configuration changes.

Never scanned: ``subject_references`` (the index, purged by the erasure itself),
``erasure_ledger`` (identifiers and keyed hashes only) and hash columns (a column whose name
holds ``hash``, ``checksum`` or ``fingerprint``, and ``read_set``, which holds hashes).

The answer is a count per table, never a text. A residue other than 0 does not undo anything:
it is reported (the erasure's response and the ledger's counts) and the erasure can be run
again. Another subject with the same name in the same scope counts too.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.erasure.redact import Redactor

TEXT_TYPES = ("text", "character varying", "jsonb", "json", "ARRAY")
"""The column types a scan reads (cast to text)."""
NEVER_SCANNED = frozenset({"subject_references", "erasure_ledger"})
HASH_MARKERS = ("hash", "checksum", "fingerprint")
HASH_COLUMNS = frozenset({"read_set"})
CATALOG_SCHEMA = "catalog"
CATALOG_PUBLIC_TABLES = ("personas", "media_assets")
STORY_SCOPE = "story"


@dataclass(frozen=True, slots=True)
class Residue:
    """What a scan found: the values still naming the subject, per table (``schema.table`` for
    a table outside ``public``), and how many values it read."""

    tables: dict[str, int] = field(default_factory=dict)
    scanned: int = 0

    @property
    def count(self) -> int:
        return sum(self.tables.values())


def _scanned_column(name: str) -> bool:
    return name not in HASH_COLUMNS and not any(marker in name for marker in HASH_MARKERS)


async def _columns(conn: AsyncConnection, schema: str, *, story: bool) -> dict[str, list[str]]:
    """``{table: [text columns]}`` of ``schema``'s base tables (only those with a ``story_id``
    column, with ``story``)."""
    found = await conn.execute(
        text(
            "SELECT c.table_name, c.column_name FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE t.table_type = 'BASE TABLE' AND c.table_schema = :schema "
            "AND c.data_type = ANY(CAST(:types AS text[])) "
            "AND (NOT :story OR EXISTS (SELECT 1 FROM information_schema.columns s "
            "WHERE s.table_schema = c.table_schema AND s.table_name = c.table_name "
            "AND s.column_name = 'story_id')) "
            "ORDER BY c.table_name, c.ordinal_position"
        ),
        {"schema": schema, "types": list(TEXT_TYPES), "story": story},
    )
    columns: dict[str, list[str]] = {}
    for row in found:
        table, column = str(row.table_name), str(row.column_name)
        if table in NEVER_SCANNED or not _scanned_column(column):
            continue
        columns.setdefault(table, []).append(column)
    return columns


def _strings(value: Any) -> Iterator[str]:
    """Every string of a column's value: itself, or a JSON document's or array's keys and
    leaves."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list | tuple):
        for child in value:
            yield from _strings(child)


def _quoted(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


async def _scan(
    conn: AsyncConnection,
    redactor: Redactor,
    residue: dict[str, int],
    label: str,
    source: str,
    columns: Sequence[str],
    where: str,
    params: dict[str, Any],
) -> int:
    """Count the values of ``columns`` in ``source``'s rows matching ``where`` that name the
    subject (into ``residue[label]``); answers how many values it read."""
    if not columns:
        return 0
    selected = ", ".join(f'"{c}"' for c in columns)
    rows = await conn.execute(
        text(f"SELECT {selected} FROM {source} WHERE {where}"),  # noqa: S608 - names from the catalog
        params,
    )
    read = 0
    for row in rows:
        for value in row:
            read += 1
            if value is not None and any(redactor.hits(s) for s in _strings(value)):
                residue[label] = residue.get(label, 0) + 1
    return read


async def verify_story(conn: AsyncConnection, story_id: str, redactor: Redactor) -> Residue:
    """The story-scope residue scan (see the module docstring)."""
    residue: dict[str, int] = {}
    read = 0
    params = {"s": story_id}
    for table, columns in (await _columns(conn, "public", story=True)).items():
        read += await _scan(
            conn,
            redactor,
            residue,
            table,
            _quoted("public", table),
            columns,
            "story_id = :s",
            params,
        )
    public = await _columns(conn, "public", story=False)
    read += await _scan(
        conn,
        redactor,
        residue,
        "stories",
        _quoted("public", "stories"),
        public.get("stories", []),
        "id = :s",
        params,
    )
    read += await _scan(
        conn,
        redactor,
        residue,
        "config_changes",
        _quoted("public", "config_changes"),
        public.get("config_changes", []),
        "scope = 'story' AND scope_id = :s",
        params,
    )
    read += await _scan(
        conn,
        redactor,
        residue,
        "idempotency_keys",
        _quoted("public", "idempotency_keys"),
        public.get("idempotency_keys", []),
        "(scope = 'story' AND scope_id = :s) OR operation_id IN "
        "(SELECT operation_id FROM operations WHERE story_id = :s)",
        params,
    )
    return Residue({k: v for k, v in residue.items() if v}, read)


async def verify_catalog(conn: AsyncConnection, redactor: Redactor) -> Residue:
    """The catalog-scope residue scan (see the module docstring)."""
    residue: dict[str, int] = {}
    read = 0
    for table, columns in (await _columns(conn, CATALOG_SCHEMA, story=False)).items():
        read += await _scan(
            conn,
            redactor,
            residue,
            f"{CATALOG_SCHEMA}.{table}",
            _quoted(CATALOG_SCHEMA, table),
            columns,
            "true",
            {},
        )
    public = await _columns(conn, "public", story=False)
    for table in CATALOG_PUBLIC_TABLES:
        read += await _scan(
            conn,
            redactor,
            residue,
            table,
            _quoted("public", table),
            public.get(table, []),
            "true",
            {},
        )
    for table, where in (
        ("idempotency_keys", "scope <> 'story' AND result IS NOT NULL"),
        ("events", "story_id IS NULL"),
        ("policies", "story_id IS NULL"),
        ("operations", "story_id IS NULL AND turn_report IS NOT NULL"),
        ("config_changes", "scope <> 'story'"),
    ):
        read += await _scan(
            conn,
            redactor,
            residue,
            table,
            _quoted("public", table),
            public.get(table, []),
            where,
            {},
        )
    return Residue({k: v for k, v in residue.items() if v}, read)


async def verify(conn: AsyncConnection, story_id: str | None, redactor: Redactor) -> Residue:
    """The residue scan of one scope: a story's (``story_id``) or the catalog's (``None``)."""
    if story_id is None:
        return await verify_catalog(conn, redactor)
    return await verify_story(conn, story_id, redactor)


__all__ = ["Residue", "verify", "verify_catalog", "verify_story"]
