"""The subject-reference index's writer (FR-ADMIN-6; #105, which #123 completes).

Every record that references a subject gets ``subject_references`` rows, written in the
record's own transaction, so erasure (#124) can find each record that names a subject, in
structured data and in prose. ``index_references`` (re)writes one record's rows:

- every **declared** subject (a structured id, e.g. a media asset's ``depicted_subjects``),
  whose ``field`` is where it was declared; and
- every subject **mentioned** in one of the record's text fields: the field's text holds one of
  the subject's terms (a name or an alias) as a whole word, ignoring case (NFC, case-folded,
  runs of whitespace as one space; terms shorter than two characters are never matched). The
  terms come from the tables that exist: for a catalog record (``story_id`` null, ``scope =
  catalog``) every catalog character's names and aliases, from its draft and every version
  (``name``, and the strings of ``metadata.aliases``, where #115 puts FR-CAT-2's aliases;
  ``ALIASES_PATH``), and, once #114 creates ``personas``, every persona's name; for a story
  record (``scope = story``) the story's characters' names. Over-matching is the safe direction
  for erasure.

``drop_references`` removes a record's rows; every delete path of an indexed record calls it
(#105's media sweep here). #123 adds the story side's aliases and name overrides to the scan,
the remaining writers, the writer registry and the rebuild tool. Log lines carry counts only
(invariant 3).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.ids import new_ulid
from epistrel.core.tables import character_catalog, character_versions, characters
from epistrel.core.tables.media import (
    ReferenceKind,
    ReferenceScope,
    SubjectKind,
    subject_references,
)

logger = logging.getLogger(__name__)

MIN_TERM_CHARS = 2
"""Terms (names and aliases) shorter than this are never matched in prose."""
PERSONAS = "public.personas"
ALIASES_PATH = ("metadata", "aliases")
"""Where a catalog character's draft and versions hold its aliases: FR-CAT-2's metadata, a list
of strings in #115's draft shape. Until #115 no draft can hold it, and the scan finds nothing
there. Anything else at this path (a value that is not a list, an entry that is not a string)
is skipped."""

Declared = tuple[SubjectKind, str, str | None]
"""A declared subject: its kind, its id and the field that declared it."""
Terms = list[tuple[SubjectKind, str, list[str]]]
"""Each subject a scan can find: its kind, its id and its terms (names and aliases)."""


def normalized(value: str) -> str:
    """``value`` as the matcher compares it: NFC, case-folded, whitespace runs as one space."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def mentions(value: str, names: Iterable[str]) -> bool:
    """Whether ``value`` holds one of ``names`` as a whole word (see the module docstring)."""
    haystack = normalized(value)
    for name in names:
        needle = normalized(name)
        if len(needle) < MIN_TERM_CHARS:
            continue
        if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack):
            return True
    return False


async def _table_has(conn: AsyncConnection, table: str, column: str) -> bool:
    found = await conn.scalar(
        sql_text(
            "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(CAST(:t AS text)) "
            "AND attname = :c AND attnum > 0 AND NOT attisdropped"
        ),
        {"t": table, "c": column},
    )
    return found is not None


async def _catalog_terms(conn: AsyncConnection) -> Terms:
    names: dict[str, set[str]] = {}
    draft, content = character_catalog.c.draft, character_versions.c.content
    drafts = select(character_catalog.c.id, draft["name"].astext, draft[ALIASES_PATH])
    versions = select(
        character_versions.c.catalog_id, content["name"].astext, content[ALIASES_PATH]
    )
    for query in (drafts, versions):
        for subject_id, name, aliases in await conn.execute(query):
            for term in [name, *(aliases if isinstance(aliases, list) else ())]:
                if isinstance(term, str) and term.strip():
                    names.setdefault(str(subject_id), set()).add(term)
    terms: Terms = [(SubjectKind.CATALOG_CHARACTER, i, sorted(n)) for i, n in names.items()]
    if await _table_has(conn, PERSONAS, "name"):
        found = await conn.execute(sql_text("SELECT id, name FROM public.personas"))
        terms += [
            (SubjectKind.PERSONA, str(subject_id), [str(name)])
            for subject_id, name in found
            if isinstance(name, str) and name.strip()
        ]
    return terms


async def _story_terms(conn: AsyncConnection, story_id: str) -> Terms:
    found = await conn.execute(
        select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
    )
    return [(SubjectKind.CHARACTER, str(i), [str(name)]) for i, name in found]


async def drop_references(conn: AsyncConnection, record_table: str, record_id: str) -> int:
    """Remove every ``subject_references`` row of one record; returns how many."""
    removed = await conn.execute(
        delete(subject_references).where(
            subject_references.c.record_table == record_table,
            subject_references.c.record_id == record_id,
        )
    )
    return int(removed.rowcount or 0)


async def index_references(
    conn: AsyncConnection,
    record_table: str,
    record_id: str,
    *,
    story_id: str | None,
    fields: Mapping[str, str | None],
    subjects: Sequence[Declared] = (),
) -> int:
    """(Re)write the record's ``subject_references`` rows in ``conn``'s transaction: its
    declared ``subjects`` and the subjects its text ``fields`` (``{dotted path: text}``)
    mention (see the module docstring). ``story_id`` null is the catalog scope. Returns how
    many rows the record has now."""
    await drop_references(conn, record_table, record_id)
    found: dict[tuple[str, str, str | None], ReferenceKind] = {}
    for kind, subject_id, field in subjects:
        found[(kind.value, subject_id, field)] = ReferenceKind.DECLARED
    texts = {path: value for path, value in fields.items() if value}
    if texts:
        terms = (
            await _catalog_terms(conn) if story_id is None else await _story_terms(conn, story_id)
        )
        for kind, subject_id, names in terms:
            for path, value in texts.items():
                key = (kind.value, subject_id, path)
                if key not in found and mentions(value, names):
                    found[key] = ReferenceKind.MENTIONED
    scope = ReferenceScope.CATALOG if story_id is None else ReferenceScope.STORY
    rows: list[dict[str, Any]] = [
        {
            "id": new_ulid(),
            "subject_kind": kind,
            "subject_id": subject_id,
            "scope": scope.value,
            "story_id": story_id,
            "record_table": record_table,
            "record_id": record_id,
            "field": field,
            "ref_kind": ref_kind.value,
        }
        for (kind, subject_id, field), ref_kind in found.items()
    ]
    if rows:
        await conn.execute(insert(subject_references), rows)
    logger.debug("indexed %d subject reference(s)", len(rows))
    return len(rows)


__all__ = [
    "ALIASES_PATH",
    "MIN_TERM_CHARS",
    "Declared",
    "drop_references",
    "index_references",
    "mentions",
    "normalized",
]
