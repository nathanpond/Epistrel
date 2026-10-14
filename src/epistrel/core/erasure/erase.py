"""Erasure (FR-ADMIN-2,6, NFR-PRIV-2; #124): remove a person from a story, or from the catalog,
so that afterwards no text in that scope names them.

``POST /admin/erasure`` (``Engine.erase``) takes ``{subject: {kind, id}, scope: {story: <id>} |
{catalog: true}, dry_run?}``. The valid pairings:

- ``character`` + ``story``: one story character;
- ``catalog_character`` + ``story``: the story's characters created from that catalog
  character; ``persona`` + ``story``: the story's player character made from that persona;
- ``catalog_character`` + ``catalog``: the catalog character itself (every version, its draft
  and its companion), and what the catalog says of it; ``persona`` + ``catalog``: the persona;
- ``character`` + ``catalog`` is 422 ``invalid_erasure_scope``.

**Discovery** goes through the subject-reference index (#123), the only lookup: the records a
subject's rows name. Two additions the index cannot give: a story's deployment-scoped stored
results (its creation's, bound to one of its operations, which the index scans for catalog
subjects only), and in the catalog scope, every catalog record is also matched against the
subject's terms (a catalog record written before the subject existed holds its name without an
index row, #123's hand-off). The subject's **terms** are its names and aliases as the index
collects them (``index.story_subjects``; a catalog character's draft and versions; a persona's
name), read before anything is deleted.

**A dry run** computes exactly what the real run would touch and writes nothing (no operation,
no idempotency key): ``{records: {table: n}, prose_segments, media, idempotency_results}`` with
``regeneration`` and the expected ``degradation``. An unknown subject's dry run answers zeros.
``records`` counts the structured rows deleted or rewritten, ``prose_segments`` the text fields
redacted (records' fields, not spans), ``media`` the assets (story scope: the images the
subject's characters show, which stay; catalog scope: those purged, retired or rewritten), and
``idempotency_results`` the stored results purged. Marker and index rows are not counted.
Catalog table names are schema-qualified (``catalog.character_versions``).

**A story-scope erasure** runs as three operations, so a failure after the first leaves a
complete erasure:

1. one ``control`` operation (it bumps the story's revision, so a turn in flight abandons, and
   runs on archived and paused stories), in one transaction under the story lock and
   ``epistrel.erasure = on``: the subject's ``characters`` rows, their ``character_snapshots``
   and the ``genesis_deferred`` items that belong only to them are deleted, each leaving one
   position-less ``erasure_marker`` event; every string of every record that names the subject
   is redacted in place (``Redactor``: each matched span becomes ``[erased]``), and a beat the
   subject authored (its ``character_id`` is theirs, or it is a reply whose
   ``render_character_ids`` are theirs alone) becomes ``[erased]`` whole, keeping its type,
   position and operation, with ``redacted_at``; their ids leave ``render_character_ids`` and
   the events' ``character_id``;
   the story's default viewpoint is cleared when it names them; stored idempotency results that
   name them are purged (``result`` null: a replay answers the reference); the projections are
   refolded and the story's index rebuilt (the subject's rows go last); nothing in flight can
   write them back (#127, ``_stop_in_flight``: queued turns fail ``timeline_changed``, the turn
   slot's lease is cleared, and the redacted events' operations have their ``membership_tx``
   moved so a background job that read them drops its output); the residue is scanned;
   the ``erasure_ledger`` row and a deployment ``audit_log`` row are written. Stages
   ``redacted`` and ``ledger_written`` go to the operation's ``stage_log``.
2. one ``consolidation`` operation regenerates the level-0 rolling-summary segments whose text
   was redacted (``redact.regenerate_summaries``: #72's compression over the redacted window,
   through the ``SummaryCompressor`` port). Without a model, or when a call fails, the
   redaction stands and ``degradation`` says ``summary_regeneration_failed``;
   ``summary_regeneration_capped`` when the run's 20 calls ran out.
3. one ``maintenance`` operation rebuilds the projections (synchronously, even when step 2
   failed; ``rebuild_failed`` otherwise), and the residue scan runs again (``residue``).

**A catalog-scope erasure** is one transaction recorded by one committed ``control``
operation: the catalog character is deleted through the catalog's own delete (its versions, the
immutability trigger let through by ``epistrel.erasure``; its companion; its relations), or the
persona through the persona delete; every other catalog record naming the subject is redacted
(drafts and published versions, scenario versions' reports, other characters' documents), and a
scenario role filled with the subject loses its ``character``; media assets that depict or name
the subject are rewritten (several subjects: the subject leaves ``depicted_subjects``, captions
and alt text are redacted) or, depicting only the subject, purged with their bytes once no other
asset shares them, unless a record the erasure leaves untouched references them and the
deployment's ``erasure_shared_media`` is ``retire`` (the asset is kept ``retired``, redacted);
deployment-scoped stored results naming the subject are purged. Stories are never touched
(FR-STORE-14): ``referencing_stories`` lists those that reference the subject (with their
characters), for the operator to erase separately. Blob bytes are deleted after the commit; a
failed delete is left to the media sweep.

**The ledger** (``erasure_ledger``, and first its file outside the database: #125,
``core.erasure.ledger``) holds identifiers, the scope, the story, the counts, the
residue and keyed hashes of the scanned terms (HMAC-SHA256 under ``EPISTREL_CONFIG_KEY``,
which must be set: 503 ``config_key_missing`` otherwise), never a name. ``ERASURE_TARGETS``
lets later stores (#126's feedback exports) take part in an erasure.

A real run on an unknown subject is 404 ``not_found``; a known subject with nothing left to
erase (one already erased: its ledger row makes it known) answers 200 with zero counts, a new
ledger row, and another attempt at regenerating still-redacted summary windows.

Log lines carry identifiers and counts only (invariant 3): one INFO line per erasure or dry run.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import Text, and_, cast, delete, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import (
    audit,
    clock,
    control,
    feedback,
    idempotency,
    ownership,
    projections,
    turn_gate,
)
from epistrel.core.config_changes import read_section
from epistrel.core.enums import (
    Authority,
    EventCategory,
    OperationKind,
    OperationStatus,
    Provenance,
)
from epistrel.core.erasure import index
from epistrel.core.erasure import ledger as ledger_store
from epistrel.core.erasure.index import (
    ALIASES_PATH,
    NAME_PATH,
    Record,
    index_records,
    records_of,
    reindex_story,
    story_subjects,
)
from epistrel.core.erasure.ledger import LedgerFile
from epistrel.core.erasure.records import (
    character_draft_record,
    character_version_record,
    idempotency_id,
    media_record,
    persona_record,
)
from epistrel.core.erasure.redact import ERASED, Redactor, regenerate_summaries
from epistrel.core.erasure.verify import Residue, verify
from epistrel.core.errors import (
    ErasureKeyMissing,
    InvalidErasureScope,
    SubjectNotFound,
    TimelineChanged,
)
from epistrel.core.events.registry import validate
from epistrel.core.ids import Ulid, new_ulid
from epistrel.core.ports import SummaryCompressor
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, ErasureSettings
from epistrel.core.tables import (
    character_catalog,
    character_snapshots,
    character_versions,
    characters,
    config_changes,
    erasure_ledger,
    events,
    genesis_deferred,
    idempotency_keys,
    jobs,
    media_assets,
    operations,
    personas,
    policies,
    scenario_catalog,
    scenario_versions,
    stories,
    turn_queue,
)
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.catalog import CATALOG_SCHEMA, ERASURE_GUC
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.media import (
    MediaState,
    ReferenceScope,
    SubjectKind,
    subject_references,
)
from epistrel.core.tables.ownership import EntityType
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

STORY = "story"
CATALOG = "catalog"
MARKER = "erasure_marker"
BEATS = frozenset({"player_input", "message_rendered", "think", "latent"})
"""Event types that are beats: one the subject authored is erased whole."""
BEAT_TEXTS = ("text", "say", "do")
SUMMARY_EVENT = "lite_summary_segment"
RENDER_IDS = "render_character_ids"
RENDERED = "message_rendered"
CHARACTER_POLICY = "character"
STORY_KEY_SCOPE = "story"
DEPLOYMENT_KEY_SCOPE = "deployment"
REGENERATION_MODEL = "model"
REGENERATION_REDACTION = "redaction"
DEGRADED_REGENERATION = "summary_regeneration_failed"
CAPPED_REGENERATION = "summary_regeneration_capped"
DEGRADED_REBUILD = "rebuild_failed"


# -- the request ---------------------------------------------------------------------------------


class SubjectRef(BaseModel):
    """Who is erased: a story ``character``, a ``catalog_character`` or a ``persona``, by id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SubjectKind
    id: str = Field(min_length=1, max_length=64)


