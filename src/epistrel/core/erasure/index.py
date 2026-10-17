"""The subject-reference index (FR-ADMIN-6; #105 created it, #123 completes it).

Every record that references a subject gets ``subject_references`` rows, written in the
record's own transaction, so erasure (#124) can find each record that names a subject, in
structured data and in prose. ``subject_references`` is the only lookup erasure uses.

**Subjects.** A subject is one of:

- a story character (``character``, ``characters.id``). Its terms are its ``name``, the name and
  aliases of the catalog version it was snapshotted from (``character_snapshots.content``'s
  ``public_card.name`` and ``metadata.aliases``), its scenario role's ``name_overrides``
  (``canonical`` and ``aliases``, from ``stories.scenario_snapshot``) and its
  ``persona_snapshot.name``;
- a catalog character (``catalog_character``, ``character_catalog.id``). Its terms are the union
  of its draft's and every version's ``public_card.name`` (``NAME_PATH``) and
  ``metadata.aliases`` (``ALIASES_PATH``). It links every story character whose
  ``source_catalog_id`` it is;
- a persona (``persona``, ``personas.id``), whose term is its ``name``. It links the player
  characters made from it (``characters.persona_id``); an ad-hoc persona has none.

Each subject is also matched by its **parts**, the words of its multi-word names (``name_parts``,
#364: a player writes "Marisol" for "Marisol Quenby"), as written, and each half of a hyphenated
word (#374): the names above, never the aliases (an epithet's words, "the Red Witch", are not
the person), less articles, particles and titles (``NAME_PARTICLES``) and words shorter than
``MIN_TERM_CHARS``. A word another subject of the same scope also uses (a shared surname, a word
of another's name or alias) is left out (``with_name_parts``): it would name a living character
too. The erasure boundary says so. How a name, an alias or a part matches is
``core.erasure.matcher``'s (ADR-154).

A story record (``scope = story``) is scanned for its story's characters, and every row that
names a linked story character gets its ``catalog_character`` or ``persona`` row too, in the
same field (so a story record naming catalog-sourced Linda has a ``character`` and a
``catalog_character`` row, both ``scope = story``). A catalog record (``scope = catalog``:
catalog drafts and versions, personas, media, and deployment-scoped events, policies,
configuration changes and stored results) is scanned for every catalog character and persona.
Terms are recomputed for each call (one read per story or for the catalog, per call); nothing
about them is persisted.

**Rows.** ``index_references`` (one record) and ``index_records`` (several, sharing one read of
the terms) delete the record's rows and write them again:

- each **declared** subject (a structured id: a beat's character, a media asset's
  ``depicted_subjects``, a scenario role's character), whose ``field`` is where it was declared.
  In a story record, a character id that is not one of the story's characters names nobody and
  is skipped;
- each subject the record **defines** (a ``characters`` row, a catalog character's draft and
  versions, a persona): ``declared``, ``field`` null;
- an event's speaking character (its ``character_id`` column): ``authored``;
- each **mention** of a subject in one of the record's text fields (``{dotted path: text}``,
  list items as ``[n]``), one row per mention, with its position (ADR-154): ``span_start`` and
  ``span_end`` (offsets in the field's NFC text, in code points) and the matcher's ``rule``
  (``name``, ``part`` or ``candidate``; ``core.erasure.matcher``), never the text matched.

**The writers.** ``SUBJECT_WRITERS`` lists every indexed table with the modules that write it
(each calls ``index_references``/``index_records``) and the reader the rebuild enumerates it
with (``core.erasure.records``). ``tests/integration/test_subject_index.py`` checks it against
``information_schema``: a new story-content table without a registration (or a registered
writer that never calls the indexer) fails it. Tables M4+ adds register here when they land
(#131's predicate registry, propositions and entities did; memories, reflections, beliefs,
States and the outbox will). ``EXEMPT_TABLES`` names the story and catalog tables that are not
indexed, and why.

``drop_references``/``drop_many`` remove a record's rows; every delete path of an indexed record
calls one. ``reindex_story`` recomputes one story's rows (a fork after its copy; an ad-hoc
character, whose name earlier records may already hold; an erasure, first). ``rebuild``
compares the index with what the records give now (``python -m epistrel.tools.reindex_subjects``;
with ``--write``, the one-time re-index of rows written before positions were recorded,
migration 0044).

Log lines carry counts and identifiers only (invariant 3): one DEBUG line per call on this
module's logger.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, delete, func, insert, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.erasure.matcher import (
    DASHES,
    MIN_TERM_CHARS,
    NAME_PARTICLES,
    NOT_WORD_CHAR,
    WORD,
    Matcher,
    matcher,
    nfc,
    normalized,
)
from epistrel.core.ids import new_ulid
from epistrel.core.tables import (
    character_catalog,
    character_snapshots,
    character_versions,
    characters,
    personas,
    stories,
)
from epistrel.core.tables.media import (
    ReferenceKind,
    ReferenceScope,
    SubjectKind,
    subject_references,
)
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

NAME_PATH = ("public_card", "name")
"""Where a catalog character's draft and versions hold its name (#115; migration 0032 moved
#103's top-level ``name`` there)."""
ALIASES_PATH = ("metadata", "aliases")
"""Where a catalog character's draft and versions hold its aliases: FR-CAT-2's metadata, a list
of strings (#115). Anything else at this path (a value that is not a list, an entry that is not
a string) is skipped."""
CHUNK = 500
"""Records written per statement batch."""

