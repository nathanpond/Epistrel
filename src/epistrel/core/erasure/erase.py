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
name; ``index.catalog_subjects``), with the words of a multi-word name that no other subject of
the scope uses (``index.with_name_parts``, #364), read before anything is deleted. The erasure
matcher (``core.erasure.matcher``, ADR-154) decides which occurrences are the subject: certain
mentions are redacted; a **candidate** (a common word of the name capitalised at the start of a
sentence) only when ``confirm_candidates`` names its id. An id the run does not meet is 422
``unknown_erasure_candidate`` (nothing written; a reapply skips it instead).

**A dry run** computes exactly what the real run would touch and writes nothing (no operation,
no idempotency key): ``{records: {table: n}, prose_segments, media, idempotency_results}`` with
``regeneration`` and the expected ``degradation``, and ``candidates`` (each with its id,
location, rule and a snippet, returned only and never stored) and ``candidates_remaining``.
An unknown subject's dry run answers zeros.
``records`` counts the structured rows deleted or rewritten, ``prose_segments`` the text fields
redacted (records' fields, not spans), ``media`` the assets (story scope: the images the
subject's characters show, which stay; catalog scope: those purged, retired or rewritten), and
``idempotency_results`` the stored results purged. Marker and index rows are not counted.
Catalog table names are schema-qualified (``catalog.character_versions``).

**A story-scope erasure** runs as three operations, so a failure after the first leaves a
complete erasure:

1. one ``control`` operation (it bumps the story's revision, so a turn in flight abandons, and
   runs on archived and paused stories), in one transaction under the story lock and
   ``epistrel.erasure = on``: the story is re-indexed first, so a stale index cannot hide a
   mention (#386); the subject's ``characters`` rows, their ``character_snapshots``
   and the ``genesis_deferred`` items that belong only to them are deleted, each leaving one
   position-less ``erasure_marker`` event; every string of every record that names the subject
   is redacted in place (``Redactor``: each matched span becomes ``[erased]``), and a beat the
   subject authored (its ``character_id`` is theirs, or it is a reply whose
   ``render_character_ids`` are theirs alone) becomes ``[erased]`` whole, keeping its type,
   position and operation, with ``redacted_at``; their ids leave ``render_character_ids`` and
   the events' ``character_id``; a memory the subject holds or voiced is deleted with a marker,
   and every other memory naming them is redacted with its vector nulled (#135);
   story images (#261, #266) that declare the subject or certainly name it in their prompt,
   caption, alt text or generation prompts are deleted on every membership, each leaving a
   marker, and their assets go with their bytes (after the commit) unless another record still
   references them, a fork's copy for one; the story's image jobs that depict or name the
   subject have ``prompt`` and ``negative_prompt`` nulled and are cancelled if unfinished (their
   other texts, like every job's, are redacted span by span);
   the story's default viewpoint is cleared when it names them; stored idempotency results that
   name them are purged (``result`` null: a replay answers the reference); the projections are
   refolded and the story's index rebuilt (the subject's rows go last); nothing in flight can
   write them back (#127, ``_stop_in_flight``: queued turns fail ``timeline_changed``, the turn
   slot's lease is cleared, and the redacted events' operations have their ``membership_tx``
   moved so a background job that read them drops its output); the residue is scanned;
   the ``erasure_ledger`` row and a deployment ``audit_log`` row are written; when a policy
   of the story was rewritten or deleted, the story's ``policy_erasure_revision`` moves, so no
   process's effective-policy cache serves its old text (#469). Stages
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
deployment-scoped stored results naming the subject are purged; image candidates (#260, #266)
that declare or name the subject, the subject's own included, are deleted with their assets,
their bytes collected after the commit, while a story image's asset is never touched (it is
indexed in its story). The transaction takes the
deployment row first (as configuration writes do) and, when a policy was rewritten, moves the
deployment's ``policy_erasure_revision`` (#469: the effective-policy cache's key; the
``config_revision`` stays, as for any catalog write). Stories are otherwise never
touched (FR-STORE-14): ``referencing_stories`` lists those that reference the subject (with
their characters), for the operator to erase separately. The one exception is the public card
a catalog character's genesis creation report copied, which is public catalog data until this
erasure: it is emptied in every story's report that casts her (#382, docs/04 C-15). Blob
bytes are deleted after the commit; a failed delete is left to the media sweep.

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
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints
from sqlalchemy import Text, and_, cast, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import array
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
    ThunkState,
)
from epistrel.core.erasure import index
from epistrel.core.erasure import ledger as ledger_store
from epistrel.core.erasure.index import (
    ALIASES_PATH,
    NAME_PATH,
    Record,
    catalog_subjects,
    index_records,
    records_of,
    reindex_story,
    story_subjects,
)
from epistrel.core.erasure.ledger import LedgerFile
from epistrel.core.erasure.matcher import matcher
from epistrel.core.erasure.records import (
    character_draft_record,
    character_version_record,
    idempotency_id,
    media_record,
    persona_record,
    predicate_words,
    upload_session_record,
)
from epistrel.core.erasure.redact import ERASED, Candidate, Redactor, regenerate_summaries
from epistrel.core.erasure.verify import Residue, verify
from epistrel.core.errors import (
    ErasureKeyMissing,
    InvalidErasureScope,
    SubjectNotFound,
    TimelineChanged,
    UnknownErasureCandidate,
)
from epistrel.core.events.registry import validate
from epistrel.core.events.types import REQUESTED_EFFECT
from epistrel.core.ids import Ulid, new_ulid
from epistrel.core.memory import retirement
from epistrel.core.ports import SummaryCompressor
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, ErasureSettings
from epistrel.core.tables import (
    amendment_parts,
    amendments,
    character_catalog,
    character_snapshots,
    character_versions,
    characters,
    config_changes,
    dependency_edges,
    erasure_ledger,
    events,
    genesis_deferred,
    idempotency_keys,
    jobs,
    media_assets,
    operations,
    personas,
    policies,
    rewrite_questions,
    rewrite_sessions,
    scenario_catalog,
    scenario_versions,
    stories,
    turn_queue,
)
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.beliefs import belief_transitions, beliefs
from epistrel.core.tables.catalog import CATALOG_SCHEMA, ERASURE_GUC
from epistrel.core.tables.commitments import thunks
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.divergences import divergences
from epistrel.core.tables.exposures import exposures
from epistrel.core.tables.facts import facts
from epistrel.core.tables.media import (
    MediaState,
    ReferenceScope,
    SubjectKind,
    UploadState,
    media_upload_sessions,
    subject_references,
)
from epistrel.core.tables.memories import memories
from epistrel.core.tables.outbox import (
    CANCELLED,
    CLAIMED,
    INTENDED,
    UNKNOWN,
    external_effect_outbox,
    outbox_outcomes,
)
from epistrel.core.tables.ownership import EntityType
from epistrel.core.tables.propositions import (
    closed_sets,
    entities,
    entity_aliases,
    predicate_proposals,
    predicates,
    propositions,
)
from epistrel.core.tables.scheduled_events import scheduled_events
from epistrel.core.tables.states import (
    STORY_SCOPE_KEY,
    state_changes,
    state_definitions,
    state_proposals,
    state_values,
)
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

STORY = "story"
CATALOG = "catalog"
MARKER = "erasure_marker"
BEATS = frozenset(
    {
        "player_input",
        "message_rendered",
        "think",
        "latent",
        # A structured turn's typed beats (#150).
        "beat_say",
        "beat_think",
        "beat_do",
        "beat_latent",
        "beat_narration",
    }
)
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


MAX_CONFIRMED = 1000
CandidateId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
"""A candidate's id (``redact.candidate_id``)."""


class SubjectRef(BaseModel):
    """Who is erased: a story ``character``, a ``catalog_character`` or a ``persona``, by id;
    for a story ``character`` erased from the catalog (#504, ADR-176), the ``story`` whose
    subject index gives its names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SubjectKind
    id: str = Field(min_length=1, max_length=64)
    story: Ulid | None = Field(
        default=None,
        description="With `kind: character` and `scope: {catalog: true}` only: the story the "
        "character belongs to, whose names for it are redacted from the catalog's records "
        "(#504). Anywhere else it is 422 `invalid_erasure_scope`.",
    )


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
    confirm_candidates: list[CandidateId] = Field(
        default_factory=list,
        max_length=MAX_CONFIRMED,
        description=(
            "Candidate mentions to erase too, by the `candidate_id` a dry run listed (ADR-154): "
            "a common word of the subject's name capitalised at the start of a sentence is "
            "erased only when confirmed here. An id the erasure does not meet is 422 "
            "`unknown_erasure_candidate`."
        ),
    )

    @property
    def story_id(self) -> str | None:
        return self.scope.story if isinstance(self.scope, StoryScope) else None

    @property
    def scope_name(self) -> str:
        return STORY if isinstance(self.scope, StoryScope) else CATALOG


def check_request(request: ErasureRequest) -> None:
    """422 ``invalid_erasure_scope`` for ``character`` + ``catalog`` without the subject's
    ``story`` (a story character's names live in its story; #504, ADR-176), and for a
    ``story`` on any other pairing."""
    subject = request.subject
    if subject.story is not None and (
        subject.kind is not SubjectKind.CHARACTER or request.story_id is not None
    ):
        raise InvalidErasureScope(
            "`subject.story` names a story character's story for a catalog-scope erasure only",
            {
                "field": "body.subject.story",
                "subject_kind": subject.kind.value,
                "scope": request.scope_name,
            },
        )
    if subject.kind is SubjectKind.CHARACTER and request.story_id is None and subject.story is None:
        raise InvalidErasureScope(
            "a story character is erased in its story (`scope: {story: <id>}`), or from the "
            "catalog with its story named (`subject.story`)",
            {"field": "body.subject.story", "subject_kind": subject.kind.value, "scope": CATALOG},
        )


def story_character(request: ErasureRequest) -> bool:
    """Whether ``request`` erases a story character from the catalog (#504)."""
    return request.subject.kind is SubjectKind.CHARACTER and request.story_id is None


def check_confirmed(redactor: Redactor) -> None:
    """422 ``unknown_erasure_candidate`` when a confirmed id names no candidate the erasure
    met (nothing is written: it runs inside the erasure's transaction)."""
    unknown = sorted(redactor.unknown)
    if unknown:
        raise UnknownErasureCandidate(
            f"{len(unknown)} confirmed candidate(s) are not among this erasure's candidates; "
            "run a dry run for the current ids",
            {"field": "body.confirm_candidates", "candidate_ids": unknown},
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


def term_hashes(terms: Iterable[str], key: str, parts: Iterable[str] = ()) -> list[str]:
    """HMAC-SHA256 under ``key`` of each certain term the matcher guards (``Matcher.guard_terms``:
    each normalised, a common word in its capitalised mid-sentence form only; ADR-154),
    sorted."""
    secret = key.encode()
    return sorted(
        {
            hmac.new(secret, term.encode(), hashlib.sha256).hexdigest()
            for term in matcher(terms, parts).guard_terms()
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
class PromotedDraft:
    """A catalog character promoted from one of the story's characters (FR-CAT-8:
    ``characters.promoted_to``), with its companion scenarios: catalog records that may hold
    the story's text, which a story-scope erasure never touches (#504). Erase the subject from
    them with ``follow_up`` (a catalog-scope erasure of the story character), before the story
    erasure removes the names it reads."""

    catalog_id: str
    character_id: str
    companion_ids: tuple[str, ...] = ()


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
    candidates_remaining: int = 0
    """The candidate mentions met and not confirmed (ADR-154): left in place."""
    candidates: tuple[Candidate, ...] = ()
    """A dry run's candidates, each with its id, location, rule and snippet (never stored)."""
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""
    promoted_drafts: tuple[PromotedDraft, ...] = ()
    """Story scope (#504): the catalog drafts promoted from the story's characters, untouched."""
    subject_story: str | None = None
    """A story character erased from the catalog (#504): its story."""


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


def _touched_policies(tally: _Tally) -> bool:
    return bool(tally.records.get(policies.name))


async def _lock_deployment_row(conn: AsyncConnection) -> None:
    """Take the deployment row ``FOR UPDATE`` first, as every configuration write does, so a
    catalog erasure's ``policy_erasure_revision`` bump cannot deadlock with a policy write."""
    from epistrel.core.tables import deployment_state

    await conn.execute(
        select(deployment_state.c.id)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .with_for_update()
    )


async def _bump_policy_erasure_revision(conn: AsyncConnection, story_id: str | None) -> None:
    """Move the ``policy_erasure_revision`` of the story (story scope) or the deployment
    (catalog scope) by one: the effective-policy cache keys on it, so no process serves a
    policy's text from before the erasure once it commits (#469). The configuration revisions
    stay where they are (an erasure records no configuration change)."""
    from epistrel.core.tables import deployment_state

    if story_id is None:
        statement = update(deployment_state).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        column = deployment_state.c.policy_erasure_revision
    else:
        statement = update(stories).where(stories.c.id == story_id)
        column = stories.c.policy_erasure_revision
    await conn.execute(statement.values({column: column + 1}))


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
    parts: tuple[str, ...] = ()
    """The parts of its names the matcher also finds (``index.with_name_parts``)."""
    source_story: str | None = None
    """A story character erased from the catalog (#504): its story."""

    def redactor(self, confirmed: Iterable[str] = ()) -> Redactor:
        return Redactor(self.terms, self.parts, confirmed=confirmed)

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


async def _story_character(conn: AsyncConnection, request: ErasureRequest) -> _Subject:
    """A story character erased from the catalog (#504, ADR-176): its names and name parts from
    its story's subject index, read before anything changes. 422 ``unknown_erasure_candidate``
    for an unknown story, or a character it does not hold (one already erased there has no
    names left to find)."""
    subject = request.subject
    story_id = subject.story
    found = (
        await conn.scalar(
            select(characters.c.id).where(
                characters.c.id == subject.id, characters.c.story_id == story_id
            )
        )
        if story_id is not None
        else None
    )
    if found is None:
        raise UnknownErasureCandidate(
            f"story {story_id} holds no character {subject.id} whose names could be erased "
            "from the catalog (erase it from the catalog before erasing it in its story)",
            {"field": "body.subject", "subject_id": subject.id, "story_id": story_id},
        )
    terms: list[str] = []
    parts: list[str] = []
    for candidate in await story_subjects(conn, str(story_id)):
        if candidate.id == subject.id:
            terms += candidate.terms
            parts += candidate.parts
    return _Subject(
        subject.kind,
        subject.id,
        None,
        (),
        tuple(dict.fromkeys(terms)),
        True,
        tuple(dict.fromkeys(parts)),
        source_story=str(story_id),
    )


async def _subject(conn: AsyncConnection, request: ErasureRequest) -> _Subject:
    """Who the request erases, in its scope, with its terms (read before anything changes)."""
    subject, story_id = request.subject, request.story_id
    if story_character(request):
        return await _story_character(conn, request)
    catalog_terms, in_catalog = await _catalog_terms(conn, subject)
    if story_id is None:
        known = in_catalog or await _erased_before(conn, subject, None)
        # The parts of its names the catalog's other subjects do not use (#364).
        parts: list[str] = []
        for candidate in await catalog_subjects(conn):
            if (candidate.kind, candidate.id) == (subject.kind, subject.id):
                catalog_terms += candidate.terms
                parts += candidate.parts
        return _Subject(
            subject.kind,
            subject.id,
            None,
            (),
            tuple(dict.fromkeys(catalog_terms)),
            known,
            tuple(dict.fromkeys(parts)),
        )
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
    story_parts: list[str] = []
    for candidate in await story_subjects(conn, story_id):
        if candidate.id in found:
            terms += candidate.terms
            story_parts += candidate.parts
    if subject.kind is not SubjectKind.CHARACTER:
        terms += catalog_terms
    known = bool(found) or in_catalog or await _erased_before(conn, subject, story_id)
    if subject.kind is SubjectKind.CHARACTER:
        known = bool(found) or await _erased_before(conn, subject, story_id)
    return _Subject(
        subject.kind,
        subject.id,
        story_id,
        found,
        tuple(dict.fromkeys(terms)),
        known,
        tuple(dict.fromkeys(story_parts)),
    )


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
            new, changed = redactor.document(payload, (events.name, str(row.id)), "payload")
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
    checksums: list[str] = field(default_factory=list)
    """The purged story images' assets' checksums (#266): bytes deleted after the commit."""


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
    await _redact_operations(
        conn,
        refs.get(operations.name, set()),
        redactor,
        tally,
        reports=operations.c.story_id == story_id,
        edit=lambda report: story_erased_report(report, removed),
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
    # #221: an amendment's payload and its parts' patches and time expressions, in place
    # (#222's as-recorded view reverses a redacted patch).
    await _redact_json_rows(
        conn,
        amendments,
        amendments.c.id,
        refs.get(amendments.name, ()),
        ("payload",),
        redactor,
        tally,
        write=write,
    )
    await _redact_json_rows(
        conn,
        amendment_parts,
        amendment_parts.c.id,
        refs.get(amendment_parts.name, ()),
        ("patch_before", "patch_after", "valid_spec"),
        redactor,
        tally,
        write=write,
    )
    # #223: a rewrite session's instruction, plan, held render and error, and its questions'
    # prompts, options, recommendations and answers, in place.
    from epistrel.core.erasure.records import (
        DIRECTIVE_TEXTS,
        REWRITE_QUESTION_TEXTS,
        REWRITE_SESSION_TEXTS,
    )
    from epistrel.core.tables import directives

    await _redact_json_rows(
        conn,
        directives,
        directives.c.id,
        refs.get(directives.name, ()),
        DIRECTIVE_TEXTS,
        redactor,
        tally,
        write=write,
    )

    await _redact_json_rows(
        conn,
        rewrite_sessions,
        rewrite_sessions.c.id,
        refs.get(rewrite_sessions.name, ()),
        REWRITE_SESSION_TEXTS,
        redactor,
        tally,
        write=write,
    )
    await _redact_json_rows(
        conn,
        rewrite_questions,
        rewrite_questions.c.id,
        refs.get(rewrite_questions.name, ()),
        REWRITE_QUESTION_TEXTS,
        redactor,
        tally,
        write=write,
    )
    await _redact_structured(conn, story_id, refs, removed, redactor, tally, run)
    await _purge_story_results(conn, story_id, refs, redactor, tally, write=write)
    await _redact_transient(conn, story_id, redactor, tally, write=write)
    await _erase_story_images(conn, refs, run, tally)
    await _erase_image_jobs(conn, story_id, run, tally)
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
        # #220: the dependency edges naming the row (as their dependent or their target) go
        # with it (ids only; the edges' trigger lets an erasure delete them).
        e = dependency_edges.c
        await conn.execute(
            delete(dependency_edges).where(
                or_(
                    and_(e.dependent_table == table, e.dependent_id == record_id),
                    e.target_id == record_id,
                )
            )
        )


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
    at = (stories.name, story_id)
    name = redactor.text(row.name, (*at, "name")) if row.name else row.name
    changed += int(name != row.name)
    snapshot, count = redactor.document(row.scenario_snapshot, at, "scenario_snapshot")
    changed += count
    settings, count = redactor.document(row.settings or {}, at, "settings")
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
        values: dict[str, Any] = {"name": name, "settings": settings}
        # A blank story has no snapshot: writing Python None to the JSONB column stores
        # JSON null, which ck_stories_scenario_snapshot refuses (#349).
        if snapshot is not None:
            values["scenario_snapshot"] = snapshot
        await conn.execute(update(stories).where(stories.c.id == story_id).values(**values))


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
                new = redactor.text(value, (characters.name, str(row.id), column))
                if new != value:
                    values[column] = new
                    changed += 1
        if row.persona_snapshot is not None:
            snapshot, count = redactor.document(
                row.persona_snapshot, (characters.name, str(row.id)), "persona_snapshot"
            )
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
        content, changed = redactor.document(
            row.content, (character_snapshots.name, f"{story_id}:{character_id}"), "content"
        )
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
        value, changed = redactor.document(
            payload.get("value"), (genesis_deferred.name, str(row.id)), "payload.value"
        )
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
                new: Any = redactor.text(value, (table.name, str(row[0]), column))
                count = int(new != value)
            else:
                new, count = redactor.document(value, (table.name, str(row[0])), column)
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


# -- creation reports (#381, #382, #390) ------------------------------------------------------

NO_IMAGES: dict[str, Any] = {"primary": None, "gallery": []}


def _report_of(turn_report: Any) -> dict[str, Any] | None:
    found = turn_report.get("creation_report") if isinstance(turn_report, Mapping) else None
    return dict(found) if isinstance(found, Mapping) else None


def _with_report(turn_report: Any, edit: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
    report = _report_of(turn_report)
    if report is None:
        return turn_report
    return {**turn_report, "creation_report": edit(report)}


def story_erased_report(report: dict[str, Any], removed: set[str]) -> dict[str, Any]:
    """A genesis creation report once a story-scope erasure removed the story characters
    ``removed``: their catalog cards are kept as the boundary's ``KEPT_CARD`` states (docs/04
    C-15): labels, public description and structural fields stay, the name is redacted, and
    the images and content rating are emptied (#390, #400); the ``deferred`` items that belong
    only to them are dropped, as their ``genesis_deferred`` rows are, the others lose their
    ids, and ``counts.deferred`` is recounted from what is left (#381)."""
    cast = [
        {**card, "images": NO_IMAGES, "content_rating": None}
        if isinstance(card, Mapping)
        and card.get("kind") == "catalog"
        and card.get("character_id") in removed
        else card
        for card in report.get("cast") or []
    ]
    found = {**report, "cast": cast} if "cast" in report else dict(report)
    deferred = report.get("deferred")
    if not isinstance(deferred, list):
        return found
    kept: list[Any] = []
    for item in deferred:
        payload = item.get("payload") if isinstance(item, Mapping) else None
        held = payload.get("characters") if isinstance(payload, Mapping) else None
        if not isinstance(payload, Mapping) or not isinstance(held, Mapping):
            kept.append(item)
            continue
        named = {c for c in held.values() if isinstance(c, str)}
        if named and named <= removed:
            continue
        characters = {r: c for r, c in held.items() if c not in removed}
        kept.append({**item, "payload": {**payload, "characters": characters}})
    found["deferred"] = kept
    counts = report.get("counts")
    if isinstance(counts, Mapping) and len(kept) != len(deferred):
        by_class: dict[str, int] = {}
        by_capability: dict[str, int] = {}
        for item in kept:
            if isinstance(item, Mapping):
                klass, capability = item.get("field_class"), item.get("capability_needed")
                by_class[str(klass)] = by_class.get(str(klass), 0) + 1
                by_capability[str(capability)] = by_capability.get(str(capability), 0) + 1
        found["counts"] = {
            **counts,
            "deferred": {"by_class": by_class, "by_capability": by_capability},
        }
    return found


def catalog_erased_report(report: dict[str, Any], catalog_id: str) -> dict[str, Any]:
    """A genesis creation report once a catalog-scope erasure removed the catalog character
    ``catalog_id``: every card cast from it is emptied of its catalog content (name, public
    description, labels, images, content rating), keeping its ids, version and status values,
    so the report still validates (#382, the owner's decision: the card is public catalog data
    until a catalog-scope erasure, docs/04 C-15)."""
    if "cast" not in report:
        return report
    cast = [
        {
            **card,
            "name": None,
            "public_description": None,
            "labels": [],
            "images": NO_IMAGES,
            "content_rating": None,
        }
        if isinstance(card, Mapping)
        and card.get("kind") == "catalog"
        and isinstance(source := card.get("source"), Mapping)
        and source.get("catalog_id") == catalog_id
        else card
        for card in report["cast"]
    ]
    return {**report, "cast": cast}


async def _redact_operations(
    conn: AsyncConnection,
    ids: Iterable[str],
    redactor: Redactor,
    tally: _Tally,
    *,
    reports: Any,
    edit: Callable[[dict[str, Any]], dict[str, Any]],
    write: bool,
) -> None:
    """Redact the ``turn_report`` of the operations ``ids`` the index names, and apply ``edit``
    to the creation report of every genesis operation ``reports`` selects (``None``: none)
    and of those ``ids``; each row rewritten counts once."""
    wanted = set(ids)
    where: list[Any] = [operations.c.operation_id.in_(sorted(wanted))] if wanted else []
    if reports is not None:
        where.append(and_(operations.c.kind == OperationKind.GENESIS.value, reports))
    if not where:
        return
    rows = await conn.execute(
        select(operations.c.operation_id, operations.c.kind, operations.c.turn_report).where(
            or_(*where)
        )
    )
    for row in rows:
        value, changed = row.turn_report, 0
        if value is None:
            continue
        if str(row.operation_id) in wanted:
            value, changed = redactor.document(
                value, (operations.name, str(row.operation_id)), "turn_report"
            )
        if str(row.kind) == OperationKind.GENESIS.value:
            value = _with_report(value, edit)
        if value == row.turn_report:
            continue
        tally.record(operations.name, changed)
        if write:
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id == row.operation_id)
                .values(turn_report=value)
            )


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


async def _erase_outbox(
    conn: AsyncConnection,
    refs: Mapping[str, set[str]],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """The outbox (#210, #211; FR-EXT-14,21): an ``intended`` row that names the subject (its
    effect's text, or a value scoped to an erased character) is cancelled before the erasure,
    never sent; a ``claimed`` or ``unknown`` one (it may have reached the provider) stays
    listed, redacted to ``{outbox_id, provider, status}``: its effect emptied and ``erased_at``
    set, so its dispatch, if in flight, abandons and it is never sent again; every other row
    that names the subject has its effect redacted in place. Nothing of the effect reaches the
    ledger or a marker (the tally counts the table).

    #217 (FR-EXT-21 (b), S-ERASURE-BOUNDARY M6): a ``confirmed`` row is redacted in place as
    above (so its listing, ``confirmed_unsettled`` or a mark's, shows only the redacted effect),
    and the outcome records of every named row, and of every consumer-applied effect whose
    ``requested_effect`` names the subject, lose their payload and raw hash (``payload =
    {}``)."""
    named = sorted(refs.get(external_effect_outbox.name, set()))
    effects = await _named_effects(conn, refs)
    if not named and not effects:
        return
    o = external_effect_outbox.c
    x = outbox_outcomes.c
    if run.write:
        await conn.execute(
            update(external_effect_outbox)
            .where(o.id.in_(named), o.status == INTENDED)
            .values(status=CANCELLED)
        )
        await conn.execute(
            update(external_effect_outbox)
            .where(o.id.in_(named), o.status.in_((CLAIMED, UNKNOWN)))
            .values(effect={}, erased_at=func.transaction_timestamp())
        )
        outcomes = await conn.execute(
            update(outbox_outcomes)
            .where(or_(x.outbox_id.in_(named), x.effect_id.in_(sorted(effects))))
            .values(payload={}, raw_hash="")
        )
        for _ in range(int(outcomes.rowcount or 0)):
            tally.record(outbox_outcomes.name)
    await _redact_json_rows(
        conn, external_effect_outbox, o.id, named, ("effect",), redactor, tally, write=run.write
    )


async def _named_effects(conn: AsyncConnection, refs: Mapping[str, set[str]]) -> set[str]:
    """#217: the effect ids of the ``requested_effect`` events the erasure names (a
    consumer-applied effect's outcome records name the effect, never the event)."""
    named = sorted(refs.get(events.name, set()))
    if not named:
        return set()
    e = events.c
    return {
        str(effect)
        for effect in await conn.scalars(
            select(e.payload["effect_id"].astext).where(e.id.in_(named), e.type == REQUESTED_EFFECT)
        )
        if effect
    }


async def _erase_scheduled(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """Scheduled events (#196): every version naming an erased character (a participant, the
    owner or, #198, a shared owner) is deleted, each with a marker; the event's canon
    ``scheduled`` fact goes with the facts (``_erase_facts``)."""
    named = set(refs.get(scheduled_events.name, set()))
    if not removed or not named:
        return
    c = scheduled_events.c
    found = await conn.execute(
        select(c.id, c.owner, c.participants, c.shared_owners).where(
            c.story_id == story_id, c.id.in_(sorted(named))
        )
    )
    for row in sorted(found, key=lambda r: str(r.id)):
        named_here = {*(row.participants or ()), *(row.shared_owners or ())}
        if row.owner not in removed and not named_here & removed:
            continue
        await _delete(
            conn,
            run,
            tally,
            scheduled_events.name,
            str(row.id),
            delete(scheduled_events).where(c.id == row.id),
        )


async def _redact_structured(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """The structured stores (#131): an erased character's entity loses its aliases (deleted,
    each with a marker); every other record naming the subject is redacted in place. A story
    predicate's name keeps its id as a suffix when redacted, so names stay unique per story.
    Facts (#133), exposures (#164), divergences (#166), beliefs (#159), memories (#135),
    thunks (#146) and States (#185) go first: see ``_erase_facts``, ``_erase_exposures``,
    ``_erase_divergences``, ``_erase_beliefs``, ``_erase_memories``, ``_erase_thunks``,
    ``_erase_states`` and (#210) ``_erase_outbox``."""
    await _erase_facts(conn, story_id, refs, removed, tally, run)
    await _erase_exposures(conn, story_id, refs, removed, tally, run)
    await _erase_divergences(conn, story_id, refs, removed, tally, run)
    await _erase_beliefs(conn, story_id, removed, tally, run, refs=refs)
    await _erase_memories(conn, story_id, refs, removed, redactor, tally, run)
    await _erase_thunks(conn, story_id, refs, removed, redactor, tally, run)
    await _erase_states(conn, story_id, refs, removed, redactor, tally, run)
    await _erase_scheduled(conn, story_id, refs, removed, tally, run)
    await _erase_outbox(conn, refs, redactor, tally, run)
    owned = list(
        await conn.scalars(
            select(entity_aliases.c.id)
            .join(entities, entities.c.id == entity_aliases.c.entity_id)
            .where(
                entity_aliases.c.story_id == story_id,
                entities.c.character_id.in_(sorted(removed)),
            )
        )
    )
    for alias_id in owned:
        await _delete(
            conn,
            run,
            tally,
            entity_aliases.name,
            str(alias_id),
            delete(entity_aliases).where(entity_aliases.c.id == alias_id),
        )
    for table, columns, others in (
        (entity_aliases, ("alias",), {str(a) for a in owned}),
        (entities, ("canonical_name", "descriptors"), set()),
        (propositions, ("literal", "qualifiers"), set()),
        (closed_sets, ("key",), set()),
        (predicate_proposals, ("proposed", "decision"), set()),
        (predicates, ("docs",), set()),
    ):
        await _redact_json_rows(
            conn,
            table,
            table.c.id,
            set(refs.get(table.name, set())) - others,
            columns,
            redactor,
            tally,
            write=run.write,
        )
    wanted = sorted(refs.get(predicates.name, set()))
    rows = await conn.execute(
        select(predicates.c.id, predicates.c.name).where(predicates.c.id.in_(wanted))
    )
    for row in rows:
        words = predicate_words(str(row.name))
        new = redactor.text(words, (predicates.name, str(row.id), "name"))
        if new == words:
            continue
        tally.record(predicates.name, 1)
        if run.write:
            renamed = "_".join(new.replace("[", "").replace("]", "").split())
            await conn.execute(
                update(predicates)
                .where(predicates.c.id == row.id)
                .values(name=f"{renamed}_{str(row.id).lower()}")
            )


async def _erase_states(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """States (#185; S-ERASURE-BOUNDARY M6): a State scoped to an erased character is removed
    for them: every change scoped to them is deleted, each with a marker, and their
    ``state_values`` rows with them (the projection; refolded again by the ``rebuilt`` stage).
    Every other change or declaration that names the subject is redacted in place (a change's
    reason and text values; a declaration's aliases, unit and text initial value, and its name,
    which keeps its id as a suffix when redacted, as a predicate's does, so names stay unique)."""
    c = state_changes.c
    if removed:
        scoped = list(
            await conn.scalars(
                select(c.id)
                .where(c.story_id == story_id, c.scope_key.in_(sorted(removed)))
                .order_by(c.id)
            )
        )
        for change_id in scoped:
            await _delete(
                conn,
                run,
                tally,
                state_changes.name,
                str(change_id),
                delete(state_changes).where(c.id == change_id),
            )
        if run.write:
            v = state_values.c
            await conn.execute(
                delete(state_values).where(
                    v.story_id == story_id,
                    v.scope_key != STORY_SCOPE_KEY,
                    v.scope_key.in_(sorted(removed)),
                )
            )
        gone = {str(i) for i in scoped}
    else:
        gone = set()
    await _redact_json_rows(
        conn,
        state_changes,
        c.id,
        set(refs.get(state_changes.name, set())) - gone,
        ("reason", "value", "old_value", "new_value"),
        redactor,
        tally,
        write=run.write,
    )
    named = sorted(refs.get(state_definitions.name, set()))
    await _redact_json_rows(
        conn,
        state_definitions,
        state_definitions.c.id,
        named,
        ("aliases", "unit", "initial_value"),
        redactor,
        tally,
        write=run.write,
    )
    d = state_definitions.c
    for row in await conn.execute(select(d.id, d.name).where(d.id.in_(named))):
        words = predicate_words(str(row.name))
        new = redactor.text(words, (state_definitions.name, str(row.id), "name"))
        if new == words:
            continue
        tally.record(state_definitions.name, 1)
        if run.write:
            renamed = "_".join(new.replace("[", "").replace("]", "").split())
            await conn.execute(
                update(state_definitions)
                .where(d.id == row.id)
                .values(name=f"{renamed[:53]}_{str(row.id).lower()}")
            )
    # Engine-proposed States (#192): the suggested unit and the evidence's quotes in place, and
    # the suggested name as a declaration's (its id keeps names unique per story).
    proposed = sorted(refs.get(state_proposals.name, set()))
    await _redact_json_rows(
        conn,
        state_proposals,
        state_proposals.c.id,
        proposed,
        ("suggested_unit", "evidence"),
        redactor,
        tally,
        write=run.write,
    )
    p = state_proposals.c
    for row in await conn.execute(select(p.id, p.name).where(p.id.in_(proposed))):
        words = predicate_words(str(row.name))
        new = redactor.text(words, (state_proposals.name, str(row.id), "name"))
        if new == words:
            continue
        tally.record(state_proposals.name, 1)
        if run.write:
            renamed = "_".join(new.replace("[", "").replace("]", "").split())
            await conn.execute(
                update(state_proposals)
                .where(p.id == row.id)
                .values(name=f"{renamed[:53]}_{str(row.id).lower()}")
            )


async def _erase_facts(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """Facts (#133): every version whose subject or object is an erased character's entity is
    deleted, each with a marker (a fact about the subject is about the subject, whatever its
    wording); a version whose ``characters`` visibility names an erased character loses that
    name, and is deleted with a marker when no character is left to see it."""
    if not removed:
        return
    erased_entities = select(entities.c.id).where(
        entities.c.story_id == story_id, entities.c.character_id.in_(sorted(removed))
    )
    about = list(
        await conn.scalars(
            select(facts.c.id)
            .select_from(facts.outerjoin(propositions, propositions.c.id == facts.c.proposition_id))
            .where(
                facts.c.story_id == story_id,
                or_(
                    facts.c.subject_id.in_(erased_entities),
                    propositions.c.object_id.in_(erased_entities),
                ),
            )
            .order_by(facts.c.id)
        )
    )
    for fact_id in about:
        await _delete(
            conn, run, tally, facts.name, str(fact_id), delete(facts).where(facts.c.id == fact_id)
        )
    wanted = sorted(set(refs.get(facts.name, set())) - {str(f) for f in about})
    if not wanted:
        return
    rows = await conn.execute(
        select(facts.c.id, facts.c.visibility).where(facts.c.id.in_(wanted)).order_by(facts.c.id)
    )
    for row in rows:
        named = [c for c in row.visibility.get("characters") or () if c not in removed]
        if named == list(row.visibility.get("characters") or ()):
            continue
        if not named:
            await _delete(
                conn, run, tally, facts.name, str(row.id), delete(facts).where(facts.c.id == row.id)
            )
            continue
        tally.record(facts.name, 1)
        if run.write:
            await conn.execute(
                update(facts)
                .where(facts.c.id == row.id)
                .values(visibility={**row.visibility, "characters": named})
            )


async def _erase_exposures(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """Exposures (#164): every exposure of or about the subject (its witness or source is an
    erased character, or its proposition is about one: the index's declared rows) is deleted,
    each with a marker."""
    x = exposures.c
    found = set(refs.get(exposures.name, ()))
    if removed:
        found |= {
            str(i)
            for i in await conn.scalars(
                select(x.id).where(
                    x.story_id == story_id,
                    or_(
                        x.witness_character_id.in_(sorted(removed)),
                        x.source_character_id.in_(sorted(removed)),
                    ),
                )
            )
        }
    for exposure_id in sorted(found):
        await _delete(
            conn,
            run,
            tally,
            exposures.name,
            exposure_id,
            delete(exposures).where(x.id == exposure_id),
        )


async def _erase_divergences(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """Divergences (#166): every divergence of or about the subject (its speaker is an erased
    character, or one of its propositions is about one: the index's declared rows) is deleted,
    each with a marker, before the beliefs it names."""
    d = divergences.c
    found = set(refs.get(divergences.name, ()))
    if removed:
        found |= {
            str(i)
            for i in await conn.scalars(
                select(d.id).where(
                    d.story_id == story_id, d.speaker_character_id.in_(sorted(removed))
                )
            )
        }
    for divergence_id in sorted(found):
        await _delete(
            conn,
            run,
            tally,
            divergences.name,
            divergence_id,
            delete(divergences).where(d.id == divergence_id),
        )


async def _erase_beliefs(
    conn: AsyncConnection,
    story_id: str,
    removed: set[str],
    tally: _Tally,
    run: _StoryRun,
    *,
    refs: Mapping[str, set[str]] | None = None,
) -> None:
    """Beliefs (#159): every belief of or about the subject (an erased character holds it, or
    its proposition's subject or object is one: the index's declared rows, #164) is deleted,
    each with a marker, and the transitions (#160) of the erased holders and of those beliefs
    with it. ``belief_state`` is refolded by the ``rebuilt`` stage."""
    about = set((refs or {}).get(beliefs.name, ()))
    if not removed and not about:
        return
    held = sorted(
        {
            *(
                str(i)
                for i in await conn.scalars(
                    select(beliefs.c.id).where(
                        beliefs.c.story_id == story_id,
                        beliefs.c.holder_character_id.in_(sorted(removed)),
                    )
                )
            ),
            *about,
        }
    )
    # #167: a nested belief goes with its inner rows (an erased attributed holder or speaker
    # deeper in its chain), outermost last.
    frontier = list(held)
    while frontier:
        outer = sorted(
            {
                str(i)
                for i in await conn.scalars(
                    select(beliefs.c.id).where(
                        beliefs.c.story_id == story_id, beliefs.c.inner_belief_id.in_(frontier)
                    )
                )
            }
            - set(held)
        )
        held += outer
        frontier = outer
    for belief_id in held:
        await _delete(
            conn,
            run,
            tally,
            beliefs.name,
            str(belief_id),
            delete(beliefs).where(beliefs.c.id == belief_id),
        )
    # Their transitions (#160) go with them, each with a marker; a transition another holder's
    # belief rests on loses the erased source (the testimony stays, its witness does not).
    t = belief_transitions.c
    gone = list(
        await conn.scalars(
            select(t.id)
            .where(
                t.story_id == story_id,
                or_(t.holder_character_id.in_(sorted(removed)), t.belief_id.in_(held)),
            )
            .order_by(t.id)
        )
    )
    for transition_id in gone:
        await _delete(
            conn,
            run,
            tally,
            belief_transitions.name,
            str(transition_id),
            delete(belief_transitions).where(t.id == transition_id),
        )
    sourced = list(
        await conn.scalars(
            select(t.id)
            .where(
                t.story_id == story_id,
                t.source_character_id.in_(sorted(removed)),
                t.holder_character_id.not_in(sorted(removed)),
            )
            .order_by(t.id)
        )
    )
    for transition_id in sourced:
        tally.record(belief_transitions.name, 1)
        if run.write:
            await conn.execute(
                update(belief_transitions)
                .where(t.id == transition_id)
                .values(source_character_id=None)
            )


async def _erase_thunks(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """Thunks (#146): every thunk about the subject (an argument or a candidate is an erased
    character's entity, or its sketch mentions them) has its sketch redacted in place and is
    expired, so no later dependence resolves it and the ledger no longer counts it (its
    ledger entries hold ids only)."""
    t = thunks.c
    erased: list[str] = []
    if removed:
        erased = [
            str(e)
            for e in await conn.scalars(
                select(entities.c.id).where(
                    entities.c.story_id == story_id,
                    entities.c.character_id.in_(sorted(removed)),
                )
            )
        ]
    about: set[str] = set(refs.get(thunks.name, set()))
    if erased:
        named = or_(
            t.subject_id.in_(erased),
            t.object_id.in_(erased),
            *(
                t.proposition_sketch["candidates"].contains([{"entity_id": e}])
                for e in sorted(erased)
            ),
        )
        about |= {
            str(i) for i in await conn.scalars(select(t.id).where(t.story_id == story_id, named))
        }
    if not about:
        return
    rows = await conn.execute(
        select(t.id, t.proposition_sketch, t.state)
        .where(t.story_id == story_id, t.id.in_(sorted(about)))
        .order_by(t.id)
    )
    for row in rows:
        sketch, changed = redactor.document(
            row.proposition_sketch, (thunks.name, str(row.id)), "proposition_sketch"
        )
        if isinstance(sketch, Mapping) and erased:
            sketch = {
                **sketch,
                "candidates": [
                    c
                    for c in sketch.get("candidates") or ()
                    if not (isinstance(c, Mapping) and c.get("entity_id") in erased)
                ],
            }
        tally.record(thunks.name, changed)
        if run.write:
            await conn.execute(
                update(thunks)
                .where(t.id == row.id)
                .values(proposition_sketch=sketch, state=ThunkState.EXPIRED.value)
            )


async def _prune_memory_visibility(
    conn: AsyncConnection,
    story_id: str,
    removed: set[str],
    owned: set[Any],
    tally: _Tally,
    run: _StoryRun,
) -> list[str]:
    """Memories scoped to characters among whom is an erased one (#178, S-ERASURE-BOUNDARY M5):
    the name leaves ``visibility.characters``; a memory no character is left to see is deleted
    with a marker (as ``_erase_facts`` does a fact's). Answers the deleted ids."""
    if not removed:
        return []
    m = memories.c
    rows = await conn.execute(
        select(m.id, m.visibility)
        .where(
            m.story_id == story_id,
            m.visibility["scope"].astext == "characters",
            m.visibility["characters"].has_any(array(sorted(removed), type_=Text)),
        )
        .order_by(m.id)
    )
    deleted: list[str] = []
    for row in rows:
        if row.id in owned:
            continue
        named = [c for c in row.visibility.get("characters") or () if c not in removed]
        if not named:
            await _delete(
                conn,
                run,
                tally,
                memories.name,
                str(row.id),
                delete(memories).where(m.id == row.id),
            )
            deleted.append(str(row.id))
            continue
        tally.record(memories.name, 1)
        if run.write:
            await conn.execute(
                update(memories)
                .where(m.id == row.id)
                .values(visibility={**row.visibility, "characters": named})
            )
    return deleted


async def _erase_memories(
    conn: AsyncConnection,
    story_id: str,
    refs: Mapping[str, set[str]],
    removed: set[str],
    redactor: Redactor,
    tally: _Tally,
    run: _StoryRun,
) -> None:
    """Memories (#135): a memory an erased character holds or voiced (its holder or its beat's
    actor) is deleted, with a marker; every other memory naming the subject has its certain
    mentions redacted in place (a candidate only when confirmed), and its vector and
    embedding-model version are nulled in this transaction, so no vector of the old text is
    left (the embed job re-embeds the redacted text)."""
    m = memories.c
    owned = (
        list(
            await conn.scalars(
                select(m.id)
                .where(
                    m.story_id == story_id,
                    or_(
                        m.holder_character_id.in_(sorted(removed)),
                        m.character_id.in_(sorted(removed)),
                    ),
                )
                .order_by(m.id)
            )
        )
        if removed
        else []
    )
    for memory_id in owned:
        await _delete(
            conn,
            run,
            tally,
            memories.name,
            str(memory_id),
            delete(memories).where(m.id == memory_id),
        )
    # #178: a memory another character holds that the subject could also see loses the
    # subject's name from its visibility; one no character is left to see is deleted.
    pruned = await _prune_memory_visibility(conn, story_id, removed, set(owned), tally, run)
    owned = [*owned, *pruned]
    if owned and run.write:
        # The hot set's counters lost rows: the next reader recounts them (#147).
        await retirement.invalidate(conn, story_id)
    wanted = sorted(set(refs.get(memories.name, set())) - {str(i) for i in owned})
    if not wanted:
        return
    rows = await conn.execute(
        select(m.id, m.text, m.quarantine_reason)
        .where(m.story_id == story_id, m.id.in_(wanted))
        .order_by(m.id)
    )
    for row in rows:
        new = redactor.text(row.text, (memories.name, str(row.id), "text"))
        # A quarantine's reason is free text too (#145).
        reason = (
            None
            if row.quarantine_reason is None
            else redactor.text(
                row.quarantine_reason, (memories.name, str(row.id), "quarantine_reason")
            )
        )
        if new == row.text and reason == row.quarantine_reason:
            continue
        tally.record(memories.name, 1)
        if run.write:
            values: dict[str, Any] = {"quarantine_reason": reason}
            if new != row.text:
                values.update(text=new, embedding=None, embedding_model_version=None)
                # A dropped vector leaves the story's vector counter (#147).
                await retirement.invalidate(conn, story_id)
            await conn.execute(update(memories).where(m.id == row.id).values(**values))


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
    # A result the index names for the subject (any mention, a candidate included) is purged
    # whole, so its candidates are not listed: the record they copy lists them.
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
            if redactor.count(row[1], (table.name, str(row[0])), column)
        ]
        await _redact_json_rows(conn, table, key, ids, (column,), redactor, tally, write=write)


def _declares(record: Record, subject: _Subject) -> bool:
    """Whether ``record`` declares the subject (a story character that is the subject, or the
    subject itself)."""
    keys = set(subject.keys)
    return any((SubjectKind(kind).value, str(i)) in keys for kind, i, _ in record.subjects)


def _names(record: Record, redactor: Redactor) -> bool:
    """Whether a text of ``record`` holds a certain or confirmed-candidate mention of the
    subject. Every field is read, so each candidate it holds is met (listed by a dry run)."""
    found = False
    for path, value in record.fields.items():
        if value and redactor.text(value, (record.table, record.record_id, path)) != value:
            found = True
    return found


async def _purge_story_assets(
    conn: AsyncConnection, rows: Sequence[Any], run: _StoryRun, tally: _Tally
) -> None:
    """The assets of the story images an erasure deletes (#266): one no other record references
    (another story image, a fork's copy included, or any other reference source) is purged with
    its bytes after the commit (``checksum``-guarded by the media sweep's ``collect_blob``);
    one still referenced stays as it is (FR-STORE-14)."""
    from epistrel.core.images.story_images import TABLE as STORY_IMAGES
    from epistrel.core.media import assets
    from epistrel.core.tables.story_images import story_images

    deleting = [str(r.id) for r in rows]
    by_asset = {str(r.asset_id): r for r in rows}
    if not by_asset:
        return
    s = story_images.c
    kept = {
        str(a)
        for a in await conn.scalars(
            select(s.asset_id).where(s.asset_id.in_(list(by_asset)), s.id.not_in(deleting))
        )
    }
    kept |= await assets.referenced(conn, list(by_asset), skip={STORY_IMAGES})
    for asset_id in sorted(set(by_asset) - kept):
        row = by_asset[asset_id]
        if row.asset_checksum is None:
            continue  # no asset row (already purged)
        tally.record(media_assets.name)
        tally.media += 1
        if run.write:
            await ownership.drop_entity(conn, EntityType.MEDIA, asset_id, principal=run.principal)
            await index.drop_references(conn, assets.RECORD_TABLE, asset_id)
            await conn.execute(delete(media_assets).where(media_assets.c.id == asset_id))
            run.checksums.append(str(row.asset_checksum))


async def _erase_story_images(
    conn: AsyncConnection, refs: Mapping[str, set[str]], run: _StoryRun, tally: _Tally
) -> None:
    """Story images (#261, #266) on every membership that declare the subject (its asset's
    depicted subjects, the audience's or the camera's character) or whose prompt, caption, alt
    text or generation prompts hold a certain or confirmed-candidate mention of it: each row is
    deleted with an erasure marker, then its asset as ``_purge_story_assets`` says."""
    from epistrel.core.erasure.records import asset_columns, story_image_record
    from epistrel.core.tables.story_images import story_images

    ids = sorted(refs.get(story_images.name, set()))
    if not ids:
        return
    rows = await conn.execute(
        select(story_images, *asset_columns(), media_assets.c.checksum.label("asset_checksum"))
        .join(media_assets, media_assets.c.id == story_images.c.asset_id, isouter=True)
        .where(story_images.c.id.in_(ids), story_images.c.story_id == run.subject.story_id)
        .order_by(story_images.c.id)
    )
    reached = []
    for row in rows:
        record = story_image_record(row)
        named = _names(record, run.redactor)
        if named or _declares(record, run.subject):
            reached.append(row)
    for row in reached:
        image_id = str(row.id)
        await _delete(
            conn,
            run,
            tally,
            story_images.name,
            image_id,
            delete(story_images).where(story_images.c.id == image_id),
        )
        if run.write:
            await index.drop_references(conn, story_images.name, image_id)
    await _purge_story_assets(conn, reached, run, tally)


IMAGE_JOB_DOCUMENTS = ("params", "safety_context", "status_detail")
"""An image job's JSON columns that can hold text (its caption, alt text, subjects' descriptors
and labels): redacted span by span. The others hold ids, positions and policy results."""
IMAGE_JOB_PROMPTS = ("prompt", "negative_prompt")


async def _erase_image_jobs(
    conn: AsyncConnection, story_id: str, run: _StoryRun, tally: _Tally
) -> None:
    """The story's image jobs (#256, #266). A job whose depicted subjects (or the subjects its
    prompt was checked with, or its audience's character) include the subject, or whose prompt
    or negative prompt holds a certain or confirmed-candidate mention of it, has both prompts
    nulled, and is cancelled in this transaction when it has not ended
    (``core.images.store.cancel_in``: it can then never promote). The job row and its accounting
    stay. Every job's JSON texts (``IMAGE_JOB_DOCUMENTS``) are redacted span by span, so a job
    that only names the subject in its caption keeps its prompt. An automatic job that has not
    prepared yet has no prompt or subjects (#265): it prepares from the erased story. Every job
    of the story is read (a story has few), as the erasure reads its transient work."""
    from epistrel.core.erasure.records import image_job_record
    from epistrel.core.images import store as image_store
    from epistrel.core.tables.images import TERMINAL_STATUSES, image_jobs

    j = image_jobs.c
    rows = await conn.execute(select(image_jobs).where(j.story_id == story_id).order_by(j.id))
    terminal = {s.value for s in TERMINAL_STATUSES}
    for row in rows:
        job_id = str(row.id)
        at = (image_jobs.name, job_id)
        named = False
        for column in IMAGE_JOB_PROMPTS:
            value = getattr(row, column)
            if value and run.redactor.text(value, (*at, column)) != value:
                named = True
        reached = named or _declares(image_job_record(row), run.subject)
        values: dict[str, Any] = {}
        changed = 0
        for column in IMAGE_JOB_DOCUMENTS:
            value = getattr(row, column)
            if value is None:
                continue
            new, count = run.redactor.document(value, at, column)
            if count:
                values[column] = new
                changed += count
        if reached:
            for column in IMAGE_JOB_PROMPTS:
                if getattr(row, column):
                    values[column] = None
                    changed += 1
        if not values:
            continue
        tally.record(image_jobs.name, changed)
        if not run.write:
            continue
        await conn.execute(update(image_jobs).where(j.id == job_id).values(**values))
        if reached and str(row.status) not in terminal:
            await image_store.cancel_in(conn, job_id)


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
    return any(redactor.finds(value) for value in record.fields.values() if value)


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


def _cast_from(catalog_id: str) -> Any:
    """The genesis operations whose creation report casts the catalog character ``catalog_id``
    (its public card, emptied by a catalog-scope erasure: #382)."""
    return operations.c.turn_report.contains(
        {"creation_report": {"cast": [{"source": {"catalog_id": catalog_id}}]}}
    )


async def character_story(conn: AsyncConnection, character_id: str) -> str | None:
    """The story a character belongs to (a catalog-scope erasure of a story character, #504,
    reapplied from the ledger, which records no story for the catalog scope)."""
    found = await conn.scalar(select(characters.c.story_id).where(characters.c.id == character_id))
    return None if found is None else str(found)


async def catalog_reach(
    conn: AsyncConnection, subject_kind: str, subject_id: str, confirmed: Sequence[str] = ()
) -> dict[str, set[str]]:
    """``{record_table: {record_id}}``: what a catalog-scope erasure of the subject would touch
    now (a pending one's reapply, #346, ADR-157). The catalog pass's own discovery (the index's
    rows, and every catalog record the subject's terms or the ``confirmed`` candidates match:
    ``_catalog_records``), the subject's own entry and a catalog character's companion
    scenarios (deleted with it), and the genesis operations whose creation report carries its
    public card (``operations``). Reads only, and reads the subject's terms as the erasure
    does, so a subject already gone reaches only what the index still names."""
    ref: dict[str, Any] = {"kind": subject_kind, "id": subject_id}
    if subject_kind == SubjectKind.CHARACTER.value:
        story = await character_story(conn, subject_id)
        if story is None:  # nothing left that names it (#504)
            return {}
        ref["story"] = story
    request = ErasureRequest.model_validate({"subject": ref, "scope": {"catalog": True}})
    subject = await _subject(conn, request)
    found = await _catalog_records(conn, subject, subject.redactor(confirmed))
    if subject.kind is SubjectKind.PERSONA:
        found.setdefault(personas.name, set()).add(subject.id)
    elif subject.kind is SubjectKind.CATALOG_CHARACTER:
        found.setdefault(character_catalog.name, set()).add(subject.id)
        companions = await conn.scalars(
            select(scenario_catalog.c.id).where(scenario_catalog.c.companion_of == subject.id)
        )
        found.setdefault(scenario_catalog.name, set()).update(str(c) for c in companions)
        carded = await conn.scalars(select(operations.c.operation_id).where(_cast_from(subject.id)))
        found.setdefault(operations.name, set()).update(str(o) for o in carded)
    return found


async def _catalog_pass(conn: AsyncConnection, run: _CatalogRun) -> _Tally:
    subject, redactor, write = run.subject, run.redactor, run.write
    tally = _Tally()
    refs = await _catalog_records(conn, subject, redactor)
    if write:
        await _guc(conn)
    # The candidates first (#266), the subject's own among them: their bytes are collected.
    await _erase_candidates(conn, refs, run, tally)
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
    await _erase_upload_sessions(conn, refs.get(media_upload_sessions.name, set()), run, tally)
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
    ):
        await _redact_json_rows(
            conn, table, key, refs.get(table.name, set()), columns, redactor, tally, write=write
        )
    carded = subject.kind is SubjectKind.CATALOG_CHARACTER
    await _redact_operations(
        conn,
        refs.get(operations.name, set()),
        redactor,
        tally,
        reports=_cast_from(subject.id) if carded else None,
        edit=lambda report: catalog_erased_report(report, subject.id) if carded else report,
        write=write,
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
        from epistrel.core.images import candidates

        # The companions' candidates go with them (#260): their bytes are collected too.
        run.checksums += await candidates.entry_checksums(
            conn, [("scenario", companion) for companion in companions]
        )
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
        draft, changed = redactor.document(row.draft, (character_catalog.name, entry_id))
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
        content, changed = redactor.document(row.content, (character_versions.name, record_id))
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
        draft, changed = redactor.document(row.draft, (scenario_catalog.name, entry_id))
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
        content, changed = redactor.document(row.content, (scenario_versions.name, record_id))
        content, unfilled = _unfill(content, subject)
        report, reported = redactor.document(
            row.report, (scenario_versions.name, record_id), "report"
        )
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


async def _erase_candidates(
    conn: AsyncConnection, refs: Mapping[str, set[str]], run: _CatalogRun, tally: _Tally
) -> None:
    """Catalog image candidates (#260, #266) that declare the subject (a character entry's own
    candidates, or an asset depicting it) or whose asset's caption, alt text or generation
    prompts hold a certain or confirmed-candidate mention of it (a story character erased from
    the catalog, #504, is matched by its story's names): each is deleted outright with its
    asset, and the bytes are collected after the commit. A story-scope erasure never reaches a
    candidate."""
    from epistrel.core.erasure.records import asset_columns, image_candidate_record
    from epistrel.core.images import candidates
    from epistrel.core.tables.images import image_candidates

    ids = sorted(refs.get(image_candidates.name, set()))
    if not ids:
        return
    rows = await conn.execute(
        select(image_candidates, *asset_columns())
        .join(media_assets, media_assets.c.id == image_candidates.c.asset_id, isouter=True)
        .where(image_candidates.c.id.in_(ids))
        .order_by(image_candidates.c.id)
    )
    reached = []
    for row in rows:
        record = image_candidate_record(row)
        named = _names(record, run.redactor)
        if named or _declares(record, run.subject):
            reached.append(str(row.id))
    for candidate_id in reached:
        tally.record(_catalog(image_candidates.name))
        tally.media += 1
        run.deleted.add((image_candidates.name, candidate_id))
    if run.write and reached:
        run.checksums += await candidates.remove(conn, reached)


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
        if row is None or row.viewpoint_basis is not None:
            # Gone with a candidate or the subject's entry; or a story image's asset, which
            # belongs to its story (FR-STORE-14; #266).
            continue
        depicted = [d for d in (row.depicted_subjects or []) if isinstance(d, Mapping)]
        others = [d for d in depicted if (d.get("kind"), d.get("id")) != (subject.kind, subject.id)]
        at = (media_assets.name, str(row.id))
        caption = redactor.text(row.caption, (*at, "caption")) if row.caption else row.caption
        alt_text = redactor.text(row.alt_text, (*at, "alt_text")) if row.alt_text else row.alt_text
        generation, prompts = redactor.document(row.generation_metadata, at, "generation_metadata")
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


async def _erase_upload_sessions(
    conn: AsyncConnection, upload_ids: Iterable[str], run: _CatalogRun, tally: _Tally
) -> None:
    """Upload sessions whose stored asset metadata names or depicts the subject (#334): the
    texts are redacted and the subject leaves ``depicted_subjects``, as for a media asset. An
    open session that depicted only the subject is aborted too (its bytes would become an
    asset of the erased subject); its parts are the media sweep's stray parts from then on."""
    subject, redactor = run.subject, run.redactor
    for upload_id in sorted(set(upload_ids)):
        row = (
            await conn.execute(
                select(media_upload_sessions).where(media_upload_sessions.c.id == upload_id)
            )
        ).one_or_none()
        if row is None:
            continue
        metadata = dict(row.metadata) if isinstance(row.metadata, Mapping) else {}
        depicted = [d for d in (metadata.get("depicted_subjects") or []) if isinstance(d, Mapping)]
        others = [d for d in depicted if (d.get("kind"), d.get("id")) != (subject.kind, subject.id)]
        texts = 0
        for key in ("caption", "alt_text"):
            value = metadata.get(key)
            if isinstance(value, str):
                redacted_text = redactor.text(
                    value, (media_upload_sessions.name, upload_id, f"metadata.{key}")
                )
                texts += int(redacted_text != value)
                metadata[key] = redacted_text
        if isinstance(metadata.get("generation_metadata"), Mapping):
            metadata["generation_metadata"], prompts = redactor.document(
                metadata["generation_metadata"],
                (media_upload_sessions.name, upload_id),
                "metadata.generation_metadata",
            )
            texts += prompts
        if "depicted_subjects" in metadata:
            metadata["depicted_subjects"] = others
        abort = row.state == UploadState.OPEN.value and depicted and not others
        tally.record(media_upload_sessions.name, texts)
        if not run.write:
            continue
        values: dict[str, Any] = {"metadata": metadata}
        if abort:
            now = clock.now()
            values.update(state=UploadState.ABORTED.value, updated_at=now, closed_at=now)
        await conn.execute(
            update(media_upload_sessions)
            .where(media_upload_sessions.c.id == upload_id)
            .values(**values)
        )
        found = (
            await conn.execute(
                select(media_upload_sessions).where(media_upload_sessions.c.id == upload_id)
            )
        ).one()
        await index_records(conn, [upload_session_record(found)])


async def _reindex_media(conn: AsyncConnection, media_id: str) -> None:
    row = (await conn.execute(select(media_assets).where(media_assets.c.id == media_id))).one()
    await index_records(conn, [media_record(row)])


async def _referencing_stories(conn: AsyncConnection, subject: _Subject) -> list[ReferencingStory]:
    """The stories that reference a catalog subject: those with characters made from it, and
    those whose index names it; for a story character (#504), its own story."""
    if subject.source_story is not None:
        return [ReferencingStory(subject.source_story, (subject.id,))]
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


async def _promoted_drafts(conn: AsyncConnection, story_id: str) -> list[PromotedDraft]:
    """The catalog characters promoted from ``story_id``'s characters that still exist, with
    their companion scenarios (#504), in promotion order per character."""
    rows = await conn.execute(
        select(characters.c.id, characters.c.promoted_to)
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    pairs = [
        (str(row.id), str(catalog_id))
        for row in rows
        for catalog_id in (row.promoted_to or [])
        if isinstance(catalog_id, str)
    ]
    if not pairs:
        return []
    ids = [catalog_id for _, catalog_id in pairs]
    existing = {
        str(i)
        for i in await conn.scalars(
            select(character_catalog.c.id).where(character_catalog.c.id.in_(ids))
        )
    }
    companions: dict[str, list[str]] = {}
    for row in await conn.execute(
        select(scenario_catalog.c.id, scenario_catalog.c.companion_of)
        .where(scenario_catalog.c.companion_of.in_(ids))
        .order_by(scenario_catalog.c.id)
    ):
        companions.setdefault(str(row.companion_of), []).append(str(row.id))
    return [
        PromotedDraft(catalog_id, character_id, tuple(companions.get(catalog_id, ())))
        for character_id, catalog_id in pairs
        if catalog_id in existing
    ]


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
    reads in one snapshot, re-indexes the story first as the real run does (#386), and rolls
    that back. A catalog dry run does the catalog pass and rolls it back:
    what the subject's own deletion removes decides whether a medium is still referenced, so the
    counts are the real run's by construction; nothing it did is ever committed (no operation,
    no key, no ledger row, no bytes deleted)."""
    async with db.connect() as conn, conn.begin() as tx:
        if request.story_id is not None:
            await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        subject = await _subject(conn, request)
        redactor = subject.redactor(request.confirm_candidates)
        revision = await config_revision(conn)
        timeline_revision: int | None = None
        windows = 0
        referencing: list[ReferencingStory] = []
        promoted: list[PromotedDraft] = []
        if subject.story_id is not None:
            promoted = await _promoted_drafts(conn, subject.story_id)
        if not subject.known:
            tally = _Tally()
        elif subject.story_id is not None:
            run = _StoryRun(subject, redactor, False, "", None, principal, clock.now())
            await reindex_story(conn, subject.story_id)
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
        check_confirmed(redactor)
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
        candidates_remaining=redactor.remaining,
        candidates=tuple(
            sorted(
                redactor.candidates.values(),
                key=lambda c: (c.record_table, c.record_id, c.field, c.candidate_id),
            )
        ),
        promoted_drafts=tuple(promoted),
        subject_story=request.subject.story,
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
        select(events.c.id, events.c.payload).where(
            events.c.id.in_(ids), events.c.type == SUMMARY_EVENT
        )
    )
    return sum(
        1
        for row in found
        if int((row.payload or {}).get("level", 0)) == 0
        and run.redactor.count(row.payload, (events.name, str(row.id)), "payload") > 0
    )


def _candidate_counts(redactor: Redactor) -> dict[str, Any]:
    """What the ledger's counts keep of the candidates (ADR-154): how many were left, and the
    ids the operator confirmed, so #125's reapply erases them too. Ids, never snippets."""
    return {
        "candidates_remaining": redactor.remaining,
        "confirmed_candidates": sorted(redactor.confirmed & redactor.candidates.keys()),
    }


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
        candidates_remaining=result.candidates_remaining,
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
    """Add one stage to a committed operation's ``stage_log`` (``Operation.stage``'s shape:
    ``{stage, status, at}``)."""
    at = clock.now().isoformat()
    statement = (
        update(operations)
        .where(operations.c.operation_id == operation_id)
        .values(
            stage_log=func.coalesce(operations.c.stage_log, text("'[]'::jsonb"))
            + func.jsonb_build_array(
                func.jsonb_build_object("stage", stage, "status", status, "at", at)
            )
        )
    )
    if isinstance(db, AsyncConnection):
        await db.execute(statement)
        return
    async with db.begin() as conn:
        await conn.execute(statement)


Collect = Callable[[str], Awaitable[object]]
"""``collect(checksum)``: delete a purged asset's bytes once no row has them (after commit)."""


async def _collect(collect: Collect | None, checksums: Iterable[str]) -> None:
    """Delete the purged assets' bytes after the commit (each once; ``collect`` keeps bytes
    another row still names); a failed delete is left to the media sweep."""
    if collect is None:
        return
    for checksum in dict.fromkeys(checksums):
        try:
            await collect(checksum)
        except Exception as exc:  # the media sweep deletes what is left
            log.warning("erasure blob delete failed", exc_type=type(exc).__name__)


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
    confirmed: Iterable[str] | None = None,
) -> ErasureResult:
    """Erase ``request.subject`` in its scope (see the module docstring). ``erasure_id`` and
    ``write_ledger`` let #125's reapply run an erasure the ledger already records, with the
    candidates the original confirmed (``confirmed``: an id the restored database no longer
    holds is skipped, not refused); ``ledger`` is the ledger file the entry is appended to
    first (the Engine's; ``None`` writes the table only)."""
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
    hashes = term_hashes(subject.terms, config_key, subject.parts)
    strict = confirmed is None
    redactor = subject.redactor(request.confirm_candidates if confirmed is None else confirmed)
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
            collect,
            write_ledger=write_ledger,
            ledger=ledger,
            strict=strict,
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
            strict=strict,
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
    collect: Collect | None,
    *,
    write_ledger: bool,
    ledger: LedgerFile | None,
    strict: bool,
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
            # #504: the drafts promoted from the story, read before the subject's rows go.
            found["promoted"] = await _promoted_drafts(conn, story_id)
            # The index is rebuilt first, so a stale one (rows written before a matcher
            # change, or lost) cannot hide a mention from the redaction (#386).
            await reindex_story(conn, story_id)
            tally = await _story_pass(conn, run)
            if strict:
                check_confirmed(redactor)
            if _touched_policies(tally):
                await _bump_policy_erasure_revision(conn, story_id)
            await _insert_markers(conn, run)
            found["failed_queue"] = await _stop_in_flight(conn, story_id, run)
            await projections.replace_with_fold(conn, story_id)
            await reindex_story(conn, story_id)
            residue = await verify(conn, story_id, redactor)
            counts = {**tally.counts(), "residue": residue.count, **_candidate_counts(redactor)}
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
    await _collect(collect, run.checksums)
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
        candidates_remaining=redactor.remaining,
        promoted_drafts=tuple(found.get("promoted", ())),
    )


async def _stop_in_flight(conn: AsyncConnection, story_id: str, run: _StoryRun) -> list[str]:
    """Nothing in flight writes the subject back (FR-ADMIN-9; #127). The erasure is a control
    action, so a turn generating meanwhile abandons at its commit (#47); here, in the erasure's
    own transaction: the turns waiting in the queue fail with ``timeline_changed``; the turn
    slot's lease is cleared (the turn holding it abandons on its own; a queued turn a drainer
    already started keeps it, #436); and the operations whose events were redacted have their
    ``membership_tx`` moved, so a background job that read them (#72's summary job declares its
    source turns, #70) drops its output at commit and is requeued. Returns the failed queued
    turns."""
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
    await turn_gate.clear_lease(conn, story_id)
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
    strict: bool,
) -> ErasureResult:
    async with db.begin() as conn:
        await _lock_deployment_row(conn)
        settings = await _settings(conn)
        referencing = await _referencing_stories(conn, subject)
        operation_id = await control.record_control_operation(conn, actor=principal)
        run = _CatalogRun(
            subject, redactor, True, principal, operation_id, settings.erasure_shared_media
        )
        tally = await _catalog_pass(conn, run)
        if strict:
            check_confirmed(redactor)
        if _touched_policies(tally):
            await _bump_policy_erasure_revision(conn, None)
        referencing = _merged(referencing, run.media_stories)
        residue = await verify(conn, None, redactor)
        counts = {**tally.counts(), "residue": residue.count, **_candidate_counts(redactor)}
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
    await _collect(collect, run.checksums)
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
        candidates_remaining=redactor.remaining,
        subject_story=subject.source_story,
    )


__all__ = [
    "ERASURE_TARGETS",
    "CatalogScope",
    "ErasureRequest",
    "ErasureResult",
    "ErasureTarget",
    "LedgerEntry",
    "PromotedDraft",
    "ReferencingStory",
    "StoryScope",
    "SubjectRef",
    "append_stage",
    "catalog_reach",
    "character_story",
    "check_request",
    "dry_run",
    "erase",
    "register_target",
    "require_key",
    "story_character",
    "term_hashes",
]
