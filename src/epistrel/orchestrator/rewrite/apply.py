"""Applying a rewrite session: one atomic amendment (FR-REWRITE-3,6,8, FR-DIRECT-4; #224).

**Preparation** (``prepare``, no transaction open across a model call, FR-CONC-2) turns the
session's plan and answers into #221 part drafts and a report:

- the **planned changes** (#223's normalized changes; an entity the instruction names for the
  first time is created in the apply transaction, ``new_entities``);
- the **incidental fixes** and **cascaded fixes** of event and memory text: the superseded
  value's surface forms replaced by the new value's name (a deterministic substitution; the
  plan only left a record cascaded when its text names the old value);
- the **relabels**: an utterance that asserted the old fact is relabelled
  (``objective_relation = false``), never rewritten (FR-DIRECT-8);
- the **resolutions** of conflicts, by the author's answer or the mode's auto-resolution:
  ``rewrite`` (the ``rewrite_record`` purpose's text, checked against the story's output
  policies and the erased subjects; a failure falls back to ``drop``), ``drop`` (an event's
  overlay drop; a memory's text emptied), ``keep`` (a ``kept_inconsistent`` notification
  only) and ``cascade_undo`` (#221's ``revert_amendment`` of the amendment the part belongs
  to, into this one).

Cascaded beliefs (``cascade``) and re-evaluated causes are re-derived by their own readers;
they are listed as ``regenerated`` notifications, not parts (#221 refuses belief record parts).

**The apply** (``apply``) is one ``Operation(kind = rewrite_session)``: its commit records one
amendment (``session_id`` set) with every part, the cascade-undo inverses and the
``rewrite_question``/``rewrite_answer``/``rewrite_closed`` audit events, and bumps the
revision by exactly one; anything that fails rolls all of it back. A record of a staged turn
that has not completed is 409 ``record_in_flight``. ``FAIL_AFTER_PARTS`` is the test fault
point ``rewrite.apply.after_part``: it fails the commit after the parts are written.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.amendments.store import (
    AmendmentKind,
    PropositionPartDraft,
    RecordPartDraft,
    record_amendment,
    revert_amendment,
)
from epistrel.core.enums import OperationKind
from epistrel.core.errors import EpistrelError, RecordInFlight
from epistrel.core.events import Operation
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import SAME_AS
from epistrel.core.tables import (
    amendment_parts,
    entities,
    events,
    memories,
    operations,
    utterances,
)
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.rewrite.dispose import ENGINE, least_change

FAIL_AFTER_PARTS = False
"""Off but under the fault ``epistrel.testing.faults.rewrite_apply_failing`` (U-09's induced
mid-apply failure)."""
UNFINISHED_STAGES = ("intent_committed", "awaiting_outcome", "outcome_recorded")
EXCERPT = 160


class InducedApplyFailure(RuntimeError):
    """The fault point ``rewrite.apply.after_part`` fired (a transient failure)."""


@dataclass
class Prepared:
    """What the apply writes: the parts (each with its report label), the amendments to undo,
    the entities to create and the report so far."""

    parts: list[tuple[str, Any, dict[str, Any]]] = field(default_factory=list)
    """``(bucket, draft, record)``: the bucket names where the part is reported."""
    pending: list[dict[str, Any]] = field(default_factory=list)
    """Planned changes naming an entity apply creates (their preview JSON)."""
    undo: list[str] = field(default_factory=list)
    beliefs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    """#225: firsthand beliefs that follow the change: ``(belief_id, the change's preview)``."""
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= EXCERPT else text[: EXCERPT - 1] + "…"


def substitute(text: str, old_forms: Sequence[str], new_name: str) -> str:
    """``text`` with each surface form of the old value replaced by ``new_name`` (whole words,
    case-insensitive, longest form first)."""
    result = text
    for form in sorted({f for f in old_forms if f}, key=len, reverse=True):
        result = re.sub(rf"\b{re.escape(form)}\b", new_name, result, flags=re.IGNORECASE)
    return result


async def _names(conn: AsyncConnection, entity_id: str | None) -> tuple[str, ...]:
    from epistrel.orchestrator.rewrite.plan import _surface_forms

    return await _surface_forms(conn, entity_id)


