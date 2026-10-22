"""Escalation records: #142's ``EscalationRecord`` as an ``escalations`` row, with its flip,
its downstream consequence and its shadow decision (FR-OBS-2,4, NFR-PERF-5; #155). (The planner
named this module ``yield.py``; ``yield`` is a Python keyword, so it is ``yields``.)

**Which records.** Every record of a turn's verifiers whose claim the gate could escalate under
the profile's ``entailment`` level: the gate said ``escalate`` (an entailment call, or an
attempt the budget or an unavailable model cut short) or ``below_gate`` (a tightened gate
skipped the call: ``skipped_by_gate``, no model verdict). Each verification pass writes its
own rows (``pass``): the turn's renders and the follow-up render are ``render``, a repair's
re-verification of its candidate is ``repair_<rung>``, the composed message's ``composed``.

**A row** (``row_of``) holds ids and codes only: the claim id, the gate reason and the shape
key, the deterministic and model confidences, the effective threshold, the tentative verdict
(the literal ``uncertain``), the model's verdict (``None`` with no valid answer), ``flipped``
(the model's answer was taken, so the claim ended other than ``uncertain``; ``None`` with no
valid answer: it never counts as a flip), how the claim ended (``final_kind``,
``final_disposition``) and its ``consequence``:

- ``repair_kept``: the claim sent its beat to #151's ladder and a correction was accepted;
- ``repair_discarded``: it did, and every correction was rejected (the safe fallback, or a
  ``think``/``latent`` beat dropped);
- ``none``: anything else (a ``supported`` flip, a ``surface`` disposition, no repair).

**The shadow** (FR-OBS-4). When the shape has a ``gate_thresholds`` row, the row records its
proposed threshold and ``shadow_decision``: whether the tightened gate (the least of the
effective threshold and the proposal) would still have escalated. When it would have skipped
the call, ``shadow_agreed`` says whether skipping agreed with the model: the model's valid
answer was not taken (the claim ended ``uncertain`` either way). An invalid or ``uncertain``
model answer counts as disagreement. Otherwise ``shadow_agreed`` is ``None``. The live
decision is never changed: the shadow is one more comparison on the record, no call.

``write`` inserts the rows in the turn's commit transaction (a derived table of the turn's
operation) and counts ``epistrel_verify_escalations_total{gate_reason,shape,flipped}`` and
``epistrel_verify_shadow_total{shape,agreed}``. Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.tables.escalations import escalations
from epistrel.core.verify import yield_queries
from epistrel.core.verify.rules import VerdictKind
from epistrel.observability.metrics import count_escalation, count_shadow
from epistrel.orchestrator.verify.verifier import EscalationRecord, Verifier

log = logging.getLogger(__name__)

RENDER = "render"
COMPOSED = "composed"
REPAIR_KEPT = "repair_kept"
REPAIR_DISCARDED = "repair_discarded"
NONE = "none"
RECORDED_OUTCOMES = frozenset({"escalate", "below_gate"})


def repair_pass(rung: int) -> str:
    return f"repair_{rung}"


def recorded(record: EscalationRecord) -> bool:
    """Whether ``record`` is an escalation (or a gated skip) #155 records."""
    return record.escalation_on and record.gate_outcome in RECORDED_OUTCOMES


def flipped(record: EscalationRecord) -> bool | None:
    """Whether the model's answer was taken over the tentative ``uncertain``; ``None`` with no
    valid answer."""
    if record.model_verdict is None:
        return None
    return record.final_kind != VerdictKind.UNCERTAIN.value


@dataclass(frozen=True, slots=True)
class Shadow:
    threshold: float | None = None
    decision: bool | None = None
    agreed: bool | None = None


def shadow_of(record: EscalationRecord, proposed: float | None) -> Shadow:
    """The shadow columns of ``record`` under the shape's ``proposed`` threshold."""
    if proposed is None:
        return Shadow()
    effective = proposed if record.threshold is None else min(record.threshold, proposed)
    decision = record.deterministic_confidence < effective
    if decision or record.gate_outcome != "escalate":
        return Shadow(proposed, decision, None)
    agreed = (
        record.model_verdict is not None
        and record.model_uncertain is None
        and flipped(record) is False
    )
    return Shadow(proposed, decision, agreed)


