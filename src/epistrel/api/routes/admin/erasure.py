"""``POST /admin/erasure``: erase a subject within a story or from the catalog (FR-ADMIN-2,6,
NFR-PRIV-2; #124). The erasure ledger's reconciliation and the erasure boundary (FR-ADMIN-7,8;
#125): ``GET /admin/erasure/status``, ``POST /admin/erasure/reconcile`` and
``GET /admin/erasure/boundary``.

One handler, one ``Engine.erase`` call. The answer is the same model for a dry run and a real
run: the counts (``records``, ``prose_segments``, ``media``, ``idempotency_results``), the
regeneration mode and any degradation, the stories that reference a catalog subject, and for a
real run the ledger entry, the operation and the residue the scan found. It carries the
deployment's ``config_revision`` (the key is deployment-scoped) and, for the story scope, the
story's ``timeline_revision`` after the erasure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep, route_flags
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.erasure.boundary import ErasureBoundary, render_boundary
from epistrel.core.erasure.erase import ErasureRequest, ErasureResult
from epistrel.core.erasure.ledger import ReconcileRun, ReconcileStatus

router = APIRouter(prefix="/erasure")


class ReferencingStoryBody(BaseModel):
    """A story that references the erased catalog subject, left untouched (FR-STORE-14): erase
    the subject there separately (``scope: {story: <id>}``)."""

    model_config = ConfigDict(extra="forbid")

    story_id: str
    character_ids: list[str]


class CandidateBody(BaseModel):
    """A mention the erasure redacts only when confirmed (ADR-154): a common word of the
    subject's name capitalised at the start of a sentence. The snippet is returned by the dry
    run only and never stored."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(description="Pass it in `confirm_candidates` to erase it too.")
    record_table: str
    record_id: str
    field: str = Field(description="The text's path in the record (dotted, list items `[n]`).")
    snippet: str = Field(description="The text around the mention, for the operator.")
    rule: str = Field(description="`candidate`.")


class LedgerEntryBody(BaseModel):
    """The erasure ledger's row: identifiers, the scope and the counts; never a name."""

    model_config = ConfigDict(extra="forbid")

    erasure_id: str
    at: datetime
    scope: str
    story_id: str | None
    counts: dict[str, Any]


class ErasureBody(DeploymentMutationResponse):
    """What the erasure did, or (``dry_run``) would do."""

    dry_run: bool
    subject: dict[str, str] = Field(description="`{kind, id}`: the subject erased.")
    scope: str = Field(description="`story` or `catalog`.")
    story_id: str | None
    records: dict[str, int] = Field(
        description="Structured rows deleted or rewritten, by table (catalog tables "
        "schema-qualified). Erasure markers and index rows are not counted."
    )
    prose_segments: int = Field(description="Text fields redacted (fields, not spans).")
    media: int = Field(
        description="Story scope: the images the subject's characters show (they stay). "
        "Catalog scope: the media assets purged, retired or rewritten."
    )
    idempotency_results: int = Field(description="Stored idempotency results purged.")
    regeneration: str = Field(
        description="`model`: rolling-summary segments that named the subject are summarised "
        "again from the redacted window; `redaction`: no model can be asked, the redacted "
        "text stands."
    )
    degradation: list[str] = Field(
        description="What did not complete, the erasure itself being complete: "
        "`summary_regeneration_failed`, `summary_regeneration_capped`, `rebuild_failed`. "
        "Run the erasure again to retry."
    )
    referencing_stories: list[ReferencingStoryBody] = Field(
        description="Catalog scope: the stories that reference the subject, untouched."
    )
    timeline_revision: int | None = Field(
        default=None, description="Story scope: the story's revision after the erasure."
    )
    erasure_id: str | None = None
    operation_id: str | None = None
    ledger_entry: LedgerEntryBody | None = None
    residue: int | None = Field(
        default=None,
        description="Values in the scope that still certainly name the subject after the "
        "erasure (the residue scan; 0 when nothing does). Unconfirmed candidates are not "
        "residue: `candidates_remaining` counts them.",
    )
    candidates_remaining: int = Field(
        default=0,
        description="Candidate mentions met and not confirmed, left in place (ADR-154).",
    )
    candidates: list[CandidateBody] = Field(
        default_factory=list,
        description="Dry run only: every candidate mention, with its id, location and a "
        "snippet. A real run lists none.",
    )

    @classmethod
    def of(cls, result: ErasureResult) -> ErasureBody:
        entry = result.ledger_entry
        return cls(
            dry_run=result.dry_run,
            subject={"kind": result.subject_kind, "id": result.subject_id},
            scope=result.scope,
            story_id=result.story_id,
            records=result.records,
            prose_segments=result.prose_segments,
            media=result.media,
            idempotency_results=result.idempotency_results,
            regeneration=result.regeneration,
            degradation=list(result.degradation),
            referencing_stories=[
                ReferencingStoryBody(story_id=s.story_id, character_ids=list(s.character_ids))
                for s in result.referencing_stories
            ],
            timeline_revision=result.timeline_revision,
            erasure_id=result.erasure_id,
            operation_id=result.operation_id,
            ledger_entry=None
            if entry is None
            else LedgerEntryBody(
                erasure_id=entry.erasure_id,
                at=entry.at,
                scope=entry.scope,
                story_id=entry.story_id,
                counts=entry.counts,
            ),
            residue=result.residue,
            candidates_remaining=result.candidates_remaining,
            candidates=[
                CandidateBody(
                    candidate_id=c.candidate_id,
                    record_table=c.record_table,
                    record_id=c.record_id,
                    field=c.field,
                    snippet=c.snippet,
                    rule=c.rule,
                )
                for c in result.candidates
            ],
            config_revision=result.config_revision,
        )


