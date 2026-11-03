"""A story's history rewrite sessions (FR-REWRITE-1,2,4,7; docs/02 §9; #223).

- ``POST /stories/{id}/rewrites``: open a session on an instruction.
- ``GET /stories/{id}/rewrites``: list the sessions, newest first.
- ``GET /stories/{id}/rewrites/{rid}``: one session, its preview and questions.
- ``POST /stories/{id}/rewrites/{rid}/answers``: answer its questions.
- ``POST /stories/{id}/rewrites/{rid}/cancel``: cancel it.

Every route is an omniscient call (``?viewpoint=omniscient``): a preview shows what every
character believes. Every handler makes one ``Engine`` call (``orchestrator.rewrite.session``
holds the rules); none moves the story's revision.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response
from fastapi import status as http_status
from pydantic import ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.orchestrator.rewrite.api import (
    AnswerRequest,
    Applied,
    ApplyRequest,
    RewriteList,
    RewriteRequest,
    RewriteSession,
    RewriteWrite,
)
from epistrel.orchestrator.rewrite.session import LIST_LIMIT

router = APIRouter(prefix="/stories", tags=["stories"])

SessionId = Annotated[str, Path(pattern=ULID_PATTERN, description="The rewrite session's id.")]
_OMNISCIENT = (
    " An omniscient call: 422 `viewpoint_unresolved` without `?viewpoint`, 403 "
    "`omniscient_required` for a character. 404 `rewrite_session_not_found` for an unknown "
    "session or another story's. The story's `timeline_revision` never moves."
)


class RewriteSessionBody(RewriteSession, MutationResponse):
    """A session write's answer: the session and the story's revision, which only an apply
    moves; ``apply`` when the call applied (``apply_directly``, #224)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    apply: Applied | None = Field(default=None, exclude_if=lambda v: v is None)

    @classmethod
    def of(cls, found: RewriteWrite) -> RewriteSessionBody:
        return cls(
            **found.session.model_dump(),
            timeline_revision=found.timeline_revision,
            apply=found.apply,
        )


class AppliedBody(MutationResponse):
    """``POST …/apply``'s answer: the session, the amendment, the report and the new
    revision."""

    session: RewriteSession
    amendment_id: str
    report: dict[str, Any]

    @classmethod
    def of(cls, found: RewriteWrite) -> AppliedBody:
        assert found.apply is not None  # noqa: S101 - an apply answers its apply
        return cls(
            session=found.session,
            amendment_id=found.apply.amendment_id,
            report=found.apply.report,
            timeline_revision=found.timeline_revision,
        )


@router.post(
    "/{story_id}/rewrites",
    response_model=RewriteSessionBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="open_rewrite",
    summary="Open a history rewrite session",
    description=(
        "An author's instruction about the story's past (FR-REWRITE-1,2), such as \"Bobby "
        'actually went to UCLA": checked against the input guardrails (422 '
        "`guardrail_violation`, nothing written), parsed into structured changes, and planned "
        "into a `preview` at the story's revision: the changes and their blast radius "
        "(incidental mentions, cascaded justifications, irreducible conflicts with a "
        "`severity`). When the instruction is ambiguous the session asks (`questions`), each "
        "question with a recommended option and the reason for it, and waits in "
        "`awaiting_answers`; otherwise it is `awaiting_approval`. `resolution_mode` "
        '(`synchronous` by default, `best_effort` or `{"threshold": "high"|"low"}`) and '
        "`preview` (`preview_first` by default, or `apply_directly`) are recorded for the "
        "apply. Nothing about the story changes (FR-REWRITE-7). 409 `story_paused`, "
        "`rewrite_session_limit`, `revision_conflict` for a stale "
        "`X-Epistrel-Expected-Revision`, and `module_off` (`module: lite_rewrites`) for a "
        "Lite story; 422 `rewrite_unparseable` and `rewrite_noop`." + _OMNISCIENT
    ),
)
async def open_rewrite(
    story_id: StoryId,
    body: RewriteRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> RewriteSessionBody:
    found = await engine.open_rewrite(ctx, story_id, body, viewpoint=viewpoint)
    return answered(RewriteSessionBody.of(found), ctx, response, replayed=found.replayed)


@router.get(
    "/{story_id}/rewrites",
    response_model=RewriteList,
    operation_id="list_rewrites",
    summary="List a story's rewrite sessions",
    description=(
        "The story's rewrite sessions, newest first, without previews or questions: each "
        "session's state and its count of unanswered required questions. `state` (repeatable) "
        "filters; `cursor` is the previous page's `next_cursor`." + _OMNISCIENT
    ),
)
async def list_rewrites(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    state: Annotated[list[str] | None, Query()] = None,
    cursor: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=LIST_LIMIT)] = LIST_LIMIT,
) -> RewriteList:
    return await engine.list_rewrites(
        ctx, story_id, viewpoint=viewpoint, states=state or (), cursor=cursor, limit=limit
    )


