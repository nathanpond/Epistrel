"""The mutation primitives and the low-level fact calls (FR-GUARD-4,5; docs/02 §4.2, §9; #145).

Three primitives correct the record directly, for an author or a consumer (no role exists:
any principal may call them, FR-AUTH-1), and one keeps the output gate's loop converging:

- **quarantine** (``quarantine``/``unquarantine``): a memory leaves retrieval (#141's every arm)
  and consolidation's input windows; working memory (events, FR-MEM-5) and the Lite window are
  untouched, and the row keeps its vector, its budget share (#147) and its history. Quarantining
  a quarantined memory is a no-op, as is releasing one that is not; a retired or discarded
  memory may be quarantined. One row only: derived records are reached through their own
  implication counts.
- **sanitize** (``sanitize``): every occurrence of an exact ``span`` of a memory's text is
  replaced by ``surrogate`` (422 ``span_not_found`` when there is none; the surrogate passed
  the input gate first, the Engine's). The operation records a ``sanitized`` narrative-state
  event (the surrogate) and a ``sanitize_original`` audit event (the span and the old text, the
  only place it remains); the memory's ``text`` is rewritten under ``epistrel.sanitize``, its
  vector is cleared and re-embedded by an ``embed`` job (``MEMORY_HOOKS``), and a consolidation
  output's ``inputs_hash`` is cleared so #143 never replays it. The new text passes the
  resurrection guard and is re-indexed for erasure (positional rows, ADR-154..156). History
  views keep the beat as it was.
- **canonize** (``commit_fact``, ``supersede_fact``, ``lock_fact``, ``unlock_fact``): #133's
  writes with ``provenance = authority = author``. A ``commit_fact`` runs #140's deterministic
  check first: a contradicting current fact of the same functional key (or the opposite
  polarity) is superseded automatically (the author wins; ``superseded`` lists them), any other
  contradiction and an ``uncertain`` verdict are warnings. ``locked`` sets ``author_locked``. A
  lock or unlock is a supersede of the same proposition with the lock flipped (canon is
  append-only); a supersede inherits provenance, authority and the lock unless given. Each
  author fact also writes an author memory (``core.memory.records.write_author``), so it is
  retrievable; a supersede that changes the proposition or its visibility supersedes that
  memory and writes the new one, and a fact the commit closed takes its author memory with it.
  The fact is position-less (it survives truncation, like a configuration change).

Every primitive is one position-less ``control`` operation that bumps the story's revision
(FR-CONC-1), so a turn generating meanwhile revalidates (a memory it retrieved rebases, #136;
a canon key it read regenerates). The expected revision is checked first. A memory primitive
on a story whose configuration has the ``episodic`` module off, and a fact call on a Lite
story, is 409 ``module_off``; an unknown memory is 404.

**FR-GUARD-5** (``implicate``): the output gate (#68) hands over the records a rejected
render's context held. Their memories' ``implication_count`` goes up by one in a ``system``
control operation (no revision bump: a derived write, FR-CONC-1's rule) with a
``memories_implicated`` event; a memory reaching the story's ``repair.implication_threshold``
(0: never) is quarantined in the same operation with reason ``repeated_implication``. A
truncation of the implicating turn takes its implications back off the counts
(``follow_implications``, a membership hook). An
author pin may be quarantined too (the owner's decision on #145: it is reported as a
``pin_quarantined`` warning). Facts are reported, never changed.

``core.mutation`` is a ``DIRECT`` write path of the resurrection guard and a ``memories``
subject writer. Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints
from sqlalchemy import and_, bindparam, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import timeline
from epistrel.core.canon import facts as canon
from epistrel.core.canon.facts import Fact, Visibility
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.enums import (
    FactAuthority,
    Membership,
    MemoryKind,
    OperationKind,
    Provenance,
    RecordProvenance,
)
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import memory_record
from epistrel.core.errors import InvalidRequest, MemoryNotFound, ModuleOff, SpanNotFound
from epistrel.core.events import Operation
from epistrel.core.events.types import (
    MemoriesImplicated,
    MemoryQuarantined,
    MemoryUnquarantined,
    Sanitized,
    SanitizeOriginal,
)
from epistrel.core.membership import supersede as supersede_rows
from epistrel.core.memory import records
from epistrel.core.memory.records import EPISODIC_MODULE, Memory
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Bound, Interval, Proposition
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.stories import require_writable
from epistrel.core.tables import events
from epistrel.core.tables.base import ulid_type
from epistrel.core.tables.facts import facts as facts_table
from epistrel.core.tables.memories import QUARANTINE_REASON_MAX, TEXT_MAX, memories
from epistrel.core.trimmed import trim_as_sent
from epistrel.core.verify.rules import Rule, VerdictKind, check_with
from epistrel.observability.metrics import QUARANTINES, SANITIZES

log = logging.getLogger(__name__)

REPEATED_IMPLICATION = "repeated_implication"
"""The reason an auto-quarantine records (reserved for the system)."""
SYSTEM_PRINCIPAL = "system"
SANITIZE_SETTING = "epistrel.sanitize"
"""Set to the memory's id for a sanitize's transaction: the guard lets that row's ``text`` and
``inputs_hash`` change (migration 0059)."""
STRUCTURED = "structured"
CANON_MODULE = "pipeline"
SURROGATE_MAX = TEXT_MAX
LIST_LIMIT_MAX = 500
DEFAULT_LIST_LIMIT = 100

Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=QUARANTINE_REASON_MAX),
    BeforeValidator(trim_as_sent(QUARANTINE_REASON_MAX)),
]


# -- requests ----------------------------------------------------------------------------------


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_EXAMPLE_PROPOSITION: dict[str, Any] = {
    "subject_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
    "predicate_id": "01JPRED0000000000000000015",
    "object_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C4",
    "valid_time": {"kind": "interval", "start": {"inner": 0, "outer": 0}, "end": None},
}
"""A ``knows`` proposition (the core vocabulary's), as the fact calls' examples show it."""


class QuarantineRequest(_Request):
    """Why the memory is quarantined (free text, at most 500 characters; the author's note).
    ``repeated_implication`` is the system's reason and cannot be given."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"reason": "Planted by an injected narration."}]},
    )

    reason: Reason


class SanitizeRequest(_Request):
    """``span``: an exact substring of the memory's text (every occurrence is replaced);
    ``surrogate``: what replaces it (it passes the input gate; ``""`` removes the span)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"span": "the vault code is 4471", "surrogate": "…"}]},
    )

    span: Annotated[str, StringConstraints(min_length=1, max_length=TEXT_MAX)]
    surrogate: Annotated[str, StringConstraints(max_length=SURROGATE_MAX)]


class FactRequest(_Request):
    """An author fact: #131's normalized proposition (JSON only, never prose), who may read it
    (``visibility``, default ``objective``), optional valid-time bounds replacing the
    proposition's (``valid_from``/``valid_to``, story-minutes, exact), a related fact
    (``anchored_to``) and ``locked`` (``author_locked``: no render may change it, FR-VERIFY-11)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"proposition": _EXAMPLE_PROPOSITION, "locked": True}]},
    )

    proposition: Proposition
    visibility: Visibility = Field(default_factory=Visibility)
    valid_from: int | None = None
    valid_to: int | None = None
    anchored_to: str | None = None
    locked: bool = False