@router.post(
    "",
    response_model=ErasureBody,
    operation_id="erase",
    summary="Erase a subject from a story or from the catalog",
    description=(
        "Right-to-be-forgotten erasure (FR-ADMIN-2, FR-ADMIN-6). `subject` is `{kind, id}` "
        "(`character`, `catalog_character` or `persona`) and `scope` `{story: <id>}` or "
        "`{catalog: true}`; a story `character` scoped to the catalog is 422 "
        "`invalid_erasure_scope`. **Story scope**: the subject's character rows, snapshots and "
        "deferred items are deleted (each leaves an erasure marker in the audit view, "
        "`include_audit=1`), every text in the story that names the subject reads `[erased]` "
        "where it did (a beat the subject said becomes `[erased]` whole, keeping its place), "
        "stored results that name it are purged, the rolling-summary segments that named it "
        "are summarised again from the redacted window (or stay redacted without a model), "
        "and the projections are rebuilt. It bumps the story's revision, so a turn in flight "
        "starts over, and runs on archived and paused stories too. **Catalog scope**: the "
        "catalog character (every version, its draft and companion) or the persona is "
        "deleted, the scenarios and other catalog records that name it are redacted, media "
        "depicting or naming only the subject are purged (or kept retired when another "
        "record still references them and `erasure_shared_media` is `retire`), and stories "
        "are never touched: `referencing_stories` lists those to erase separately. "
        "`dry_run: true` answers the counts and changes nothing (zero counts for an unknown "
        "subject). **Mentions** (ADR-154): the subject's names, aliases and name words are "
        "erased where they certainly refer to it; a name word that is also a common word "
        "(`Will`, `Rose`) only when capitalised, and at the start of a sentence it is a "
        "*candidate*: the dry run lists each one (`candidates`, with an id and a snippet "
        "returned only, never stored), and an erasure removes it only when "
        "`confirm_candidates` names its id (422 `unknown_erasure_candidate` for an id it does "
        "not meet). `candidates_remaining` counts those left. The ledger records identifiers, "
        "scope, story and counts (with the confirmed candidate ids), never content. "
        "404 for an unknown story, or on a real run an unknown subject; 503 "
        "`config_key_missing` without `EPISTREL_CONFIG_KEY`; 503 `ledger_unavailable` when the "
        "erasure ledger's file cannot be written (nothing is erased). The erasure boundary, "
        "what erasure does not reach, is `GET /admin/erasure/boundary`."
    ),
)
async def erase(
    body: ErasureRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> ErasureBody:
    result = await engine.erase(ctx, body)
    return answered(ErasureBody.of(result), ctx, response, replayed=result.replayed)


class PendingErasureBody(BaseModel):
    """An erasure the ledger holds and this database has not applied yet, and its story (null
    for the catalog scope)."""

    model_config = ConfigDict(extra="forbid")

    erasure_id: str
    story_id: str | None


class ErasureStatusBody(BaseModel):
    """The erasure ledger's reconciliation: what is pending and applied, and the last run."""

    model_config = ConfigDict(extra="forbid")

    pending: list[PendingErasureBody] = Field(
        description="Entries not applied yet, the catalog scope first, then oldest first. Each "
        "story among them is `blocked_by_erasure`: 503 `erasure_pending` until it is applied."
    )
    pending_count: int
    applied_count: int = Field(description="Entries applied to this database.")
    in_progress: bool = Field(description="Whether a reapply is running now.")
    last_run_at: datetime | None = Field(description="When the last reapply run ended.")
    last_error: str | None = Field(
        description="The last run's error, as an exception class (never a message), or null."
    )
    attempts: dict[str, int] = Field(
        description="Failed reapply attempts per pending entry; retried every 5 minutes."
    )

    @classmethod
    def of(cls, found: ReconcileStatus) -> ErasureStatusBody:
        return cls(
            pending=[
                PendingErasureBody(erasure_id=p.erasure_id, story_id=p.story_id)
                for p in found.pending
            ],
            pending_count=found.pending_count,
            applied_count=found.applied_count,
            in_progress=found.in_progress,
            last_run_at=found.last_run_at,
            last_error=found.last_error,
            attempts=dict(found.attempts),
        )


class ReconcileRunBody(BaseModel):
    """A reconcile run: its id, whether it ran (``false``: another process was running it),
    what it settled and failed, and what is still pending."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    ran: bool
    settled: int
    failed: int
    pending_count: int

    @classmethod
    def of(cls, run: ReconcileRun) -> ReconcileRunBody:
        return cls(
            run_id=run.run_id,
            ran=run.ran,
            settled=run.settled,
            failed=run.failed,
            pending_count=run.pending_count,
        )


class ErasureBoundaryBody(BaseModel):
    """What an erasure covers, what it cannot reach, and what the logs hold (FR-ADMIN-8)."""

    model_config = ConfigDict(extra="forbid")

    in_scope: list[str]
    out_of_scope: list[str]
    logs: str

    @classmethod
    def of(cls, boundary: ErasureBoundary) -> ErasureBoundaryBody:
        return cls(
            in_scope=list(boundary.in_scope),
            out_of_scope=list(boundary.out_of_scope),
            logs=boundary.logs,
        )


@router.get(
    "/status",
    response_model=ErasureStatusBody,
    operation_id="erasure_status",
    summary="Read the erasure ledger's reconciliation",
    description=(
        "The erasure ledger (FR-ADMIN-7) is kept outside the database, in "
        "`EPISTREL_ERASURE_LEDGER_DIR`, and copied into the `erasure_ledger` table that every "
        "backup carries. At start-up, Epistrel compares the two: an erasure the database lacks "
        "(a restored backup that predates it) becomes pending, and its story is "
        "`blocked_by_erasure` (every read and write of it is 503 `erasure_pending`) until "
        "Epistrel has reapplied the erasure itself. This reports what is pending (with each "
        "story), how many entries are applied, whether a reapply is running, and the last "
        "run's time, error class and attempts."
    ),
)
async def erasure_status(ctx: ContextDep, engine: EngineDep) -> ErasureStatusBody:
    return ErasureStatusBody.of(await engine.erasure_status(ctx))


@router.post(
    "/reconcile",
    response_model=ReconcileRunBody,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="reconcile_erasure",
    summary="Reconcile the erasure ledger now",
    description=(
        "Compare the erasure ledger's file with the database and reapply every pending "
        "erasure now (FR-ADMIN-7), as start-up and the 5-minute retry do. It answers when the "
        "run has ended; `ran` is `false` when another process was already running it, and "
        "`GET /admin/erasure/status` shows the outcome. It changes no story content of its "
        "own (each reapply is the erasure the ledger already records), takes no "
        "`Idempotency-Key` and records no operation: running it again is harmless."
    ),
    **route_flags(mutating=False),
)
async def reconcile_erasure(ctx: ContextDep, engine: EngineDep) -> ReconcileRunBody:
    return ReconcileRunBody.of(await engine.reconcile_erasure(ctx))


@router.get(
    "/boundary",
    response_model=ErasureBoundaryBody,
    operation_id="erasure_boundary",
    summary="Read the erasure boundary",
    description=(
        "What an erasure covers and what it cannot reach (FR-ADMIN-8), as data. The same text "
        "is in `docs/OPERATIONS.md` and at the end of this API's description:\n\n"
        + render_boundary()
    ),
)
async def erasure_boundary(ctx: ContextDep, engine: EngineDep) -> ErasureBoundaryBody:
    return ErasureBoundaryBody.of(await engine.erasure_boundary(ctx))


__all__ = [
    "ErasureBody",
    "ErasureBoundaryBody",
    "ErasureStatusBody",
    "LedgerEntryBody",
    "PendingErasureBody",
    "ReconcileRunBody",
    "ReferencingStoryBody",
    "router",
]
