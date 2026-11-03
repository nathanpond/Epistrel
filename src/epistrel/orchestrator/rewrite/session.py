"""Rewrite sessions: open, read, answer, cancel and expire (FR-REWRITE-1,2,4,7; #223).

A session is a story resource beside the turn flow. Opening, answering and cancelling one write
``rewrite_sessions`` and ``rewrite_questions`` rows only (and their subject-index rows): never
an event, a fact, a State change, the clock, a scheduled event, an outbox row or the story's
revision (FR-REWRITE-7). The Engine runs what needs a model and the story's policies first:
#125's erasure gate, the viewpoint check (``omniscient`` only), ``precheck`` (404, 409
``story_paused``, a stale expected revision, 409 ``rewrite_session_limit``), then #56's input
gate on the instruction (and on each free-text answer). This module then refuses text that
names an erased subject (409 ``erased_subject_referenced``), dispatches to the story's planner
(``plan.PLANNERS``; a pipeline with none is 409 ``module_off {module: "lite_rewrites"}``,
nothing written), parses (``parse``), plans and stores.

**State.** ``awaiting_answers`` while a required question is unanswered, else
``awaiting_approval``; ``cancelled`` and ``expired`` end it (``applying``, ``applied`` and
``abandoned`` are #224's and #237's). Any write to a session that has ended is 409
``rewrite_session_closed``; an unknown id, or another story's, is 404
``rewrite_session_not_found``. A write pushes ``expires_at`` out by
``rewrite_session_ttl_minutes``; a read does not. The recurring task
``rewrite_session_expiry`` expires idle sessions (a global sweep, 500 a run).

**Answers** are validated all together (422 ``invalid_answer``, nothing stored): an option of a
question still asked, free text, or ``accept_recommended``. An option binds its question
(``bindings`` by question key); an answer to a question the model asked, and free text, are
also handed back to ``rewrite_parse`` with the instruction, which parses again. Then the session
re-plans at the story's current revision: questions the new parse asks again keep their rows
(and answers), new ones are added, and those no longer asked are marked ``superseded_by``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import read_section
from epistrel.core.errors import (
    BaseRevisionRequired,
    EpistrelError,
    ErasedSubjectReferenced,
    InvalidAnswer,
    ModuleOff,
    RevisionConflict,
    RewriteApplyFailed,
    RewriteApplyInProgress,
    RewriteNoop,
    RewriteQuestionsPending,
    RewriteQuestionSuperseded,
    RewriteSessionClosed,
    RewriteSessionLimit,
    RewriteSessionNotFound,
    RewriteStale,
    StoryNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.settings_schema import RewriteSettings
from epistrel.core.tables import rewrite_questions, rewrite_sessions, stories
from epistrel.core.tables.rewrites import OPEN_STATES
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.registry import RewriteParseResult
from epistrel.orchestrator.rewrite import apply as rewrite_apply
from epistrel.orchestrator.rewrite import parse as parsing
from epistrel.orchestrator.rewrite import revalidate
from epistrel.orchestrator.rewrite.api import (
    AmendmentSessionRequest,
    AnswerRequest,
    Applied,
    ApplyRequest,
    MergeRequest,
    Preview,
    QuestionOption,
    RewriteList,
    RewriteQuestion,
    RewriteRequest,
    RewriteSession,
    RewriteSummary,
    RewriteWrite,
    ThresholdMode,
)
from epistrel.orchestrator.rewrite.dispose import ENGINE, dispose
from epistrel.orchestrator.rewrite.parse import question_key
from epistrel.orchestrator.rewrite.plan import PLANNERS, Plan

LITE_REWRITES = "lite_rewrites"
EXPIRY_BATCH = 500
TERMINAL = frozenset({"applied", "cancelled", "expired", "abandoned"})
LIST_LIMIT = 50


# -- reading ---------------------------------------------------------------------------------------


async def stored_plan(conn: AsyncConnection, story_id: str, session_id: str) -> dict[str, Any]:
    """The session's stored plan (#241: a directed resolution's direction is replayed from
    it)."""
    return dict((await _row(conn, story_id, session_id)).plan or {})


async def _row(conn: AsyncConnection, story_id: str, session_id: str, *, lock: bool = False) -> Any:
    query = select(rewrite_sessions).where(rewrite_sessions.c.id == session_id)
    if lock:
        query = query.with_for_update()
    found = (await conn.execute(query)).one_or_none()
    if found is None or str(found.story_id) != story_id:
        raise RewriteSessionNotFound(
            "the story has no such rewrite session",
            {"story_id": story_id, "session_id": session_id},
        )
    return found


async def _questions(conn: AsyncConnection, session_id: str) -> list[Any]:
    q = rewrite_questions.c
    return list(
        await conn.execute(
            select(rewrite_questions)
            .where(q.session_id == session_id, q.superseded_by.is_(None))
            .order_by(q.created_at, q.id)
        )
    )


def _question_view(row: Any) -> RewriteQuestion:
    return RewriteQuestion(
        id=str(row.id),
        kind=str(row.kind),
        prompt=str(row.prompt),
        options=[QuestionOption.model_validate(o) for o in row.options or []],
        recommended_option_id=str(row.recommended_option_id),
        recommendation_reason=str(row.recommendation_reason),
        severity=row.severity,
        required=bool(row.required),
        answer_option_id=row.answer_option_id,
        answer_text=row.answer_text,
        accepted_recommendation=row.accepted_recommendation,
        answered_by=row.answered_by,
        answered_at=row.answered_at,
    )


def _mode(row: Any) -> Any:
    if row.resolution_mode == "threshold":
        return ThresholdMode(threshold=row.threshold_severity)
    return str(row.resolution_mode)


async def view(conn: AsyncConnection, row: Any) -> RewriteSession:
    plan = dict(row.plan or {})
    preview = plan.get("preview")
    return RewriteSession(
        id=str(row.id),
        story_id=str(row.story_id),
        kind=str(row.kind),
        state=str(row.state),
        instruction=row.instruction,
        resolution_mode=_mode(row),
        preview_mode=row.preview,
        pipeline=row.pipeline,
        principal=str(row.principal),
        target_amendment_id=None
        if row.target_amendment_id is None
        else str(row.target_amendment_id),
        base_revision=None if row.base_revision is None else int(row.base_revision),
        preview=None
        if preview is None
        else Preview.model_validate(
            {**preview, **({"reach": plan["reach"]} if plan.get("reach") else {})}
        ),
        questions=[_question_view(q) for q in await _questions(conn, str(row.id))],
        stale_count=int(row.stale_count),
        result_amendment_id=row.result_amendment_id,
        report=row.report,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
    )


async def get(db: AsyncEngine, story_id: str, session_id: str) -> RewriteSession:
    async with db.connect() as conn:
        await _story(conn, story_id)
        return await view(conn, await _row(conn, story_id, session_id))


async def list_sessions(
    db: AsyncEngine,
    story_id: str,
    *,
    states: Sequence[str] = (),
    cursor: str | None = None,
    limit: int = LIST_LIMIT,
) -> RewriteList:
    """The story's sessions, newest first (a ULID id is creation order), a page at a time
    (``cursor``: the last id of the previous page)."""
    s, q = rewrite_sessions.c, rewrite_questions.c
    async with db.connect() as conn:
        await _story(conn, story_id)
        where = [s.story_id == story_id]
        if states:
            where.append(s.state.in_(list(states)))
        if cursor:
            where.append(s.id < cursor)
        open_count = (
            select(func.count())
            .where(
                q.session_id == s.id,
                q.superseded_by.is_(None),
                q.required.is_(True),
                q.answered_at.is_(None),
            )
            .scalar_subquery()
        )
        rows = list(
            await conn.execute(
                select(rewrite_sessions, open_count.label("open_questions"))
                .where(*where)
                .order_by(s.id.desc())
                .limit(limit + 1)
            )
        )
    items = [
        RewriteSummary(
            id=str(r.id),
            kind=str(r.kind),
            state=str(r.state),
            instruction=r.instruction,
            open_questions=int(r.open_questions or 0),
            created_at=r.created_at,
            updated_at=r.updated_at,
            expires_at=r.expires_at,
        )
        for r in rows[:limit]
    ]
    return RewriteList(items=items, next_cursor=items[-1].id if len(rows) > limit else None)


# -- prechecks -------------------------------------------------------------------------------------


async def _story(conn: AsyncConnection, story_id: str, *, lock: bool = False) -> Any:
    query = select(stories.c.id, stories.c.status, stories.c.timeline_revision).where(
        stories.c.id == story_id
    )
    if lock:
        query = query.with_for_update()
    found = (await conn.execute(query)).one_or_none()
    if found is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return found


async def _open_count(conn: AsyncConnection, story_id: str) -> int:
    s = rewrite_sessions.c
    return int(
        await conn.scalar(
            select(func.count()).where(
                s.story_id == story_id,
                s.state.in_(list(OPEN_STATES)),
                s.kind != "support_check",
            )
        )
        or 0
    )


async def _limit(conn: AsyncConnection, story_id: str, settings: RewriteSettings) -> None:
    found = await _open_count(conn, story_id)
    if found >= settings.rewrite_max_open_sessions:
        raise RewriteSessionLimit(
            "the story has as many open rewrite sessions as it may",
            {"limit": settings.rewrite_max_open_sessions, "open": found},
        )


async def precheck(db: AsyncEngine, story_id: str, *, expected_revision: int | None) -> None:
    """Create's checks before the input gate: 404 story, 409 ``story_paused``, a stale
    ``X-Epistrel-Expected-Revision`` (409 ``revision_conflict``), 409
    ``rewrite_session_limit``."""
    from epistrel.core import pause

    async with db.connect() as conn:
        found = await _story(conn, story_id)
        await pause.refuse(conn, story_id, found.status)
        revision = int(found.timeline_revision)
        if expected_revision is not None and expected_revision != revision:
            raise RevisionConflict(
                "the story moved past the expected revision",
                {"current_revision": revision, "attempts": 0, "reason": "expected_revision"},
            )
        await _limit(conn, story_id, await read_section(conn, RewriteSettings))


async def pipeline_of(conn: AsyncConnection, story_id: str) -> str:
    from epistrel.core.profiles import schema
    from epistrel.core.profiles.effective import effective_configuration

    settings = (await effective_configuration(conn, story_id, validate=False)).settings
    return str(schema.level_of(settings, "pipeline") or "lite")


def _planner_of(pipeline: str) -> Any:
    planner = PLANNERS.get(pipeline)
    if planner is None:
        raise ModuleOff(
            "history rewrites are not available for a Lite story yet",
            {"module": LITE_REWRITES, "pipeline": pipeline},
        )
    return planner


async def require_planner(db: AsyncEngine, story_id: str) -> str:
    """The story's pipeline, once a planner serves it (409 ``module_off`` otherwise)."""
    async with db.connect() as conn:
        pipeline = await pipeline_of(conn, story_id)
    _planner_of(pipeline)
    return pipeline