async def _text_of(conn: AsyncConnection, table: str, record_id: str) -> str | None:
    if table == "events":
        payload = await conn.scalar(select(events.c.payload).where(events.c.id == record_id))
        text = dict(payload or {}).get("text")
        return text if isinstance(text, str) else None
    if table == "memories":
        found = await conn.scalar(select(memories.c.text).where(memories.c.id == record_id))
        return None if found is None else str(found)
    return None


def _fix_draft(table: str, record_id: str, text: str) -> Any:
    return RecordPartDraft(table, record_id, "replace", {"text": text})


def _drop_draft(table: str, record_id: str) -> Any:
    if table == "events":
        return RecordPartDraft("events", record_id, "drop")
    return RecordPartDraft(table, record_id, "replace", {"text": ""})


async def _new_value(conn: AsyncConnection, change: Mapping[str, Any]) -> str | None:
    """The name the new value is written with (an entity's canonical name, or a literal)."""
    obj = change.get("object_id")
    if isinstance(obj, str) and not obj.startswith("new:"):
        name = await conn.scalar(select(entities.c.canonical_name).where(entities.c.id == obj))
        return None if name is None else str(name)
    if isinstance(obj, str):
        return obj.removeprefix("new:")
    literal = change.get("literal")
    return None if literal is None else str(literal)


async def _rewrite_text(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    text: str,
    kind: str,
    old: Sequence[str],
    canon: Sequence[str],
    guidance: str,
) -> str | None:
    """``rewrite_record``'s repair of ``text``, or ``None`` when it fails (no usable answer, an
    output policy refuses it, or it names a subject erased from the story)."""
    from epistrel.core.erasure import guard
    from epistrel.orchestrator.budget import TurnBudget
    from epistrel.orchestrator.models.adapter import ModelParams
    from epistrel.orchestrator.models.registry import (
        REWRITE_RECORD,
        RewriteRecordInputs,
        RewrittenRecord,
    )
    from epistrel.orchestrator.models.structured import Uncertain
    from epistrel.orchestrator.policies import output_gate
    from epistrel.orchestrator.policies import store as policy_store

    try:
        found = await models.structured_call(
            REWRITE_RECORD.name,
            RewrittenRecord,
            RewriteRecordInputs(
                record_text=text[:4_000],
                record_kind=kind,
                old_value=", ".join(old)[:500],
                new_canon=list(canon)[:20],
                guidance=guidance[:2_000],
            ),
            params=ModelParams(temperature=0.0),
            budget=TurnBudget(max_calls=2, deadline_s=60.0),
        )
    except EpistrelError:
        return None
    if isinstance(found, Uncertain) or not found.text.strip():
        return None
    context = await policy_store.guard_context(db, story_id)
    policies = output_gate.output_policies(context)
    if policies:
        checked = await output_gate.check_output(
            models, found.text, policies, context, budget=TurnBudget(max_calls=8, deadline_s=60.0)
        )
        if not checked.passed:
            return None
    try:
        async with db.connect() as conn:
            await guard.check_content(
                conn,
                story_id,
                [guard.Content((found.text,), frozenset(), consumer=False)],
                base_revision=None,
                path="epistrel.orchestrator.rewrite.session",
            )
    except EpistrelError:
        return None
    return found.text


def _answers(questions: Sequence[Any]) -> dict[str, tuple[str, bool, str | None]]:
    """Each conflict question's chosen action by its key: ``(action, auto, guidance)``."""
    chosen: dict[str, tuple[str, bool, str | None]] = {}
    for q in questions:
        if q.kind != "conflict":
            continue
        options = {str(o["id"]): o for o in q.options or []}
        option = options.get(str(q.answer_option_id or q.recommended_option_id))
        action = str((option or {}).get("effect", {}).get("action") or "keep")
        auto = q.answered_by in (None, ENGINE)
        chosen[str(q.key)] = (action, auto, q.answer_text)
    return chosen