class SupersedeRequest(_Request):
    """The successor of a current fact version: its proposition (same subject, predicate and
    key qualifiers, else 422 ``key_mismatch``); visibility, valid-time and the lock carry over
    unless given."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"proposition": _EXAMPLE_PROPOSITION, "valid_from": 600}]},
    )

    proposition: Proposition
    visibility: Visibility | None = None
    valid_from: int | None = None
    valid_to: int | None = None
    locked: bool | None = None


def _valid_time(valid_from: int | None, valid_to: int | None) -> Interval | None:
    if valid_from is None and valid_to is None:
        return None
    return Interval(
        start=None if valid_from is None else Bound.at(valid_from),
        end=None if valid_to is None else Bound.at(valid_to),
    )


# -- results -----------------------------------------------------------------------------------


class MemoryView(BaseModel):
    """A memory as the omniscient admin reads it, its quarantine state included."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: MemoryKind
    text: str
    provenance: RecordProvenance
    holder_character_id: str | None = None
    character_id: str | None = None
    visibility: Visibility = Field(default_factory=Visibility)
    narrative_turn: int | None = None
    beat: int | None = None
    membership: Membership
    embedded: bool = False
    quarantined: bool = False
    quarantined_at: datetime | None = None
    quarantine_reason: str | None = None
    quarantined_by: str | None = None
    implication_count: int = 0

    @classmethod
    def of(cls, memory: Memory) -> MemoryView:
        return cls(
            id=memory.id,
            kind=memory.kind,
            text=memory.text,
            provenance=memory.provenance,
            holder_character_id=memory.holder_character_id,
            character_id=memory.character_id,
            visibility=memory.visibility,
            narrative_turn=memory.narrative_turn,
            beat=memory.beat,
            membership=memory.membership,
            embedded=memory.embedded,
            quarantined=memory.quarantined_at is not None,
            quarantined_at=memory.quarantined_at,
            quarantine_reason=memory.quarantine_reason,
            quarantined_by=memory.quarantined_by,
            implication_count=memory.implication_count,
        )