class StoryScope(BaseModel):
    """Erase within one story."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story: Ulid


class CatalogScope(BaseModel):
    """Erase from the catalog (its characters, scenarios, personas and media)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog: Literal[True]


class ErasureRequest(BaseModel):
    """``POST /admin/erasure``'s body (see the module docstring)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "subject": {"kind": "character", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"},
                    "scope": {"story": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C4"},
                    "dry_run": True,
                }
            ]
        },
    )

    subject: SubjectRef
    scope: StoryScope | CatalogScope
    dry_run: StrictBool = Field(
        default=False,
        description="Report what would be erased and change nothing (no operation, no key).",
    )

    @property
    def story_id(self) -> str | None:
        return self.scope.story if isinstance(self.scope, StoryScope) else None

    @property
    def scope_name(self) -> str:
        return STORY if isinstance(self.scope, StoryScope) else CATALOG


def check_request(request: ErasureRequest) -> None:
    """422 ``invalid_erasure_scope`` for ``character`` + ``catalog``: a story character exists
    only in its story."""
    if request.subject.kind is SubjectKind.CHARACTER and request.story_id is None:
        raise InvalidErasureScope(
            "a story character is erased in its story: scope it to `{story: <id>}`",
            {"field": "body.scope", "subject_kind": request.subject.kind.value, "scope": CATALOG},
        )


def require_key(config_key: str | None) -> str:
    """The configuration key the ledger's term hashes are keyed with; 503
    ``config_key_missing`` without one."""
    if not config_key:
        raise ErasureKeyMissing(
            "erasure needs EPISTREL_CONFIG_KEY: the ledger keys the hashes of the terms it "
            "scanned with it",
            {"setting": "config_key"},
        )
    return config_key


def term_hashes(terms: Iterable[str], key: str) -> list[str]:
    """HMAC-SHA256 under ``key`` of each normalised term (``index.normalized``), sorted."""
    secret = key.encode()
    return sorted(
        {
            hmac.new(secret, index.normalized(term).encode(), hashlib.sha256).hexdigest()
            for term in terms
        }
    )


# -- the result ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferencingStory:
    """A story that references the subject: its id and the characters there that are the
    subject (a catalog-scope erasure leaves it alone, FR-STORE-14)."""

    story_id: str
    character_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """The ledger row an erasure wrote, as the response shows it: identifiers and counts."""

    erasure_id: str
    at: datetime
    scope: str
    story_id: str | None
    counts: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ErasureResult:
    """What an erasure did (or, ``dry_run``, would do); see the module docstring."""

    dry_run: bool
    subject_kind: str
    subject_id: str
    scope: str
    story_id: str | None
    records: dict[str, int]
    prose_segments: int
    media: int
    idempotency_results: int
    regeneration: str
    degradation: tuple[str, ...]
    referencing_stories: tuple[ReferencingStory, ...]
    config_revision: int
    timeline_revision: int | None = None
    erasure_id: str | None = None
    operation_id: str | None = None
    ledger_entry: LedgerEntry | None = None
    residue: int | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- other stores (ERASURE_TARGETS) --------------------------------------------------------------


class ErasureTarget(Protocol):
    """A store outside the event store that must take part in an erasure (#126's feedback
    exports): it redacts what it holds of the subject (``write`` false: counts only) and
    answers how many of its records it changed."""

    name: str

    async def erase(
        self,
        conn: AsyncConnection,
        *,
        story_id: str | None,
        redactor: Redactor,
        write: bool,
    ) -> int: ...


ERASURE_TARGETS: list[ErasureTarget] = []
"""The other stores an erasure reaches (see ``ErasureTarget``); empty in M3 until #126."""


def register_target(target: ErasureTarget) -> None:
    if all(t.name != target.name for t in ERASURE_TARGETS):
        ERASURE_TARGETS.append(target)


# -- counting ------------------------------------------------------------------------------------


@dataclass
class _Tally:
    records: dict[str, int] = field(default_factory=dict)
    prose: int = 0
    media: int = 0
    results: int = 0

    def record(self, table: str, fields: int = 0) -> None:
        self.records[table] = self.records.get(table, 0) + 1
        self.prose += fields

    def counts(self) -> dict[str, Any]:
        return {
            "records": dict(sorted(self.records.items())),
            "prose_segments": self.prose,
            "media": self.media,
            "idempotency_results": self.results,
        }


def _catalog(table: str) -> str:
    return f"{CATALOG_SCHEMA}.{table}"


async def _guc(conn: AsyncConnection) -> None:
    await conn.execute(select(func.set_config(ERASURE_GUC, "on", True)))


def _without(ids: Any, removed: set[str]) -> Any:
    return [i for i in ids if i not in removed] if isinstance(ids, list) else ids


def _drop_render_ids(document: Any, removed: set[str]) -> Any:
    """``document`` with ``removed`` taken out of every ``render_character_ids`` list."""
    if isinstance(document, Mapping):
        return {
            key: _without(value, removed) if key == RENDER_IDS else _drop_render_ids(value, removed)
            for key, value in document.items()
        }
    if isinstance(document, list):
        return [_drop_render_ids(item, removed) for item in document]
    return document


def _voiced_only_by(row: Any, payload: Mapping[str, Any], removed: set[str]) -> bool:
    """A reply with no speaker of its own, rendered for the subject alone: she authored it
    (``render_character_ids`` are the characters it voices)."""
    if row.type != RENDERED or row.character_id is not None or not removed:
        return False
    voiced = payload.get(RENDER_IDS)
    return isinstance(voiced, list) and bool(voiced) and set(voiced) <= removed