def row_of(
    record: EscalationRecord,
    *,
    story_id: str,
    operation_id: str,
    position: tuple[int | None, int | None],
    pass_: str,
    consequence: str,
    proposed: float | None,
) -> dict[str, Any]:
    """``record`` as an ``escalations`` row (see the module docstring)."""
    shadow = shadow_of(record, proposed)
    return {
        "id": new_ulid(),
        "story_id": story_id,
        "operation_id": operation_id,
        "narrative_turn": position[0],
        "turn_variant": position[1],
        "claim_id": record.claim_id,
        "pass": pass_,
        "gate_reason": record.gate_reason,
        "predicate_shape": record.predicate_shape,
        "deterministic_confidence": record.deterministic_confidence,
        "threshold": record.threshold,
        "called": record.called,
        "reused": record.reused,
        "skipped_by_gate": record.gate_outcome == "below_gate",
        "tentative_verdict": record.tentative_verdict,
        "model_verdict": record.model_verdict,
        "model_confidence": record.model_confidence,
        "model_uncertain": record.model_uncertain,
        "flipped": flipped(record),
        "final_kind": record.final_kind,
        "final_disposition": record.disposition,
        "consequence": consequence,
        "shadow_threshold": shadow.threshold,
        "shadow_decision": shadow.decision,
        "shadow_agreed": shadow.agreed,
    }


@dataclass(frozen=True, slots=True)
class VerifyPass:
    """One verifier of the turn and the pass it ran as."""

    verifier: Verifier
    name: str


def consequence_of(record: EscalationRecord, repaired: Mapping[str, bool]) -> str:
    """``record``'s consequence: ``repaired`` maps the claim ids that sent a beat to the ladder
    to whether a correction was accepted."""
    if record.claim_id is None or record.claim_id not in repaired:
        return NONE
    if record.final_kind == VerdictKind.SUPPORTED.value:
        return NONE
    return REPAIR_KEPT if repaired[record.claim_id] else REPAIR_DISCARDED


async def write(
    conn: AsyncConnection,
    *,
    story_id: str,
    operation_id: str,
    position: tuple[int | None, int | None],
    passes: Iterable[VerifyPass],
    repaired: Mapping[str, bool],
    proposed: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Write the turn's ``escalations`` rows in ``conn``'s transaction (its commit) and count
    them; answers the rows written. ``proposed``: the shapes' proposed thresholds (``None``:
    read ``gate_thresholds`` now)."""
    shadows = (
        {s: t.threshold for s, t in (await yield_queries.thresholds(conn)).items()}
        if proposed is None
        else dict(proposed)
    )
    rows = [
        row_of(
            record,
            story_id=story_id,
            operation_id=operation_id,
            position=position,
            pass_=found.name,
            consequence=consequence_of(record, repaired) if found.name == RENDER else NONE,
            proposed=shadows.get(record.predicate_shape),
        )
        for found in passes
        for record in found.verifier.escalations
        if recorded(record)
    ]
    await insert_derived(conn, escalations, rows)
    count(rows)
    if rows:
        log.debug("escalations: %d recorded", len(rows))
    return rows


def count(rows: Sequence[Mapping[str, Any]]) -> None:
    """The rows' metrics (enum and shape-key labels only)."""
    for row in rows:
        if row["skipped_by_gate"]:
            continue
        count_escalation(str(row["gate_reason"]), str(row["predicate_shape"]), row["flipped"])
        if row["shadow_decision"] is False:
            count_shadow(str(row["predicate_shape"]), bool(row["shadow_agreed"]))


__all__ = [
    "COMPOSED",
    "NONE",
    "RENDER",
    "REPAIR_DISCARDED",
    "REPAIR_KEPT",
    "Shadow",
    "VerifyPass",
    "consequence_of",
    "count",
    "flipped",
    "recorded",
    "repair_pass",
    "row_of",
    "shadow_of",
    "write",
]