@dataclass(frozen=True, slots=True)
class MemoryWrite:
    """A memory primitive's answer: the memory after it, the story's revision, and the
    ``control`` operation (``None`` for a no-op)."""

    memory: MemoryView
    timeline_revision: int
    operation_id: str | None = None
    replayed: bool = False


class FactWarning(BaseModel):
    """Something an author fact's check found that did not stop it: ``uncertain`` (the
    deterministic check could not decide; ``reason``) or ``contradicted`` (a rule the write
    did not resolve by superseding; ``rules``)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["uncertain", "contradicted"]
    reason: str | None = None
    rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FactWrite:
    """A fact call's answer: the new version, the versions it closed (``superseded``), the
    author memory it wrote (``memory_id``), the check's warnings, the story's revision and the
    ``control`` operation."""

    fact: Fact
    timeline_revision: int
    operation_id: str | None = None
    superseded: tuple[str, ...] = ()
    memory_id: str | None = None
    warnings: tuple[FactWarning, ...] = ()
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class FactPage:
    """One page of a story's canon (omniscient), ordered by id, and where the next one starts."""

    facts: tuple[Fact, ...]
    timeline_revision: int
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class Implication:
    """What ``implicate`` did: the memories it counted, the ones it quarantined (and which of
    those were author pins), and the facts it reported."""

    counted: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    pins: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecordRef:
    """A record a render's context held: ``memories`` or ``facts``, and its id."""

    table: Literal["memories", "facts"]
    id: str


# -- prechecks ---------------------------------------------------------------------------------


async def _settings(db: AsyncEngine, story_id: str) -> ProfileSettings:
    async with db.connect() as conn:
        return (await effective_configuration(conn, story_id, validate=False)).settings


async def require_episodic(db: AsyncEngine, story_id: str) -> ProfileSettings:
    """404 for an unknown story; 409 ``module_off`` unless the story's configuration has the
    ``episodic`` module on (the memory primitives' stores)."""
    await check_expected_revision(db, story_id, None)
    settings = await _settings(db, story_id)
    if not module_on(settings, EPISODIC_MODULE):
        raise ModuleOff(
            "the story's configuration has episodic memory off",
            {"story_id": story_id, "module": EPISODIC_MODULE},
        )
    return settings


async def require_canon(db: AsyncEngine, story_id: str) -> ProfileSettings:
    """404 for an unknown story; 409 ``module_off`` for a Lite story (canon exists under every
    structured profile)."""
    await check_expected_revision(db, story_id, None)
    settings = await _settings(db, story_id)
    if settings.pipeline != STRUCTURED:
        raise ModuleOff(
            "a Lite story keeps no canon; the facts calls need the structured pipeline",
            {"story_id": story_id, "module": CANON_MODULE},
        )
    return settings


async def _memory(conn: AsyncConnection, story_id: str, memory_id: str) -> Memory:
    found = await records.memories_by_id(conn, story_id, [memory_id])
    if not found:
        raise MemoryNotFound(
            f"memory {memory_id} is not one of the story's",
            {"story_id": story_id, "memory_id": memory_id},
        )
    return found[0]


async def read_memory(db: AsyncEngine, story_id: str, memory_id: str) -> Memory:
    """The story's memory ``memory_id`` (any membership); 404 when there is none."""
    async with db.connect() as conn:
        return await _memory(conn, story_id, memory_id)


def _control(
    db: AsyncEngine, story_id: str, principal: str, idempotency_key: str | None
) -> Operation:
    return Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    )