def _erase_beat(payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """A beat the subject authored: each of its texts becomes ``[erased]`` whole."""
    erased = dict(payload)
    changed = 0
    for key in BEAT_TEXTS:
        value = erased.get(key)
        if isinstance(value, str) and value != ERASED:
            erased[key] = ERASED
            changed += 1
    return erased, changed


async def _settings(conn: AsyncConnection) -> ErasureSettings:
    return await read_section(conn, ErasureSettings)


async def config_revision(conn: AsyncConnection) -> int:
    """The deployment's ``config_revision`` (an erasure leaves it as catalog writes do)."""
    from epistrel.core.tables import deployment_state

    found = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(found or 0)


# -- the subject ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Subject:
    kind: SubjectKind
    id: str
    story_id: str | None
    characters: tuple[str, ...]
    """The story characters that are the subject (story scope)."""
    terms: tuple[str, ...]
    known: bool

    @property
    def keys(self) -> list[tuple[str, str]]:
        found = [(SubjectKind.CHARACTER.value, c) for c in self.characters]
        found.append((self.kind.value, self.id))
        return list(dict.fromkeys(found))


async def _catalog_terms(conn: AsyncConnection, subject: SubjectRef) -> tuple[list[str], bool]:
    """The subject's catalog terms and whether it exists in the catalog."""
    if subject.kind is SubjectKind.PERSONA:
        name = await conn.scalar(select(personas.c.name).where(personas.c.id == subject.id))
        return ([str(name)] if name else []), name is not None
    if subject.kind is not SubjectKind.CATALOG_CHARACTER:
        return [], False
    terms: list[str] = []
    draft = await conn.scalar(
        select(character_catalog.c.draft).where(character_catalog.c.id == subject.id)
    )
    documents = [draft] if draft is not None else []
    documents += list(
        await conn.scalars(
            select(character_versions.c.content).where(
                character_versions.c.catalog_id == subject.id
            )
        )
    )
    for document in documents:
        terms += index._strings(index._at(document, NAME_PATH))
        terms += index._strings(index._at(document, ALIASES_PATH))
    return list(dict.fromkeys(terms)), draft is not None


async def _erased_before(conn: AsyncConnection, subject: SubjectRef, story_id: str | None) -> bool:
    """Whether the ledger holds an earlier erasure of ``subject`` in this scope."""
    where = [
        erasure_ledger.c.subject_kind == subject.kind.value,
        erasure_ledger.c.subject_id == subject.id,
    ]
    where.append(
        erasure_ledger.c.scope == CATALOG
        if story_id is None
        else erasure_ledger.c.story_ref == story_id
    )
    return await conn.scalar(select(erasure_ledger.c.id).where(*where).limit(1)) is not None


async def _subject(conn: AsyncConnection, request: ErasureRequest) -> _Subject:
    """Who the request erases, in its scope, with its terms (read before anything changes)."""
    subject, story_id = request.subject, request.story_id
    catalog_terms, in_catalog = await _catalog_terms(conn, subject)
    if story_id is None:
        known = in_catalog or await _erased_before(conn, subject, None)
        return _Subject(subject.kind, subject.id, None, (), tuple(catalog_terms), known)
    c = characters.c
    linked = {
        SubjectKind.CHARACTER: c.id,
        SubjectKind.CATALOG_CHARACTER: c.source_catalog_id,
        SubjectKind.PERSONA: c.persona_id,
    }[subject.kind]
    found = tuple(
        str(i)
        for i in await conn.scalars(
            select(c.id).where(c.story_id == story_id, linked == subject.id).order_by(c.ordinal)
        )
    )
    terms: list[str] = []
    for candidate in await story_subjects(conn, story_id):
        if candidate.id in found:
            terms += candidate.terms
    if subject.kind is not SubjectKind.CHARACTER:
        terms += catalog_terms
    known = bool(found) or in_catalog or await _erased_before(conn, subject, story_id)
    if subject.kind is SubjectKind.CHARACTER:
        known = bool(found) or await _erased_before(conn, subject, story_id)
    return _Subject(subject.kind, subject.id, story_id, found, tuple(dict.fromkeys(terms)), known)


async def _referenced_records(
    conn: AsyncConnection, subject: _Subject, scope: ReferenceScope
) -> dict[str, set[str]]:
    """``{record_table: {record_id}}`` the index names for the subject in ``scope``."""
    sr = subject_references.c
    where = [
        sr.scope == scope.value,
        or_(*(and_(sr.subject_kind == k, sr.subject_id == i) for k, i in subject.keys)),
    ]
    if scope is ReferenceScope.STORY:
        where.append(sr.story_id == subject.story_id)
    found: dict[str, set[str]] = {}
    for row in await conn.execute(select(sr.record_table, sr.record_id).where(*where).distinct()):
        found.setdefault(str(row.record_table), set()).add(str(row.record_id))
    return found


# -- events (both scopes) ------------------------------------------------------------------------


async def _redact_events(
    conn: AsyncConnection,
    event_ids: Iterable[str],
    redactor: Redactor,
    tally: _Tally,
    *,
    removed: set[str],
    write: bool,
    now: datetime,
) -> list[Any]:
    """Redact the events ``event_ids`` in place (see the module docstring); answers the rows
    rewritten (as ``event_record`` needs them, with the new payload)."""
    ids = sorted(set(event_ids))
    rewritten: list[Any] = []
    for start in range(0, len(ids), index.CHUNK):
        rows = await conn.execute(
            select(
                events.c.id,
                events.c.story_id,
                events.c.type,
                events.c.character_id,
                events.c.payload,
            )
            .where(events.c.id.in_(ids[start : start + index.CHUNK]))
            .order_by(events.c.id)
        )
        for row in rows:
            payload = dict(row.payload or {})
            new, changed = redactor.document(payload)
            authored = row.character_id is not None and str(row.character_id) in removed
            if (authored or _voiced_only_by(row, payload, removed)) and row.type in BEATS:
                new, whole = _erase_beat(new)
                changed = max(changed, whole)
            new = _drop_render_ids(new, removed)
            speaker = None if authored else row.character_id
            if new == payload and speaker == row.character_id:
                continue
            validate(row.type, new)  # still a valid payload of its type
            tally.record(events.name, changed)
            if write:
                await conn.execute(
                    update(events)
                    .where(events.c.id == row.id)
                    .values(payload=new, character_id=speaker, redacted_at=now)
                )
            rewritten.append(
                dataclasses.replace(_EventRow(**row._asdict()), payload=new, character_id=speaker)
            )
    return rewritten


@dataclass(frozen=True, slots=True)
class _EventRow:
    id: str
    story_id: str | None
    type: str
    character_id: str | None
    payload: Any


# -- the story scope -----------------------------------------------------------------------------


@dataclass
class _StoryRun:
    subject: _Subject
    redactor: Redactor
    write: bool
    erasure_id: str
    operation_id: str | None
    principal: str
    now: datetime
    markers: list[tuple[str, str]] = field(default_factory=list)


async def _story_pass(conn: AsyncConnection, run: _StoryRun) -> _Tally:
    """The story-scope work of step 1 (or, ``write`` false, its count)."""
    subject, redactor, write = run.subject, run.redactor, run.write
    story_id = subject.story_id
    assert story_id is not None  # noqa: S101 - a story-scope erasure
    removed = set(subject.characters)
    tally = _Tally()
    refs = await _referenced_records(conn, subject, ReferenceScope.STORY)
    if write:
        await _guc(conn)

    await _redact_events(
        conn,
        refs.get(events.name, ()),
        redactor,
        tally,
        removed=removed,
        write=write,
        now=run.now,
    )
    await _redact_story_row(conn, story_id, removed, redactor, tally, write=write)
    await _redact_characters(conn, story_id, refs, removed, redactor, tally, run)
    await _redact_snapshots(conn, story_id, refs, removed, redactor, tally, run)
    await _redact_deferred(conn, story_id, refs, removed, redactor, tally, run)
    await _redact_json_rows(
        conn,
        operations,
        operations.c.operation_id,
        refs.get(operations.name, ()),
        ("turn_report",),
        redactor,
        tally,
        write=write,
    )
    await _redact_policies(conn, story_id, refs, removed, redactor, tally, run)
    redacted = await feedback.redact_for_erasure(
        conn,
        story_id,
        rating_ids=refs.get(feedback.RATING_RECORDS, set()),
        history_ids=refs.get(feedback.HISTORY_RECORDS, set()),
        redactor=redactor,
        write=write,
    )
    for _ in range(redacted.ratings):
        tally.record(feedback.RATING_RECORDS, 1)
    for _ in range(redacted.history):
        tally.record(feedback.HISTORY_RECORDS, 1)
    await _redact_json_rows(
        conn,
        config_changes,
        config_changes.c.change_id,
        refs.get(config_changes.name, ()),
        ("old", "new"),
        redactor,
        tally,
        write=write,
    )
    await _purge_story_results(conn, story_id, refs, redactor, tally, write=write)
    await _redact_transient(conn, story_id, redactor, tally, write=write)
    for target in ERASURE_TARGETS:
        changed = await target.erase(conn, story_id=story_id, redactor=redactor, write=write)
        for _ in range(changed):
            tally.record(target.name)
    tally.media += await _story_media(conn, story_id, removed)
    # The subject's own rows go last: their markers, then the rows.
    for character_id in subject.characters:
        await _delete(
            conn,
            run,
            tally,
            characters.name,
            character_id,
            delete(characters).where(characters.c.id == character_id),
        )
    return tally


async def _delete(
    conn: AsyncConnection, run: _StoryRun, tally: _Tally, table: str, record_id: str, statement: Any
) -> None:
    tally.record(table)
    run.markers.append((table, record_id))
    if run.write:
        await conn.execute(statement)


async def _redact_story_row(
    conn: AsyncConnection,
    story_id: str,
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    *,
    write: bool,
) -> None:
    row = (
        await conn.execute(
            select(stories.c.name, stories.c.settings, stories.c.scenario_snapshot).where(
                stories.c.id == story_id
            )
        )
    ).one()
    changed = 0
    name = redactor.text(row.name) if row.name else row.name
    changed += int(name != row.name)
    snapshot, count = redactor.document(row.scenario_snapshot)
    changed += count
    settings, count = redactor.document(row.settings or {})
    changed += count
    viewpoint = settings.get("default_output_viewpoint") if isinstance(settings, dict) else None
    cleared = isinstance(viewpoint, str) and any(
        viewpoint == f"{CHARACTER_VIEWPOINT_PREFIX}{c}" for c in removed
    )
    if cleared:
        settings = {**settings, "default_output_viewpoint": None}
    if not changed and not cleared:
        return
    tally.record(stories.name, changed)
    if write:
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(name=name, settings=settings, scenario_snapshot=snapshot)
        )


