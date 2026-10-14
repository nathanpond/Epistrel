"""``POST /admin/erasure``: erase a subject within a story or from the catalog (FR-ADMIN-2,6,
NFR-PRIV-2; #124).

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

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.erasure.erase import ErasureRequest, ErasureResult

router = APIRouter(prefix="/erasure")


class ReferencingStoryBody(BaseModel):
    """A story that references the erased catalog subject, left untouched (FR-STORE-14): erase
    the subject there separately (``scope: {story: <id>}``)."""

    model_config = ConfigDict(extra="forbid")

    story_id: str
    character_ids: list[str]


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
        description="Values in the scope that still name the subject after the erasure (the "
        "residue scan; 0 when nothing does).",
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
        "subject). The ledger records identifiers, scope, story and counts, never content. "
        "404 for an unknown story, or on a real run an unknown subject; 503 "
        "`config_key_missing` without `EPISTREL_CONFIG_KEY`. Out of scope: delivered exports, "
        "data a model provider holds, and backups taken before the erasure."
    ),
)
async def erase(
    body: ErasureRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> ErasureBody:
    result = await engine.erase(ctx, body)
    return answered(ErasureBody.of(result), ctx, response, replayed=result.replayed)


__all__ = ["ErasureBody", "LedgerEntryBody", "ReferencingStoryBody", "router"]