async def prepare(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    plan: Mapping[str, Any],
    changes: Sequence[Any],
    questions: Sequence[Any],
) -> Prepared:
    """The apply's parts and report (see the module docstring); model calls only, no
    transaction across them."""
    from epistrel.orchestrator.rewrite.parse import question_key

    preview = dict(plan.get("preview") or {})
    radius = dict(preview.get("blast_radius") or {})
    prepared = Prepared()
    seen: set[tuple[str, str]] = set()
    by_key = {c.key: c for c in changes}
    for change in changes:
        if change.draft is None:
            prepared.pending.append(change.summary)
            continue
        if change.kind in ("record", "intent"):
            record = change.summary["record"]
            seen.add((str(record["table"]), str(record["id"])))
        if change.kind == "intent":
            prepared.notifications.append(change.summary["notice"])
        prepared.parts.append(("changes", change.draft, {"change_key": change.key}))
    regenerate: list[dict[str, Any]] = []
    async with db.connect() as conn:
        for bucket in ("incidental", "cascaded"):
            for entry in radius.get(bucket) or []:
                record = (entry["record"]["table"], entry["record"]["id"])
                if record in seen:
                    continue
                if entry.get("action") == "fix" and record[0] in ("events", "memories"):
                    change = by_key.get(entry.get("change_key"))
                    text = await _text_of(conn, *record)
                    if change is None or text is None:
                        continue
                    old = await _names(conn, change.summary.get("superseded_object_id"))
                    new = await _new_value(conn, change.summary)
                    if not old or new is None:
                        continue
                    fixed = substitute(text, old, new)
                    if fixed != text:
                        seen.add(record)
                        prepared.parts.append(
                            (bucket, _fix_draft(*record, fixed), {"record": entry["record"]})
                        )
                elif entry.get("action") == "cascade" and record[0] == "beliefs":
                    change = by_key.get(entry.get("change_key"))
                    if change is not None and change.kind == "proposition":
                        prepared.beliefs.append((record[1], change.summary))
                        seen.add(record)
                elif entry.get("action") == "keep" and record[0] == "beliefs":
                    change = by_key.get(entry.get("change_key"))
                    if change is not None and await _now_false(conn, record[1], change.summary):
                        prepared.notifications.append(
                            {"kind": "belief_now_false", "record": entry["record"]}
                        )
                elif entry.get("action") in ("re_evaluate", "regenerate") and record[0] in (
                    "events",
                    "memories",
                ):
                    regenerate.append(entry)
                elif entry.get("action") in ("re_evaluate", "regenerate", "cascade"):
                    prepared.notifications.append(
                        {"kind": "regenerated", "record": entry["record"]}
                    )
        followers = await _holders(conn, [b for b, _ in prepared.beliefs])
        for entry in preview.get("relabels") or []:
            record = (entry["record"]["table"], entry["record"]["id"])
            if record[0] != "utterances" or record in seen:
                continue
            exists = await conn.scalar(select(utterances.c.id).where(utterances.c.id == record[1]))
            if exists is None:
                continue
            change = by_key.get(entry.get("change_key"))
            relabel = (
                None
                if change is None
                else await _relabel(conn, record[1], change.summary, followers)
            )
            if relabel is None:
                continue
            patch, notice = relabel
            seen.add(record)
            prepared.parts.append(
                (
                    "relabels",
                    RecordPartDraft("utterances", record[1], "relabel", patch),
                    {"record": entry["record"]},
                )
            )
            if notice is not None:
                prepared.notifications.append(notice)
        for change in changes:
            if change.kind == "proposition" and change.summary.get("predicate_id") == SAME_AS:
                for record, patch, notice in await _merged_speech(
                    conn, story_id, change.summary, seen
                ):
                    seen.add(record)
                    prepared.parts.append(
                        (
                            "relabels",
                            RecordPartDraft("utterances", record[1], "relabel", patch),
                            {"record": {"table": record[0], "id": record[1]}},
                        )
                    )
                    prepared.notifications.append(notice)
        texts = {
            (e["record"]["table"], e["record"]["id"]): await _text_of(
                conn, e["record"]["table"], e["record"]["id"]
            )
            for e in radius.get("irreducible") or []
        }
        canon: dict[str, list[str]] = {}
        olds: dict[str, tuple[str, ...]] = {}
        for change in changes:
            new = await _new_value(conn, change.summary)
            canon[change.key] = [f"{change.summary.get('kind')}: now {new}"] if new else []
            olds[change.key] = await _names(conn, change.summary.get("superseded_object_id"))
    for entry in regenerate:
        record = (entry["record"]["table"], entry["record"]["id"])
        if record in seen:
            continue
        async with db.connect() as conn:
            before = await _text_of(conn, *record)
        rewritten = (
            None
            if before is None
            else await _rewrite_text(
                db,
                models,
                story_id,
                before,
                record[0],
                olds.get(entry["change_key"], ()),
                canon.get(entry["change_key"], []),
                "",
            )
        )
        seen.add(record)
        if rewritten is None:
            prepared.notifications.append(
                {"kind": "regeneration_failed", "record": entry["record"]}
            )
            continue
        prepared.parts.append(
            (
                "cascaded",
                _fix_draft(*record, rewritten),
                {"record": entry["record"], "regenerated": True},
            )
        )
    chosen = _answers(questions)
    for conflict in radius.get("irreducible") or []:
        record = (conflict["record"]["table"], conflict["record"]["id"])
        key = question_key("conflict", conflict["conflict_id"])
        action, auto, guidance = chosen.get(key, (least_change(conflict), True, None))
        before = texts.get(record)
        after: str | None = None
        if action == "rewrite":
            rewritten = (
                None
                if before is None
                else await _rewrite_text(
                    db,
                    models,
                    story_id,
                    before,
                    record[0],
                    olds.get(conflict["change_key"], ()),
                    canon.get(conflict["change_key"], []),
                    guidance or "",
                )
            )
            if rewritten is None:
                action = "drop"
            else:
                after = rewritten
                prepared.parts.append(
                    ("resolutions", _fix_draft(*record, rewritten), {"record": conflict["record"]})
                )
        if action == "drop":
            after = ""
            prepared.parts.append(
                ("resolutions", _drop_draft(*record), {"record": conflict["record"]})
            )
        elif action == "keep" and conflict.get("reason") == "decision_effects":
            async with db.connect() as conn:
                effects = await _effects_of(conn, record[1])
            prepared.notifications.append(
                {"kind": "decision_effects_kept", "record": conflict["record"], **effects}
            )
        elif action == "keep":
            prepared.notifications.append(
                {"kind": "kept_inconsistent", "record": conflict["record"]}
            )
        elif action == "cascade_undo":
            async with db.connect() as conn:
                target = await conn.scalar(
                    select(amendment_parts.c.amendment_id).where(amendment_parts.c.id == record[1])
                )
            if target is not None and str(target) not in prepared.undo:
                prepared.undo.append(str(target))
        seen.add(record)
        prepared.resolutions.append(
            {
                "conflict_id": conflict["conflict_id"],
                "action": action,
                "auto": auto,
                "severity": conflict.get("severity"),
                "records": [
                    {
                        "table": record[0],
                        "id": record[1],
                        "before_excerpt": _excerpt(before),
                        "after_excerpt": _excerpt(after),
                    }
                ],
            }
        )
    for entity in plan.get("preview", {}).get("new_entities") or []:
        prepared.notifications.append({"kind": "new_entity", "name": entity.get("name")})
    return prepared