@router.get(
    "/{story_id}/rewrites/{session_id}",
    response_model=RewriteSession,
    operation_id="get_rewrite",
    summary="Read a rewrite session",
    description=(
        "A rewrite session with its `preview` (`base_revision`, `plan_hash`, `changes`, "
        "`blast_radius`, `relabels`, `refused_changes`, `notifications`, `timing_uncertain`, "
        "`suggested_ports`; null while a required question is unanswered) and its "
        "questions." + _OMNISCIENT
    ),
)
async def get_rewrite(
    story_id: StoryId,
    session_id: SessionId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> RewriteSession:
    return await engine.get_rewrite(ctx, story_id, session_id, viewpoint=viewpoint)


@router.post(
    "/{story_id}/rewrites/{session_id}/answers",
    response_model=RewriteSessionBody,
    operation_id="answer_rewrite",
    summary="Answer a rewrite session's questions",
    description=(
        "Answers to the session's questions, validated all together (422 `invalid_answer`, "
        "nothing stored): an `option_id`, free `text` (checked against the input guardrails "
        "and parsed with the instruction), or `accept_recommended`. The session re-plans at "
        "the story's current revision. Allowed on a paused story. 409 "
        "`rewrite_session_closed` once the session ended." + _OMNISCIENT
    ),
)
async def answer_rewrite(
    story_id: StoryId,
    session_id: SessionId,
    body: AnswerRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> RewriteSessionBody:
    found = await engine.answer_rewrite(ctx, story_id, session_id, body, viewpoint=viewpoint)
    return answered(RewriteSessionBody.of(found), ctx, response, replayed=found.replayed)


@router.post(
    "/{story_id}/rewrites/{session_id}/cancel",
    response_model=RewriteSessionBody,
    operation_id="cancel_rewrite",
    summary="Cancel a rewrite session",
    description=(
        "Cancel the session: `cancelled`, nothing committed. 409 `rewrite_session_closed` once "
        "the session ended." + _OMNISCIENT
    ),
)
async def cancel_rewrite(
    story_id: StoryId,
    session_id: SessionId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> RewriteSessionBody:
    found = await engine.cancel_rewrite(ctx, story_id, session_id, viewpoint=viewpoint)
    return answered(RewriteSessionBody.of(found), ctx, response, replayed=found.replayed)


@router.post(
    "/{story_id}/rewrites/{session_id}/apply",
    response_model=AppliedBody,
    operation_id="apply_rewrite",
    summary="Apply a rewrite session",
    description=(
        "Apply the session's plan (#224, FR-REWRITE-6) as one atomic amendment: the planned "
        "changes, the incidental and cascaded repairs, the conflicts' resolutions (as answered, "
        "or the least change the mode chose) and any cascade undo, all or nothing; the story's "
        "`timeline_revision` moves by exactly one. The questions and answers are recorded as "
        "audit events. The answer is the session, the `amendment_id` and the `report` "
        "(`resolutions`, `applied`, `notifications`). 409 `rewrite_session_closed`, "
        "`rewrite_apply_in_progress`, `story_paused`, `rewrite_stale` (a moved revision, or a "
        "changed pipeline: the session is then abandoned), `rewrite_questions_pending`, "
        "`record_in_flight`; 422 `base_revision_required`; 503 `rewrite_apply_failed` "
        "(retryable; nothing was written and the session awaits approval again)." + _OMNISCIENT
    ),
)
async def apply_rewrite(
    story_id: StoryId,
    session_id: SessionId,
    body: ApplyRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> AppliedBody:
    found = await engine.apply_rewrite(ctx, story_id, session_id, body, viewpoint=viewpoint)
    return answered(AppliedBody.of(found), ctx, response, replayed=found.replayed)


__all__ = ["router"]