async def _guard(conn: AsyncConnection, story_id: str, texts: Sequence[str]) -> None:
    """409 ``erased_subject_referenced`` for author text that names a subject erased from the
    story (#127: typed after the erasure it is accepted, as the guard decides)."""
    from epistrel.core.erasure import guard

    await guard.check_content(
        conn,
        story_id,
        [guard.Content(tuple(texts), frozenset(), consumer=True)],
        base_revision=None,
        path=__name__,
    )


# -- planning --------------------------------------------------------------------------------------


async def _registry(conn: AsyncConnection, story_id: str) -> Any:
    from epistrel.core.propositions.registry import load

    return await load(conn, story_id)


async def _compute(
    conn: AsyncConnection,
    story_id: str,
    pipeline: str,
    result: RewriteParseResult,
    bindings: Mapping[str, dict[str, Any]],
    settings: RewriteSettings,
    *,
    gated: Sequence[dict[str, Any]] = (),
    mode: str = "synchronous",
    threshold: str | None = None,
) -> tuple[parsing.Parsed, Plan | None, int]:
    story = await parsing.load_story(
        conn, story_id, await _registry(conn, story_id), margin=settings.rewrite_ambiguity_margin
    )
    parsed = await parsing.normalize(conn, story, result, bindings)
    parsed.refused.extend(gated)
    revision = int((await _story(conn, story_id)).timeline_revision)
    if any(q.required for q in parsed.questions):
        return parsed, None, revision
    plan = await _planner_of(pipeline).plan(conn, story, parsed, base_revision=revision)
    # #224: the mode decides who settles each conflict (the plan itself is mode-free).
    parsed.questions.extend(dispose(plan.irreducible, mode, threshold).questions)
    return parsed, plan, revision


def _stored_plan(
    result: RewriteParseResult,
    bindings: Mapping[str, dict[str, Any]],
    answers: Sequence[str],
    parsed: parsing.Parsed,
    plan: Plan | None,
    gated: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "parse": result.model_dump(mode="json", by_alias=True),
        "gated": list(gated),
        "bindings": dict(bindings),
        "answers": list(answers),
        "noops": parsed.noops,
        "preview": None if plan is None else plan.preview(),
    }


GATE_CALLS = 8
GATE_DEADLINE_S = 60.0


async def gate_new_texts(
    db: AsyncEngine, models: ModelAccessLayer, story_id: str, result: RewriteParseResult
) -> tuple[RewriteParseResult, list[dict[str, Any]]]:
    """Each record change's model-written ``new_text`` checked against the story's output
    policies (#56) before it enters the plan: a change whose text breaks one is taken out and
    listed in ``refused_changes`` (reason ``guardrail``, the policy ids, never the text)."""
    from epistrel.orchestrator.budget import TurnBudget
    from epistrel.orchestrator.policies import output_gate
    from epistrel.orchestrator.policies import store as policy_store

    texts = [c for c in result.changes if c.kind == "record" and c.new_text]
    if not texts:
        return result, []
    context = await policy_store.guard_context(db, story_id)
    policies = output_gate.output_policies(context)
    if not policies:
        return result, []
    budget = TurnBudget(max_calls=GATE_CALLS * len(texts), deadline_s=GATE_DEADLINE_S)
    kept: list[Any] = []
    refused: list[dict[str, Any]] = []
    for n, change in enumerate(result.changes):
        if change.kind == "record" and change.new_text:
            checked = await output_gate.check_output(
                models, change.new_text, policies, context, budget=budget
            )
            if not checked.passed:
                refused.append(
                    {
                        "index": n,
                        "reason": "guardrail",
                        "policy_ids": sorted({v.policy_id for v in checked.violations}),
                    }
                )
                continue
        kept.append(change)
    return result.model_copy(update={"changes": kept}), refused


async def _index(conn: AsyncConnection, session_id: str) -> None:
    from epistrel.core.erasure.index import index_records
    from epistrel.core.erasure.records import rewrite_question_record, rewrite_session_record

    head = (
        await conn.execute(select(rewrite_sessions).where(rewrite_sessions.c.id == session_id))
    ).one()
    asked = list(
        await conn.execute(
            select(rewrite_questions).where(rewrite_questions.c.session_id == session_id)
        )
    )
    await index_records(
        conn, [rewrite_session_record(head), *(rewrite_question_record(q) for q in asked)]
    )


async def _write_questions(
    conn: AsyncConnection,
    story_id: str,
    session_id: str,
    asked: Sequence[parsing.Question],
    *,
    refresh: bool = False,
) -> None:
    """Keep the current questions the parse asks again, add new ones, and mark those no longer
    asked superseded (``superseded_by``: this re-plan's id). With ``refresh`` (#237's stale
    re-plan) a kept question takes the new plan's options and recommendation, and loses its
    answer when the answered option is gone or names another effect (it is asked again; an
    engine-settled conflict takes the new recommendation)."""
    current = {str(q.key): q for q in await _questions(conn, session_id)}
    wanted = {q.key for q in asked}
    replan = new_ulid()
    stale = [str(q.id) for key, q in current.items() if key not in wanted and q.answered_at is None]
    if stale:
        await conn.execute(
            update(rewrite_questions)
            .where(rewrite_questions.c.id.in_(stale))
            .values(superseded_by=replan)
        )
    now = await conn.scalar(select(func.transaction_timestamp()))
    for question in asked:
        if question.key in current:
            if refresh:
                await _refresh_question(conn, current[question.key], question, now)
            continue
        auto = question.kind == "conflict" and not question.required
        await conn.execute(
            insert(rewrite_questions).values(
                id=new_ulid(),
                story_id=story_id,
                session_id=session_id,
                kind=question.kind,
                key=question.key,
                prompt=question.prompt,
                options=list(question.options),
                recommended_option_id=question.recommended,
                recommendation_reason=question.reason,
                severity=question.severity,
                required=question.required,
                # #224: a conflict the mode settles is pre-answered by the engine with its
                # recommendation; the author may override it until the apply.
                answer_option_id=question.recommended if auto else None,
                accepted_recommendation=True if auto else None,
                answered_by=ENGINE if auto else None,
                answered_at=now if auto else None,
            )
        )