async def _now_false(conn: AsyncConnection, belief_id: str, change: Mapping[str, Any]) -> bool:
    """Whether a kept belief now contradicts canon: it holds the value the change supersedes."""
    from epistrel.core.tables.beliefs import beliefs
    from epistrel.core.tables.propositions import propositions

    row = (
        await conn.execute(
            select(propositions.c.subject_id, propositions.c.predicate_id, propositions.c.object_id)
            .join(beliefs, beliefs.c.proposition_id == propositions.c.id)
            .where(beliefs.c.id == belief_id)
        )
    ).one_or_none()
    if row is None:
        return False
    return (
        str(row.subject_id) == change.get("subject_id")
        and str(row.predicate_id) == change.get("predicate_id")
        and row.object_id is not None
        and str(row.object_id) == change.get("superseded_object_id")
    )


RELABEL_LABELS = {("sincere_error", "true"): "sincere", ("sincere", "false"): "sincere_error"}


@dataclass(frozen=True, slots=True)
class SpeechAct:
    """An utterance's three independent dimensions and its #166 label (FR-TURN-6)."""

    objective: str
    belief: str
    intent: str
    label: str | None


def relabelled(
    act: SpeechAct,
    asserted: Sequence[str | None],
    *,
    new_object: str | None,
    old_object: str | None,
    speaker_follows: bool,
) -> SpeechAct:
    """What a retcon makes of an utterance (FR-DIRECT-8; #225), pure: its objective relation
    re-evaluated against the new canon (``true`` when it asserted the new value, ``false`` the
    superseded one); its belief relation re-evaluated only when the speaker's own belief on the
    key follows the change (``speaker_follows``: consistent when it asserted the new value,
    inconsistent the old); its intent never; its label moved only where these decide it (a
    sincere error now true is sincere, a sincere line now false a sincere error; a lie stays a
    lie, a joke a joke)."""
    objective, belief = act.objective, act.belief
    for claim in asserted:
        if claim is None:
            continue
        if claim == new_object:
            objective = "true"
            if speaker_follows:
                belief = "consistent"
        elif claim == old_object:
            objective = "false"
            if speaker_follows:
                belief = "inconsistent"
    label = RELABEL_LABELS.get((act.label or "", objective), act.label)
    return SpeechAct(objective, belief, act.intent, label)


