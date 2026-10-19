"""The structured turn's resolution screen: a model-driven binding that conflicts with recorded
history is rejected and steered elsewhere (FR-COMMIT-14, FR-COMMIT-11, FR-REPAIR-1; #154).

A render's ``think``, ``do`` or narration beat that fills an open thunk's slot is a dependence
(#146): at the commit, ``core.commit.lifecycle.depend`` resolves the thunk with it, through
``core.commit.reconcile``. ``screen`` asks the same reconciliation earlier, in the turn's check
(``orchestrator.structured.pipeline.check_turn``, its first pass and every repair re-check), so a
binding the commit would reject never reaches it: a ``Conflict`` turns the claim's verdict into a
``contradicted`` one (``VerifyResult.resolution_conflict``, the conflicting records as its
evidence) that #151's ladder repairs like any contradiction. Its constraint is ``avoid_binding``
on the bound entity (``orchestrator.repair.ladder``), and its ``render_rejected`` audit records
carry the verdict's ``cause = resolution_conflict``. The ladder is the steering loop: rung 1
narrows the beat (the claim goes, the thunk stays open with no obligation), rung 3 regenerates it
under the constraint, at most ``repair.max_attempts`` times; whatever it accepts is checked
again by this screen. The could-have-known soft flag (FR-COMMIT-11, FR-EPI-4) is M5's
(``core.commit.epistemic``): never a rejection.

The screen only reads. Each rejection is reported (``RejectedBinding``: ids only, invariant 3)
in ``turn_report.commit.rejected_bindings`` and in the turn's degradation.

This module does not import ``core.feedback*`` (contract ``feedback-is-flag-only``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.commit import lifecycle
from epistrel.core.commit.reconcile import Conflict, reconcile
from epistrel.core.commit.sketch import fills
from epistrel.core.propositions.registry import Registry
from epistrel.core.readset import StructuredReadSet
from epistrel.core.verify.rules import Evidence, Rule, Verdict, VerdictKind
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import VerifyResult

log = logging.getLogger(__name__)

BINDING_REJECTED = "binding_rejected"
"""Degradation: a render bound an open thunk against recorded history, so the binding was
rejected and the beat repaired (or left standing, labelled, with repair off)."""


@dataclass(frozen=True, slots=True)
class RejectedBinding:
    """One rejected binding: the claim, the thunk, the entity it bound and the records it
    conflicts with (ids only)."""

    claim_id: str | None
    beat: int | None
    thunk_id: str
    bound_entity: str | None
    conflicting_records: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "beat": self.beat,
            "thunk_id": self.thunk_id,
            "bound_entity": self.bound_entity,
            "conflicting_records": list(self.conflicting_records),
        }


def candidates(checked: Sequence[CheckedClaim]) -> list[CheckedClaim]:
    """The claims that would resolve an open thunk at the commit (``extraction.dependences``'
    shape, before repair takes any)."""
    return [
        c
        for c in checked
        if c.claim.matched_thunk_id is not None
        and c.claim.proposition is not None
        and c.claim.channel in lifecycle.DEPENDENCE_CHANNELS
        and (c.result is None or not c.result.needs_repair)
    ]


def rejected_result(c: CheckedClaim, conflict: Conflict) -> VerifyResult:
    """``c``'s verdict as a rejected binding: ``contradicted``, citing the conflicting
    records."""
    proposition = c.claim.proposition
    evidence = tuple(Evidence(Rule.IMPLICATION, fact_id=i) for i in conflict.conflicting_records)
    verdict = Verdict(VerdictKind.CONTRADICTED, evidence=evidence)
    if c.result is None:
        return VerifyResult(
            claim=proposition,
            verdict=verdict,
            kind=VerdictKind.CONTRADICTED,
            resolution_conflict=True,
            avoid=conflict.bound_entity,
        )
    return replace(
        c.result,
        verdict=verdict,
        kind=VerdictKind.CONTRADICTED,
        disposition=None,
        resolution_conflict=True,
        avoid=conflict.bound_entity,
    )


async def screen(
    conn: AsyncConnection,
    story_id: str,
    checked: Sequence[CheckedClaim],
    *,
    read_set: StructuredReadSet,
    registry: Registry | None = None,
) -> tuple[list[CheckedClaim], list[RejectedBinding]]:
    """Reconcile every claim that would resolve an open thunk (see the module docstring):
    ``checked`` with each rejected binding's verdict replaced, and the rejections."""
    wanted = {id(c) for c in candidates(checked)}
    out: list[CheckedClaim] = []
    rejected: list[RejectedBinding] = []
    for c in checked:
        proposition = c.claim.proposition
        thunk_id = c.claim.matched_thunk_id
        if id(c) not in wanted or proposition is None or thunk_id is None:
            out.append(c)
            continue
        thunk = await lifecycle.read_thunk(conn, story_id, thunk_id)
        if thunk is None or not thunk.open or not fills(thunk.sketch, proposition):
            out.append(c)
            continue
        found = await reconcile(
            conn,
            story_id,
            thunk,
            lifecycle.binding_of(thunk, proposition),
            registry=registry,
            read_set=read_set,
        )
        if not isinstance(found, Conflict):
            out.append(c)
            continue
        out.append(replace(c, result=rejected_result(c, found)))
        rejected.append(
            RejectedBinding(
                claim_id=c.claim_id,
                beat=c.claim.beat,
                thunk_id=thunk.id,
                bound_entity=found.bound_entity,
                conflicting_records=found.conflicting_records,
            )
        )
    if rejected:
        log.debug("resolution screen: %d bindings rejected in story %s", len(rejected), story_id)
    return out, rejected


__all__ = [
    "BINDING_REJECTED",
    "RejectedBinding",
    "candidates",
    "rejected_result",
    "screen",
]