async def _refresh_question(
    conn: AsyncConnection, row: Any, question: parsing.Question, now: Any
) -> None:
    options = {str(o["id"]): o for o in question.options}
    values: dict[str, Any] = {
        "options": list(question.options),
        "recommended_option_id": question.recommended,
        "recommendation_reason": question.reason,
        "severity": question.severity,
        "required": question.required,
    }
    answered = row.answer_option_id
    old = {str(o["id"]): o for o in row.options or []}
    if answered is not None and (
        answered not in options
        or dict(options[answered].get("effect") or {})
        != dict(old.get(answered, {}).get("effect") or {})
    ):
        auto = question.kind == "conflict" and not question.required
        values.update(
            answer_option_id=question.recommended if auto else None,
            answer_text=None,
            accepted_recommendation=True if auto else None,
            answered_by=ENGINE if auto else None,
            answered_at=now if auto else None,
        )
    elif row.answered_by == ENGINE and answered is not None:
        values.update(answer_option_id=question.recommended, accepted_recommendation=True)
    elif answered is not None:
        values["accepted_recommendation"] = answered == question.recommended
    await conn.execute(
        update(rewrite_questions).where(rewrite_questions.c.id == row.id).values(**values)
    )


async def _state_of(conn: AsyncConnection, session_id: str) -> str:
    open_required = [
        q for q in await _questions(conn, session_id) if q.required and q.answered_at is None
    ]
    return "awaiting_answers" if open_required else "awaiting_approval"


async def _expires(conn: AsyncConnection, settings: RewriteSettings) -> datetime:
    now = await conn.scalar(select(func.transaction_timestamp()))
    assert isinstance(now, datetime)  # noqa: S101 - Postgres answers a timestamp
    return now + timedelta(minutes=settings.rewrite_session_ttl_minutes)


async def _answer(conn: AsyncConnection, story_id: str, session_id: str) -> RewriteWrite:
    row = await _row(conn, story_id, session_id)
    revision = int((await _story(conn, story_id)).timeline_revision)
    return RewriteWrite(await view(conn, row), revision)


# -- create ----------------------------------------------------------------------------------------


async def open_session(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    request: RewriteRequest,
    *,
    principal: str,
    kind: str = "rewrite",
) -> RewriteWrite:
    """Open a session (see the module docstring); the Engine has run the prechecks and the
    input gate. ``kind``: ``directive`` for a durable directive about the past (#226)."""
    async with db.connect() as conn:
        settings = await read_section(conn, RewriteSettings)
        pipeline = await pipeline_of(conn, story_id)
        _planner_of(pipeline)
        edit = request.line_edit
        await _guard(
            conn, story_id, [edit.new_text if edit is not None else request.instruction or ""]
        )
        story = await parsing.load_story(
            conn,
            story_id,
            await _registry(conn, story_id),
            margin=settings.rewrite_ambiguity_margin,
        )
    from epistrel.orchestrator.rewrite import lite as lite_rewrite

    gated: list[dict[str, Any]]
    no_canon: list[dict[str, Any]] = []
    reach_meta: dict[str, Any] | None = None
    if edit is not None:
        # #700: a direct line edit plans its one record change with no model call (no parse,
        # no reach, no output gate: the author's text passed the input gate).
        result = await lite_rewrite.line_edit_change(db, story_id, edit)
        gated = []
    else:
        assert request.instruction is not None  # noqa: S101 - the request validates one
        # The model call runs with no transaction open (FR-CONC-2).
        result = await parsing.call_parse(models, story, request.instruction, [])
        if pipeline != "structured":
            # #245 (FR-LITE-8): a Lite story's reach is model-searched over its transcript.
            reached = await lite_rewrite.reach(db, models, story_id, request.instruction)
            if reached.unfixed[:1] == [{"reason": "reach_unavailable"}]:
                from epistrel.core.errors import RewriteReachFailed

                raise RewriteReachFailed(
                    "the reach pass could not search the transcript", {"retryable": True}
                )
            reach_meta = reached.meta()
            result, no_canon = lite_rewrite.lite_result(result, reached.changes)
            no_canon.extend(reached.unfixed)
        result, gated = await gate_new_texts(db, models, story_id, result)
        gated = [*gated, *no_canon]
    mode, threshold = request.mode()
    async with db.begin() as conn:
        parsed, plan, revision = await _compute(
            conn,
            story_id,
            pipeline,
            result,
            {},
            settings,
            gated=gated,
            mode=mode,
            threshold=threshold,
        )
        if not parsed.changes and not parsed.questions and not parsed.refused:
            raise RewriteNoop(
                "the instruction asks for no change the story does not already have",
                {"already_true": len(parsed.noops)},
            )
        external = [r for r in parsed.refused if r.get("reason") == "external_owned"]
        if external and not parsed.changes and not parsed.questions:
            # #242: a rewrite whose only changes are game-owned values opens no session.
            from epistrel.core.errors import ExternalValueNotOverridable

            raise ExternalValueNotOverridable(
                str(external[0]["explanation"]),
                {
                    "changes": [
                        {
                            "provider": r.get("provider"),
                            "value_name": r.get("value_name"),
                            "scope_key": None,
                        }
                        for r in external
                    ],
                    "explanation": str(external[0]["explanation"]),
                },
            )
        locked = await _story(conn, story_id, lock=True)
        from epistrel.core import pause

        await pause.refuse(conn, story_id, locked.status)
        await _limit(conn, story_id, settings)
        session_id = new_ulid()
        await conn.execute(
            insert(rewrite_sessions).values(
                id=session_id,
                story_id=story_id,
                kind=kind,
                instruction=request.instruction,
                principal=principal,
                resolution_mode=mode,
                threshold_severity=threshold,
                # #700: a truncated reach always previews first.
                preview="preview_first"
                if reach_meta is not None and reach_meta["truncated"]
                else request.preview,
                state="awaiting_answers",
                pipeline=pipeline,
                base_revision=revision,
                plan={
                    **_stored_plan(result, {}, [], parsed, plan, gated),
                    **({} if edit is None else {"line_edit": edit.model_dump(mode="json")}),
                    **_lite_note(pipeline, request.instruction, no_canon),
                    **({} if reach_meta is None else {"reach": reach_meta}),
                },
                plan_hash=None if plan is None else plan.plan_hash,
                plan_read_set=None if plan is None else plan.read_set,
                expires_at=await _expires(conn, settings),
            )
        )
        await _write_questions(conn, story_id, session_id, parsed.questions)
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(state=await _state_of(conn, session_id))
        )
        await _index(conn, session_id)
        written = await _answer(conn, story_id, session_id)
    return await _apply_directly(db, models, story_id, written, principal=principal)


LITE_NOTE_PREFIX = "Author correction: "