async def _relabel(
    conn: AsyncConnection,
    utterance_id: str,
    change: Mapping[str, Any],
    followers: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """An utterance's relabel under the change (``relabelled``); ``None`` when nothing changes.
    ``followers`` are the characters whose firsthand belief on the key follows the change. The
    notification (``scene_meaning_may_change``) is given when the label changes, or when a lie's
    objective relation flips (an attempted lie that happened to be true)."""
    from epistrel.core.tables.propositions import propositions

    row = (
        await conn.execute(
            select(
                utterances.c.proposition_ids,
                utterances.c.speaker_character_id,
                utterances.c.objective_relation,
                utterances.c.belief_relation,
                utterances.c.intent,
                utterances.c.classification,
            ).where(utterances.c.id == utterance_id)
        )
    ).one_or_none()
    if row is None:
        return None
    claims = list(
        await conn.execute(
            select(
                propositions.c.subject_id, propositions.c.predicate_id, propositions.c.object_id
            ).where(propositions.c.id.in_(list(row.proposition_ids or [])))
        )
    )
    asserted = [
        None if c.object_id is None else str(c.object_id)
        for c in claims
        if (str(c.subject_id), str(c.predicate_id))
        == (change.get("subject_id"), change.get("predicate_id"))
    ]
    before = SpeechAct(
        str(row.objective_relation),
        str(row.belief_relation),
        str(row.intent),
        None if row.classification is None else str(row.classification),
    )
    after = relabelled(
        before,
        asserted,
        new_object=change.get("object_id"),
        old_object=change.get("superseded_object_id"),
        speaker_follows=str(row.speaker_character_id) in followers,
    )
    if after == before:
        return None
    patch: dict[str, Any] = {}
    if after.objective != before.objective:
        patch["objective_relation"] = after.objective
    if after.belief != before.belief:
        patch["belief_relation"] = after.belief
    if after.label != before.label:
        patch["classification"] = after.label
    notice = None
    if after.label != before.label or before.label == "lie":
        notice = {
            "kind": "scene_meaning_may_change",
            "utterance_id": utterance_id,
            "before": dataclasses.asdict(before),
            "after": dataclasses.asdict(after),
        }
    return patch, notice


OBJECTIVE_OF = {"supported": "true", "contradicted": "false"}


async def _merged_speech(
    conn: AsyncConnection,
    story_id: str,
    change: Mapping[str, Any],
    seen: set[tuple[str, str]],
) -> list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]]:
    """#225's identity merge: what people said about the merged-away entity keeps its text,
    intent and belief relation; its objective relation is re-evaluated against canon with the
    entity read as the survivor (each claim checked with the other's id replaced)."""
    from epistrel.core.propositions.codec import proposition_from_row
    from epistrel.core.readset import KeyedLookup, StructuredReadSet
    from epistrel.core.tables.propositions import propositions
    from epistrel.core.verify.rules import check_with

    survivor, other = str(change["subject_id"]), str(change["object_id"])
    p, u = propositions.c, utterances.c
    named = [
        str(x)
        for x in await conn.scalars(
            select(p.id).where(
                p.story_id == story_id, or_(p.subject_id == other, p.object_id == other)
            )
        )
    ]
    found: list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]] = []
    if not named:
        return found
    rows = list(
        await conn.execute(
            select(utterances).where(u.story_id == story_id, u.proposition_ids.overlap(named))
        )
    )
    lookup = KeyedLookup(conn, StructuredReadSet())
    for row in rows:
        record = ("utterances", str(row.id))
        if record in seen:
            continue
        claims = [
            proposition_from_row(r)
            for r in await conn.execute(
                select(propositions).where(p.id.in_(list(row.proposition_ids or [])))
            )
        ]
        verdicts = []
        for claim in claims:
            swapped = claim.model_copy(
                update={
                    "subject_id": survivor if claim.subject_id == other else claim.subject_id,
                    "object_id": survivor if claim.object_id == other else claim.object_id,
                }
            )
            verdict = await check_with(lookup, story_id, swapped)
            verdicts.append(OBJECTIVE_OF.get(str(verdict.kind.value), "unresolved"))
        objective = (
            "false"
            if "false" in verdicts
            else "true"
            if verdicts and all(v == "true" for v in verdicts)
            else str(row.objective_relation)
        )
        if objective == str(row.objective_relation):
            continue
        label = None if row.classification is None else str(row.classification)
        new_label = RELABEL_LABELS.get((label or "", objective), label)
        patch: dict[str, Any] = {"objective_relation": objective}
        if new_label != label:
            patch["classification"] = new_label
        before = SpeechAct(
            str(row.objective_relation), str(row.belief_relation), str(row.intent), label
        )
        after = SpeechAct(objective, before.belief, before.intent, new_label)
        found.append(
            (
                record,
                patch,
                {
                    "kind": "scene_meaning_may_change",
                    "utterance_id": record[1],
                    "before": dataclasses.asdict(before),
                    "after": dataclasses.asdict(after),
                },
            )
        )
    return found