Declared = tuple[SubjectKind, str, str | None]
"""A declared subject: its kind, its id and the field that declared it."""
Defined = tuple[SubjectKind, str]
"""A subject a record defines (its own row): its kind and id."""
RowKey = tuple[
    str, str, str, str | None, str, str, str | None, str, int | None, int | None, str | None
]
"""A row as the rebuild compares it: ``(subject_kind, subject_id, scope, story_id,
record_table, record_id, field, ref_kind, span_start, span_end, rule)`` (the surrogate id left
out; the last three are null except on a mention)."""


@dataclass(frozen=True, slots=True)
class Record:
    """One indexed record: its table and id, its story (``None``: the catalog scope), its text
    fields by dotted path, its declared subjects, the subjects it defines, and its speaking
    character (``authored``)."""

    table: str
    record_id: str
    story_id: str | None
    fields: Mapping[str, str | None] = field(default_factory=dict)
    subjects: Sequence[Declared] = ()
    defines: Sequence[Defined] = ()
    authored_by: str | None = None


@dataclass(frozen=True, slots=True)
class Subject:
    """A subject a scan can find: its kind, id, terms (names and aliases) and parts (the words
    of its names no other subject of the scope uses), and the subjects it links (a story
    character's catalog character and persona)."""

    kind: SubjectKind
    id: str
    terms: tuple[str, ...]
    links: tuple[Defined, ...] = ()
    parts: tuple[str, ...] = ()

    def matcher(self) -> Matcher:
        return matcher(self.terms, self.parts)


def mentions(value: str, names: Iterable[str]) -> bool:
    """Whether ``value`` certainly names one of ``names`` (``core.erasure.matcher``)."""
    return matcher(names).hits(value)


# -- terms -------------------------------------------------------------------------------------

_WORD_EDGES = re.compile(rf"^{NOT_WORD_CHAR}+|{NOT_WORD_CHAR}+$")
_DASH = re.compile(f"[{re.escape(DASHES)}]")


def _words(value: str) -> list[str]:
    """``value``'s whitespace-separated words, NFC and as written, without leading or trailing
    punctuation (``"(Marisol,"`` is ``Marisol``)."""
    return [w for w in (_WORD_EDGES.sub("", word) for word in nfc(value).split()) if w]


def name_parts(name: str) -> list[str]:
    """The parts of ``name`` a subject is also matched by (see the module docstring), as
    written: the words of a multi-word name and the halves of a hyphenated word (#374), at any
    dash (``matcher.DASHES``, #385)."""
    words = _words(name)
    found = list(words) if len(words) > 1 else []
    for word in words:
        halves = _DASH.split(word)
        if len(halves) > 1:  # any dash, not only "-" (#385)
            found += [half for half in (_WORD_EDGES.sub("", h) for h in halves) if half]
    return list(
        dict.fromkeys(
            w for w in found if len(w) >= MIN_TERM_CHARS and w.casefold() not in NAME_PARTICLES
        )
    )