def _lite_note(
    pipeline: str, instruction: str | None, refused: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """#700 (FR-LITE-8): a Lite rewrite whose instruction states a world change (the parse
    gave proposition or State changes, which Lite cannot hold: ``lite_no_canon``) records one
    ``lite_note`` part with the instruction, rendered under "Author corrections"."""
    from epistrel.orchestrator.rewrite.lite import NO_CANON

    if pipeline == "structured" or not instruction:
        return {}
    if not any(r.get("reason") == NO_CANON for r in refused):
        return {}
    return {"lite_note": LITE_NOTE_PREFIX + instruction}


async def _apply_directly(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    written: RewriteWrite,
    *,
    principal: str,
    run: rewrite_apply.Run | None = None,
) -> RewriteWrite:
    """``apply_directly``: the call that leaves nothing pending applies (FR-REWRITE-4)."""
    session = written.session
    if session.preview_mode != "apply_directly" or session.state != "awaiting_approval":
        return written
    return await apply_session(
        db,
        models,
        story_id,
        session.id,
        ApplyRequest(base_revision=session.base_revision),
        principal=principal,
        run=run,
    )


# -- identity merges (#225) -------------------------------------------------------------------


def merge_parsed(entity_ids: Sequence[str]) -> parsing.Parsed:
    """An identity merge's changes: each other entity ``same_as`` the first (the survivor), a
    timeless proposition change; no ``rewrite_parse`` call."""
    from epistrel.core.amendments.store import PropositionPartDraft
    from epistrel.core.propositions.model import Interval, Proposition
    from epistrel.core.propositions.vocabulary import SAME_AS

    survivor, *others = entity_ids
    found = parsing.Parsed()
    for other in others:
        key = parsing.prop_key(survivor, SAME_AS) + f":{other}"
        proposition = Proposition(
            subject_id=survivor,
            predicate_id=SAME_AS,
            object_id=other,
            valid_time=Interval.of(None, None),
        )
        summary = {
            "kind": "proposition",
            "key": key,
            "subject_id": survivor,
            "predicate_id": SAME_AS,
            "object_id": other,
            "literal": None,
            "polarity": "affirmed",
            "valid_from_minutes": None,
            "valid_to_minutes": None,
            "superseded_object_id": None,
        }
        found.changes.append(
            parsing.Normalized(key, "proposition", PropositionPartDraft(proposition), summary)
        )
    return found


async def _check_merge(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> None:
    from epistrel.core.canon import facts as canon
    from epistrel.core.errors import MergeInvalid
    from epistrel.core.propositions.vocabulary import SAME_AS
    from epistrel.core.tables import entities

    def invalid(reason: str) -> MergeInvalid:
        return MergeInvalid(
            "the entities cannot be merged", {"reason": reason, "entity_ids": list(ids)}
        )

    if len(set(ids)) < 2 or len(set(ids)) != len(ids):
        raise invalid("too_few")
    rows = list(
        await conn.execute(
            select(entities.c.id, entities.c.kind).where(
                entities.c.story_id == story_id, entities.c.id.in_(list(ids))
            )
        )
    )
    if len(rows) != len(ids):
        raise invalid("not_in_story")
    if len({str(r.kind) for r in rows}) != 1:
        raise invalid("type_mismatch")
    survivor = ids[0]
    joined = {survivor}
    for fact in await canon.canon(conn, story_id, predicate=SAME_AS):
        p = fact.proposition
        if fact.valid_to is None and p.object_id is not None:
            pair = {str(p.subject_id), str(p.object_id)}
            if pair & joined:
                joined |= pair
    if set(ids[1:]) <= joined:
        raise invalid("already_same")


async def open_merge(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    request: MergeRequest,
    *,
    principal: str,
) -> RewriteWrite:
    """An identity merge (#225, FR-PROP-7) as a rewrite session of kind ``entity_merge``: one
    ``same_as`` change per merged entity, planned, disposed and applied like any rewrite."""
    ids = list(request.entity_ids)
    mode, threshold = request.mode()
    async with db.begin() as conn:
        settings = await read_section(conn, RewriteSettings)
        pipeline = await pipeline_of(conn, story_id)
        planner = _planner_of(pipeline)
        await _check_merge(conn, story_id, ids)
        parsed = merge_parsed(ids)
        story = await parsing.load_story(
            conn,
            story_id,
            await _registry(conn, story_id),
            margin=settings.rewrite_ambiguity_margin,
        )
        revision = int((await _story(conn, story_id)).timeline_revision)
        plan = await planner.plan(conn, story, parsed, base_revision=revision)
        from epistrel.orchestrator.rewrite import hypothetical

        # #713: what the merged entities' facts say against the survivor's canon.
        plan.irreducible = [
            *plan.irreducible,
            *await hypothetical.merge_contradictions(conn, story_id, ids),
        ]
        parsed.questions.extend(dispose(plan.irreducible, mode, threshold).questions)
        locked = await _story(conn, story_id, lock=True)
        from epistrel.core import pause

        await pause.refuse(conn, story_id, locked.status)
        await _limit(conn, story_id, settings)
        session_id = new_ulid()
        stored = _stored_plan(RewriteParseResult(), {}, [], parsed, plan)
        stored["merge"] = {
            "entity_ids": ids,
            "identity_proposal_id": request.identity_proposal_id,
        }
        await conn.execute(
            insert(rewrite_sessions).values(
                id=session_id,
                story_id=story_id,
                kind="entity_merge",
                instruction=None,
                principal=principal,
                resolution_mode=mode,
                threshold_severity=threshold,
                preview=request.preview,
                state="awaiting_answers",
                pipeline=pipeline,
                base_revision=revision,
                plan=stored,
                plan_hash=plan.plan_hash,
                plan_read_set=plan.read_set,
                expires_at=await _expires(conn, settings),
            )
        )
        await _write_questions(conn, story_id, session_id, parsed.questions)
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(state=await _state_of(conn, session_id))
        )
        await _index(conn, session_id)
        written = await _answer(conn, story_id, session_id)
    return await _apply_directly(db, models, story_id, written, principal=principal)


# -- undo and redo (#238) -----------------------------------------------------------------------


async def open_amendment_session(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    amendment_id: str,
    request: AmendmentSessionRequest,
    *,
    kind: str,
    principal: str,
) -> RewriteWrite:
    """An undo or redo of an amendment (#238, FR-REWRITE-10) as a rewrite session of that kind
    (``target_amendment_id`` the amendment): its changes are the inverse of the amendment's
    active parts (undo) or its original parts (redo), planned, disposed and applied like any
    rewrite's (see ``rewrite.undo``). The Engine has run the prechecks; the checks here, in
    order: the open-session limit, the amendment's status (``undo.check_undo`` /
    ``check_redo``), then the planner dispatch (a Lite story 409 ``module_off``)."""
    from epistrel.core import pause
    from epistrel.orchestrator.rewrite import undo

    if kind not in ("undo", "redo"):
        raise ValueError(f"not an amendment session: {kind}")
    mode, threshold = request.mode()
    async with db.begin() as conn:
        settings = await read_section(conn, RewriteSettings)
        locked = await _story(conn, story_id, lock=True)
        await pause.refuse(conn, story_id, locked.status)
        await _limit(conn, story_id, settings)
        if kind == "undo":
            await undo.check_undo(conn, story_id, amendment_id)
        else:
            await undo.check_redo(conn, story_id, amendment_id)
        pipeline = await pipeline_of(conn, story_id)
        _planner_of(pipeline)
        stored_source = {kind: {"amendment_id": amendment_id}}
        sourced = await _sourced(conn, story_id, stored_source)
        assert sourced is not None  # noqa: S101 - an undo or redo is sourced
        parsed, plan, revision = await _plan_sourced(
            conn, story_id, pipeline, sourced, settings, stored_source, mode, threshold
        )
        session_id = new_ulid()
        stored = _stored_plan(RewriteParseResult(), {}, [], parsed, plan)
        stored.update(stored_source)
        await conn.execute(
            insert(rewrite_sessions).values(
                id=session_id,
                story_id=story_id,
                kind=kind,
                instruction=None,
                target_amendment_id=amendment_id,
                principal=principal,
                resolution_mode=mode,
                threshold_severity=threshold,
                preview=request.preview,
                state="awaiting_answers",
                pipeline=pipeline,
                base_revision=revision,
                plan=stored,
                plan_hash=plan.plan_hash,
                plan_read_set=plan.read_set,
                expires_at=await _expires(conn, settings),
            )
        )
        await _write_questions(conn, story_id, session_id, parsed.questions)
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(state=await _state_of(conn, session_id))
        )
        await _index(conn, session_id)
        written = await _answer(conn, story_id, session_id)
    return await _apply_directly(db, models, story_id, written, principal=principal)