async def _holders(conn: AsyncConnection, belief_ids: Sequence[str]) -> frozenset[str]:
    from epistrel.core.tables.beliefs import beliefs

    if not belief_ids:
        return frozenset()
    found = await conn.scalars(
        select(beliefs.c.holder_character_id).where(beliefs.c.id.in_(list(belief_ids)))
    )
    return frozenset(str(x) for x in found)


async def _effects_of(conn: AsyncConnection, event_id: str) -> dict[str, list[str]]:
    """The State changes and outbox rows a decision's beat committed (#225)."""
    from epistrel.core.tables import external_effect_outbox, state_changes

    row = (
        await conn.execute(
            select(events.c.operation_id, events.c.beat).where(events.c.id == event_id)
        )
    ).one()
    beat = row.beat or 0
    changes = await conn.scalars(
        select(state_changes.c.id).where(
            state_changes.c.operation_id == row.operation_id,
            state_changes.c.beat == beat,
            state_changes.c.amendment_part_id.is_(None),
        )
    )
    sent = await conn.scalars(
        select(external_effect_outbox.c.id).where(
            external_effect_outbox.c.operation_id == row.operation_id,
            external_effect_outbox.c.beat == beat,
        )
    )
    return {"state_change_ids": sorted(map(str, changes)), "outbox_ids": sorted(map(str, sent))}


