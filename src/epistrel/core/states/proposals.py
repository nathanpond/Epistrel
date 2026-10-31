"""Engine-proposed States (FR-STATE-3's proposal half; docs/02 §7.1; #192).

The engine may **propose** a tracked value for the author's approval, but never creates one.
Proposals come from an on-demand analysis of the whole story history (owner decision): a
background job (``orchestrator.jobs.handlers.state_proposals``), with no model call and nothing
in the respond path; no turn ever writes one.

- ``request_analysis(db, story_id, principal=)`` records the request as a ``maintenance``
  operation (no revision moves) that enqueues the ``state_proposal_analysis`` job (#70) in its
  commit, and answers both ids (REST 202 ``{job_id, operation_id}``).
- ``scan(conn, story_id)`` is the analysis, deterministic and read-only. It reads the story's
  active ``beat_*`` events (``core.events.types.StructuredBeat``): each declared proposition
  with a ``quantity`` literal (keyed by its normalized unit: casefolded, as #148's unit table
  lemmatizes it) or a ``holds``/``owns`` proposition about an object entity (#139-resolved; keyed
  by that entity; a character is not a resource), and each ``detected`` claim of a ``quantity``,
  ``possession``, ``transfer`` or ``consumption`` category, whose span of ``text`` it re-parses
  with #148's quantity parser (``core.verify.detect.parse_quantities``; a ``DetectedRef`` keeps
  only its span). A key recurring in at least ``MIN_TURNS`` distinct turns that no active State
  matches (by name, alias, unit or ``entity_id``) is a ``Candidate``, with up to
  ``EVIDENCE_MAX`` pieces of evidence (``{turn, event_id, span, text}``, the text quoting the
  beat).
- ``write(conn, op, story_id, candidates, request_operation_id)`` stores them in the job's
  commit: ``state_proposals`` is unique on ``(story_id, name)``, so a second run writes no
  duplicate and a dismissed proposal is never proposed again. Definitions are matched again
  there, under the story lock.
- ``list_proposals``, ``approve`` and ``dismiss`` are the review. ``approve`` declares the State
  through ``core.states.define_state`` (``created_by = proposal``) in an inter-turn slot
  operation (#193's ``open_inter_turn``; the declaration's rules as #186's ``POST /states``)
  and marks the proposal ``approved`` in the same commit; ``dismiss`` marks it ``dismissed``.
  Only a ``proposed`` proposal can be decided (404 ``not_found``, ``reason:
  no_pending_proposal``). Until approval nothing is tracked: no definition and no value.

Every route is gated by ``states.level`` (409 ``module_off``). A proposal's texts (its name,
unit and evidence) are indexed for erasure as it is written, after the resurrection guard;
erasure redacts them in place (``core.erasure.erase``). This module logs nothing (invariant 3).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import states
from epistrel.core.enums import (
    OperationKind,
    Provenance,
    StateCreatedBy,
    StateProposalStatus,
    StateScope,
    StateType,
)
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import state_proposal_record
from epistrel.core.errors import InvalidRequest, StoryNotFound
from epistrel.core.events.operation import Operation
from epistrel.core.events.types import BEAT_EVENT_PREFIX
from epistrel.core.events.types import StateDefined as StateDefinedEvent
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_operation
from epistrel.core.propositions.vocabulary import HOLDS, OWNS
from epistrel.core.states import api
from epistrel.core.states.api import JsonScalar, StateConstraintsBody, StateDeclaration
from epistrel.core.states.model import NAME_PATTERN, StateDefinition
from epistrel.core.tables import entities, events, operations, stories
from epistrel.core.tables.states import state_proposals
from epistrel.core.timeline import boundary_label
from epistrel.core.verify.detect import parse_quantities, tables
from epistrel.core.world_clock.slots import open_inter_turn, slot_position

ANALYSIS_KIND = "state_proposal_analysis"
"""The job kind of the history analysis (#70's queue)."""
MIN_TURNS = 3
"""A key is proposed once it recurs in this many distinct turns."""
EVIDENCE_MAX = 10
"""The most evidence a proposal keeps (the earliest)."""
DETECTED_CATEGORIES: frozenset[str] = frozenset(
    {"quantity", "possession", "transfer", "consumption"}
)
"""The ``DetectedRef`` categories whose span may name an amount."""
SOURCE = "proposal"
"""The ``state_defined`` event's ``source`` of an approval."""
_POSSESSION = frozenset({HOLDS, OWNS})
_NOT_NAME = re.compile(r"[^A-Za-z0-9_]+")


class StateDefinitionOverrides(BaseModel):
    """What an approval changes of a proposal's suggested declaration: any member of a
    ``POST /stories/{id}/states`` definition but its owner (a proposed State is the story's)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    type: Literal["number", "boolean", "enum", "text"] | None = None
    scope: Literal["story", "character"] | None = None
    constraints: StateConstraintsBody | None = None
    initial_value: JsonScalar | None = None
    aliases: list[str] | None = None
    unit: str | None = None
    entity_id: str | None = None
    visibility: Literal["player_visible", "hidden"] | None = None
    silent: StrictBool | None = None
    rules: dict[str, Any] | None = None
    authorization: dict[str, Any] | None = None
    transfer: dict[str, Any] | None = None
    reservation_expiry_minutes: StrictInt | None = None


class StateProposalApproveRequest(BaseModel):
    """``POST /stories/{id}/state-proposals/{pid}/approve``: the ``definition`` members that
    override the proposal's suggestions (none: declare it as suggested)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"definition": {"initial_value": 20, "constraints": {"min": 0}}}]
        },
    )

    definition: StateDefinitionOverrides = Field(default_factory=StateDefinitionOverrides)


