"""Amendments across truncation, swap and fork: the support check (FR-STORE-22,29, FR-REWRITE-12;
#239).

Amendments occupy no narrative position, so they outlive the timeline edits that move records in
and out of view, and are re-checked when those edits commit. ``support_check`` runs as a
``post_commit`` membership hook (``core.timeline``: truncate, untruncate, swap; and a fork's
copy, on the fork). It never changes story truth or the revision: it **surfaces**.

- **Unsupported**: an active amendment one of whose ``rationale_record_ids`` records, or one of
  whose parts' #220 ``relies_on`` targets, is no longer active in the story (``discarded`` or
  ``dormant``, or absent: a fork did not copy it).
- **Conflict**: an active proposition part whose written fact is no longer current canon (a
  newly active chain superseded it).
- **Port** (after a swap): a record part on a record that went dormant, where the newly active
  variant of the same turn has a record of the same type and speaker whose text contains the
  part's replaced text: the engine suggests porting the fix, and ports it only when the author
  approves (never by itself).

Each finding is a question of one open system session of kind ``support_check`` per story
(``principal = system``, never expiring while open, not counted against the session limit):
``support`` questions offer ``keep`` | ``undo`` | ``rewrite`` (recommended ``keep`` when the
amendment's parts still hold, ``undo`` otherwise); ``port`` questions offer each candidate and
``skip`` (recommended ``port`` on an exact match). A question's ``key`` is the sha256 of the
amendment, the reason and the refs, so the same finding is never asked twice; a later check
that finds support restored supersedes the open question. The amendment stays in force and
unchanged until answered.

**Answers** (``answer``): ``keep`` records the decision (a kept conflict is a notification in
the session's report); ``undo`` opens a #238 undo session; ``rewrite`` opens a rewrite session
on the free text; ``port`` records a new amendment with the record part on the chosen record
(``payload.ported_from``); ``skip`` records nothing. The session closes (``applied``) when its
last question is answered.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import timeline
from epistrel.core.enums import Membership
from epistrel.core.ids import new_ulid
from epistrel.core.tables import (
    amendment_parts,
    amendments,
    events,
    facts,
    metadata,
    operations,
    rewrite_questions,
    rewrite_sessions,
    transcript_messages,
)
from epistrel.core.tables.edges import dependency_edges

log = logging.getLogger(__name__)

SYSTEM = "system"
KIND = "support_check"
NEVER = timedelta(days=365 * 100)
SUPPORT_OPTIONS = ("keep", "undo", "rewrite")
LABELS = {
    "keep": "Keep the amendment as it is",
    "undo": "Undo the amendment",
    "rewrite": "Rewrite it (give the new instruction as free text)",
    "port": "Port the fix to this line",
    "skip": "Leave the new line as it is",
}
PROMPTS = {
    "unsupported": (
        "An amendment relied on records that are no longer in the active story. What should "
        "happen to it?"
    ),
    "conflict": (
        "An amendment's correction no longer agrees with the active story. What should happen "
        "to it?"
    ),
    "port": (
        "A line an amendment fixed is no longer in the active story; the active version of the "
        "turn has a line like it. Port the fix?"
    ),
}
REASONS = {
    "keep": "The amendment's corrections still hold against the active story.",
    "undo": "The amendment's corrections no longer hold against the active story.",
    "port": "The active line contains the fixed text exactly.",
    "skip": "The active line only resembles the fixed text.",
}


@dataclass(frozen=True)
class Finding:
    """One thing to ask about: the amendment (or, for a port, the part), why, the refs and
    whether its parts still verify."""

    amendment_id: str
    reason: str
    refs: tuple[tuple[str, str], ...]
    verifies: bool = True
    part_id: str | None = None
    candidates: tuple[tuple[str, str, bool], ...] = ()
    """A port's candidates: ``(table, id, exact)``."""

    @property
    def key(self) -> str:
        raw = json.dumps(
            [self.amendment_id, self.reason, self.part_id, sorted(self.refs)], sort_keys=True
        )
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Report:
    """A check's outcome: the questions it added and those it closed."""

    session_id: str | None = None
    added: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)


def recommend(verifies: bool) -> str:
    """A support question's recommendation: ``keep`` when the amendment's parts still hold
    against the active story, else ``undo`` (owner decision 4: always one)."""
    return "keep" if verifies else "undo"


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


# -- findings ----------------------------------------------------------------------------------