async def _cascade_beliefs(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    cascades: Sequence[tuple[str, Mapping[str, Any]]],
    written: Sequence[str],
    event_id: str,
) -> list[dict[str, Any]]:
    """#225: each firsthand belief follows the change: the old belief is forgotten and the new
    proposition held from the same position with its confidence (``forgetting``, then the
    belief's own firsthand acquisition again, citing the same perception). A belief that cannot
    follow (the transition refuses) is reported, not failed."""
    from epistrel.core.enums import BeliefTransitionKind as Kind
    from epistrel.core.epistemic import partitions, transitions
    from epistrel.core.errors import InvalidRequest

    del event_id  # the amendment's own event cannot attest the belief (self-attestation)
    done: list[dict[str, Any]] = []
    for belief_id, change in cascades:
        belief = await partitions.read_belief(conn, story_id, belief_id)
        if belief is None or not written or change.get("object_id") is None:
            continue
        new = belief.proposition.model_copy(update={"object_id": change["object_id"]})
        try:
            async with conn.begin_nested():
                # The old belief is let go, and the same perception is observed again under
                # the corrected canon: held from the same position with the same confidence.
                history = await transitions.transitions_of(conn, story_id, belief_ids=[belief_id])
                first = next(
                    (t for t in history if t.kind in (Kind.OBSERVATION, Kind.DISCOVERY)), None
                )
                if first is None:
                    raise InvalidRequest(
                        "the belief has no firsthand acquisition", {"reason": "not_firsthand"}
                    )
                await transitions.transition(
                    conn,
                    op,
                    holder=belief.holder_character_id,
                    kind=Kind.FORGETTING,
                    proposition=belief.proposition,
                    antecedent=transitions.Antecedent.record("beliefs", belief_id),
                    # The step after the belief's own (an antecedent precedes its dependent).
                    position=dataclasses.replace(belief.held_from, step=belief.held_from.step + 1),
                )
                made = await transitions.transition(
                    conn,
                    op,
                    holder=belief.holder_character_id,
                    kind=first.kind,
                    proposition=new,
                    antecedent=first.antecedent,
                    position=belief.held_from,
                    source=first.source_character_id,
                    confidence=transitions.Confidence(belief.confidence_level),
                )
        except EpistrelError as refused:
            done.append(
                {
                    "belief_id": belief_id,
                    "cascaded": False,
                    "reason": refused.code,
                    "details": str(refused.details)[:600],
                }
            )
            continue
        done.append(
            {
                "belief_id": belief_id,
                "cascaded": True,
                "new_belief_id": None if made.belief is None else made.belief.id,
            }
        )
    return done


async def _in_flight(conn: AsyncConnection, parts: Sequence[Any]) -> None:
    """409 ``record_in_flight`` for a part on an event of a staged turn still under way."""
    ids = [
        d.record_id for _, d, _ in parts if isinstance(d, RecordPartDraft) and d.table == "events"
    ]
    if not ids:
        return
    found = (
        await conn.execute(
            select(events.c.id)
            .join(operations, operations.c.operation_id == events.c.operation_id)
            .where(events.c.id.in_(ids), operations.c.turn_stage.in_(UNFINISHED_STAGES))
            .limit(1)
        )
    ).first()
    if found is not None:
        raise RecordInFlight(
            "the record belongs to a staged turn that has not completed",
            {"record_table": "events", "record_id": str(found.id)},
        )


async def _create_pending(
    conn: AsyncConnection, story_id: str, pending: Sequence[Mapping[str, Any]]
) -> list[Any]:
    """The planned changes that name a new entity: the entities created, the parts built."""
    from epistrel.core.propositions import store

    made: dict[str, str] = {}
    drafts: list[Any] = []
    for change in pending:
        if str(change.get("predicate_id", "")).startswith("register:"):
            continue  # a predicate registration is not applied by a rewrite (logged)

        async def entity(value: Any) -> Any:
            if not isinstance(value, str) or not value.startswith("new:"):
                return value
            name = value.removeprefix("new:")
            if name not in made:
                made[name] = await store.add_entity(conn, story_id, "other", name)
            return made[name]

        subject = await entity(change["subject_id"])
        obj = await entity(change.get("object_id"))
        proposition = Proposition(
            subject_id=subject,
            predicate_id=change["predicate_id"],
            object_id=obj,
            polarity=Polarity.NEGATED if change.get("polarity") == "negated" else Polarity.AFFIRMED,
            valid_time=Interval.of(
                change.get("valid_from_minutes"), change.get("valid_to_minutes")
            ),
        )
        drafts.append(
            PropositionPartDraft(
                proposition,
                valid_from_minutes=change.get("valid_from_minutes"),
                valid_to_minutes=change.get("valid_to_minutes"),
            )
        )
    return drafts


@dataclass(frozen=True, slots=True)
class Applied:
    amendment_id: str
    operation_id: str
    report: dict[str, Any]
    timeline_revision: int