async def open_directed(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    document: Mapping[str, Any],
    *,
    resolution_mode: Any,
    preview: str,
    principal: str,
    run: rewrite_apply.Run | None = None,
) -> RewriteWrite:
    """#241 (FR-COMMIT-12): an author-directed binding that conflicts with recorded history
    proceeds as an amendment: a ``directed_resolution`` session (``instruction``: the
    direction) whose changes are the binding's obligations and whose conflicts are the lines
    and records it contradicts, under the request's modes. ``document``: the direction
    (``orchestrator.commit.directed.Directed.document``) with its ``obligations``, ``lines``,
    ``records`` and the ``regenerate`` overrides to replay at apply."""
    from epistrel.core import pause

    mode = "threshold" if isinstance(resolution_mode, ThresholdMode) else str(resolution_mode)
    threshold = resolution_mode.threshold if isinstance(resolution_mode, ThresholdMode) else None
    async with db.begin() as conn:
        settings = await read_section(conn, RewriteSettings)
        locked = await _story(conn, story_id, lock=True)
        await pause.refuse(conn, story_id, locked.status)
        await _limit(conn, story_id, settings)
        pipeline = await pipeline_of(conn, story_id)
        _planner_of(pipeline)
        stored_source = {"directed": dict(document)}
        sourced = await _sourced(conn, story_id, stored_source)
        assert sourced is not None  # noqa: S101 - a directed resolution is sourced
        parsed, plan, revision = await _plan_sourced(
            conn, story_id, pipeline, sourced, settings, stored_source, mode, threshold
        )
        session_id = new_ulid()
        stored = _stored_plan(RewriteParseResult(), {}, [], parsed, plan)
        stored.update(stored_source)
        await conn.execute(
            insert(rewrite_sessions).values(
                id=session_id,
                story_id=story_id,
                kind="directed_resolution",
                instruction=str(document.get("text") or ""),
                principal=principal,
                resolution_mode=mode,
                threshold_severity=threshold,
                preview=preview,
                state="awaiting_answers",
                pipeline=pipeline,
                base_revision=revision,
                plan=stored,
                plan_hash=plan.plan_hash,
                plan_read_set=plan.read_set,
                expires_at=await _expires(conn, settings),
            )
        )
        await _write_questions(conn, story_id, session_id, parsed.questions)
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(state=await _state_of(conn, session_id))
        )
        await _index(conn, session_id)
        written = await _answer(conn, story_id, session_id)
    return await _apply_directly(db, models, story_id, written, principal=principal, run=run)


# -- answers ---------------------------------------------------------------------------------------


def _open(row: Any) -> None:
    if str(row.state) in TERMINAL:
        raise RewriteSessionClosed(
            "the rewrite session has ended", {"session_id": str(row.id), "state": str(row.state)}
        )


async def _superseded(conn: AsyncConnection, session_id: str, question_id: str, live: Any) -> None:
    """409 ``rewrite_question_superseded`` for an answer to a question a re-plan superseded
    (#237), naming the question now asked under its key."""
    q = rewrite_questions.c
    old = (
        await conn.execute(
            select(q.key).where(
                q.session_id == session_id, q.id == question_id, q.superseded_by.is_not(None)
            )
        )
    ).first()
    if old is None:
        return
    now = next((str(x.id) for x in live if str(x.key) == str(old.key)), None)
    raise RewriteQuestionSuperseded(
        "a re-plan superseded that question",
        {"question_id": question_id, "new_question_id": now},
    )


def _placement(question: Any) -> bool:
    """Whether a question places a correction in the story (#240's ``timing`` question)."""
    return str(question.kind) == "timing" and any(
        "bound" in dict(o.get("effect") or {}) for o in question.options or []
    )


def free_texts(request: AnswerRequest) -> list[str]:
    """The free-text answers (gated by the Engine before ``answer``)."""
    return [a.text for a in request.answers if a.text is not None]


async def _store_answers(
    conn: AsyncConnection, chosen: Sequence[tuple[Any, Any, dict[str, Any] | None]], principal: str
) -> Any:
    now = await conn.scalar(select(func.transaction_timestamp()))
    for item, question, option in chosen:
        await conn.execute(
            update(rewrite_questions)
            .where(rewrite_questions.c.id == question.id)
            .values(
                answer_option_id=None if option is None else str(option["id"]),
                answer_text=item.text,
                accepted_recommendation=item.accept_recommended
                or (option is not None and str(option["id"]) == question.recommended_option_id),
                answered_by=principal,
                answered_at=now,
            )
        )
    return now


async def _record_answers(
    db: AsyncEngine,
    story_id: str,
    session_id: str,
    chosen: Sequence[tuple[Any, Any, dict[str, Any] | None]],
    principal: str,
) -> None:
    async with db.begin() as conn:
        row = await _row(conn, story_id, session_id, lock=True)
        _open(row)
        await _store_answers(conn, chosen, principal)


async def answer(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    session_id: str,
    request: AnswerRequest,
    *,
    principal: str,
) -> RewriteWrite:
    """Record answers and re-plan at the story's current revision (see the module
    docstring)."""
    async with db.connect() as conn:
        settings = await read_section(conn, RewriteSettings)
        row = await _row(conn, story_id, session_id)
        _open(row)
    if str(row.kind) == "support_check":
        # #239: the system session's questions are answered by the support check.
        from epistrel.orchestrator.rewrite import support_check

        found: RewriteWrite = await support_check.answer(
            db, models, story_id, session_id, request, principal=principal
        )
        return found
    async with db.connect() as conn:
        row = await _row(conn, story_id, session_id)
        asked = {str(q.id): q for q in await _questions(conn, session_id)}
        chosen: list[tuple[Any, Any, dict[str, Any] | None]] = []
        for item in request.answers:
            question = asked.get(item.question_id)
            if question is None:
                await _superseded(conn, session_id, item.question_id, asked.values())
                raise InvalidAnswer(
                    "the session does not ask that question",
                    {"question_id": item.question_id},
                )
            options = {str(o["id"]): o for o in question.options or []}
            option_id = (
                str(question.recommended_option_id) if item.accept_recommended else item.option_id
            )
            if option_id is not None and option_id not in options:
                raise InvalidAnswer(
                    "the question has no such option",
                    {"question_id": item.question_id, "option_id": option_id},
                )
            chosen.append((item, question, None if option_id is None else options[option_id]))
        texts = free_texts(request)
        if texts:
            await _guard(conn, story_id, texts)
        cancelling = any(
            option is not None and dict(option.get("effect") or {}).get("action") == "cancel"
            for _, _, option in chosen
        )
        plan = dict(row.plan or {})
        bindings: dict[str, dict[str, Any]] = dict(plan.get("bindings") or {})
        answers: list[str] = list(plan.get("answers") or [])
        reparse = False
        for item, question, option in chosen:
            if option is not None:
                effect = dict(option.get("effect") or {})
                bindings[str(question.key)] = effect
                if "choice" in effect:
                    answers.append(f"{question.prompt} {option['label']}")
                    reparse = True
            elif _placement(question):
                # #240: a free-text placement is read deterministically, never guessed.
                from epistrel.orchestrator.rewrite import timing

                bound = timing.parse_bound(str(item.text or ""), await timing.head(conn, story_id))
                if bound is None:
                    raise InvalidAnswer(
                        "the placement could not be read: name a turn of the story "
                        "(before/after turn N) or the story's start",
                        {"question_id": str(question.id)},
                    )
                bindings[str(question.key)] = {"bound": bound}
            else:
                answers.append(f"{question.prompt} {item.text}")
                reparse = True
        pipeline = str(row.pipeline)
        _planner_of(pipeline)
        story = await parsing.load_story(
            conn,
            story_id,
            await _registry(conn, story_id),
            margin=settings.rewrite_ambiguity_margin,
        )
    if cancelling:
        # #238: a dependent amendment's ``cancel`` cancels the whole undo session.
        await _record_answers(db, story_id, session_id, chosen, principal)
        return await cancel(db, story_id, session_id, principal=principal)
    result = RewriteParseResult.model_validate(plan.get("parse") or {})
    gated: list[dict[str, Any]] = list(plan.get("gated") or [])
    if reparse:
        try:
            result = await parsing.call_parse(models, story, str(row.instruction or ""), answers)
        except EpistrelError as failed:
            if failed.code != "rewrite_unparseable":
                raise
            raise InvalidAnswer(
                "the answer could not be parsed", {"session_id": session_id}
            ) from failed
        result, gated = await gate_new_texts(db, models, story_id, result)
    async with db.begin() as conn:
        row = await _row(conn, story_id, session_id, lock=True)
        _open(row)
        now = await _store_answers(conn, chosen, principal)
        computed: Plan | None
        sourced = await _sourced(conn, story_id, plan)
        if sourced is not None:
            # A merge, undo or redo re-plans from its source, keeping it (#225, #238).
            parsed, computed, revision = await _plan_sourced(
                conn,
                story_id,
                pipeline,
                sourced,
                settings,
                plan,
                str(row.resolution_mode),
                row.threshold_severity,
            )
        else:
            parsed, computed, revision = await _compute(
                conn,
                story_id,
                pipeline,
                result,
                bindings,
                settings,
                gated=gated,
                mode=str(row.resolution_mode),
                threshold=row.threshold_severity,
            )
        stored = _stored_plan(result, bindings, answers, parsed, computed, gated)
        for source in KEPT:
            if plan.get(source) is not None:
                stored[source] = plan[source]
        await _write_questions(conn, story_id, session_id, parsed.questions)
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(
                plan=stored,
                plan_hash=None if computed is None else computed.plan_hash,
                plan_read_set=None if computed is None else computed.read_set,
                base_revision=revision,
                state=await _state_of(conn, session_id),
                updated_at=now,
                expires_at=await _expires(conn, settings),
            )
        )
        await _index(conn, session_id)
        written = await _answer(conn, story_id, session_id)
    return await _apply_directly(db, models, story_id, written, principal=principal)