class NotPending(StoryNotFound):
    """The story has no ``proposed`` proposal of that id (404 ``not_found``, ``reason:
    no_pending_proposal``): unknown, approved or dismissed."""


class AnalysisPayload(BaseModel):
    """The ``state_proposal_analysis`` job's payload: the story and the request's operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class AnalysisQueued:
    """An analysis handed to a worker (REST 202): its job, the request's ``maintenance``
    operation (each proposal it writes names it), and the story's revision (unchanged)."""

    job_id: str
    operation_id: str
    timeline_revision: int = 0
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Where a key recurs: the beat's turn and event, the span (``None``: a declared
    proposition, which has none) and the text it quotes."""

    turn: int
    event_id: str
    span: tuple[int, int] | None
    text: str

    def document(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "event_id": self.event_id,
            "span": None if self.span is None else list(self.span),
            "text": self.text,
        }


@dataclass(slots=True)
class Candidate:
    """A recurring key with no State: the suggested name, type, scope, unit or entity, and
    its evidence."""

    name: str
    suggested_unit: str | None = None
    suggested_entity_id: str | None = None
    turns: set[int] = field(default_factory=set)
    evidence: list[Evidence] = field(default_factory=list)
    suggested_type: StateType = StateType.NUMBER
    suggested_scope: StateScope = StateScope.CHARACTER


@dataclass(frozen=True, slots=True)
class ProposalView:
    """One proposal as the review reads it."""

    id: str
    name: str
    suggested_type: str
    suggested_scope: str
    suggested_unit: str | None
    suggested_entity_id: str | None
    evidence: list[dict[str, Any]]
    status: str
    created_by_operation_id: str
    created_at: datetime
    decided_at: datetime | None
    definition_id: str | None


@dataclass(frozen=True, slots=True)
class ProposalList:
    """``GET /stories/{id}/state-proposals``: the story's proposals, oldest first."""

    proposals: list[ProposalView]