async def apply(
    db: AsyncEngine,
    story_id: str,
    session_id: str,
    prepared: Prepared,
    *,
    principal: str,
    audit: Sequence[tuple[str, Mapping[str, Any]]],
) -> Applied:
    """The one atomic apply (see the module docstring)."""
    found: dict[str, Any] = {}
    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.REWRITE_SESSION,
        principal=principal,
        bumps_revision=True,
    ) as op:
        for type_, payload in audit:
            op.append(type_, dict(payload))
        op.append(
            "rewrite_closed",
            {"session_id": session_id, "state": "applied", "amendment_id": None},
        )

        async def step(conn: AsyncConnection) -> None:
            await _in_flight(conn, prepared.parts)
            created = await _create_pending(conn, story_id, prepared.pending)
            parts = [d for _, d, _ in prepared.parts]
            labels = [(bucket, record) for bucket, _, record in prepared.parts]
            for draft in created:
                parts.insert(0, draft)
                labels.insert(0, ("changes", {"created": True}))
            made = await record_amendment(
                conn,
                op,
                kind=AmendmentKind.REWRITE,
                parts=parts,
                session_id=session_id,
                payload={"parts": len(parts), "session_id": session_id},
            )
            for target in prepared.undo:
                await revert_amendment(conn, op, target, by_amendment_id=made.amendment_id)
            found["beliefs"] = await _cascade_beliefs(
                conn, op, story_id, prepared.beliefs, made.written_fact_ids, made.event_id
            )
            if FAIL_AFTER_PARTS:
                raise InducedApplyFailure("rewrite.apply.after_part")
            applied: dict[str, list[dict[str, Any]]] = {
                "incidental": [],
                "cascaded": [],
                "relabels": [],
            }
            for (bucket, record), part_id in zip(labels, made.part_ids[: len(labels)], strict=True):
                if bucket in applied:
                    applied[bucket].append({**record, "part_id": part_id})
            found["amendment_id"] = made.amendment_id
            found["applied"] = applied

        op.add_commit_step(step)
        await op.commit()
    report = {
        "resolutions": prepared.resolutions,
        "applied": {**found["applied"], "beliefs": found["beliefs"]},
        "notifications": [*prepared.notifications, *found.get("belief_notes", [])],
    }
    return Applied(found["amendment_id"], op.operation_id, report, int(op.timeline_revision or 0))


async def close_audit(
    db: AsyncEngine,
    story_id: str,
    session_id: str,
    state: str,
    audit: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    principal: str,
) -> None:
    """A session that ended without an apply (``cancelled``, ``expired``, ``abandoned``): its
    questions, answers and end as audit events in a non-bumping ``rewrite_session``
    operation."""
    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.REWRITE_SESSION,
        principal=principal,
        bumps_revision=False,
    ) as op:
        for type_, payload in audit:
            op.append(type_, dict(payload))
        op.append("rewrite_closed", {"session_id": session_id, "state": state})
        await op.commit()


def audit_events(session_id: str, questions: Sequence[Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every question row's ``rewrite_question`` and, when answered, its ``rewrite_answer``
    (superseded rows flagged)."""
    found: list[tuple[str, dict[str, Any]]] = []
    for q in questions:
        found.append(
            (
                "rewrite_question",
                {
                    "session_id": session_id,
                    "question_id": str(q.id),
                    "kind": str(q.kind),
                    "prompt": str(q.prompt),
                    "options": [dict(o) for o in q.options or []],
                    "recommended_option_id": str(q.recommended_option_id),
                    "recommendation_reason": str(q.recommendation_reason),
                    "severity": q.severity,
                    "superseded": q.superseded_by is not None,
                },
            )
        )
        if q.answered_at is not None:
            found.append(
                (
                    "rewrite_answer",
                    {
                        "session_id": session_id,
                        "question_id": str(q.id),
                        "option_id": q.answer_option_id,
                        "text": q.answer_text,
                        "accepted_recommendation": q.accepted_recommendation,
                        "principal": str(q.answered_by or ENGINE),
                    },
                )
            )
    return found


__all__ = [
    "FAIL_AFTER_PARTS",
    "Applied",
    "InducedApplyFailure",
    "Prepared",
    "SpeechAct",
    "apply",
    "audit_events",
    "close_audit",
    "prepare",
    "relabelled",
    "substitute",
]