# -- apply (#224) --------------------------------------------------------------------------------


async def _changes(conn: AsyncConnection, story_id: str, row: Any) -> list[Any]:
    """The session's normalized changes, again (the stored parse and bindings; deterministic)."""
    settings = await read_section(conn, RewriteSettings)
    plan = dict(row.plan or {})
    sourced = await _sourced(conn, story_id, plan)
    if sourced is not None:
        return list(sourced.changes)
    story = await parsing.load_story(
        conn, story_id, await _registry(conn, story_id), margin=settings.rewrite_ambiguity_margin
    )
    parsed = await parsing.normalize(
        conn,
        story,
        RewriteParseResult.model_validate(plan.get("parse") or {}),
        dict(plan.get("bindings") or {}),
    )
    return list(parsed.changes)


async def _abandon(db: AsyncEngine, story_id: str, session_id: str) -> None:
    async with db.begin() as conn:
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(state="abandoned", updated_at=func.transaction_timestamp())
        )
        asked = await _all_questions(conn, session_id)
    await rewrite_apply.close_audit(
        db,
        story_id,
        session_id,
        "abandoned",
        rewrite_apply.audit_events(session_id, asked),
        principal=ENGINE,
    )


async def _all_questions(conn: AsyncConnection, session_id: str) -> list[Any]:
    q = rewrite_questions.c
    return list(
        await conn.execute(
            select(rewrite_questions).where(q.session_id == session_id).order_by(q.created_at, q.id)
        )
    )


UNDO_DEPTH = 3
"""How many levels of dependent amendments an undo's blast radius follows."""
SOURCES = ("merge", "undo", "redo", "directed")
"""Sessions whose changes come from the engine, not a parse: an identity merge (#225), an undo
and a redo (#238); the stored plan names the source."""
KEPT = (*SOURCES, "line_edit", "lite_note", "reach")
"""What a re-plan keeps of the stored plan (#700: a line edit's target, a Lite world note)."""


async def _sourced(
    conn: AsyncConnection, story_id: str, stored: Mapping[str, Any]
) -> parsing.Parsed | None:
    """A sourced session's changes, again (deterministic); ``None`` for a parsed one."""
    from epistrel.orchestrator.rewrite import undo

    if stored.get("merge") is not None:
        return merge_parsed(list(stored["merge"]["entity_ids"]))
    if stored.get("undo") is not None:
        return await undo.undo_parsed(conn, story_id, str(stored["undo"]["amendment_id"]))
    if stored.get("redo") is not None:
        return await undo.redo_parsed(conn, story_id, str(stored["redo"]["amendment_id"]))
    if stored.get("directed") is not None:
        return _directed_parsed(stored["directed"])
    return None


def _directed_parsed(document: Mapping[str, Any]) -> parsing.Parsed:
    """#241: a directed resolution's changes: the binding's implied obligations at the thunk's
    time, as proposition parts (the resolution fact itself is the regenerated turn's)."""
    from epistrel.core.amendments.store import PropositionPartDraft
    from epistrel.core.propositions.model import Proposition

    parsed = parsing.Parsed()
    for raw in document.get("obligations") or []:
        proposition = Proposition.model_validate(raw)
        key = parsing.prop_key(proposition.subject_id, proposition.predicate_id)
        valid = proposition.valid_time
        start = getattr(getattr(valid, "start", None), "inner", None)
        end = getattr(getattr(valid, "end", None), "inner", None)
        summary = {
            "kind": "proposition",
            "key": key,
            "subject_id": proposition.subject_id,
            "predicate_id": proposition.predicate_id,
            "object_id": proposition.object_id,
            "literal": None,
            "polarity": proposition.polarity.value,
            "valid_from_minutes": start,
            "valid_to_minutes": end,
            "superseded_object_id": None,
            "directed": True,
        }
        draft = PropositionPartDraft(proposition, valid_from_minutes=start, valid_to_minutes=end)
        parsed.changes.append(parsing.Normalized(key, "proposition", draft, summary))
    return parsed


async def _directed_conflicts(
    conn: AsyncConnection, document: Mapping[str, Any], plan: Plan
) -> list[dict[str, Any]]:
    """#241: the directed binding's conflicts, beside the plan's own: each conflicting line of
    the bound character (``rewrite | drop | keep``) and each conflicting record
    (``drop | keep``), recommended by #224's least change."""
    from epistrel.orchestrator.rewrite.apply import _text_of
    from epistrel.orchestrator.rewrite.plan import _conflict_id

    known = {(c["record"]["table"], c["record"]["id"]) for c in plan.irreducible}
    change_key = plan.changes[0]["key"] if plan.changes else "directed"
    found: list[dict[str, Any]] = []
    for table, ids, options in (
        ("events", document.get("lines") or [], ["rewrite", "drop", "keep"]),
        ("facts", document.get("records") or [], ["drop", "keep"]),
    ):
        for record_id in ids:
            if (table, str(record_id)) in known:
                continue
            text = await _text_of(conn, table, str(record_id)) if table == "events" else None
            found.append(
                {
                    "record": {"table": table, "id": str(record_id)},
                    "edge_type": "asserts" if table == "events" else "presupposes",
                    "edges": [],
                    "change_key": change_key,
                    "target": {"kind": "fact", "id": str(record_id)},
                    "action": "fix",
                    "conflict_id": _conflict_id(change_key, table, str(record_id)),
                    "reason": "directed_conflict",
                    "severity": "high",
                    "options": options,
                    "excerpt": None if text is None else text[:160],
                    "before_excerpt": None if text is None else text[:160],
                }
            )
    return found


