"""``/admin/reembed``: the re-embed run a change of the embedding model starts (FR-EMBED-3,4;
#138).

- ``GET /status`` answers the last change's ``from_version`` and ``to_version``, when it started
  and (once no story's job is queued or running) finished, and one page of stories, by id: each
  story's latest ``reembed`` job status (``queued``, ``running``, ``done``, ``failed`` with the
  exception class as ``error``), its hot memories (``total``) and how many are already in the new
  version (``done``). ``limit`` (1 to 200, default 50) and ``after`` (the last page's ``next``)
  page it. Before any model change every field is ``null`` and ``stories`` is empty.
- ``POST /retry`` enqueues a new job for every story whose latest one failed and names them
  (``Idempotency-Key`` replays the first answer).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.orchestrator.jobs.handlers.reembed import (
    STATUS_PAGE_MAX,
    ReembedRetry,
    ReembedStatus,
)

router = APIRouter(prefix="/reembed")


class StoryReembedBody(BaseModel):
    """One story's re-embed."""

    model_config = ConfigDict(extra="forbid")

    story_id: str
    total: int = Field(description="The story's hot active memories.")
    done: int = Field(description="How many of them have a vector in `to_version`.")
    status: str = Field(description="`queued`, `running`, `done` or `failed`.")
    error: str | None = Field(description="A failed job's exception class; never a message.")


class ReembedStatusBody(BaseModel):
    """The last embedding-model change's re-embed run."""

    model_config = ConfigDict(extra="forbid")

    from_version: str | None
    to_version: str | None
    started_at: datetime | None
    finished_at: datetime | None = Field(
        description="When the last story's job ended; `null` while any is queued or running."
    )
    stories: list[StoryReembedBody]
    next: str | None = Field(description="Pass as `after` for the next page; `null` at the end.")

    @classmethod
    def of(cls, run: ReembedStatus) -> ReembedStatusBody:
        return cls(
            from_version=run.from_version,
            to_version=run.to_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            stories=[
                StoryReembedBody(
                    story_id=s.story_id, total=s.total, done=s.done, status=s.status, error=s.error
                )
                for s in run.stories
            ],
            next=run.next,
        )


class ReembedRetryBody(DeploymentMutationResponse):
    """The stories a new re-embed job was enqueued for."""

    model_config = ConfigDict(extra="forbid")

    retried: list[str]

    @classmethod
    def of(cls, retry: ReembedRetry) -> ReembedRetryBody:
        return cls(retried=list(retry.retried), config_revision=retry.config_revision)


@router.get(
    "/status",
    response_model=ReembedStatusBody,
    operation_id="reembed_status",
    summary="Read the re-embed run",
)
async def reembed_status(
    ctx: ContextDep,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=STATUS_PAGE_MAX)] = 50,
    after: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> ReembedStatusBody:
    return ReembedStatusBody.of(await engine.reembed_status(ctx, limit=limit, after=after))


@router.post(
    "/retry",
    response_model=ReembedRetryBody,
    operation_id="retry_reembed",
    summary="Retry the failed re-embed jobs",
)
async def retry_reembed(ctx: ContextDep, engine: EngineDep, response: Response) -> ReembedRetryBody:
    retry = await engine.retry_reembed(ctx)
    return answered(ReembedRetryBody.of(retry), ctx, response, replayed=retry.replayed)


__all__ = ["ReembedRetryBody", "ReembedStatusBody", "StoryReembedBody", "router"]