async def _redact_characters(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """Other characters' rows that name the subject (a definition, a persona copy)."""
    others = sorted(refs.get(characters.name, set()) - removed)
    if not others:
        return
    rows = await conn.execute(
        select(characters).where(characters.c.story_id == story_id, characters.c.id.in_(others))
    )
    for row in rows:
        values: dict[str, Any] = {}
        changed = 0
        for column in ("name", "definition", "safe_fallback_text", "deflection_style"):
            value = getattr(row, column)
            if isinstance(value, str):
                new = redactor.text(value)
                if new != value:
                    values[column] = new
                    changed += 1
        if row.persona_snapshot is not None:
            snapshot, count = redactor.document(row.persona_snapshot)
            if count:
                values["persona_snapshot"] = snapshot
                changed += count
        if not values:
            continue
        tally.record(characters.name, changed)
        if run.write:
            await conn.execute(update(characters).where(characters.c.id == row.id).values(**values))


async def _redact_snapshots(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    named = {r.partition(":")[2] for r in refs.get(character_snapshots.name, set())} | removed
    rows = await conn.execute(
        select(character_snapshots.c.character_id, character_snapshots.c.content).where(
            character_snapshots.c.story_id == story_id,
            character_snapshots.c.character_id.in_(sorted(named)),
        )
    )
    for row in rows:
        character_id = str(row.character_id)
        where = and_(
            character_snapshots.c.story_id == story_id,
            character_snapshots.c.character_id == character_id,
        )
        if character_id in removed:
            await _delete(
                conn,
                run,
                tally,
                character_snapshots.name,
                f"{story_id}:{character_id}",
                delete(character_snapshots).where(where),
            )
            continue
        content, changed = redactor.document(row.content)
        if changed:
            tally.record(character_snapshots.name, changed)
            if run.write:
                await conn.execute(update(character_snapshots).where(where).values(content=content))


async def _redact_deferred(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    ids = sorted(refs.get(genesis_deferred.name, set()))
    if not ids:
        return
    rows = await conn.execute(
        select(genesis_deferred.c.id, genesis_deferred.c.payload).where(
            genesis_deferred.c.story_id == story_id, genesis_deferred.c.id.in_(ids)
        )
    )
    for row in rows:
        payload = dict(row.payload or {})
        held = payload.get("characters")
        resolved = dict(held) if isinstance(held, Mapping) else {}
        named = {c for c in resolved.values() if isinstance(c, str)}
        if named and named <= removed:
            await _delete(
                conn,
                run,
                tally,
                genesis_deferred.name,
                str(row.id),
                delete(genesis_deferred).where(genesis_deferred.c.id == row.id),
            )
            continue
        value, changed = redactor.document(payload.get("value"))
        new = {**payload, "value": value}
        if isinstance(held, Mapping):
            new["characters"] = {r: c for r, c in resolved.items() if c not in removed}
        if new == payload:
            continue
        tally.record(genesis_deferred.name, changed)
        if run.write:
            await conn.execute(
                update(genesis_deferred).where(genesis_deferred.c.id == row.id).values(payload=new)
            )


async def _redact_json_rows(
    conn: AsyncConnection,
    table: Any,
    key: Any,
    ids: Iterable[str],
    columns: Sequence[str],
    redactor: Redactor,
    tally: _Tally,
    *,
    write: bool,
    label: str | None = None,
) -> int:
    """Redact the JSON (or text) ``columns`` of ``table``'s rows ``ids``; answers how many
    rows changed."""
    wanted = sorted(set(ids))
    if not wanted:
        return 0
    rows = await conn.execute(select(key, *(table.c[c] for c in columns)).where(key.in_(wanted)))
    rewritten = 0
    for row in rows:
        values: dict[str, Any] = {}
        changed = 0
        for column in columns:
            value = getattr(row, column)
            if value is None:
                continue
            if isinstance(value, str):
                new: Any = redactor.text(value)
                count = int(new != value)
            else:
                new, count = redactor.document(value)
            if count:
                values[column] = new
                changed += count
        if not values:
            continue
        rewritten += 1
        tally.record(label or table.name, changed)
        if write:
            await conn.execute(update(table).where(key == row[0]).values(**values))
    return rewritten


async def _redact_policies(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """A policy of one of the subject's characters goes with it; another policy naming the
    subject is redacted."""
    owned = list(
        await conn.scalars(
            select(policies.c.id).where(
                policies.c.story_id == story_id,
                policies.c.scope == CHARACTER_POLICY,
                policies.c.scope_id.in_(sorted(removed)),
            )
        )
    )
    for policy_id in owned:
        await _delete(
            conn,
            run,
            tally,
            policies.name,
            str(policy_id),
            delete(policies).where(policies.c.id == policy_id),
        )
    await _redact_json_rows(
        conn,
        policies,
        policies.c.id,
        set(refs.get(policies.name, set())) - {str(p) for p in owned},
        ("name", "message", "rule"),
        redactor,
        tally,
        write=run.write,
    )


async def _purge_results(
    conn: AsyncConnection, keys: Sequence[Any], tally: _Tally, *, write: bool
) -> None:
    """Null the stored results of ``keys`` (a replay then answers the reference)."""
    if not keys:
        return
    tally.results += len(keys)
    if not write:
        return
    for key in keys:
        await conn.execute(
            update(idempotency_keys)
            .where(
                idempotency_keys.c.scope == key.scope,
                idempotency_keys.c.scope_id == key.scope_id,
                idempotency_keys.c.key == key.key,
            )
            .values(result=None)
        )
    await idempotency.drop_results(conn, keys)


async def _purge_story_results(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    redactor: Redactor,
    tally: _Tally,
    *,
    write: bool,
) -> None:
    """The story's stored results that name the subject: its own keys the index names, and the
    deployment-scoped keys bound to its operations whose result names the subject."""
    k = idempotency_keys.c
    indexed = refs.get(idempotency_keys.name, set())
    found = await conn.execute(
        select(k.scope, k.scope_id, k.key, k.result).where(
            k.result.is_not(None),
            or_(
                and_(k.scope == STORY_KEY_SCOPE, k.scope_id == story_id),
                k.operation_id.in_(
                    select(operations.c.operation_id).where(operations.c.story_id == story_id)
                ),
            ),
        )
    )
    keys = [
        row
        for row in found
        if idempotency_id(str(row.scope), str(row.scope_id), str(row.key)) in indexed
        or redactor.count(row.result)
    ]
    await _purge_results(conn, keys, tally, write=write)


async def _redact_transient(
    conn: AsyncConnection, story_id: str, redactor: Redactor, tally: _Tally, *, write: bool
) -> None:
    """Work in progress for the story (queued turns' requests, job payloads): not indexed (it
    is transient), so matched against the terms here."""
    for table, key, column in (
        (turn_queue, turn_queue.c.operation_id, "request"),
        (jobs, jobs.c.id, "payload"),
    ):
        ids = [
            str(row[0])
            for row in await conn.execute(
                select(key, table.c[column]).where(table.c.story_id == story_id)
            )
            if redactor.count(row[1])
        ]
        await _redact_json_rows(conn, table, key, ids, (column,), redactor, tally, write=write)


async def _story_media(conn: AsyncConnection, story_id: str, removed: set[str]) -> int:
    """The images the subject's characters show (counted; story erasure leaves the assets)."""
    found = 0
    for images in await conn.scalars(
        select(characters.c.images).where(
            characters.c.story_id == story_id, characters.c.id.in_(sorted(removed))
        )
    ):
        found += len(images) if isinstance(images, list) else 0
    return found


async def _insert_markers(conn: AsyncConnection, run: _StoryRun) -> None:
    """One position-less ``erasure_marker`` event per deleted record (in step 1's commit)."""
    if not run.markers:
        return
    story_id = run.subject.story_id
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == story_id)
    )
    seq = int(last or 0)
    rows = []
    for table, record_id in run.markers:
        seq += 1
        payload = validate(
            MARKER,
            {
                "erasure_id": run.erasure_id,
                "subject_kind": run.subject.kind.value,
                "subject_id": run.subject.id,
                "scope": STORY,
                "record_table": table,
                "record_id": record_id,
            },
        )
        rows.append(
            {
                "id": new_ulid(),
                "story_id": story_id,
                "seq": seq,
                "operation_id": run.operation_id,
                "category": EventCategory.CONTROL.value,
                "principal": run.principal,
                "type": MARKER,
                "payload": payload.model_dump(mode="json"),
                "provenance": Provenance.SYSTEM.value,
                "authority": Authority.AUTHORITATIVE.value,
            }
        )
    await conn.execute(insert(events), rows)


# -- the catalog scope ---------------------------------------------------------------------------


@dataclass
class _CatalogRun:
    subject: _Subject
    redactor: Redactor
    write: bool
    principal: str
    operation_id: str | None
    shared_media: str
    checksums: list[str] = field(default_factory=list)
    media_stories: list[ReferencingStory] = field(default_factory=list)
    deleted: set[tuple[str, str]] = field(default_factory=set)
    """The records the subject's own deletion removes (a dry run must not count them again)."""


def _names_subject(record: Record, subject: _Subject, redactor: Redactor) -> bool:
    key = (subject.kind, subject.id)
    if key in {(k, i) for k, i, _ in record.subjects} or key in set(record.defines):
        return True
    return any(redactor.hits(value) for value in record.fields.values() if value)


async def _catalog_records(
    conn: AsyncConnection, subject: _Subject, redactor: Redactor
) -> dict[str, set[str]]:
    """The catalog records naming the subject: the index's, and every catalog record whose
    declared subjects or texts name it now (#123: a record written before the subject existed
    holds its name without a row)."""
    found = await _referenced_records(conn, subject, ReferenceScope.CATALOG)
    for record in await records_of(conn, None):
        if _names_subject(record, subject, redactor):
            found.setdefault(record.table, set()).add(record.record_id)
    return found


async def _catalog_pass(conn: AsyncConnection, run: _CatalogRun) -> _Tally:
    subject, redactor, write = run.subject, run.redactor, run.write
    tally = _Tally()
    refs = await _catalog_records(conn, subject, redactor)
    if write:
        await _guc(conn)
    if subject.kind is SubjectKind.CATALOG_CHARACTER and await conn.scalar(
        select(character_catalog.c.id).where(character_catalog.c.id == subject.id)
    ):
        await _delete_catalog_character(conn, run, tally)
    if subject.kind is SubjectKind.PERSONA and await conn.scalar(
        select(personas.c.id).where(personas.c.id == subject.id)
    ):
        from epistrel.core.catalog import personas as persona_store

        tally.record(personas.name)
        if write:
            await persona_store.delete_in(
                conn, subject.id, principal=run.principal, operation_id=run.operation_id
            )
    await _redact_character_documents(conn, refs, run, tally)
    await _redact_scenarios(conn, refs, run, tally)
    await _redact_json_rows(
        conn,
        personas,
        personas.c.id,
        set(refs.get(personas.name, set())) - {subject.id},
        ("name", "gender", "description"),
        redactor,
        tally,
        write=write,
    )
    if write:
        for persona_id in set(refs.get(personas.name, set())) - {subject.id}:
            row = (
                await conn.execute(select(personas).where(personas.c.id == persona_id))
            ).one_or_none()
            if row is not None:
                await index_records(conn, [persona_record(row)])
    await _erase_media(conn, refs.get(media_assets.name, set()), run, tally)
    k = idempotency_keys.c
    indexed = refs.get(idempotency_keys.name, set())
    keys = [
        row
        for row in await conn.execute(
            select(k.scope, k.scope_id, k.key, k.result).where(
                k.scope != STORY_KEY_SCOPE, k.result.is_not(None)
            )
        )
        if idempotency_id(str(row.scope), str(row.scope_id), str(row.key)) in indexed
    ]
    await _purge_results(conn, keys, tally, write=write)
    rewritten = await _redact_events(
        conn,
        refs.get(events.name, set()),
        redactor,
        tally,
        removed=set(),
        write=write,
        now=clock.now(),
    )
    if write and rewritten:
        from epistrel.core.erasure.records import event_record

        await index_records(conn, [event_record(r) for r in rewritten])
    for table, key, columns in (
        (policies, policies.c.id, ("name", "message", "rule")),
        (config_changes, config_changes.c.change_id, ("old", "new")),
        (operations, operations.c.operation_id, ("turn_report",)),
    ):
        await _redact_json_rows(
            conn, table, key, refs.get(table.name, set()), columns, redactor, tally, write=write
        )
    for target in ERASURE_TARGETS:
        changed = await target.erase(conn, story_id=None, redactor=redactor, write=write)
        for _ in range(changed):
            tally.record(target.name)
    return tally


async def _delete_catalog_character(conn: AsyncConnection, run: _CatalogRun, tally: _Tally) -> None:
    """The catalog character goes through the catalog's own delete: its draft, every version
    (the immutability trigger lets the erasure through), its companion and its relations."""
    from epistrel.core.catalog.characters import SERVICE

    subject = run.subject
    versions = [
        int(v)
        for v in await conn.scalars(
            select(character_versions.c.version).where(
                character_versions.c.catalog_id == subject.id
            )
        )
    ]
    companions = [
        str(i)
        for i in await conn.scalars(
            select(scenario_catalog.c.id).where(scenario_catalog.c.companion_of == subject.id)
        )
    ]
    companion_versions = [
        (str(row.catalog_id), int(row.version))
        for row in await conn.execute(
            select(scenario_versions.c.catalog_id, scenario_versions.c.version).where(
                scenario_versions.c.catalog_id.in_(companions)
            )
        )
    ]
    tally.record(_catalog(character_catalog.name))
    run.deleted.add((character_catalog.name, subject.id))
    for version in versions:
        tally.record(_catalog(character_versions.name))
        run.deleted.add((character_versions.name, f"{subject.id}@{version}"))
    for companion in companions:
        tally.record(_catalog(scenario_catalog.name))
        run.deleted.add((scenario_catalog.name, companion))
    for companion, version in companion_versions:
        tally.record(_catalog(scenario_versions.name))
        run.deleted.add((scenario_versions.name, f"{companion}@{version}"))
    if run.write:
        await SERVICE.delete_in(
            conn, subject.id, principal=run.principal, operation_id=run.operation_id
        )


async def _redact_character_documents(
    conn: AsyncConnection, refs: Mapping[str, set[str]], run: _CatalogRun, tally: _Tally
) -> None:
    """Other catalog characters' drafts and versions that name the subject."""
    redactor, write = run.redactor, run.write
    for entry_id in sorted(refs.get(character_catalog.name, set())):
        if (character_catalog.name, entry_id) in run.deleted:
            continue
        row = (
            await conn.execute(
                select(character_catalog.c.draft).where(character_catalog.c.id == entry_id)
            )
        ).one_or_none()
        if row is None:
            continue  # the subject's own draft, gone with it
        draft, changed = redactor.document(row.draft)
        if not changed:
            continue
        tally.record(_catalog(character_catalog.name), changed)
        if write:
            await conn.execute(
                update(character_catalog)
                .where(character_catalog.c.id == entry_id)
                .values(
                    draft=draft,
                    draft_revision=character_catalog.c.draft_revision + 1,
                    updated_at=clock.now(),
                )
            )
            await index_records(conn, [character_draft_record(entry_id, draft)])
    for record_id in sorted(refs.get(character_versions.name, set())):
        if (character_versions.name, record_id) in run.deleted:
            continue
        entry_id, _, version = record_id.partition("@")
        where = and_(
            character_versions.c.catalog_id == entry_id,
            character_versions.c.version == int(version),
        )
        row = (await conn.execute(select(character_versions.c.content).where(where))).one_or_none()
        if row is None:
            continue
        content, changed = redactor.document(row.content)
        if not changed:
            continue
        tally.record(_catalog(character_versions.name), changed)
        if write:
            await conn.execute(update(character_versions).where(where).values(content=content))
            await index_records(conn, [character_version_record(entry_id, int(version), content)])


def _unfill(document: Any, subject: _Subject) -> tuple[Any, int]:
    """``document`` (a scenario) with every role filled with the subject left without its
    ``character``; how many roles changed."""
    if subject.kind is not SubjectKind.CATALOG_CHARACTER or not isinstance(document, Mapping):
        return document, 0
    cast = document.get("cast")
    roles = cast.get("roles") if isinstance(cast, Mapping) else None
    if not isinstance(roles, list):
        return document, 0
    changed = 0
    new_roles = []
    for role in roles:
        character = role.get("character") if isinstance(role, Mapping) else None
        if isinstance(character, Mapping) and character.get("catalog_id") == subject.id:
            role = {**role, "character": None}
            changed += 1
        new_roles.append(role)
    if not changed:
        return document, 0
    assert isinstance(cast, Mapping)  # noqa: S101 - read above
    return {**document, "cast": {**cast, "roles": new_roles}}, changed


async def _redact_scenarios(
    conn: AsyncConnection, refs: Mapping[str, set[str]], run: _CatalogRun, tally: _Tally
) -> None:
    """Scenario drafts and versions (their seeds, roles and reports) that name the subject."""
    from epistrel.core.catalog import scenarios

    redactor, subject, write = run.redactor, run.subject, run.write
    for entry_id in sorted(refs.get(scenario_catalog.name, set())):
        if (scenario_catalog.name, entry_id) in run.deleted:
            continue
        row = (
            await conn.execute(
                select(scenario_catalog.c.draft).where(scenario_catalog.c.id == entry_id)
            )
        ).one_or_none()
        if row is None:
            continue  # the subject's companion, gone with it
        draft, changed = redactor.document(row.draft)
        draft, unfilled = _unfill(draft, subject)
        if not changed and not unfilled:
            continue
        tally.record(_catalog(scenario_catalog.name), changed)
        if write:
            await conn.execute(
                update(scenario_catalog)
                .where(scenario_catalog.c.id == entry_id)
                .values(
                    draft=draft,
                    draft_revision=scenario_catalog.c.draft_revision + 1,
                    updated_at=clock.now(),
                )
            )
            await index_records(conn, [scenarios.draft_record(entry_id, draft)])
    for record_id in sorted(refs.get(scenario_versions.name, set())):
        if (scenario_versions.name, record_id) in run.deleted:
            continue
        entry_id, _, version = record_id.partition("@")
        where = and_(
            scenario_versions.c.catalog_id == entry_id,
            scenario_versions.c.version == int(version),
        )
        row = (
            await conn.execute(
                select(scenario_versions.c.content, scenario_versions.c.report).where(where)
            )
        ).one_or_none()
        if row is None:
            continue
        content, changed = redactor.document(row.content)
        content, unfilled = _unfill(content, subject)
        report, reported = redactor.document(row.report)
        if not changed and not unfilled and not reported:
            continue
        tally.record(_catalog(scenario_versions.name), changed + reported)
        if write:
            await conn.execute(
                update(scenario_versions)
                .where(where)
                .values(content=content, report=report, redacted_at=clock.now())
            )
            await index_records(
                conn, [scenarios.version_record(entry_id, int(version), content, report)]
            )


async def _media_stories(conn: AsyncConnection, media_id: str) -> list[ReferencingStory]:
    """The stories whose records reference a media asset (a character's images, a snapshot, the
    scenario snapshot), with the characters that hold it."""
    found: dict[str, set[str]] = {}
    needle = f'%"{media_id}"%'
    for row in await conn.execute(
        select(characters.c.story_id, characters.c.id).where(
            cast(characters.c.images, Text).like(needle)
        )
    ):
        found.setdefault(str(row.story_id), set()).add(str(row.id))
    for row in await conn.execute(
        select(character_snapshots.c.story_id, character_snapshots.c.character_id).where(
            cast(character_snapshots.c.content, Text).like(needle)
        )
    ):
        found.setdefault(str(row.story_id), set()).add(str(row.character_id))
    for story_id in await conn.scalars(
        select(stories.c.id).where(cast(stories.c.scenario_snapshot, Text).like(needle))
    ):
        found.setdefault(str(story_id), set())
    return [ReferencingStory(s, tuple(sorted(c))) for s, c in sorted(found.items())]


def _merged(*groups: Iterable[ReferencingStory]) -> list[ReferencingStory]:
    found: dict[str, set[str]] = {}
    for group in groups:
        for story in group:
            found.setdefault(story.story_id, set()).update(story.character_ids)
    return [ReferencingStory(s, tuple(sorted(c))) for s, c in sorted(found.items())]


async def _erase_media(
    conn: AsyncConnection, media_ids: Iterable[str], run: _CatalogRun, tally: _Tally
) -> None:
    """Media assets that depict or name the subject (see the module docstring)."""
    from epistrel.core.media import assets

    subject, redactor = run.subject, run.redactor
    for media_id in sorted(set(media_ids)):
        row = (
            await conn.execute(select(media_assets).where(media_assets.c.id == media_id))
        ).one_or_none()
        if row is None:
            continue
        depicted = [d for d in (row.depicted_subjects or []) if isinstance(d, Mapping)]
        others = [d for d in depicted if (d.get("kind"), d.get("id")) != (subject.kind, subject.id)]
        caption = redactor.text(row.caption) if row.caption else row.caption
        alt_text = redactor.text(row.alt_text) if row.alt_text else row.alt_text
        generation, prompts = redactor.document(row.generation_metadata)
        texts = int(caption != row.caption) + int(alt_text != row.alt_text) + prompts
        redacted = {
            "caption": caption,
            "alt_text": alt_text,
            "generation_metadata": generation,
            "depicted_subjects": others,
        }
        tally.media += 1
        if others:
            tally.record(media_assets.name, texts)
            if run.write:
                await conn.execute(
                    update(media_assets).where(media_assets.c.id == media_id).values(**redacted)
                )
                await _reindex_media(conn, media_id)
            continue
        referenced = bool(await assets.referenced(conn, [media_id]))
        if referenced:
            run.media_stories.extend(await _media_stories(conn, media_id))
        if referenced and run.shared_media == "retire":
            tally.record(media_assets.name, texts)
            if run.write:
                await conn.execute(
                    update(media_assets)
                    .where(media_assets.c.id == media_id)
                    .values(state=MediaState.RETIRED.value, **redacted)
                )
                await _reindex_media(conn, media_id)
            continue
        tally.record(media_assets.name)
        if run.write:
            await ownership.drop_entity(conn, EntityType.MEDIA, media_id, principal=run.principal)
            await index.drop_references(conn, assets.RECORD_TABLE, media_id)
            await conn.execute(delete(media_assets).where(media_assets.c.id == media_id))
            run.checksums.append(str(row.checksum))


async def _reindex_media(conn: AsyncConnection, media_id: str) -> None:
    row = (await conn.execute(select(media_assets).where(media_assets.c.id == media_id))).one()
    await index_records(conn, [media_record(row)])


async def _referencing_stories(conn: AsyncConnection, subject: _Subject) -> list[ReferencingStory]:
    """The stories that reference a catalog subject: those with characters made from it, and
    those whose index names it."""
    found: dict[str, set[str]] = {}
    linked = (
        characters.c.source_catalog_id
        if subject.kind is SubjectKind.CATALOG_CHARACTER
        else characters.c.persona_id
    )
    for row in await conn.execute(
        select(characters.c.story_id, characters.c.id).where(linked == subject.id)
    ):
        found.setdefault(str(row.story_id), set()).add(str(row.id))
    sr = subject_references.c
    for story_id in await conn.scalars(
        select(sr.story_id)
        .where(
            sr.scope == ReferenceScope.STORY.value,
            sr.subject_kind == subject.kind.value,
            sr.subject_id == subject.id,
        )
        .distinct()
    ):
        found.setdefault(str(story_id), set())
    return [ReferencingStory(s, tuple(sorted(c))) for s, c in sorted(found.items())]


# -- running it ----------------------------------------------------------------------------------


def _degradation(compressor: SummaryCompressor, windows: int) -> tuple[str, ...]:
    if windows and not compressor.available:
        return (DEGRADED_REGENERATION,)
    return ()


async def _windows(conn: AsyncConnection, story_id: str) -> int:
    from epistrel.core.erasure.redact import redacted_windows

    return len(await redacted_windows(conn, story_id))


async def dry_run(
    db: AsyncEngine,
    request: ErasureRequest,
    *,
    compressor: SummaryCompressor,
    principal: str,
) -> ErasureResult:
    """What ``erase`` would do, written nowhere (see the module docstring). A story's dry run
    reads in one read-only snapshot. A catalog dry run does the catalog pass and rolls it back:
    what the subject's own deletion removes decides whether a medium is still referenced, so the
    counts are the real run's by construction; nothing it did is ever committed (no operation,
    no key, no ledger row, no bytes deleted)."""
    async with db.connect() as conn, conn.begin() as tx:
        if request.story_id is not None:
            await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        subject = await _subject(conn, request)
        redactor = Redactor(subject.terms)
        revision = await config_revision(conn)
        timeline_revision: int | None = None
        windows = 0
        referencing: list[ReferencingStory] = []
        if not subject.known:
            tally = _Tally()
        elif subject.story_id is not None:
            run = _StoryRun(subject, redactor, False, "", None, principal, clock.now())
            tally = await _story_pass(conn, run)
            windows = await _summary_events(conn, run) + await _windows(conn, subject.story_id)
        else:
            settings = await _settings(conn)
            referencing = await _referencing_stories(conn, subject)
            catalog_run = _CatalogRun(
                subject, redactor, True, principal, None, settings.erasure_shared_media
            )
            tally = await _catalog_pass(conn, catalog_run)
            referencing = _merged(referencing, catalog_run.media_stories)
        if subject.story_id is not None:
            timeline_revision = await conn.scalar(
                select(stories.c.timeline_revision).where(stories.c.id == subject.story_id)
            )
        await tx.rollback()
    result = ErasureResult(
        dry_run=True,
        subject_kind=request.subject.kind.value,
        subject_id=request.subject.id,
        scope=request.scope_name,
        story_id=request.story_id,
        **_counts(tally),
        regeneration=REGENERATION_MODEL if compressor.available else REGENERATION_REDACTION,
        degradation=_degradation(compressor, windows),
        referencing_stories=tuple(referencing),
        config_revision=revision,
        timeline_revision=None if timeline_revision is None else int(timeline_revision),
    )
    _log("erasure dry run", result)
    return result


async def _summary_events(conn: AsyncConnection, run: _StoryRun) -> int:
    """For a dry run: the level-0 summary events step 1 would redact (each a window step 2
    regenerates)."""
    refs = await _referenced_records(conn, run.subject, ReferenceScope.STORY)
    ids = sorted(refs.get(events.name, set()))
    if not ids:
        return 0
    found = await conn.execute(
        select(events.c.payload).where(events.c.id.in_(ids), events.c.type == SUMMARY_EVENT)
    )
    return sum(
        1
        for row in found
        if int((row.payload or {}).get("level", 0)) == 0 and run.redactor.count(row.payload) > 0
    )


def _counts(tally: _Tally) -> dict[str, Any]:
    return {
        "records": dict(sorted(tally.records.items())),
        "prose_segments": tally.prose,
        "media": tally.media,
        "idempotency_results": tally.results,
    }


def _log(event: str, result: ErasureResult) -> None:
    log.info(
        event,
        erasure_id=result.erasure_id,
        subject_kind=result.subject_kind,
        subject_id=result.subject_id,
        scope=result.scope,
        story_id=result.story_id,
        operation_id=result.operation_id,
        records=sum(result.records.values()),
        prose_segments=result.prose_segments,
        media=result.media,
        idempotency_results=result.idempotency_results,
        residue=result.residue,
        degradation=",".join(result.degradation) or None,
    )


async def _write_ledger(
    conn: AsyncConnection,
    *,
    erasure_id: str,
    subject: _Subject,
    scope: str,
    principal: str,
    operation_id: str | None,
    counts: dict[str, Any],
    hashes: list[str],
    ledger: LedgerFile | None,
) -> datetime:
    """The ledger's line in its file (``ledger``; #125) and its row, then the audit row."""
    at = clock.now()
    await ledger_store.write(
        conn,
        ledger,
        ledger_store.Entry(
            erasure_id=erasure_id,
            at=at,
            subject_kind=subject.kind.value,
            subject_id=subject.id,
            scope=scope,
            story_id=subject.story_id,
            principal=principal,
            operation_id=operation_id,
            counts=counts,
            scan_term_hashes=tuple(hashes),
        ),
        applied_at=at,
    )
    await audit.record(
        conn,
        AuditKind.ERASURE,
        principal=principal,
        story_id=subject.story_id,
        details={
            "erasure_id": erasure_id,
            "subject_kind": subject.kind.value,
            "subject_id": subject.id,
            "scope": scope,
            "operation_id": operation_id,
            "counts": counts,
        },
    )
    return at


async def append_stage(
    db: AsyncEngine | AsyncConnection, operation_id: str, stage: str, status: str
) -> None:
    """Add one stage to a committed operation's ``stage_log`` (``Operation.stage``'s shape)."""
    statement = (
        update(operations)
        .where(operations.c.operation_id == operation_id)
        .values(
            stage_log=func.coalesce(operations.c.stage_log, text("'[]'::jsonb"))
            + func.jsonb_build_array(func.jsonb_build_object("stage", stage, "status", status))
        )
    )
    if isinstance(db, AsyncConnection):
        await db.execute(statement)
        return
    async with db.begin() as conn:
        await conn.execute(statement)


Collect = Callable[[str], Awaitable[object]]
"""``collect(checksum)``: delete a purged asset's bytes once no row has them (after commit)."""


async def erase(
    db: AsyncEngine,
    request: ErasureRequest,
    *,
    principal: str,
    config_key: str,
    compressor: SummaryCompressor,
    collect: Collect | None = None,
    erasure_id: str | None = None,
    write_ledger: bool = True,
    ledger: LedgerFile | None = None,
) -> ErasureResult:
    """Erase ``request.subject`` in its scope (see the module docstring). ``erasure_id`` and
    ``write_ledger`` let #125's reapply run an erasure the ledger already records; ``ledger``
    is the ledger file the entry is appended to first (the Engine's; ``None`` writes the table
    only)."""
    async with db.connect() as conn:
        subject = await _subject(conn, request)
    if not subject.known:
        raise SubjectNotFound(
            f"no {request.subject.kind.value} {request.subject.id} to erase in this scope",
            {
                "subject_kind": request.subject.kind.value,
                "subject_id": request.subject.id,
                **({"story_id": request.story_id} if request.story_id else {}),
            },
        )
    erasure_id = erasure_id or new_ulid()
    hashes = term_hashes(subject.terms, config_key)
    redactor = Redactor(subject.terms)
    if subject.story_id is not None:
        result = await _erase_story(
            db,
            request,
            subject,
            redactor,
            erasure_id,
            principal,
            compressor,
            hashes,
            write_ledger=write_ledger,
            ledger=ledger,
        )
    else:
        result = await _erase_catalog(
            db,
            request,
            subject,
            redactor,
            erasure_id,
            principal,
            hashes,
            collect,
            write_ledger=write_ledger,
            ledger=ledger,
        )
    _log("erasure applied", result)
    return result


async def _erase_story(
    db: AsyncEngine,
    request: ErasureRequest,
    subject: _Subject,
    redactor: Redactor,
    erasure_id: str,
    principal: str,
    compressor: SummaryCompressor,
    hashes: list[str],
    *,
    write_ledger: bool,
    ledger: LedgerFile | None,
) -> ErasureResult:
    from epistrel.core.events.operation import Operation

    story_id = subject.story_id
    assert story_id is not None  # noqa: S101 - the story scope
    found: dict[str, Any] = {}
    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        bumps_revision=True,
        control_action=True,
        erasure=True,
    ) as op:
        run = _StoryRun(
            subject, redactor, True, erasure_id, op.operation_id, principal, clock.now()
        )

        async def step(conn: AsyncConnection) -> None:
            tally = await _story_pass(conn, run)
            await _insert_markers(conn, run)
            found["failed_queue"] = await _stop_in_flight(conn, story_id, run)
            await projections.replace_with_fold(conn, story_id)
            await reindex_story(conn, story_id)
            residue = await verify(conn, story_id, redactor)
            counts = {**tally.counts(), "residue": residue.count}
            found["tally"], found["counts"] = tally, counts
            if write_ledger:
                found["at"] = await _write_ledger(
                    conn,
                    erasure_id=erasure_id,
                    subject=subject,
                    scope=STORY,
                    principal=principal,
                    operation_id=op.operation_id,
                    counts=counts,
                    hashes=hashes,
                    ledger=ledger,
                )

        op.stage("redacted")
        if write_ledger:
            op.stage("ledger_written")
        op.add_commit_step(step)
        await op.commit()
    timeline_revision = op.timeline_revision
    if found.get("failed_queue"):
        turn_gate.wakeups.wake(story_id)
    degradation: list[str] = []
    try:
        regenerated = await regenerate_summaries(
            db, story_id, redactor, compressor, principal=principal
        )
        degradation += regenerated.degradation
        await append_stage(db, op.operation_id, "regenerated", regenerated.status)
    except Exception as exc:  # the redaction stands; a later erasure tries the windows again
        log.warning(
            "erasure summary regeneration failed", story_id=story_id, exc_type=type(exc).__name__
        )
        degradation.append(DEGRADED_REGENERATION)
        await append_stage(db, op.operation_id, "regenerated", "failed")
    try:
        await projections.rebuild(db, story_id, principal=principal)
        await append_stage(db, op.operation_id, "rebuilt", "done")
    except Exception as exc:  # the erasure itself is complete; the rebuild can be re-run
        log.warning("erasure rebuild failed", story_id=story_id, exc_type=type(exc).__name__)
        degradation.append(DEGRADED_REBUILD)
        await append_stage(db, op.operation_id, "rebuilt", "failed")
    async with db.connect() as conn:
        residue: Residue = await verify(conn, story_id, redactor)
        revision = await config_revision(conn)
    tally: _Tally = found["tally"]
    return ErasureResult(
        dry_run=False,
        subject_kind=subject.kind.value,
        subject_id=subject.id,
        scope=STORY,
        story_id=story_id,
        **_counts(tally),
        regeneration=REGENERATION_MODEL if compressor.available else REGENERATION_REDACTION,
        degradation=tuple(dict.fromkeys(degradation)),
        referencing_stories=(),
        config_revision=revision,
        timeline_revision=timeline_revision,
        erasure_id=erasure_id,
        operation_id=op.operation_id,
        ledger_entry=LedgerEntry(erasure_id, found["at"], STORY, story_id, found["counts"])
        if write_ledger
        else None,
        residue=residue.count,
    )


async def _stop_in_flight(conn: AsyncConnection, story_id: str, run: _StoryRun) -> list[str]:
    """Nothing in flight writes the subject back (FR-ADMIN-9; #127). The erasure is a control
    action, so a turn generating meanwhile abandons at its commit (#47); here, in the erasure's
    own transaction: the turns waiting in the queue fail with ``timeline_changed``; the turn
    slot's lease is cleared (the turn holding it abandons on its own); and the operations whose
    events were redacted have their ``membership_tx`` moved, so a background job that read them
    (#72's summary job declares its source turns, #70) drops its output at commit and is
    requeued. Returns the failed queued turns."""
    redacted = select(events.c.operation_id).where(
        events.c.story_id == story_id, events.c.redacted_at == run.now
    )
    await conn.execute(
        update(operations)
        .where(
            operations.c.operation_id.in_(redacted),
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        .values(membership_tx=func.transaction_timestamp())
    )
    revision = await conn.scalar(
        select(stories.c.timeline_revision).where(stories.c.id == story_id)
    )
    failed = await turn_gate.fail_queued(
        conn,
        story_id,
        TimelineChanged(
            "an erasure changed the timeline before the queued turn started",
            {
                "current_revision": int(revision or 0) + 1,
                "control_operation_id": run.operation_id,
            },
        ),
    )
    await conn.execute(
        update(stories)
        .where(stories.c.id == story_id)
        .values(in_flight_turn=None, in_flight_lease_until=None)
    )
    return failed


async def _erase_catalog(
    db: AsyncEngine,
    request: ErasureRequest,
    subject: _Subject,
    redactor: Redactor,
    erasure_id: str,
    principal: str,
    hashes: list[str],
    collect: Collect | None,
    *,
    write_ledger: bool,
    ledger: LedgerFile | None,
) -> ErasureResult:
    async with db.begin() as conn:
        settings = await _settings(conn)
        referencing = await _referencing_stories(conn, subject)
        operation_id = await control.record_control_operation(conn, actor=principal)
        run = _CatalogRun(
            subject, redactor, True, principal, operation_id, settings.erasure_shared_media
        )
        tally = await _catalog_pass(conn, run)
        referencing = _merged(referencing, run.media_stories)
        residue = await verify(conn, None, redactor)
        counts = {**tally.counts(), "residue": residue.count}
        at = clock.now()
        if write_ledger:
            at = await _write_ledger(
                conn,
                erasure_id=erasure_id,
                subject=subject,
                scope=CATALOG,
                principal=principal,
                operation_id=operation_id,
                counts=counts,
                hashes=hashes,
                ledger=ledger,
            )
        await append_stage(conn, operation_id, "redacted", "done")
        if write_ledger:
            await append_stage(conn, operation_id, "ledger_written", "done")
        revision = await config_revision(conn)
    for checksum in dict.fromkeys(run.checksums):
        if collect is None:
            continue
        try:
            await collect(checksum)
        except Exception as exc:  # the media sweep deletes what is left
            log.warning("erasure blob delete failed", exc_type=type(exc).__name__)
    return ErasureResult(
        dry_run=False,
        subject_kind=subject.kind.value,
        subject_id=subject.id,
        scope=CATALOG,
        story_id=None,
        **_counts(tally),
        regeneration=REGENERATION_REDACTION,
        degradation=(),
        referencing_stories=tuple(referencing),
        config_revision=revision,
        erasure_id=erasure_id,
        operation_id=operation_id,
        ledger_entry=LedgerEntry(erasure_id, at, CATALOG, None, counts) if write_ledger else None,
        residue=residue.count,
    )


__all__ = [
    "ERASURE_TARGETS",
    "CatalogScope",
    "ErasureRequest",
    "ErasureResult",
    "ErasureTarget",
    "LedgerEntry",
    "ReferencingStory",
    "StoryScope",
    "SubjectRef",
    "append_stage",
    "check_request",
    "dry_run",
    "erase",
    "register_target",
    "require_key",
    "term_hashes",
]