async def _active(conn: AsyncConnection, story_id: str, table: str, record_id: str) -> bool:
    """Whether the story still holds ``record_id`` as an active record (a record of another
    story, such as one a fork left behind, is not held)."""
    t = metadata.tables.get(table)
    if t is None or "id" not in t.c:
        return True
    mine = t.c.id == record_id
    if "story_id" in t.c:
        mine = mine & (t.c.story_id == story_id)
    if "membership" in t.c:
        found = await conn.scalar(select(t.c.membership).where(mine))
        return found is not None and str(found) == Membership.ACTIVE.value
    if "operation_id" in t.c:
        op = await conn.scalar(select(t.c.operation_id).where(mine))
        if op is None:
            return False
        membership = await conn.scalar(
            select(operations.c.membership).where(operations.c.operation_id == op)
        )
        return str(membership) == Membership.ACTIVE.value
    return await conn.scalar(select(t.c.id).where(mine)) is not None


async def _current(conn: AsyncConnection, story_id: str, fact_id: str) -> bool:
    """Whether the fact (or a later version of its lineage an amendment wrote) is current."""
    from epistrel.core.canon import facts as canon

    row = (
        await conn.execute(
            select(facts.c.proposition_id, facts.c.membership).where(facts.c.id == fact_id)
        )
    ).one_or_none()
    if row is None or str(row.membership) != Membership.ACTIVE.value:
        return False
    _, current = await canon._load(conn, story_id, fact_id)
    return bool(current)


async def findings(conn: AsyncConnection, story_id: str) -> list[Finding]:
    """The story's unsupported and conflicting amendments (see the module docstring)."""
    a, p, e = amendments.c, amendment_parts.c, dependency_edges.c
    found: list[Finding] = []
    heads = list(
        await conn.execute(
            select(a.id, a.rationale_record_ids)
            .where(a.story_id == story_id, a.status == "active")
            .order_by(a.tx_time, a.id)
        )
    )
    for head in heads:
        parts = list(
            await conn.execute(
                select(p.id, p.kind, p.written_fact_id, p.state_id).where(
                    p.amendment_id == head.id, p.status == "active"
                )
            )
        )
        excluded: list[tuple[str, str]] = []
        for ref in head.rationale_record_ids or []:
            table, record_id = str(ref.get("table")), str(ref.get("id"))
            if not await _active(conn, story_id, table, record_id):
                excluded.append((table, record_id))
        part_ids = [str(x.id) for x in parts]
        if part_ids:
            relies = await conn.execute(
                select(e.target_kind, e.target_id).where(
                    e.story_id == story_id,
                    e.dependent_table == "amendment_parts",
                    e.dependent_id.in_(part_ids),
                )
            )
            for kind, target in relies:
                kind = str(getattr(kind, "value", kind))
                if kind == "fact":
                    gone = await conn.scalar(select(facts.c.membership).where(facts.c.id == target))
                    if gone is None or str(gone) != Membership.ACTIVE.value:
                        excluded.append(("facts", str(target)))
                elif kind == "amendment_part":
                    status = await conn.scalar(select(p.status).where(p.id == target))
                    if status is None or str(status) != "active":
                        excluded.append(("amendment_parts", str(target)))
        contradicted = [
            ("facts", str(x.written_fact_id))
            for x in parts
            if x.kind == "proposition"
            and x.state_id is None
            and x.written_fact_id is not None
            and not await _current(conn, story_id, str(x.written_fact_id))
        ]
        verifies = not contradicted
        if excluded:
            found.append(
                Finding(str(head.id), "unsupported", tuple(sorted(set(excluded))), verifies)
            )
        if contradicted:
            found.append(Finding(str(head.id), "conflict", tuple(sorted(set(contradicted))), False))
    return found


async def _speaker(conn: AsyncConnection, event_id: str) -> tuple[Any, Any, Any]:
    row = (
        await conn.execute(
            select(events.c.type, events.c.character_id, operations.c.narrative_turn)
            .select_from(
                events.join(operations, operations.c.operation_id == events.c.operation_id)
            )
            .where(events.c.id == event_id)
        )
    ).one_or_none()
    return (None, None, None) if row is None else (row.type, row.character_id, row.narrative_turn)


async def _text(conn: AsyncConnection, event_id: str) -> str | None:
    found = await conn.scalar(
        select(transcript_messages.c.text).where(transcript_messages.c.message_id == event_id)
    )
    if found is not None:
        return str(found)
    payload = await conn.scalar(select(events.c.payload).where(events.c.id == event_id))
    text = dict(payload or {}).get("text")
    return None if text is None else str(text)