async def _plan_sourced(
    conn: AsyncConnection,
    story_id: str,
    pipeline: str,
    parsed: parsing.Parsed,
    settings: RewriteSettings,
    stored: Mapping[str, Any],
    mode: str,
    threshold: str | None,
) -> tuple[parsing.Parsed, Plan, int]:
    """Plan a sourced session's changes and dispose of its conflicts by the mode (an undo's
    conflicts take its options, its dependent amendments ``dependent`` questions)."""
    import dataclasses

    from epistrel.orchestrator.rewrite import undo

    story = await parsing.load_story(
        conn, story_id, await _registry(conn, story_id), margin=settings.rewrite_ambiguity_margin
    )
    revision = int((await _story(conn, story_id)).timeline_revision)
    plan = await _planner_of(pipeline).plan(conn, story, parsed, base_revision=revision)
    if stored.get("undo") is not None:
        target = str(stored["undo"]["amendment_id"])
        # A dependent amendment is recommended for cascade-undo, so the undo's blast radius
        # takes in what depends on it too (#238), to ``UNDO_DEPTH`` levels.
        undone = {target}
        for _ in range(UNDO_DEPTH):
            relying = await undo.relying_amendments(conn, plan.irreducible, undone)
            if not relying:
                break
            for amendment_id in sorted(relying):
                parsed.changes.extend(
                    (await undo.undo_parsed(conn, story_id, amendment_id)).changes
                )
            undone |= relying
            plan = await _planner_of(pipeline).plan(conn, story, parsed, base_revision=revision)
        mine = {target}
        plan.irreducible = [
            c
            for c in await undo.checked_undo_options(
                conn, story_id, plan.irreducible, parsed.changes
            )
            if not await _own_part(conn, c, mine)
        ]
        plan.cascaded = [c for c in plan.cascaded if not await _own_part(conn, c, mine)]
        plan.incidental = [c for c in plan.incidental if not await _own_part(conn, c, mine)]
    if stored.get("directed") is not None:
        plan.irreducible = [
            *plan.irreducible,
            *await _directed_conflicts(conn, stored["directed"], plan),
        ]
    if stored.get("merge") is not None:
        from epistrel.orchestrator.rewrite import hypothetical

        # #713: what the merged entities' facts say against the survivor's canon.
        plan.irreducible = [
            *plan.irreducible,
            *await hypothetical.merge_contradictions(
                conn, story_id, list(stored["merge"]["entity_ids"])
            ),
        ]
    asked = dispose(plan.irreducible, mode, threshold).questions
    if stored.get("undo") is not None:
        dependent = {
            question_key("conflict", c["conflict_id"])
            for c in plan.irreducible
            if c["record"]["table"] == "amendment_parts"
        }
        asked = tuple(
            dataclasses.replace(q, kind="dependent") if q.key in dependent else q for q in asked
        )
    parsed.questions.extend(asked)
    return parsed, plan, revision


async def _own_part(conn: AsyncConnection, entry: Mapping[str, Any], undone: set[str]) -> bool:
    """Whether a planned entry is one of the undone amendment's own parts, or a part no longer
    active (neither is a dependent)."""
    from epistrel.core.tables import amendment_parts

    record = entry["record"]
    if record["table"] != "amendment_parts":
        return False
    row = (
        await conn.execute(
            select(amendment_parts.c.amendment_id, amendment_parts.c.status).where(
                amendment_parts.c.id == record["id"]
            )
        )
    ).one_or_none()
    return row is None or str(row.amendment_id) in undone or str(row.status) != "active"


async def _recompute(
    conn: AsyncConnection, story_id: str, row: Any, settings: RewriteSettings
) -> tuple[parsing.Parsed, Plan | None, int, dict[str, Any]]:
    """The session's plan again at the current state, from its stored parse and bindings (no
    model call): the parse, the plan, the revision it was computed at and the stored plan."""
    stored = dict(row.plan or {})
    result = RewriteParseResult.model_validate(stored.get("parse") or {})
    bindings: dict[str, dict[str, Any]] = dict(stored.get("bindings") or {})
    gated: list[dict[str, Any]] = list(stored.get("gated") or [])
    mode, threshold = str(row.resolution_mode), row.threshold_severity
    plan: Plan | None
    sourced = await _sourced(conn, story_id, stored)
    if sourced is not None:
        parsed, plan, revision = await _plan_sourced(
            conn, story_id, str(row.pipeline), sourced, settings, stored, mode, threshold
        )
    else:
        parsed, plan, revision = await _compute(
            conn,
            story_id,
            str(row.pipeline),
            result,
            bindings,
            settings,
            gated=gated,
            mode=mode,
            threshold=threshold,
        )
    again = _stored_plan(result, bindings, list(stored.get("answers") or []), parsed, plan, gated)
    for source in KEPT:
        if stored.get(source) is not None:
            again[source] = stored[source]
    return parsed, plan, revision, again


def _revalidation(story_id: str, row: Any, settings: RewriteSettings) -> Any:
    """The apply's commit-time check (#237): ``revalidate`` under the story row lock; anything
    but ``Fresh`` raises ``Revalidated`` (the apply rolls back)."""

    async def replan(conn: AsyncConnection) -> Mapping[str, Any] | None:
        _, plan, _, _ = await _recompute(conn, story_id, row, settings)
        return None if plan is None else plan.read_set

    async def check(conn: AsyncConnection) -> None:
        current = int(
            await conn.scalar(select(stories.c.timeline_revision).where(stories.c.id == story_id))
            or 0
        )
        outcome = await revalidate.revalidate(
            conn,
            story_id,
            base_revision=int(row.base_revision or 0),
            current_revision=current,
            read_set=dict(row.plan_read_set or {}),
            replan=replan,
        )
        if not isinstance(outcome, revalidate.Fresh):
            raise revalidate.Revalidated(outcome)

    return check


async def _restale(
    db: AsyncEngine, story_id: str, session_id: str, reasons: list[dict[str, Any]]
) -> tuple[RewriteWrite, dict[str, Any]]:
    """A stale plan, recomputed outside any lock at the current revision (#237): questions whose
    key the new plan asks again keep their answers (carried) unless the answered option is gone
    or changed its effect; new ones are added and the rest superseded. ``stale_count`` + 1,
    ``last_error {code: rewrite_stale, reasons}``, ``expires_at`` extended. The details of the
    409 the caller raises."""
    async with db.begin() as conn:
        settings = await read_section(conn, RewriteSettings)
        row = await _row(conn, story_id, session_id, lock=True)
        before = {str(q.key): q for q in await _questions(conn, session_id)}
        parsed, computed, revision, stored = await _recompute(conn, story_id, row, settings)
        await _write_questions(conn, story_id, session_id, parsed.questions, refresh=True)
        after = await _questions(conn, session_id)
        carried = [
            {
                "question_id": str(q.id),
                "carried_from": str(before[str(q.key)].id),
                **(
                    {"option_id": q.answer_option_id}
                    if q.answer_option_id is not None
                    else {"text": q.answer_text}
                ),
            }
            for q in after
            if str(q.key) in before and q.answered_at is not None and q.answered_by != ENGINE
        ]
        fresh = [
            _question_view(q).model_dump(mode="json") for q in after if str(q.key) not in before
        ]
        count = int(row.stale_count) + 1
        now = await conn.scalar(select(func.transaction_timestamp()))
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(
                plan=stored,
                plan_hash=None if computed is None else computed.plan_hash,
                plan_read_set=None if computed is None else computed.read_set,
                base_revision=revision,
                stale_count=count,
                last_error={
                    "code": "rewrite_stale",
                    "reasons": reasons,
                    "at": str(now),
                },
                state=await _state_of(conn, session_id),
                updated_at=now,
                expires_at=await _expires(conn, settings),
            )
        )
        await _index(conn, session_id)
        written = await _answer(conn, story_id, session_id)
    session = written.session
    details = {
        "session_id": session_id,
        "base_revision": revision,
        "preview": None if session.preview is None else session.preview.model_dump(mode="json"),
        "carried_answers": carried,
        "new_questions": fresh,
        "reasons": reasons,
        "stale_count": count,
    }
    return written, details


async def _abandon_erased(
    db: AsyncEngine, story_id: str, session_id: str, outcome: revalidate.Abandon
) -> ErasedSubjectReferenced:
    """An erasure touched what the plan reads (#237): the session is ``abandoned``; the 409
    withholds the subject id."""
    await _abandon(db, story_id, session_id)
    return ErasedSubjectReferenced(
        "an erasure touched what this rewrite reads; the session is abandoned",
        {"erasure_id": outcome.erasure_id, "session_id": session_id},
    )