@dataclass(frozen=True, slots=True)
class ProposalDismissed:
    """A dismissed proposal (never proposed again), and the story's revision (unchanged)."""

    proposal: ProposalView
    timeline_revision: int = 0
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class ProposalApproved:
    """An approved proposal: the definition it declared, the declaration's slot, its operation
    and the story's revision after it."""

    proposal: ProposalView
    definition: dict[str, Any]
    position: str
    slot: dict[str, int]
    operation_id: str
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- keys --------------------------------------------------------------------------------------


def normalized_unit(unit: str) -> str:
    """A unit as the analysis keys it: casefolded, whitespace collapsed, and #148's lemma
    (``coins`` → ``coin``)."""
    found = " ".join(unit.casefold().split())
    lemmas: Mapping[str, Any] = tables().quantity["unit_lemmas"]
    return str(lemmas.get(found, found))


def state_name(text: str) -> str | None:
    """``text`` as a State name (letters, digits and ``_``, starting with a letter), or
    ``None``."""
    found = _NOT_NAME.sub("_", text.strip()).strip("_").lower()[:80]
    return found if found and NAME_PATTERN.match(found) else None


def _matches(definition: StateDefinition, words: set[str], entity_id: str | None) -> bool:
    if entity_id is not None and definition.entity_id == entity_id:
        return True
    names = {definition.name.casefold(), definition.name.replace("_", " ").casefold()}
    names |= {normalized_unit(a) for a in definition.aliases}
    if definition.unit:
        names.add(normalized_unit(definition.unit))
    names |= {normalized_unit(n) for n in tuple(names)}
    return bool(names & words)


def _tracked(candidate: Candidate, defined: Sequence[StateDefinition]) -> bool:
    words = {candidate.name.casefold(), candidate.name.replace("_", " ").casefold()}
    if candidate.suggested_unit:
        words.add(normalized_unit(candidate.suggested_unit))
    return any(_matches(d, words, candidate.suggested_entity_id) for d in defined)


# -- the analysis ------------------------------------------------------------------------------


@dataclass(slots=True)
class _Scan:
    candidates: dict[tuple[str, str], Candidate] = field(default_factory=dict)

    def hit(
        self,
        key: tuple[str, str],
        name: str,
        evidence: Evidence,
        *,
        unit: str | None = None,
        entity_id: str | None = None,
    ) -> None:
        found = self.candidates.get(key)
        if found is None:
            found = Candidate(name=name, suggested_unit=unit, suggested_entity_id=entity_id)
            self.candidates[key] = found
        found.turns.add(evidence.turn)
        if len(found.evidence) < EVIDENCE_MAX:
            found.evidence.append(evidence)


def _quantity(scan: _Scan, unit: str, evidence: Evidence) -> None:
    key = normalized_unit(unit)
    name = state_name(key)
    if name is not None:
        scan.hit(("unit", key), name, evidence, unit=key)


async def scan(conn: AsyncConnection, story_id: str) -> list[Candidate]:
    """The analysis (see the module docstring): the keys recurring in ``MIN_TURNS`` distinct
    turns that no active State matches, by name."""
    e, o = events.c, operations.c
    rows = await conn.execute(
        select(e.id, e.narrative_turn, e.payload)
        .select_from(events.join(operations, o.operation_id == e.operation_id))
        .where(
            e.story_id == story_id,
            e.type.startswith(BEAT_EVENT_PREFIX),
            e.narrative_turn.is_not(None),
            e.redacted_at.is_(None),
            active_operation(operations),
        )
        .order_by(e.seq)
    )
    found = _Scan()
    objects: dict[str, tuple[int, str, str]] = {}
    for row in rows:
        payload: Mapping[str, Any] = row.payload or {}
        text = str(payload.get("text") or "")
        turn, event_id = int(row.narrative_turn), str(row.id)
        for declared in payload.get("propositions") or ():
            proposition = declared.get("proposition") or {}
            literal = proposition.get("literal") or {}
            if literal.get("kind") == "quantity" and literal.get("unit"):
                quoted = f"{literal.get('value')} {literal['unit']}"
                _quantity(found, str(literal["unit"]), Evidence(turn, event_id, None, quoted))
            elif (
                proposition.get("predicate_id") in _POSSESSION
                and proposition.get("object_id")
                and proposition.get("polarity", "affirmed") == "affirmed"
            ):
                objects.setdefault(str(proposition["object_id"]), (turn, event_id, text))
                key = ("entity", str(proposition["object_id"]))
                found.hit(
                    key,
                    key[1],
                    Evidence(turn, event_id, None, text[:200]),
                    entity_id=key[1],
                )
        for ref in payload.get("detected") or ():
            if ref.get("category") not in DETECTED_CATEGORIES:
                continue
            start, end = int(ref.get("start", 0)), int(ref.get("end", 0))
            span_text = text[start:end]
            for literal_found in parse_quantities(span_text):
                if literal_found.unit:
                    _quantity(
                        found,
                        literal_found.unit,
                        Evidence(turn, event_id, (start, end), span_text),
                    )
    await _name_entities(conn, story_id, found)
    defined = await states.definitions(conn, story_id)
    return [
        c
        for c in found.candidates.values()
        if c.name and len(c.turns) >= MIN_TURNS and not _tracked(c, defined)
    ]