async def ports(conn: AsyncConnection, story_id: str) -> list[Finding]:
    """Port suggestions: active ``replace`` record parts on events that are no longer active,
    with the newly active records of the same turn, type and speaker containing the replaced
    text (see the module docstring)."""
    a, p = amendments.c, amendment_parts.c
    found: list[Finding] = []
    rows = await conn.execute(
        select(p.id, p.amendment_id, p.record_id, p.patch_before)
        .select_from(amendment_parts.join(amendments, a.id == p.amendment_id))
        .where(
            a.story_id == story_id,
            a.status == "active",
            p.status == "active",
            p.kind == "record",
            p.record_table == "events",
            p.action == "replace",
        )
    )
    for part in rows:
        if await _active(conn, story_id, "events", str(part.record_id)):
            continue
        before = dict(part.patch_before or {}).get("text")
        if not before:
            continue
        kind, speaker, turn = await _speaker(conn, str(part.record_id))
        if turn is None:
            continue
        others = await conn.execute(
            select(events.c.id)
            .select_from(
                events.join(operations, operations.c.operation_id == events.c.operation_id)
            )
            .where(
                events.c.story_id == story_id,
                events.c.type == kind,
                events.c.character_id.is_(None)
                if speaker is None
                else events.c.character_id == speaker,
                operations.c.narrative_turn == turn,
                operations.c.membership == Membership.ACTIVE.value,
                events.c.id != part.record_id,
            )
            .order_by(events.c.seq)
        )
        candidates: list[tuple[str, str, bool]] = []
        for (other,) in others:
            text = await _text(conn, str(other))
            if text is None:
                continue
            if str(before) in text:
                candidates.append(("events", str(other), True))
            elif _normal(str(before)) in _normal(text):
                candidates.append(("events", str(other), False))
        if candidates:
            found.append(
                Finding(
                    str(part.amendment_id),
                    "port",
                    (("events", str(part.record_id)),),
                    part_id=str(part.id),
                    candidates=tuple(candidates),
                )
            )
    return found


# -- the system session ------------------------------------------------------------------------


async def system_session(conn: AsyncConnection, story_id: str, *, create: bool) -> str | None:
    """The story's open ``support_check`` session (made when ``create``)."""
    r = rewrite_sessions.c
    found = await conn.scalar(
        select(r.id).where(
            r.story_id == story_id,
            r.kind == KIND,
            r.state.in_(("awaiting_answers", "awaiting_approval")),
        )
    )
    if found is not None or not create:
        return None if found is None else str(found)
    pipeline = "structured"
    now = await conn.scalar(select(func.transaction_timestamp()))
    assert isinstance(now, datetime)  # noqa: S101 - Postgres answers a timestamp
    session_id = new_ulid()
    await conn.execute(
        insert(rewrite_sessions).values(
            id=session_id,
            story_id=story_id,
            kind=KIND,
            instruction=None,
            principal=SYSTEM,
            resolution_mode="synchronous",
            preview="preview_first",
            state="awaiting_answers",
            pipeline=pipeline,
            plan={"support_check": True},
            expires_at=now + NEVER,
        )
    )
    return session_id


def _options(finding: Finding) -> tuple[list[dict[str, Any]], str, str]:
    """The question's options (each effect names the amendment, and a port's the part), its
    recommendation and the reason."""
    about: dict[str, str] = {"amendment_id": finding.amendment_id}
    if finding.reason == "port":
        about["part_id"] = str(finding.part_id)
        options: list[dict[str, Any]] = [
            {
                "id": f"o{n}",
                "label": LABELS["port"],
                "effect": {"action": "port", "record": {"table": t, "id": i}, **about},
            }
            for n, (t, i, _) in enumerate(finding.candidates, start=1)
        ]
        options.append(
            {
                "id": f"o{len(options) + 1}",
                "label": LABELS["skip"],
                "effect": {"action": "skip", **about},
            }
        )
        exact = next((n for n, (_, _, ok) in enumerate(finding.candidates) if ok), None)
        if exact is not None:
            return options, str(options[exact]["id"]), REASONS["port"]
        return options, str(options[-1]["id"]), REASONS["skip"]
    options = [
        {"id": f"o{n}", "label": LABELS[a], "effect": {"action": a, **about}}
        for n, a in enumerate(SUPPORT_OPTIONS, start=1)
    ]
    best = recommend(finding.verifies)
    chosen = SUPPORT_OPTIONS.index(best)
    return options, str(options[chosen]["id"]), REASONS[best]