def _revision(op: Operation) -> int:
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return int(op.timeline_revision)


# -- quarantine ----------------------------------------------------------------------------------


async def _set_quarantine(
    conn: AsyncConnection,
    story_id: str,
    memory_id: str,
    *,
    reason: str | None,
    by: str | None,
) -> None:
    values: dict[str, Any] = {
        "quarantined_at": None if reason is None else func.transaction_timestamp(),
        "quarantine_reason": reason,
        "quarantined_by": by,
    }
    await conn.execute(
        update(memories)
        .where(memories.c.story_id == story_id, memories.c.id == memory_id)
        .values(**values)
    )
    row = (
        await conn.execute(
            select(memories.c.id, memories.c.story_id, memories.c.text)
            .add_columns(
                memories.c.holder_character_id,
                memories.c.character_id,
                memories.c.quarantine_reason,
            )
            .where(memories.c.id == memory_id)
        )
    ).one()
    # The reason is text: it is indexed with the memory (and erased with it, #124).
    await index_records(conn, [memory_record(row)])


async def quarantine(
    db: AsyncEngine,
    story_id: str,
    memory_id: str,
    request: QuarantineRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> MemoryWrite:
    """Quarantine memory ``memory_id`` (see the module docstring)."""
    if request.reason == REPEATED_IMPLICATION:
        raise InvalidRequest(
            f"{REPEATED_IMPLICATION} is the system's reason; give your own",
            {"field": "body.reason", "reason": "reserved_reason"},
        )
    await require_writable(db, story_id)
    await require_episodic(db, story_id)
    revision = await check_expected_revision(db, story_id, expected_revision)
    current = await read_memory(db, story_id, memory_id)
    if current.quarantined_at is not None:
        return MemoryWrite(MemoryView.of(current), revision)
    async with _control(db, story_id, principal, idempotency_key) as op:
        op.append(
            "memory_quarantined", MemoryQuarantined(memory_id=memory_id, reason=request.reason)
        )

        async def step(conn: AsyncConnection) -> None:
            await _memory(conn, story_id, memory_id)
            await _set_quarantine(conn, story_id, memory_id, reason=request.reason, by=principal)

        op.add_commit_step(step)
        await op.commit()
    QUARANTINES.labels(reason="author").inc()
    log.info("memory quarantined story=%s memory=%s", story_id, memory_id)
    return MemoryWrite(
        MemoryView.of(await read_memory(db, story_id, memory_id)), _revision(op), op.operation_id
    )


async def unquarantine(
    db: AsyncEngine,
    story_id: str,
    memory_id: str,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> MemoryWrite:
    """Reverse a quarantine (a no-op for a memory that is not quarantined)."""
    await require_writable(db, story_id)
    await require_episodic(db, story_id)
    revision = await check_expected_revision(db, story_id, expected_revision)
    current = await read_memory(db, story_id, memory_id)
    if current.quarantined_at is None:
        return MemoryWrite(MemoryView.of(current), revision)
    async with _control(db, story_id, principal, idempotency_key) as op:
        op.append("memory_unquarantined", MemoryUnquarantined(memory_id=memory_id))

        async def step(conn: AsyncConnection) -> None:
            await _memory(conn, story_id, memory_id)
            await _set_quarantine(conn, story_id, memory_id, reason=None, by=None)

        op.add_commit_step(step)
        await op.commit()
    return MemoryWrite(
        MemoryView.of(await read_memory(db, story_id, memory_id)), _revision(op), op.operation_id
    )


# -- sanitize ------------------------------------------------------------------------------------


async def sanitize(
    db: AsyncEngine,
    story_id: str,
    memory_id: str,
    request: SanitizeRequest,
    *,
    principal: str,
    gate: Any,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> MemoryWrite:
    """Replace every occurrence of ``request.span`` in the memory's text by the surrogate (see
    the module docstring). ``gate(texts)`` is the input gate (the Engine's), run over the
    surrogate before anything is written."""
    await require_writable(db, story_id)
    await require_episodic(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    current = await read_memory(db, story_id, memory_id)
    occurrences = current.text.count(request.span)
    if occurrences == 0:
        raise SpanNotFound(
            "the span does not occur in the memory's text",
            {"memory_id": memory_id, "field": "body.span"},
        )
    rewritten = current.text.replace(request.span, request.surrogate)
    if len(rewritten) > TEXT_MAX or not rewritten.strip():
        raise InvalidRequest(
            f"the sanitized text has 1 to {TEXT_MAX} characters",
            {"field": "body.surrogate", "reason": "invalid_length"},
        )
    if request.surrogate.strip():
        await gate([("surrogate", request.surrogate)])
    async with _control(db, story_id, principal, idempotency_key) as op:
        op.append(
            "sanitized",
            Sanitized(memory_id=memory_id, surrogate=request.surrogate, occurrences=occurrences),
        )
        op.append(
            "sanitize_original",
            SanitizeOriginal(memory_id=memory_id, span=request.span, text=current.text),
        )

        async def step(conn: AsyncConnection) -> None:
            found = await _memory(conn, story_id, memory_id)
            if request.span not in found.text:
                raise SpanNotFound(
                    "the span does not occur in the memory's text",
                    {"memory_id": memory_id, "field": "body.span"},
                )
            text = found.text.replace(request.span, request.surrogate)
            references = frozenset(
                r for r in (found.character_id, found.holder_character_id) if r is not None
            )
            # A raw row update is no staged event: the new text passes the guard itself.
            await guard.check_content(
                conn,
                story_id,
                [guard.Content((text,), references)],
                base_revision=op.base_revision,
                path=__name__,
            )
            await conn.execute(select(func.set_config(SANITIZE_SETTING, memory_id, True)))
            values: dict[str, Any] = {
                "text": text,
                "embedding": None,
                "embedding_model_version": None,
            }
            if found.inputs_hash is not None:
                values["inputs_hash"] = None
            await conn.execute(
                update(memories)
                .where(memories.c.story_id == story_id, memories.c.id == memory_id)
                .values(**values)
            )
            await conn.execute(select(func.set_config(SANITIZE_SETTING, "", True)))
            row = (
                await conn.execute(
                    select(
                        memories.c.id,
                        memories.c.story_id,
                        memories.c.text,
                        memories.c.holder_character_id,
                        memories.c.character_id,
                        memories.c.quarantine_reason,
                    ).where(memories.c.id == memory_id)
                )
            ).one()
            await index_records(conn, [memory_record(row)])
            await records.after_rewrite(conn, story_id, (memory_id,))

        op.add_commit_step(step)
        await op.commit()
    SANITIZES.inc()
    log.info("memory sanitized story=%s memory=%s occurrences=%d", story_id, memory_id, occurrences)
    return MemoryWrite(
        MemoryView.of(await read_memory(db, story_id, memory_id)), _revision(op), op.operation_id
    )


# -- canon ---------------------------------------------------------------------------------------


async def _check(conn: AsyncConnection, story_id: str, claim: Proposition) -> list[FactWarning]:
    """#140's deterministic check of an author claim, read through a throwaway keyed lookup:
    an ``uncertain`` verdict and a contradiction the write does not resolve are warnings."""
    lookup = KeyedLookup(conn, StructuredReadSet())
    verdict = await check_with(lookup, story_id, claim)
    if verdict.kind is VerdictKind.UNCERTAIN:
        reason = None if verdict.reason is None else verdict.reason.value
        return [FactWarning(kind="uncertain", reason=reason)]
    if verdict.kind is VerdictKind.CONTRADICTED:
        resolved = {Rule.FUNCTIONAL_KEY, Rule.POLARITY}
        others = sorted(r.value for r in verdict.rules - resolved)
        if others:
            return [FactWarning(kind="contradicted", rules=tuple(others))]
    return []


async def _author_memory(
    conn: AsyncConnection, op: Operation, fact: Fact, settings: ProfileSettings
) -> str | None:
    """The author memory of ``fact`` (its context line), when the story keeps memories."""
    from epistrel.core.resolution.context import load_index
    from epistrel.core.retrieval.pipeline import canon_text

    if not module_on(settings, EPISODIC_MODULE):
        return None
    index = await load_index(conn, fact.story_id)
    registry = await load_registry(conn, fact.story_id)
    names = {pid: p.name for pid, p in registry.predicates.items()}
    text = canon_text(fact, index, names)
    written = await records.write_author(conn, op, fact, text, settings=settings)
    return written.id if written is not None else None


async def _retire_author_memories(
    conn: AsyncConnection, story_id: str, fact_ids: Sequence[str]
) -> int:
    """Supersede the author memories of the fact versions ``fact_ids`` (a supersede's old
    version, or a version an author commit closed)."""
    if not fact_ids:
        return 0
    rows = (
        await conn.execute(
            select(facts_table.c.proposition_id, facts_table.c.source_event_ids).where(
                facts_table.c.story_id == story_id, facts_table.c.id.in_(list(fact_ids))
            )
        )
    ).all()
    sources = sorted({str(e) for r in rows for e in (r.source_event_ids or ())})
    props = sorted({str(r.proposition_id) for r in rows if r.proposition_id is not None})
    if not sources or not props:
        return 0
    m = memories.c
    linked = list(
        await conn.scalars(
            select(m.id).where(
                m.story_id == story_id,
                m.provenance == RecordProvenance.AUTHOR.value,
                m.membership == Membership.ACTIVE.value,
                m.source_event_ids.overlap(
                    bindparam("retired_sources", sources, type_=ARRAY(ulid_type()))
                ),
                m.proposition_ids.overlap(
                    bindparam("retired_props", props, type_=ARRAY(ulid_type()))
                ),
            )
        )
    )
    return await supersede_rows(conn, memories, [str(i) for i in linked], story_id=story_id)


async def commit_fact(
    db: AsyncEngine,
    story_id: str,
    request: FactRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> FactWrite:
    """Write an author fact (see the module docstring)."""
    await require_writable(db, story_id)
    settings = await require_canon(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    found: dict[str, Any] = {}
    async with _control(db, story_id, principal, idempotency_key) as op:

        async def step(conn: AsyncConnection) -> None:
            warnings = await _check(conn, story_id, request.proposition)
            fact = await canon.commit_fact(
                conn,
                story_id,
                request.proposition,
                provenance=RecordProvenance.AUTHOR,
                authority=FactAuthority.AUTHOR,
                visibility=request.visibility,
                operation=op,
                anchored_to=request.anchored_to,
                valid_time=_valid_time(request.valid_from, request.valid_to),
                author_locked=request.locked,
            )
            await _retire_author_memories(conn, story_id, fact.superseded_ids)
            found["fact"] = fact
            found["warnings"] = tuple(warnings)
            found["memory"] = await _author_memory(conn, op, fact, settings)

        op.add_commit_step(step)
        await op.commit()
    fact = found["fact"]
    log.info(
        "author fact story=%s fact=%s superseded=%d", story_id, fact.id, len(fact.superseded_ids)
    )
    return FactWrite(
        fact=fact,
        timeline_revision=_revision(op),
        operation_id=op.operation_id,
        superseded=fact.superseded_ids,
        memory_id=found["memory"],
        warnings=found["warnings"],
    )


async def supersede_fact(
    db: AsyncEngine,
    story_id: str,
    fact_id: str,
    request: SupersedeRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> FactWrite:
    """Supersede current version ``fact_id`` with ``request.proposition`` (#133's
    ``supersede``; see the module docstring)."""
    return await _supersede(
        db,
        story_id,
        fact_id,
        principal=principal,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        proposition=request.proposition,
        visibility=request.visibility,
        valid_time=_valid_time(request.valid_from, request.valid_to),
        locked=request.locked,
    )


async def set_lock(
    db: AsyncEngine,
    story_id: str,
    fact_id: str,
    *,
    locked: bool,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> FactWrite:
    """Lock (``locked``) or unlock current version ``fact_id``: a supersede of the same
    proposition with ``author_locked`` flipped. A version already in that state is answered
    unchanged (no operation)."""
    return await _supersede(
        db,
        story_id,
        fact_id,
        principal=principal,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        proposition=None,
        visibility=None,
        valid_time=None,
        locked=locked,
    )


async def _supersede(
    db: AsyncEngine,
    story_id: str,
    fact_id: str,
    *,
    principal: str,
    idempotency_key: str | None,
    expected_revision: int | None,
    proposition: Proposition | None,
    visibility: Visibility | None,
    valid_time: Interval | None,
    locked: bool | None,
) -> FactWrite:
    await require_writable(db, story_id)
    settings = await require_canon(db, story_id)
    revision = await check_expected_revision(db, story_id, expected_revision)
    if proposition is None:
        async with db.connect() as conn:
            current = await canon.read_version(conn, story_id, fact_id)
        if locked is not None and current.author_locked == locked:
            return FactWrite(fact=current, timeline_revision=revision)
    found: dict[str, Any] = {}
    async with _control(db, story_id, principal, idempotency_key) as op:

        async def step(conn: AsyncConnection) -> None:
            old = await canon.read_version(conn, story_id, fact_id)
            claim = proposition if proposition is not None else old.proposition
            warnings = await _check(conn, story_id, claim) if proposition is not None else []
            fact = await canon.supersede(
                conn,
                story_id,
                fact_id,
                claim,
                operation=op,
                visibility=visibility,
                valid_time=valid_time
                if valid_time is not None or proposition is not None
                else old.valid_time,
                author_locked=locked,
            )
            changed = proposition is not None or (
                visibility is not None and visibility != old.visibility
            )
            memory_id = None
            if changed:
                await _retire_author_memories(conn, story_id, fact.superseded_ids)
                memory_id = await _author_memory(conn, op, fact, settings)
            found.update(fact=fact, warnings=tuple(warnings), memory=memory_id)

        op.add_commit_step(step)
        await op.commit()
    fact = found["fact"]
    return FactWrite(
        fact=fact,
        timeline_revision=_revision(op),
        operation_id=op.operation_id,
        superseded=fact.superseded_ids,
        memory_id=found["memory"],
        warnings=found["warnings"],
    )


async def list_facts(
    db: AsyncEngine,
    story_id: str,
    *,
    as_of_story_time: int | None = None,
    as_of_record_time: datetime | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    include_superseded: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> FactPage:
    """The story's canon under ``omniscient`` (FR-BITEMP-3): the current versions, or every
    version in the record with ``include_superseded``; ``as_of_story_time`` keeps those true
    then, ``as_of_record_time`` reads the record as it stood then. Ordered by id; ``cursor`` (a
    page's ``next_cursor``) continues after it. 409 ``module_off`` for a Lite story."""
    await require_canon(db, story_id)
    limit = max(1, min(LIST_LIMIT_MAX, limit))
    async with db.connect() as conn:
        revision = await check_expected_revision(conn, story_id, None)
        if include_superseded:
            found = await canon.versions(
                conn, story_id, subject=subject, predicate=predicate, story_time=as_of_story_time
            )
        else:
            found = await canon.canon(
                conn,
                story_id,
                story_time=as_of_story_time,
                record_time=as_of_record_time,
                subject=subject,
                predicate=predicate,
            )
    ordered = sorted(found, key=lambda f: f.id)
    if cursor is not None:
        ordered = [f for f in ordered if f.id > cursor]
    page = ordered[:limit]
    more = len(ordered) > limit
    return FactPage(tuple(page), revision, page[-1].id if more and page else None)


# -- FR-GUARD-5: implication ------------------------------------------------------------------


async def implicate(
    db: AsyncEngine,
    story_id: str,
    refs: Sequence[RecordRef],
    *,
    turn_operation_id: str,
    attempt: int,
    threshold: int,
    pins: Sequence[str] = (),
) -> Implication:
    """Count one rejected render's implication of ``refs`` and quarantine what reaches
    ``threshold`` (see the module docstring), in one ``system`` control operation committed
    before the regeneration. ``pins`` are the memory ids among ``refs`` that are author pins
    (reported as such when quarantined)."""
    memory_ids = sorted({r.id for r in refs if r.table == "memories"})
    fact_ids = sorted({r.id for r in refs if r.table == "facts"})
    if not memory_ids and not fact_ids:
        return Implication()
    found: dict[str, Any] = {"counted": (), "quarantined": ()}
    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=SYSTEM_PRINCIPAL,
        provenance=Provenance.SYSTEM,
    ) as op:
        op.append(
            "memories_implicated",
            MemoriesImplicated(
                turn=turn_operation_id,
                attempt=attempt,
                memory_ids=tuple(memory_ids),
                fact_ids=tuple(fact_ids),
            ),
        )
        if memory_ids and threshold > 0:
            async with db.connect() as conn:
                counts = {
                    str(r.id): int(r.implication_count)
                    for r in await conn.execute(
                        select(
                            memories.c.id,
                            memories.c.implication_count,
                            memories.c.quarantined_at,
                        ).where(
                            memories.c.story_id == story_id,
                            memories.c.id.in_(memory_ids),
                            memories.c.quarantined_at.is_(None),
                        )
                    )
                }
            due = sorted(i for i, n in counts.items() if n + 1 >= threshold)
            for memory_id in due:
                op.append(
                    "memory_quarantined",
                    MemoryQuarantined(
                        memory_id=memory_id,
                        reason=REPEATED_IMPLICATION,
                        automatic=True,
                        pinned=memory_id in pins,
                    ),
                )

        async def step(conn: AsyncConnection) -> None:
            counted = list(
                await conn.scalars(
                    update(memories)
                    .where(memories.c.story_id == story_id, memories.c.id.in_(memory_ids))
                    .values(implication_count=memories.c.implication_count + 1)
                    .returning(memories.c.id, memories.c.implication_count)
                )
            )
            found["counted"] = tuple(sorted(str(i) for i in counted))
            if threshold <= 0:
                return
            due = list(
                await conn.scalars(
                    select(memories.c.id).where(
                        and_(
                            memories.c.story_id == story_id,
                            memories.c.id.in_(memory_ids),
                            memories.c.quarantined_at.is_(None),
                            memories.c.implication_count >= threshold,
                        )
                    )
                )
            )
            for memory_id in sorted(str(i) for i in due):
                await _set_quarantine(
                    conn, story_id, memory_id, reason=REPEATED_IMPLICATION, by=SYSTEM_PRINCIPAL
                )
            found["quarantined"] = tuple(sorted(str(i) for i in due))

        op.add_commit_step(step)
        await op.commit()
    quarantined: tuple[str, ...] = found["quarantined"]
    for _ in quarantined:
        QUARANTINES.labels(reason=REPEATED_IMPLICATION).inc()
    log.info(
        "implicated story=%s memories=%d facts=%d quarantined=%d",
        story_id,
        len(memory_ids),
        len(fact_ids),
        len(quarantined),
    )
    return Implication(
        counted=found["counted"],
        quarantined=quarantined,
        pins=tuple(i for i in quarantined if i in pins),
        facts=tuple(fact_ids),
        operation_id=op.operation_id,
    )


async def follow_implications(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
    """A truncation resets the implication counts its turns added, and an untruncation or swap
    that brings them back restores them (#145): every ``memories_implicated`` event naming one
    of the changed operations as its ``turn`` moves its memories' ``implication_count`` by one
    (never below zero). A quarantine stays as it is (a decision, undone by a release). A fork
    copies the counts with the rows, so its joining operations change nothing."""
    if change.cause == "fork" or not change.operation_ids:
        return
    leaving = change.new is not Membership.ACTIVE
    if leaving == (change.old is not Membership.ACTIVE):
        return
    found = await conn.execute(
        select(events.c.payload).where(
            events.c.story_id == change.story_id,
            events.c.type == "memories_implicated",
            events.c.payload["turn"].astext.in_(list(change.operation_ids)),
        )
    )
    counts: dict[str, int] = {}
    for row in found:
        for memory_id in row.payload.get("memory_ids") or ():
            counts[str(memory_id)] = counts.get(str(memory_id), 0) + 1
    for memory_id, n in sorted(counts.items()):
        delta = -n if leaving else n
        await conn.execute(
            update(memories)
            .where(memories.c.story_id == change.story_id, memories.c.id == memory_id)
            .values(implication_count=func.greatest(0, memories.c.implication_count + delta))
        )


timeline.on_membership_change(follow_implications)


__all__ = [
    "REPEATED_IMPLICATION",
    "SANITIZE_SETTING",
    "SYSTEM_PRINCIPAL",
    "FactPage",
    "FactRequest",
    "FactWarning",
    "FactWrite",
    "Implication",
    "MemoryView",
    "MemoryWrite",
    "QuarantineRequest",
    "RecordRef",
    "SanitizeRequest",
    "SupersedeRequest",
    "commit_fact",
    "follow_implications",
    "implicate",
    "list_facts",
    "quarantine",
    "read_memory",
    "require_canon",
    "require_episodic",
    "sanitize",
    "set_lock",
    "supersede_fact",
    "unquarantine",
]