async def _name_entities(conn: AsyncConnection, story_id: str, found: _Scan) -> None:
    """An entity key's name is its entity's canonical name; a character, or an entity that
    is not the story's, proposes nothing."""
    wanted = sorted(k[1] for k in found.candidates if k[0] == "entity")
    if not wanted:
        return
    rows = await conn.execute(
        select(entities.c.id, entities.c.canonical_name, entities.c.character_id).where(
            entities.c.story_id == story_id, entities.c.id.in_(wanted)
        )
    )
    known = {str(r.id): r for r in rows}
    for entity_id in wanted:
        row = known.get(entity_id)
        name = None if row is None or row.character_id else state_name(str(row.canonical_name))
        if name is None:
            del found.candidates[("entity", entity_id)]
        else:
            found.candidates[("entity", entity_id)].name = name


def _texts(candidate: Candidate) -> tuple[str, ...]:
    texts = [candidate.name, *(e.text for e in candidate.evidence)]
    if candidate.suggested_unit:
        texts.append(candidate.suggested_unit)
    return tuple(texts)


async def write(
    conn: AsyncConnection,
    op: Operation,
    story_id: str,
    candidates: Sequence[Candidate],
    request_operation_id: str,
) -> list[str]:
    """Store ``candidates`` in ``op``'s commit transaction (see the module docstring); answers
    the ids of the proposals it wrote."""
    defined = await states.definitions(conn, story_id)
    wanted = [c for c in sorted(candidates, key=lambda c: c.name) if not _tracked(c, defined)]
    if not wanted:
        return []
    await guard.check_content(
        conn,
        story_id,
        [guard.Content(_texts(c), frozenset(), False) for c in wanted],
        base_revision=op.base_revision,
        path=__name__,
    )
    written: list[str] = []
    for candidate in wanted:
        row = {
            "id": new_ulid(),
            "story_id": story_id,
            "name": candidate.name,
            "suggested_type": candidate.suggested_type.value,
            "suggested_scope": candidate.suggested_scope.value,
            "suggested_unit": candidate.suggested_unit,
            "suggested_entity_id": candidate.suggested_entity_id,
            "evidence": [e.document() for e in candidate.evidence],
            "created_by_operation_id": request_operation_id,
        }
        stored = await conn.scalar(
            insert(state_proposals)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["story_id", "name"])
            .returning(state_proposals.c.id)
        )
        if stored is None:
            continue
        written.append(str(stored))
        await index_records(conn, [state_proposal_record(_Row(row))])
    return written