async def run(db: AsyncEngine, story_id: str, *, cause: str, operation_id: str) -> Report:
    """Check the story's amendments (see the module docstring); idempotent: it reads the
    latest state, and a finding already asked is not asked again."""
    report = Report()
    async with db.begin() as conn:
        found = await findings(conn, story_id)
        if cause in ("swap", "regenerate", "untruncate", "truncate"):
            found += await ports(conn, story_id)
        q = rewrite_questions.c
        session_id = await system_session(conn, story_id, create=bool(found))
        report.session_id = session_id
        if session_id is None:
            return report
        open_rows = list(
            await conn.execute(
                select(q.id, q.key, q.kind).where(
                    q.session_id == session_id, q.superseded_by.is_(None), q.answered_at.is_(None)
                )
            )
        )
        known = {
            str(k) for (k,) in await conn.execute(select(q.key).where(q.session_id == session_id))
        }
        wanted = {f.key for f in found}
        restored = [str(r.id) for r in open_rows if str(r.key) not in wanted]
        if restored:
            await conn.execute(
                update(rewrite_questions)
                .where(rewrite_questions.c.id.in_(restored))
                .values(superseded_by=operation_id)
            )
            report.closed += restored
        for finding in found:
            if finding.key in known:
                continue
            options, recommended, reason = _options(finding)
            question_id = new_ulid()
            await conn.execute(
                insert(rewrite_questions).values(
                    id=question_id,
                    story_id=story_id,
                    session_id=session_id,
                    kind="port" if finding.reason == "port" else "support",
                    key=finding.key,
                    prompt=PROMPTS[finding.reason],
                    options=options,
                    recommended_option_id=recommended,
                    recommendation_reason=reason,
                    severity="low" if finding.verifies else "high",
                    required=True,
                )
            )
            report.added.append(question_id)
        await _settle(conn, session_id)
    log.info(
        "support check %s after %s: %d added, %d closed",
        story_id,
        cause,
        len(report.added),
        len(report.closed),
    )
    return report


async def _settle(conn: AsyncConnection, session_id: str) -> None:
    """Close the session when no question is left open."""
    q = rewrite_questions.c
    left = await conn.scalar(
        select(func.count()).where(
            q.session_id == session_id, q.superseded_by.is_(None), q.answered_at.is_(None)
        )
    )
    if not left:
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(state="applied", updated_at=func.transaction_timestamp())
        )


async def pending(conn: AsyncConnection, story_id: str) -> dict[str, Any] | None:
    """``{count, session_id}`` of the story's open support questions (``None`` when none)."""
    session_id = await system_session(conn, story_id, create=False)
    if session_id is None:
        return None
    q = rewrite_questions.c
    count = await conn.scalar(
        select(func.count()).where(
            q.session_id == session_id, q.superseded_by.is_(None), q.answered_at.is_(None)
        )
    )
    return None if not count else {"count": int(count), "session_id": session_id}


# -- answers -----------------------------------------------------------------------------------