async def apply_session(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    session_id: str,
    request: ApplyRequest,
    *,
    principal: str,
    run: rewrite_apply.Run | None = None,
) -> RewriteWrite:
    """Apply the session's plan as one amendment (#224; see ``rewrite.apply``). The checks, in
    order: 404; an ended session 409 ``rewrite_session_closed``, one being applied 409
    ``rewrite_apply_in_progress``; a paused story 409 ``story_paused``; a pipeline that changed
    since the session opened 409 ``rewrite_stale {reason: pipeline_changed}`` (the session is
    ``abandoned``); no ``base_revision`` 422 ``base_revision_required``, another one than the
    session's preview base 409 ``rewrite_stale {reason: base_mismatch}`` (with the current
    preview, nothing recomputed); unanswered required questions 409
    ``rewrite_questions_pending``.

    Then (#237) the apply's commit revalidates the plan under the story row lock
    (``revalidate``): a story that moved only outside the blast radius applies; a stale plan is
    recomputed (``_restale``) and, under ``apply_directly`` with nothing left to answer, applied
    again up to ``rewrite_stale_retries`` times, else 409 ``rewrite_stale`` with the new
    preview; an erasure touching the plan abandons the session (409
    ``erased_subject_referenced``).

    ``run`` (#712): a ``directed_resolution`` session's apply is staged on its directed
    regeneration's turn, which ``run`` plays and commits: the variant and the amendment commit
    in one transaction, or neither does."""
    retries = 0
    while True:
        try:
            return await _apply_once(
                db, models, story_id, session_id, request, principal=principal, run=run
            )
        except revalidate.Revalidated as signal:
            outcome = signal.outcome
            if isinstance(outcome, revalidate.Abandon):
                raise await _abandon_erased(db, story_id, session_id, outcome) from None
            written, details = await _restale(db, story_id, session_id, outcome.reasons)
            async with db.connect() as conn:
                settings = await read_section(conn, RewriteSettings)
            session = written.session
            if (
                session.preview_mode == "apply_directly"
                and session.state == "awaiting_approval"
                and retries < settings.rewrite_stale_retries
            ):
                retries += 1
                request = ApplyRequest(base_revision=session.base_revision)
                continue
            raise RewriteStale("the plan went stale; it was recomputed", details) from None


async def _apply_once(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    session_id: str,
    request: ApplyRequest,
    *,
    principal: str,
    run: rewrite_apply.Run | None = None,
) -> RewriteWrite:
    from epistrel.core import pause

    async with db.begin() as conn:
        settings = await read_section(conn, RewriteSettings)
        row = await _row(conn, story_id, session_id, lock=True)
        if str(row.state) == "applying":
            raise RewriteApplyInProgress(
                "the rewrite session is being applied", {"session_id": session_id}
            )
        _open(row)
        story = await _story(conn, story_id)
        await pause.refuse(conn, story_id, story.status)
        current = int(story.timeline_revision)
        pipeline = await pipeline_of(conn, story_id)
        changed = pipeline != str(row.pipeline)
        if not changed:
            _planner_of(pipeline)
            if request.base_revision is None:
                raise BaseRevisionRequired(
                    "an apply names the revision its preview was computed against",
                    {"field": "base_revision"},
                )
            base = None if row.base_revision is None else int(row.base_revision)
            if request.base_revision != base:
                preview = dict(row.plan or {}).get("preview")
                raise RewriteStale(
                    "the apply names another revision than the session's preview",
                    {
                        "reason": "base_mismatch",
                        "base_revision": base,
                        "requested_base_revision": request.base_revision,
                        "current_revision": current,
                        "preview": preview,
                        "stale_count": int(row.stale_count),
                    },
                )
            pending = [
                str(q.id)
                for q in await _questions(conn, session_id)
                if q.required and q.answered_at is None
            ]
            if pending:
                raise RewriteQuestionsPending(
                    "the session has questions to answer first", {"question_ids": pending}
                )
            await conn.execute(
                update(rewrite_sessions)
                .where(rewrite_sessions.c.id == session_id)
                .values(state="applying", updated_at=func.transaction_timestamp())
            )
    if changed:
        await _abandon(db, story_id, session_id)
        raise RewriteStale(
            "the story's pipeline changed since the session opened",
            {
                "reason": "pipeline_changed",
                "base_revision": request.base_revision,
                "current_revision": current,
            },
        )
    async with db.connect() as conn:
        abandon = await revalidate.erased(conn, story_id, dict(row.plan_read_set or {}))
    if abandon is not None:
        raise revalidate.Revalidated(abandon)
    try:
        async with db.connect() as conn:
            changes = await _changes(conn, story_id, row)
            asked = await _all_questions(conn, session_id)
        prepared = await rewrite_apply.prepare(
            db, models, story_id, dict(row.plan or {}), changes, asked
        )
        done = await rewrite_apply.apply(
            db,
            story_id,
            session_id,
            prepared,
            principal=principal,
            audit=rewrite_apply.audit_events(session_id, asked),
            check=_revalidation(story_id, row, settings),
            run=run,
        )
    except revalidate.Revalidated:
        raise
    except Exception as failed:
        await _failed(db, session_id, failed)
        if isinstance(failed, rewrite_apply.InducedApplyFailure | OperationalError):
            raise RewriteApplyFailed(
                "the rewrite could not be applied; nothing was written",
                {"retryable": True, "session_id": session_id},
            ) from failed
        raise
    async with db.begin() as conn:
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(
                state="applied",
                result_amendment_id=done.amendment_id,
                operation_id=done.operation_id,
                report=done.report,
                last_error=None,
                updated_at=func.transaction_timestamp(),
            )
        )
        await _index(conn, session_id)
        written = await _answer(conn, story_id, session_id)
    return RewriteWrite(
        written.session,
        done.timeline_revision,
        Applied(
            amendment_id=done.amendment_id,
            report=done.report,
            timeline_revision=done.timeline_revision,
        ),
    )


async def _failed(db: AsyncEngine, session_id: str, failed: BaseException) -> None:
    """A failed apply is not terminal: back to ``awaiting_approval`` with ``last_error``."""
    code = getattr(failed, "code", type(failed).__name__)
    async with db.begin() as conn:
        attempt = await conn.scalar(
            select(rewrite_sessions.c.last_error).where(rewrite_sessions.c.id == session_id)
        )
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(
                state="awaiting_approval",
                last_error={
                    "code": str(code),
                    "message": type(failed).__name__,
                    "at": str(await conn.scalar(select(func.transaction_timestamp()))),
                    "attempt": int(dict(attempt or {}).get("attempt", 0)) + 1,
                },
                updated_at=func.transaction_timestamp(),
            )
        )


# -- cancel and expiry -----------------------------------------------------------------------------


async def cancel(
    db: AsyncEngine, story_id: str, session_id: str, *, principal: str = ENGINE
) -> RewriteWrite:
    """Cancel the session: ``cancelled``, nothing committed; its questions and answers are
    recorded as audit events in a non-bumping operation (#224)."""
    async with db.begin() as conn:
        row = await _row(conn, story_id, session_id, lock=True)
        if str(row.state) == "applying":
            raise RewriteApplyInProgress(
                "the rewrite session is being applied", {"session_id": session_id}
            )
        _open(row)
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(state="cancelled", updated_at=func.transaction_timestamp())
        )
        asked = await _all_questions(conn, session_id)
    await rewrite_apply.close_audit(
        db,
        story_id,
        session_id,
        "cancelled",
        rewrite_apply.audit_events(session_id, asked),
        principal=principal,
    )
    async with db.connect() as conn:
        return await _answer(conn, story_id, session_id)


async def expire_idle(db: AsyncEngine) -> int:
    """``rewrite_session_expiry``: move sessions idle past their ``expires_at`` to ``expired``
    (``applying`` never expires); answers how many moved."""
    s = rewrite_sessions.c
    async with db.begin() as conn:
        due = (
            select(s.id)
            .where(
                and_(
                    s.state.in_(["awaiting_answers", "awaiting_approval"]),
                    s.expires_at < func.transaction_timestamp(),
                )
            )
            .order_by(s.expires_at)
            .limit(EXPIRY_BATCH)
            .with_for_update(skip_locked=True)
        )
        moved = list(
            await conn.execute(
                update(rewrite_sessions)
                .where(s.id.in_(due.scalar_subquery()))
                .values(state="expired", updated_at=func.transaction_timestamp())
                .returning(s.id, s.story_id)
            )
        )
    for session_id, story_id in moved:
        async with db.connect() as conn:
            asked = await _all_questions(conn, str(session_id))
        await rewrite_apply.close_audit(
            db,
            str(story_id),
            str(session_id),
            "expired",
            rewrite_apply.audit_events(str(session_id), asked),
            principal=ENGINE,
        )
    return len(moved)


__all__ = [
    "LITE_REWRITES",
    "answer",
    "apply_session",
    "cancel",
    "expire_idle",
    "free_texts",
    "get",
    "list_sessions",
    "merge_parsed",
    "open_merge",
    "open_session",
    "pipeline_of",
    "precheck",
    "require_planner",
    "view",
]