class _Row:
    """A row mapping read as attributes (what the erasure record builders take)."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.__dict__.update(values)


# -- the request -----------------------------------------------------------------------------


async def _revision(conn: AsyncConnection, story_id: str) -> int:
    found = await conn.scalar(select(stories.c.timeline_revision).where(stories.c.id == story_id))
    if found is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return int(found)


StartJob = Callable[[AsyncConnection, AnalysisPayload], Awaitable[str]]
"""Enqueue the analysis job in the caller's transaction and return its id (the Engine's, which
owns the Orchestrator's job queue)."""


async def request_analysis(
    db: AsyncEngine, story_id: str, *, principal: str, start_job: StartJob
) -> AnalysisQueued:
    """Record the request and enqueue the analysis (see the module docstring); 409
    ``module_off`` with ``states.level`` off."""
    from epistrel.core.stories import require_writable  # here: stories declares States

    await require_writable(db, story_id)
    async with db.connect() as conn:
        await states.require_module(conn, story_id)
        revision = await _revision(conn, story_id)
    queued: list[str] = []
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.MAINTENANCE,
        principal=principal,
        provenance=Provenance.CONSUMER,
    )

    async def enqueue(conn: AsyncConnection) -> None:
        await states.require_module(conn, story_id)
        payload = AnalysisPayload(story_id=story_id, operation_id=op.operation_id)
        queued.append(await start_job(conn, payload))

    async with op:
        op.add_commit_step(enqueue)
        await op.commit()
    return AnalysisQueued(
        job_id=queued[0], operation_id=op.operation_id, timeline_revision=revision
    )


# -- the review --------------------------------------------------------------------------------


def _view(row: Any) -> ProposalView:
    return ProposalView(
        id=str(row.id),
        name=str(row.name),
        suggested_type=str(getattr(row.suggested_type, "value", row.suggested_type)),
        suggested_scope=str(getattr(row.suggested_scope, "value", row.suggested_scope)),
        suggested_unit=row.suggested_unit,
        suggested_entity_id=None
        if row.suggested_entity_id is None
        else str(row.suggested_entity_id),
        evidence=list(row.evidence or ()),
        status=str(getattr(row.status, "value", row.status)),
        created_by_operation_id=str(row.created_by_operation_id),
        created_at=row.created_at,
        decided_at=row.decided_at,
        definition_id=None if row.definition_id is None else str(row.definition_id),
    )


async def list_proposals(db: AsyncEngine, story_id: str) -> ProposalList:
    """The story's proposals, oldest first; 409 ``module_off`` with ``states.level`` off."""
    async with db.connect() as conn:
        await _revision(conn, story_id)
        await states.require_module(conn, story_id)
        p = state_proposals.c
        rows = await conn.execute(
            select(state_proposals).where(p.story_id == story_id).order_by(p.created_at, p.id)
        )
        return ProposalList([_view(r) for r in rows])


async def _pending(
    conn: AsyncConnection, story_id: str, proposal_id: str, *, lock: bool = False
) -> Any:
    p = state_proposals.c
    query = select(state_proposals).where(
        p.story_id == story_id,
        p.id == proposal_id,
        p.status == StateProposalStatus.PROPOSED.value,
    )
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise NotPending(
            f"the story has no pending State proposal {proposal_id}",
            {"story_id": story_id, "proposal_id": proposal_id, "reason": "no_pending_proposal"},
        )
    return row


async def _decide(
    conn: AsyncConnection,
    story_id: str,
    proposal_id: str,
    status: StateProposalStatus,
    principal: str,
    definition_id: str | None = None,
) -> ProposalView:
    p = state_proposals.c
    row = (
        await conn.execute(
            update(state_proposals)
            .where(p.id == proposal_id, p.story_id == story_id)
            .values(
                status=status.value,
                decided_at=func.transaction_timestamp(),
                decided_by=principal,
                definition_id=definition_id,
            )
            .returning(state_proposals)
        )
    ).one()
    return _view(row)