async def answer(
    db: AsyncEngine,
    models: Any,
    story_id: str,
    session_id: str,
    request: Any,
    *,
    principal: str,
) -> Any:
    """Answer a support session's questions (see the module docstring)."""
    from epistrel.core.errors import InvalidAnswer
    from epistrel.orchestrator.rewrite import session as rewrite_session
    from epistrel.orchestrator.rewrite.api import AmendmentSessionRequest, RewriteRequest

    q = rewrite_questions.c
    decisions: list[dict[str, Any]] = []
    async with db.connect() as conn:
        asked = {
            str(r.id): r
            for r in await conn.execute(
                select(rewrite_questions).where(
                    q.session_id == session_id, q.superseded_by.is_(None)
                )
            )
        }
    chosen: list[tuple[Any, Any, dict[str, Any]]] = []
    for item in request.answers:
        question = asked.get(item.question_id)
        if question is None or question.answered_at is not None:
            raise InvalidAnswer(
                "the session does not ask that question", {"question_id": item.question_id}
            )
        options = {str(o["id"]): o for o in question.options or []}
        option_id = (
            str(question.recommended_option_id) if item.accept_recommended else item.option_id
        )
        if option_id is None or option_id not in options:
            raise InvalidAnswer(
                "the question has no such option",
                {"question_id": item.question_id, "option_id": option_id},
            )
        action = str(options[option_id]["effect"]["action"])
        if action == "rewrite" and not item.text:
            raise InvalidAnswer(
                "a rewrite answer carries the new instruction as text",
                {"question_id": item.question_id},
            )
        chosen.append((item, question, options[option_id]))
    for item, question, option in chosen:
        effect = dict(option["effect"])
        action = str(effect["action"])
        amendment_id = None if effect.get("amendment_id") is None else str(effect["amendment_id"])
        decision: dict[str, Any] = {
            "question_id": str(question.id),
            "action": action,
            "amendment_id": amendment_id,
        }
        if action == "undo" and amendment_id is not None:
            opened = await rewrite_session.open_amendment_session(
                db,
                models,
                story_id,
                amendment_id,
                AmendmentSessionRequest(),
                kind="undo",
                principal=principal,
            )
            decision["session_id"] = opened.session.id
        elif action == "rewrite":
            opened = await rewrite_session.open_session(
                db,
                models,
                story_id,
                RewriteRequest(instruction=str(item.text)),
                principal=principal,
            )
            decision["session_id"] = opened.session.id
        elif action == "port":
            decision["ported_amendment_id"] = await _port(
                db, story_id, str(effect["part_id"]), dict(effect["record"]), principal=principal
            )
        elif action == "keep" and question.severity == "high":
            decision["notification"] = {"kind": "kept_conflicting", "amendment_id": amendment_id}
        decisions.append(decision)
    async with db.begin() as conn:
        now = await conn.scalar(select(func.transaction_timestamp()))
        for item, question, option in chosen:
            await conn.execute(
                update(rewrite_questions)
                .where(q.id == question.id)
                .values(
                    answer_option_id=str(option["id"]),
                    answer_text=item.text,
                    accepted_recommendation=str(option["id"]) == question.recommended_option_id,
                    answered_by=principal,
                    answered_at=now,
                )
            )
        report = dict(
            (
                await conn.scalar(
                    select(rewrite_sessions.c.report).where(rewrite_sessions.c.id == session_id)
                )
            )
            or {}
        )
        report["decisions"] = [*report.get("decisions", []), *decisions]
        report["notifications"] = [
            *report.get("notifications", []),
            *(d["notification"] for d in decisions if "notification" in d),
        ]
        await conn.execute(
            update(rewrite_sessions)
            .where(rewrite_sessions.c.id == session_id)
            .values(report=report, updated_at=now)
        )
        await _settle(conn, session_id)
        return await rewrite_session._answer(conn, story_id, session_id)


async def _port(
    db: AsyncEngine, story_id: str, part_id: str, record: Mapping[str, str], *, principal: str
) -> str:
    """Record the approved port: a new amendment whose record part puts the fixed text on the
    chosen record (``payload.ported_from``: the part it ports)."""
    from epistrel.core.amendments.store import AmendmentKind, RecordPartDraft, record_amendment
    from epistrel.core.enums import OperationKind
    from epistrel.core.events import Operation

    async with db.connect() as conn:
        part = (
            await conn.execute(
                select(amendment_parts.c.patch_after).where(amendment_parts.c.id == part_id)
            )
        ).one()
    found: dict[str, str] = {}
    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.REWRITE_SESSION,
        principal=principal,
        bumps_revision=True,
    ) as op:

        async def step(conn: AsyncConnection) -> None:
            made = await record_amendment(
                conn,
                op,
                kind=AmendmentKind.REWRITE,
                parts=[
                    RecordPartDraft(
                        str(record["table"]),
                        str(record["id"]),
                        "replace",
                        dict(part.patch_after or {}),
                    )
                ],
                rationale_record_ids=[dict(record)],
                payload={"parts": 1, "ported_from": part_id},
            )
            found["id"] = made.amendment_id

        op.add_commit_step(step)
        await op.commit()
    return found["id"]


# -- the hook ----------------------------------------------------------------------------------


async def on_membership_change(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
    """The post-commit hook: check the story the edit (or the fork) changed. The hook's
    connection is the check's engine (a failure is logged by ``core.timeline``)."""
    from epistrel.orchestrator.rewrite.session import pipeline_of

    engine = conn.engine
    pipeline = await pipeline_of(conn, change.story_id)
    if conn.in_transaction():
        await conn.commit()
    if pipeline != "structured":
        return
    await run(engine, change.story_id, cause=change.cause, operation_id=change.control_operation_id)


timeline.on_membership_change(on_membership_change, phase=timeline.HookPhase.POST_COMMIT)


__all__ = [
    "KIND",
    "SYSTEM",
    "Finding",
    "Report",
    "answer",
    "findings",
    "on_membership_change",
    "pending",
    "ports",
    "recommend",
    "run",
    "system_session",
]