def with_name_parts(
    named: Sequence[tuple[Sequence[str], Sequence[str]]],
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """For each subject of one scope, given as ``(names, terms)`` (``terms`` holds the names),
    its terms and its parts: the parts of its names no other subject of the scope uses."""
    # A word counts as used wherever the matcher would find it: "Ada's Shadow" uses "ada".
    owned = [
        {
            w
            for term in terms
            for w in (
                *(normalized(word) for word in _words(term)),
                *WORD.findall(normalized(term)),
            )
        }
        for _, terms in named
    ]
    users: dict[str, int] = {}
    for words in owned:
        for word in words:
            users[word] = users.get(word, 0) + 1
    found: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for (names, terms), words in zip(named, owned, strict=True):
        parts = [
            part
            for name in names
            for part in name_parts(name)
            if users.get(normalized(part), 0) - (normalized(part) in words) == 0
        ]
        found.append((tuple(dict.fromkeys(terms)), tuple(dict.fromkeys(parts))))
    return found


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _at(document: Any, path: Sequence[str]) -> Any:
    found = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found


async def catalog_subjects(conn: AsyncConnection) -> list[Subject]:
    """Every catalog character (names and aliases of its draft and every version) and every
    persona (its name)."""
    names: dict[str, set[str]] = {}
    terms: dict[str, set[str]] = {}
    draft, content = character_catalog.c.draft, character_versions.c.content
    drafts = select(character_catalog.c.id, draft[NAME_PATH].astext, draft[ALIASES_PATH])
    versions = select(
        character_versions.c.catalog_id, content[NAME_PATH].astext, content[ALIASES_PATH]
    )
    for query in (drafts, versions):
        for subject_id, name, aliases in await conn.execute(query):
            names.setdefault(str(subject_id), set()).update(_strings(name))
            held = terms.setdefault(str(subject_id), set())
            held.update(_strings(name))
            held.update(_strings(aliases))
    keys: list[tuple[SubjectKind, str]] = []
    named: list[tuple[list[str], list[str]]] = []
    for subject_id, held in terms.items():
        keys.append((SubjectKind.CATALOG_CHARACTER, subject_id))
        named.append((sorted(names.get(subject_id, ())), sorted(held)))
    for subject_id, name in await conn.execute(select(personas.c.id, personas.c.name)):
        keys.append((SubjectKind.PERSONA, str(subject_id)))
        named.append((_strings(name), _strings(name)))
    return [
        Subject(kind, subject_id, terms, parts=parts)
        for (kind, subject_id), (terms, parts) in zip(keys, with_name_parts(named), strict=True)
    ]


def _overrides(snapshot: Any) -> dict[str, tuple[list[str], list[str]]]:
    """The scenario snapshot's ``name_overrides`` by role id: its names (``canonical``) and its
    aliases."""
    roles = _at(snapshot, ("cast", "roles"))
    found: dict[str, tuple[list[str], list[str]]] = {}
    for role in roles if isinstance(roles, list) else []:
        if not isinstance(role, Mapping) or not isinstance(role.get("id"), str):
            continue
        overrides = role.get("name_overrides")
        if isinstance(overrides, Mapping):
            found[role["id"]] = (
                _strings(overrides.get("canonical")),
                _strings(overrides.get("aliases")),
            )
    return found


async def story_subjects(conn: AsyncConnection, story_id: str) -> list[Subject]:
    """The story's characters (see the module docstring for their terms and links)."""
    snapshot = await conn.scalar(
        select(stories.c.scenario_snapshot).where(stories.c.id == story_id)
    )
    overrides = _overrides(snapshot)
    snapshotted: dict[str, tuple[list[str], list[str]]] = {}
    for character_id, content in await conn.execute(
        select(character_snapshots.c.character_id, character_snapshots.c.content).where(
            character_snapshots.c.story_id == story_id
        )
    ):
        snapshotted[str(character_id)] = (
            _strings(_at(content, NAME_PATH)),
            _strings(_at(content, ALIASES_PATH)),
        )
    keys: list[tuple[str, tuple[Defined, ...]]] = []
    named: list[tuple[list[str], list[str]]] = []
    rows = await conn.execute(
        select(
            characters.c.id,
            characters.c.name,
            characters.c.role_id,
            characters.c.source_catalog_id,
            characters.c.persona_id,
            characters.c.persona_snapshot,
        ).where(characters.c.story_id == story_id)
    )
    for row in rows:
        character_id = str(row.id)
        snapshot_names, snapshot_aliases = snapshotted.get(character_id, ([], []))
        role_names, role_aliases = (
            overrides.get(row.role_id, ([], [])) if row.role_id is not None else ([], [])
        )
        names = [
            *_strings(row.name),
            *snapshot_names,
            *role_names,
            *_strings(_at(row.persona_snapshot, ("name",))),
        ]
        terms = [
            *_strings(row.name),
            *snapshot_names,
            *snapshot_aliases,
            *role_names,
            *role_aliases,
            *_strings(_at(row.persona_snapshot, ("name",))),
        ]
        links: list[Defined] = []
        if row.source_catalog_id is not None:
            links.append((SubjectKind.CATALOG_CHARACTER, str(row.source_catalog_id)))
        if row.persona_id is not None:
            links.append((SubjectKind.PERSONA, str(row.persona_id)))
        keys.append((character_id, tuple(links)))
        named.append((names, list(dict.fromkeys(terms))))
    return [
        Subject(SubjectKind.CHARACTER, character_id, terms, links, parts)
        for (character_id, links), (terms, parts) in zip(keys, with_name_parts(named), strict=True)
    ]


# -- computing a record's rows -----------------------------------------------------------------


Span = tuple[int, int, str] | None
"""A mention's ``(span_start, span_end, rule)``; ``None`` for a declared or authored row."""
Found = dict[tuple[str, str, str | None, int | None], tuple[ReferenceKind, Span]]


def _scan(record: Record, subjects: Sequence[Subject]) -> Found:
    story = record.story_id is not None
    by_id = {s.id: s for s in subjects if s.kind is SubjectKind.CHARACTER} if story else {}
    found: Found = {}

    def add(
        kind: SubjectKind, subject_id: str, at: str | None, how: ReferenceKind, span: Span = None
    ) -> None:
        if story and kind is SubjectKind.CHARACTER and subject_id not in by_id:
            return  # names no character of this story
        start = None if span is None else span[0]
        found.setdefault((SubjectKind(kind).value, subject_id, at, start), (how, span))

    for kind, subject_id in record.defines:
        add(kind, subject_id, None, ReferenceKind.DECLARED)
    if record.authored_by is not None:
        add(SubjectKind.CHARACTER, record.authored_by, "character_id", ReferenceKind.AUTHORED)
    for kind, subject_id, at in record.subjects:
        add(kind, subject_id, at, ReferenceKind.DECLARED)
    texts = {path: value for path, value in record.fields.items() if value}
    if texts:
        for subject in subjects:
            found_by = subject.matcher()
            for path, value in texts.items():
                for mention in found_by.mentions(value):
                    add(
                        subject.kind,
                        subject.id,
                        path,
                        ReferenceKind.MENTIONED,
                        (mention.start, mention.end, mention.rule.value),
                    )
    if story:
        for (named_kind, named_id, at, start), (how, span) in list(found.items()):
            linked = by_id.get(named_id) if named_kind == SubjectKind.CHARACTER.value else None
            for link_kind, link_id in linked.links if linked is not None else ():
                found.setdefault((link_kind.value, link_id, at, start), (how, span))
    return found


def _row(
    key: tuple[str, str, str | None, int | None],
    how: ReferenceKind,
    span: Span,
    scope: ReferenceScope,
    record: Record,
) -> RowKey:
    kind, subject_id, at, _ = key
    start, end, rule = (None, None, None) if span is None else span
    return (
        kind,
        subject_id,
        scope.value,
        record.story_id,
        record.table,
        record.record_id,
        at,
        how.value,
        start,
        end,
        rule,
    )


class Terms:
    """The subjects each scope scans for, read once per scope and kept for one call."""

    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn
        self._read: dict[str | None, list[Subject]] = {}

    async def of(self, story_id: str | None) -> list[Subject]:
        if story_id not in self._read:
            self._read[story_id] = (
                await catalog_subjects(self.conn)
                if story_id is None
                else await story_subjects(self.conn, story_id)
            )
        return self._read[story_id]


async def compute(
    conn: AsyncConnection, records: Sequence[Record], terms: Terms | None = None
) -> list[RowKey]:
    """The rows ``records`` give now (none written)."""
    terms = terms or Terms(conn)
    rows: list[RowKey] = []
    for record in records:
        scope = ReferenceScope.CATALOG if record.story_id is None else ReferenceScope.STORY
        for key, (how, span) in _scan(record, await terms.of(record.story_id)).items():
            rows.append(_row(key, how, span, scope, record))
    return rows


def _values(row: RowKey) -> dict[str, Any]:
    kind, subject_id, scope, story_id, table, record_id, at, how, start, end, rule = row
    return {
        "id": new_ulid(),
        "subject_kind": kind,
        "subject_id": subject_id,
        "scope": scope,
        "story_id": story_id,
        "record_table": table,
        "record_id": record_id,
        "field": at,
        "ref_kind": how,
        "span_start": start,
        "span_end": end,
        "rule": rule,
    }


async def _delete_records(conn: AsyncConnection, keys: Sequence[tuple[str, str]]) -> int:
    removed = 0
    for start in range(0, len(keys), CHUNK):
        chunk = keys[start : start + CHUNK]
        result = await conn.execute(
            delete(subject_references).where(
                tuple_(subject_references.c.record_table, subject_references.c.record_id).in_(chunk)
            )
        )
        removed += int(result.rowcount or 0)
    return removed


async def _write(conn: AsyncConnection, rows: Sequence[RowKey]) -> None:
    for start in range(0, len(rows), CHUNK):
        await conn.execute(
            insert(subject_references), [_values(r) for r in rows[start : start + CHUNK]]
        )


# -- the writers' calls ------------------------------------------------------------------------


_SPY: ContextVar[list[str] | None] = ContextVar("subject_index_spy", default=None)


@contextmanager
def recording() -> Iterator[list[str]]:
    """Record the ``record_table`` of every record indexed in this context (and the tasks it
    starts): the runtime half of the writer-coverage check (#123; #127 reuses it)."""
    seen: list[str] = []
    token = _SPY.set(seen)
    try:
        yield seen
    finally:
        _SPY.reset(token)


async def index_records(conn: AsyncConnection, records: Sequence[Record]) -> int:
    """(Re)write the rows of every one of ``records`` in ``conn``'s transaction (see the module
    docstring); returns how many rows they have now."""
    if not records:
        return 0
    spy = _SPY.get()
    if spy is not None:
        spy.extend(r.table for r in records)
    rows = await compute(conn, records)
    await _delete_records(conn, list(dict.fromkeys((r.table, r.record_id) for r in records)))
    await _write(conn, rows)
    stories_named = {r.story_id for r in records}
    log.debug(
        "subject references indexed",
        record_table=records[0].table if len({r.table for r in records}) == 1 else "*",
        record_id=records[0].record_id if len(records) == 1 else None,
        story_id=next(iter(stories_named)) if len(stories_named) == 1 else None,
        records=len(records),
        rows=len(rows),
    )
    return len(rows)


async def index_references(
    conn: AsyncConnection,
    record_table: str,
    record_id: str,
    *,
    story_id: str | None,
    fields: Mapping[str, str | None],
    subjects: Sequence[Declared] = (),
    defines: Sequence[Defined] = (),
    authored_by: str | None = None,
) -> int:
    """(Re)write one record's ``subject_references`` rows in ``conn``'s transaction: its
    declared ``subjects``, the subjects it ``defines``, its speaking character and the subjects
    its text ``fields`` (``{dotted path: text}``) mention (see the module docstring).
    ``story_id`` null is the catalog scope. Returns how many rows the record has now."""
    return await index_records(
        conn,
        [
            Record(
                record_table,
                record_id,
                story_id,
                fields,
                tuple(subjects),
                tuple(defines),
                authored_by,
            )
        ],
    )


async def drop_references(conn: AsyncConnection, record_table: str, record_id: str) -> int:
    """Remove every ``subject_references`` row of one record; returns how many."""
    removed = await conn.execute(
        delete(subject_references).where(
            subject_references.c.record_table == record_table,
            subject_references.c.record_id == record_id,
        )
    )
    return int(removed.rowcount or 0)


async def drop_catalog_mentions(conn: AsyncConnection, kind: SubjectKind, subject_id: str) -> int:
    """Remove every catalog ``mentioned`` row naming a catalog subject that is being deleted
    (#350): other records that named it no longer reference anything, and a rebuild, which
    scans only the subjects that exist, would not produce them. Returns how many."""
    sr = subject_references.c
    removed = await conn.execute(
        delete(subject_references).where(
            sr.subject_kind == kind.value,
            sr.subject_id == subject_id,
            sr.scope == ReferenceScope.CATALOG.value,
            sr.ref_kind == ReferenceKind.MENTIONED.value,
        )
    )
    return int(removed.rowcount or 0)


async def drop_many(conn: AsyncConnection, record_table: str, record_ids: Iterable[str]) -> int:
    """Remove the rows of every record of ``record_table`` in ``record_ids`` (a bulk delete or
    purge of the records); returns how many."""
    keys = [(record_table, str(i)) for i in dict.fromkeys(record_ids)]
    removed = await _delete_records(conn, keys)
    if keys:
        log.debug(
            "subject references dropped", record_table=record_table, records=len(keys), rows=removed
        )
    return removed


async def drop_prefixed(conn: AsyncConnection, record_table: str, prefix: str) -> int:
    """Remove the rows of every record of ``record_table`` whose id starts with ``prefix`` (a
    catalog entry's versions, ``<id>@``)."""
    removed = await conn.execute(
        delete(subject_references).where(
            and_(
                subject_references.c.record_table == record_table,
                subject_references.c.record_id.startswith(prefix, autoescape=True),
            )
        )
    )
    return int(removed.rowcount or 0)


# -- the registry ------------------------------------------------------------------------------


Reader = Callable[[AsyncConnection, str | None], Awaitable[list[Record]]]
"""Every record of a table in one scope (a story id, or ``None`` for the catalog)."""


@dataclass(frozen=True, slots=True)
class SubjectWriter:
    """An indexed table: the ``record_table`` its rows name, the modules that write it (each
    calls the indexer), and its rebuild reader (``module:function``)."""

    table: str
    writers: tuple[str, ...]
    reader: str

    def read(self) -> Reader:
        module, _, name = self.reader.partition(":")
        found: Reader = getattr(importlib.import_module(module), name)
        return found


_RECORDS = "epistrel.core.erasure.records"
_REGISTRY = "epistrel.core.propositions.registry"
_PROPOSITIONS = "epistrel.core.propositions.store"

SUBJECT_WRITERS: tuple[SubjectWriter, ...] = (
    SubjectWriter(
        "events",
        (
            "epistrel.core.events.operation",
            "epistrel.core.config_changes",
            "epistrel.core.ratings.change",
            "epistrel.core.fork",
        ),
        f"{_RECORDS}:read_events",
    ),
    SubjectWriter(
        "characters",
        ("epistrel.core.stories", "epistrel.core.genesis.create"),
        f"{_RECORDS}:read_characters",
    ),
    SubjectWriter(
        "character_snapshots", ("epistrel.core.genesis.create",), f"{_RECORDS}:read_snapshots"
    ),
    SubjectWriter(
        "stories",
        ("epistrel.core.stories", "epistrel.core.genesis.create", "epistrel.core.fork"),
        f"{_RECORDS}:read_stories",
    ),
    SubjectWriter(
        "genesis_deferred", ("epistrel.core.genesis.create",), f"{_RECORDS}:read_deferred"
    ),
    SubjectWriter(
        "operations",
        ("epistrel.core.genesis.create", "epistrel.orchestrator.lite.pipeline"),
        f"{_RECORDS}:read_operations",
    ),
    SubjectWriter(
        "policies", ("epistrel.orchestrator.policies.admin",), f"{_RECORDS}:read_policies"
    ),
    SubjectWriter("message_feedback", ("epistrel.core.feedback",), f"{_RECORDS}:read_ratings"),
    SubjectWriter(
        "feedback_history", ("epistrel.core.feedback",), f"{_RECORDS}:read_rating_history"
    ),
    SubjectWriter(
        "config_changes", ("epistrel.core.config_changes",), f"{_RECORDS}:read_config_changes"
    ),
    SubjectWriter(
        "idempotency_keys", ("epistrel.core.idempotency",), f"{_RECORDS}:read_idempotency"
    ),
    SubjectWriter(
        "character_catalog",
        ("epistrel.core.catalog.characters",),
        f"{_RECORDS}:read_character_drafts",
    ),
    SubjectWriter(
        "character_versions",
        ("epistrel.core.catalog.characters",),
        f"{_RECORDS}:read_character_versions",
    ),
    SubjectWriter(
        "scenario_catalog", ("epistrel.core.catalog.scenarios",), f"{_RECORDS}:read_scenario_drafts"
    ),
    SubjectWriter(
        "scenario_versions",
        ("epistrel.core.catalog.scenarios",),
        f"{_RECORDS}:read_scenario_versions",
    ),
    SubjectWriter("personas", ("epistrel.core.catalog.personas",), f"{_RECORDS}:read_personas"),
    SubjectWriter("media_assets", ("epistrel.core.media.assets",), f"{_RECORDS}:read_media"),
    SubjectWriter(
        "media_upload_sessions",
        ("epistrel.core.media.uploads",),
        f"{_RECORDS}:read_upload_sessions",
    ),
    # The structured stores (#131): the predicate registry, propositions and entities.
    SubjectWriter("predicates", (_REGISTRY,), f"{_RECORDS}:read_predicates"),
    SubjectWriter("predicate_proposals", (_REGISTRY,), f"{_RECORDS}:read_proposals"),
    SubjectWriter("propositions", (_PROPOSITIONS,), f"{_RECORDS}:read_propositions"),
    SubjectWriter("closed_sets", (_PROPOSITIONS,), f"{_RECORDS}:read_closed_sets"),
    SubjectWriter("entities", (_PROPOSITIONS,), f"{_RECORDS}:read_entities"),
    SubjectWriter("entity_aliases", (_PROPOSITIONS,), f"{_RECORDS}:read_aliases"),
)
"""Every indexed table (see the module docstring); the coverage test's source of truth."""

EXEMPT_TABLES: Mapping[str, str] = {
    "subject_references": "the index itself",
    "transcript_messages": "a projection of the story's events, which are indexed",
    "story_texts": "a projection of the pinned_memories_set and custom_instructions_set events",
    "story_heads": "a projection (the active variants)",
    "lite_summary_segments": "a projection of the lite_summary_segment events (refolded by a "
    "rebuild and a fork), which are indexed",
    "turn_queue": "transient: a queued turn's request until it runs (its input is then an event)",
    "jobs": "transient work items, deleted by the retention sweep",
    "timers": "transient: a timer's kind and target id",
    "client_events": "ingest de-duplication: client ids and the ids of the events written",
    "audit_log": "deployment audit records: identifiers and outcomes (docs/04's M4 clause)",
    "story_profile_overrides": "engine-profile dial values (numbers and enums)",
    "rating_check_jobs": "a rating check's progress and verdicts by record key and policy id",
    "guardrail_check_results": "verdicts by record key and policy id",
    # Deployment tables (#334: the coverage test enumerates every table with text or JSON).
    "alembic_version": "the schema migration's revision id",
    "schema_meta": "schema bookkeeping keys and values",
    "deployment_state": "deployment settings, tag and listing-status sets, reconcile state",
    "content_ratings": "operator-authored rating names and the retiring principal",
    "content_rating_versions": "operator-authored rating guidance and policy ids",
    "engine_profiles": "an engine profile's name and owner principal (configuration)",
    "engine_profile_versions": "engine-profile dial values (numbers and enums)",
    "model_config": "model endpoints, model names, parameters and an encrypted key",
    "entity_relations": "entity ids, principals and free-text relation names",
    "ownership_events": "ownership changes: entity ids, principals, relation names",
    "offboarding_jobs": "a principal's offboarding: ids, actions, counts and its report of ids",
    "erasure_ledger": "the erasure ledger: identifiers, scope and hashes, never content",
    "feedback_export_log": "an export's filters, blob key and checksum (the file is erasure's "
    "feedback_export target)",
    "recurring_tasks": "a recurring task's name and last status",
    "worker_heartbeats": "worker ids and modes",
}
"""Tables with text or JSON columns that are not indexed, and why."""


def writer(table: str) -> SubjectWriter:
    """The registration of ``table``."""
    for entry in SUBJECT_WRITERS:
        if entry.table == table:
            return entry
    raise KeyError(table)


async def records_of(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    """Every indexed record in one scope (a story, or ``None``: the catalog), read through the
    registry's readers."""
    found: list[Record] = []
    for entry in SUBJECT_WRITERS:
        found.extend(await entry.read()(conn, story_id))
    return found


async def current(conn: AsyncConnection, story_id: str | None) -> list[RowKey]:
    """The rows the index holds for one scope."""
    sr = subject_references.c
    where = (
        sr.scope == ReferenceScope.CATALOG.value if story_id is None else sr.story_id == story_id
    )
    found = await conn.execute(
        select(
            sr.subject_kind,
            sr.subject_id,
            sr.scope,
            sr.story_id,
            sr.record_table,
            sr.record_id,
            sr.field,
            sr.ref_kind,
            sr.span_start,
            sr.span_end,
            sr.rule,
        ).where(where)
    )
    return [
        (
            str(r.subject_kind),
            str(r.subject_id),
            str(r.scope),
            None if r.story_id is None else str(r.story_id),
            str(r.record_table),
            str(r.record_id),
            r.field,
            str(r.ref_kind),
            None if r.span_start is None else int(r.span_start),
            None if r.span_end is None else int(r.span_end),
            r.rule,
        )
        for r in found
    ]


@dataclass(frozen=True, slots=True)
class Difference:
    """What a rebuild of one scope found: rows the records give that the index lacks
    (``missing``) and rows the index holds that the records no longer give (``extra``)."""

    story_id: str | None
    missing: tuple[RowKey, ...]
    extra: tuple[RowKey, ...]

    @property
    def clean(self) -> bool:
        return not self.missing and not self.extra


async def rebuild(
    conn: AsyncConnection, story_id: str | None, *, write: bool = False
) -> Difference:
    """Recompute one scope's rows from its records and compare them with the index (surrogate
    ids and order ignored). With ``write``, the scope's rows are replaced by the recomputed
    ones in ``conn``'s transaction."""
    expected = await compute(conn, await records_of(conn, story_id))
    held = await current(conn, story_id)
    want, have = set(expected), set(held)
    difference = Difference(
        story_id,
        tuple(sorted(want - have, key=_order)),
        tuple(sorted(have - want, key=_order)),
    )
    if write and not difference.clean:
        sr = subject_references.c
        await conn.execute(
            delete(subject_references).where(
                sr.scope == ReferenceScope.CATALOG.value
                if story_id is None
                else sr.story_id == story_id
            )
        )
        await _write(conn, sorted(want, key=_order))
    log.debug(
        "subject references rebuilt",
        story_id=story_id,
        rows=len(want),
        missing=len(difference.missing),
        extra=len(difference.extra),
    )
    return difference


def _order(row: RowKey) -> tuple[str, ...]:
    return tuple("" if v is None else f"{v:012d}" if isinstance(v, int) else v for v in row)


async def rebuild_scope(
    db: AsyncEngine, story_id: str | None, *, write: bool = False, principal: str = "system"
) -> Difference:
    """``rebuild`` for one scope in a transaction of its own: a story's as a ``maintenance``
    operation holding the story lock when it writes (no event, no revision bump), else read in
    one repeatable-read transaction; the catalog's under the deployment lock."""
    from epistrel.core.enums import OperationKind
    from epistrel.core.events.operation import DEPLOYMENT_LOCK_KEY, Operation

    if story_id is not None and write:
        found: list[Difference] = []

        async def step(conn: AsyncConnection) -> None:
            found.append(await rebuild(conn, story_id, write=True))

        async with Operation(
            db, story_id=story_id, kind=OperationKind.MAINTENANCE, principal=principal
        ) as op:
            op.add_commit_step(step)
            await op.commit()
        return found[0]
    async with db.connect() as conn, conn.begin():
        await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        if story_id is None:
            await conn.execute(select(func.pg_advisory_xact_lock(DEPLOYMENT_LOCK_KEY)))
        return await rebuild(conn, story_id, write=write)


async def story_ids(db: AsyncEngine) -> list[str]:
    """Every story's id, in id order."""
    async with db.connect() as conn:
        return [str(i) for i in await conn.scalars(select(stories.c.id).order_by(stories.c.id))]


async def reindex_story(conn: AsyncConnection, story_id: str) -> int:
    """Rewrite every row of one story from its records (in ``conn``'s transaction); returns
    how many it has now."""
    sr = subject_references.c
    await conn.execute(delete(subject_references).where(sr.story_id == story_id))
    rows = await compute(conn, await records_of(conn, story_id))
    await _write(conn, rows)
    log.debug("subject references reindexed", story_id=story_id, rows=len(rows))
    return len(rows)


async def reindex_catalog_subject(conn: AsyncConnection, kind: SubjectKind, subject_id: str) -> int:
    """Rewrite one catalog subject's ``mentioned`` rows from every catalog record (in
    ``conn``'s transaction), after its terms were created or changed: a record that named it
    before it existed, or before it took an alias, is indexed from now on, and one that named
    only a dropped term no longer is (#335; ``reindex_story`` is the story scope's
    counterpart). Declared and authored rows do not depend on terms and are left alone.
    Returns how many mention rows it has now."""
    sr = subject_references.c
    subject = next(
        (s for s in await catalog_subjects(conn) if (s.kind, s.id) == (kind, subject_id)), None
    )
    rows: list[RowKey] = []
    if subject is not None:
        for record in await records_of(conn, None):
            if record.story_id is not None:
                continue
            for key, (how, span) in _scan(record, [subject]).items():
                if how is ReferenceKind.MENTIONED and key[:2] == (kind.value, subject_id):
                    rows.append(_row(key, how, span, ReferenceScope.CATALOG, record))
    await conn.execute(
        delete(subject_references).where(
            sr.subject_kind == kind.value,
            sr.subject_id == subject_id,
            sr.scope == ReferenceScope.CATALOG.value,
            sr.ref_kind == ReferenceKind.MENTIONED.value,
        )
    )
    await _write(conn, sorted(set(rows), key=_order))
    log.debug("catalog subject reindexed", subject_kind=kind.value, rows=len(rows))
    return len(rows)


__all__ = [
    "ALIASES_PATH",
    "EXEMPT_TABLES",
    "MIN_TERM_CHARS",
    "NAME_PARTICLES",
    "NAME_PATH",
    "SUBJECT_WRITERS",
    "Declared",
    "Defined",
    "Difference",
    "Record",
    "RowKey",
    "Subject",
    "SubjectWriter",
    "Terms",
    "catalog_subjects",
    "compute",
    "current",
    "drop_catalog_mentions",
    "drop_many",
    "drop_prefixed",
    "drop_references",
    "index_records",
    "index_references",
    "mentions",
    "name_parts",
    "normalized",
    "rebuild",
    "rebuild_scope",
    "recording",
    "records_of",
    "reindex_catalog_subject",
    "reindex_story",
    "story_ids",
    "story_subjects",
    "with_name_parts",
    "writer",
]