async def dismiss(
    db: AsyncEngine, story_id: str, proposal_id: str, *, principal: str
) -> ProposalDismissed:
    """Dismiss a pending proposal: it is never proposed again (see the module docstring)."""
    from epistrel.core.stories import require_writable  # here: stories declares States

    await require_writable(db, story_id)
    async with db.begin() as conn:
        await states.require_module(conn, story_id)
        revision = await _revision(conn, story_id)
        await _pending(conn, story_id, proposal_id, lock=True)
        view = await _decide(conn, story_id, proposal_id, StateProposalStatus.DISMISSED, principal)
    return ProposalDismissed(view, revision)


def declaration(row: Any, overrides: StateDefinitionOverrides) -> dict[str, Any]:
    """The State a proposal declares: its suggestions, with ``overrides`` on top, declared by
    a ``proposal``."""
    suggested: dict[str, Any] = {
        "name": str(row.name),
        "type": str(getattr(row.suggested_type, "value", row.suggested_type)),
        "scope": str(getattr(row.suggested_scope, "value", row.suggested_scope)),
    }
    if row.suggested_unit:
        suggested["unit"] = row.suggested_unit
    if row.suggested_entity_id:
        suggested["entity_id"] = str(row.suggested_entity_id)
    try:
        model = StateDeclaration.model_validate(
            {**suggested, **overrides.model_dump(exclude_unset=True)}
        )
    except ValueError as exc:
        errors = getattr(exc, "errors", None)
        if callable(errors):
            raise InvalidRequest.from_errors(errors()) from None
        raise
    found = model.definition()
    found["created_by"] = StateCreatedBy.PROPOSAL.value
    return found


async def approve(
    db: AsyncEngine,
    story_id: str,
    proposal_id: str,
    request: StateProposalApproveRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> ProposalApproved:
    """Approve a pending proposal: declare its State, as ``POST /states`` declares one, and
    mark it ``approved`` in the same commit (see the module docstring)."""
    from epistrel.core.stories import require_writable  # here: stories declares States

    await require_writable(db, story_id)
    async with db.connect() as conn:
        await states.require_module(conn, story_id)
        row = await _pending(conn, story_id, proposal_id)
        declared = declaration(row, request.definition)
        await api._check_declarable(conn, story_id, declared)
    stored: list[StateDefinition] = []
    decided: list[ProposalView] = []

    async def write_definition(conn: AsyncConnection) -> None:
        await _pending(conn, story_id, proposal_id, lock=True)
        definition = await states.define_state(conn, op, story_id, declared)
        stored.append(definition)
        decided.append(
            await _decide(
                conn,
                story_id,
                proposal_id,
                StateProposalStatus.APPROVED,
                principal,
                definition.id,
            )
        )

    operation_id = new_ulid()
    async with api.turn_slot(db, story_id, operation_id):
        op = open_inter_turn(
            db,
            story_id,
            source=Provenance.CONSUMER,
            principal=principal,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
        )
        op.holds_turn_slot = True
        async with op:
            op.append(
                "state_defined", StateDefinedEvent(source=SOURCE), provenance=Provenance.CONSUMER
            )
            op.add_commit_step(write_definition)
            await op.commit()
    position = slot_position(op)
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return ProposalApproved(
        proposal=decided[0],
        definition=stored[0].document(),
        position=boundary_label(position.turn, position.slot_k),
        slot=position.document(),
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
    )


__all__ = [
    "ANALYSIS_KIND",
    "DETECTED_CATEGORIES",
    "EVIDENCE_MAX",
    "MIN_TURNS",
    "AnalysisPayload",
    "AnalysisQueued",
    "Candidate",
    "Evidence",
    "NotPending",
    "ProposalApproved",
    "ProposalDismissed",
    "ProposalList",
    "ProposalView",
    "StartJob",
    "StateDefinitionOverrides",
    "StateProposalApproveRequest",
    "approve",
    "declaration",
    "dismiss",
    "list_proposals",
    "normalized_unit",
    "request_analysis",
    "scan",
    "state_name",
    "write",
]
