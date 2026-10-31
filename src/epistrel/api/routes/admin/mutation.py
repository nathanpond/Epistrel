"""``/admin/stories/{id}/memories`` and ``/admin/stories/{id}/facts``: the mutation primitives
and the low-level fact calls (FR-GUARD-4,5, FR-AUTH-1; #145).

Operator endpoints under ``/admin`` (C-02 M4) so a deployment can restrict them at its edge;
like every call they need the service credential and, to mutate, a principal, and any principal
may make them (Epistrel has no roles, S-TRUST-BOUNDARY M4):

- ``POST …/memories/{memory_id}/quarantine`` ``{reason}`` and ``DELETE`` (the reverse);
- ``POST …/memories/{memory_id}/sanitize`` ``{span, surrogate}``;
- ``GET …/facts`` and ``GET …/facts/{fact_id}`` (``omniscient`` only; #535) and ``POST …/facts``
  (``commit_fact``, 201);
- ``POST …/facts/{fact_id}/supersede`` (201) and ``POST|DELETE …/facts/{fact_id}/lock``.

Each write is a position-less ``control`` operation that bumps the story's timeline revision
and answers ``{timeline_revision, memory | fact, …}``; ``Idempotency-Key`` and
``X-Epistrel-Expected-Revision`` are honoured. Thin handlers over ``Engine``; the rules are in
``epistrel.core.mutation``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.canon.facts import Fact, Visibility
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.mutation import (
    DEFAULT_LIST_LIMIT,
    LIST_LIMIT_MAX,
    FactPage,
    FactRequest,
    FactWarning,
    FactWrite,
    MemoryView,
    MemoryWrite,
    QuarantineRequest,
    SanitizeRequest,
    SupersedeRequest,
)
from epistrel.core.propositions.model import Proposition

router = APIRouter(prefix="/stories")

MemoryId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The memory's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C4"],
    ),
]
FactId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The fact version's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C5"],
    ),
]

WRITE_RULES = (
    "\n\nA position-less `control` operation that bumps the story's timeline revision, so a "
    "turn generating meanwhile revalidates before it commits. Any principal may call it. "
    "`Idempotency-Key` and `X-Epistrel-Expected-Revision` are optional."
)
MEMORY_RULES = (
    " 404 for an unknown memory; 409 `module_off` when the story's configuration keeps no "
    "episodic memory (a Lite story)."
)
FACT_RULES = " 409 `module_off` for a Lite story (canon exists under every structured profile)."


class MemoryWritten(MutationResponse):
    """The memory after the call (its quarantine state included), the story's revision and the
    ``control`` operation (``null`` when nothing changed)."""

    memory: MemoryView
    operation_id: str | None

    @classmethod
    def of(cls, written: MemoryWrite) -> MemoryWritten:
        return cls(
            memory=written.memory,
            operation_id=written.operation_id,
            timeline_revision=written.timeline_revision,
        )


class FactBody(BaseModel):
    """One fact version: its proposition, valid-time (story-minutes; outer and inner bounds),
    transaction-time, provenance, authority, lock, visibility, lineage and what it closed."""

    id: str
    lineage_id: str
    proposition_id: str
    proposition: Proposition
    valid_from: int | None
    valid_to: int | None
    valid_from_inner: int | None
    valid_to_inner: int | None
    tx_from: datetime
    tx_to: datetime | None
    provenance: str
    authority: str
    author_locked: bool
    visibility: Visibility
    membership: str
    narrative_turn: int | None
    superseded_by: str | None
    supersedes: list[str]
    anchored_to: str | None

    @classmethod
    def of(cls, fact: Fact) -> FactBody:
        return cls(
            id=fact.id,
            lineage_id=fact.lineage_id,
            proposition_id=fact.proposition_id,
            proposition=fact.proposition,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            valid_from_inner=fact.valid_from_inner,
            valid_to_inner=fact.valid_to_inner,
            tx_from=fact.tx_from,
            tx_to=fact.tx_to,
            provenance=fact.provenance.value,
            authority=fact.authority.value,
            author_locked=fact.author_locked,
            visibility=fact.visibility,
            membership=fact.membership.value,
            narrative_turn=fact.narrative_turn,
            superseded_by=fact.superseded_by,
            supersedes=list(fact.supersedes),
            anchored_to=fact.anchored_to,
        )


class FactWritten(MutationResponse):
    """The new fact version, the versions it closed (``superseded``), the author memory it
    wrote (``memory_id``, ``null`` when the story keeps no episodic memory or the call changed
    nothing it shows), the deterministic check's warnings, the story's revision and the
    ``control`` operation (``null`` when nothing changed)."""

    fact: FactBody
    superseded: list[str]
    memory_id: str | None
    warnings: list[FactWarning]
    operation_id: str | None

    @classmethod
    def of(cls, written: FactWrite) -> FactWritten:
        return cls(
            fact=FactBody.of(written.fact),
            superseded=list(written.superseded),
            memory_id=written.memory_id,
            warnings=list(written.warnings),
            operation_id=written.operation_id,
            timeline_revision=written.timeline_revision,
        )


class FactListBody(BaseModel):
    """A page of the story's canon, by id, the story's revision, and the cursor of the next
    page (``null`` on the last)."""

    facts: list[FactBody]
    timeline_revision: int
    next_cursor: str | None

    @classmethod
    def of(cls, page: FactPage) -> FactListBody:
        return cls(
            facts=[FactBody.of(f) for f in page.facts],
            timeline_revision=page.timeline_revision,
            next_cursor=page.next_cursor,
        )


def _memory(written: MemoryWrite, ctx: Any, response: Response) -> MemoryWritten:
    return answered(MemoryWritten.of(written), ctx, response, replayed=written.replayed)


def _created(written: FactWrite, ctx: Any, response: Response) -> JSONResponse:
    body = answered(FactWritten.of(written), ctx, response, replayed=written.replayed)
    return JSONResponse(
        body.model_dump(mode="json"), status_code=201, headers=dict(response.headers)
    )


@router.post(
    "/{story_id}/memories/{memory_id}/quarantine",
    response_model=MemoryWritten,
    operation_id="quarantine_memory",
    summary="Quarantine a memory",
    description=(
        "Exclude the memory from retrieval and from every generation context (FR-GUARD-4): it "
        "is never retrieved, never a consolidation input, and never in a structured render's "
        "context again until the quarantine is reversed. History and working memory (the "
        "story's events) are untouched, and the memory keeps its vector. `reason` (1 to 500 "
        "characters) is recorded; `repeated_implication` is reserved for the output gate's "
        "own quarantines (FR-GUARD-5). A quarantined memory answers unchanged."
        + MEMORY_RULES
        + WRITE_RULES
    ),
)
async def quarantine_memory(
    story_id: StoryId,
    memory_id: MemoryId,
    body: QuarantineRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> MemoryWritten:
    written = await engine.quarantine_memory(ctx, story_id, memory_id, body)
    return _memory(written, ctx, response)


@router.delete(
    "/{story_id}/memories/{memory_id}/quarantine",
    response_model=MemoryWritten,
    operation_id="unquarantine_memory",
    summary="Reverse a memory's quarantine",
    description=(
        "Make a quarantined memory retrievable again. A memory that is not quarantined answers "
        "unchanged." + MEMORY_RULES + WRITE_RULES
    ),
)
async def unquarantine_memory(
    story_id: StoryId,
    memory_id: MemoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> MemoryWritten:
    written = await engine.unquarantine_memory(ctx, story_id, memory_id)
    return _memory(written, ctx, response)


@router.post(
    "/{story_id}/memories/{memory_id}/sanitize",
    response_model=MemoryWritten,
    operation_id="sanitize_memory",
    summary="Replace a span of a memory's text with a surrogate",
    description=(
        "Replace every occurrence of `span` (an exact substring) in the memory's text by "
        "`surrogate` (FR-GUARD-4). The surrogate passes the input gate first (422 "
        "`guardrail_violation`). The operation records a `sanitized` event with the "
        "surrogate. The span and the old text are kept only in a `sanitize_original` audit "
        "record. The memory is re-indexed and re-embedded, and its history beat is "
        "unchanged. 422 `span_not_found` when the span is not in the text."
        + MEMORY_RULES
        + WRITE_RULES
    ),
)
async def sanitize_memory(
    story_id: StoryId,
    memory_id: MemoryId,
    body: SanitizeRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> MemoryWritten:
    written = await engine.sanitize_memory(ctx, story_id, memory_id, body)
    return _memory(written, ctx, response)


@router.get(
    "/{story_id}/facts",
    response_model=FactListBody,
    operation_id="list_facts",
    summary="List the story's canon",
    description=(
        "The story's canon facts (FR-BITEMP-3), `omniscient` only (403 `omniscient_required` "
        "otherwise): the current versions, or every version with `include_superseded`. "
        "`as_of_story_time` (story-minutes) keeps those true then, `as_of_record_time` reads "
        "the record as it stood then, `subject` and `predicate` filter. Paged by id: `limit` "
        f"(1 to {LIST_LIMIT_MAX}, default {DEFAULT_LIST_LIMIT}) and `cursor` (a page's "
        "`next_cursor`)." + FACT_RULES
    ),
)
async def list_facts(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    as_of_story_time: int | None = None,
    as_of_record_time: datetime | None = None,
    subject: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    predicate: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    include_superseded: bool = False,
    limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = DEFAULT_LIST_LIMIT,
    cursor: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
) -> FactListBody:
    page = await engine.list_facts(
        ctx,
        story_id,
        viewpoint=viewpoint,
        as_of_story_time=as_of_story_time,
        as_of_record_time=as_of_record_time,
        subject=subject,
        predicate=predicate,
        include_superseded=include_superseded,
        limit=limit,
        cursor=cursor,
    )
    return FactListBody.of(page)


@router.get(
    "/{story_id}/facts/{fact_id}",
    response_model=FactBody,
    operation_id="get_fact",
    summary="Read one fact version",
    description=(
        "One version of the story's canon by id (#535), `omniscient` only (403 "
        "`omniscient_required` otherwise): any version, current or superseded (`tx_to`, "
        "`superseded_by`), its `membership` saying whether it is on the active timeline. 404 "
        "`fact_not_found` for an id that is no version of this story's, another story's "
        "included." + FACT_RULES
    ),
)
async def get_fact(
    story_id: StoryId,
    fact_id: FactId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> FactBody:
    return FactBody.of(await engine.get_fact(ctx, story_id, fact_id, viewpoint=viewpoint))


@router.post(
    "/{story_id}/facts",
    response_model=FactWritten,
    status_code=201,
    operation_id="commit_fact",
    summary="Commit an author fact",
    description=(
        "Write #131's normalized `proposition` (JSON, never prose) as canon with `provenance = "
        "authority = author` (FR-GUARD-4's canonize; FR-BITEMP-1). Optional: `visibility` "
        "(default `objective`), `valid_from`/`valid_to` (story-minutes, replacing the "
        "proposition's bounds), `anchored_to` (a related fact) and `locked` (`author_locked`: "
        "no render may change it). #140's deterministic check runs first. A current fact of the "
        "same functional key, or of the opposite polarity, is superseded (the author wins; "
        "`superseded` lists them), and an `uncertain` verdict or another contradiction is a "
        "warning. The fact is position-less (a truncation never removes it). In a story that "
        "keeps episodic memory it is also written as an author memory, so retrieval finds it "
        "(`memory_id`)." + FACT_RULES + WRITE_RULES
    ),
)
async def commit_fact(
    story_id: StoryId,
    body: FactRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> JSONResponse:
    return _created(await engine.commit_fact(ctx, story_id, body), ctx, response)


@router.post(
    "/{story_id}/facts/{fact_id}/supersede",
    response_model=FactWritten,
    status_code=201,
    operation_id="supersede_fact",
    summary="Supersede a fact version",
    description=(
        "Close current version `fact_id` in transaction-time and write `proposition` as its "
        "successor in the same lineage (FR-BITEMP-2: nothing is deleted). Provenance, "
        "authority, visibility, valid-time and the lock carry over unless given. 404 "
        "`fact_not_found`; 409 `fact_superseded` (supersede the lineage's current version) or "
        "`fact_inactive`; 422 `key_mismatch` when the subject, predicate or key qualifiers "
        "change." + FACT_RULES + WRITE_RULES
    ),
)
async def supersede_fact(
    story_id: StoryId,
    fact_id: FactId,
    body: SupersedeRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> JSONResponse:
    return _created(await engine.supersede_fact(ctx, story_id, fact_id, body), ctx, response)


@router.post(
    "/{story_id}/facts/{fact_id}/lock",
    response_model=FactWritten,
    operation_id="lock_fact",
    summary="Lock a fact version",
    description=(
        "Set `author_locked` on current version `fact_id` (FR-VERIFY-11: a render may not "
        "change locked canon): canon is append-only, so this writes a successor of the same "
        "proposition with the lock set. A locked version answers unchanged (no operation)."
        + FACT_RULES
        + WRITE_RULES
    ),
)
async def lock_fact(
    story_id: StoryId,
    fact_id: FactId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> FactWritten:
    written = await engine.lock_fact(ctx, story_id, fact_id)
    return answered(FactWritten.of(written), ctx, response, replayed=written.replayed)


@router.delete(
    "/{story_id}/facts/{fact_id}/lock",
    response_model=FactWritten,
    operation_id="unlock_fact",
    summary="Unlock a fact version",
    description=(
        "Clear `author_locked` on current version `fact_id` (a successor of the same "
        "proposition, as for the lock). An unlocked version answers unchanged."
        + FACT_RULES
        + WRITE_RULES
    ),
)
async def unlock_fact(
    story_id: StoryId,
    fact_id: FactId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> FactWritten:
    written = await engine.unlock_fact(ctx, story_id, fact_id)
    return answered(FactWritten.of(written), ctx, response, replayed=written.replayed)
