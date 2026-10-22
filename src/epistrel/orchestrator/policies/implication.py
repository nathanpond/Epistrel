"""Guardrail-violation feedback: implicate a rejected render's context so the loop converges
(FR-GUARD-5; docs/02 §4.2; #145).

The output gate (#68, ``output_gate.gate_output``) calls an ``Implicator`` for every rejected
draft whose context held records. **What is implicated**: every memory and fact retrieval
ranked into the draft's prompt (#141's assembled context: the occasion's episodes and
assessments, the ranked memories, the canon lines; ``context_of``), plus each pin whose text
overlaps the violation: a pin the violated deterministic policies flag by itself, or one that
shares a run of ``OVERLAP_WORDS`` words with the rejected text (the gate's violations carry no
spans, #49). Ledger items and standing profiles are never implicated (they are no records the
author put in the context).

**What happens**: ``core.mutation.implicate`` raises each memory's ``implication_count`` in a
``system`` control operation committed before the regeneration, and quarantines a memory
implicated in ``repair.implication_threshold`` rejected renders of the story (the configuration
the turn captured; 0: never) with reason ``repeated_implication``, an author pin included (the
owner's decision on #145; reported as a ``pin_quarantined`` warning). Facts are reported in the
``memories_implicated`` event and never changed. The regeneration's ``produce`` retrieves again,
and the quarantined memory is no longer a candidate, so the next prompt excludes it.

A Lite story's drafts carry no records (its pins and rolling summary are no ``memories`` rows),
so a Lite turn passes no implicator and keeps #68's behaviour. The structured pipeline (#150)
builds its drafts with ``draft_context`` and passes an ``Implicator``.

Logs carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import idempotency, mutation
from epistrel.core.mutation import RecordRef
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import Policy, Target
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.retrieval.pipeline import RetrievalResult
from epistrel.orchestrator.policies.output_gate import Draft, Implicated, OutputCheck, PinText

MEMORY_KINDS = frozenset({"episode", "assessment", "memory"})
"""The assembled context's item kinds that are ``memories`` rows."""
CANON_KIND = "canon"
PIN_KIND = "pin"
OVERLAP_WORDS = 4
"""The run of words a pin must share with the rejected text to be implicated by overlap."""

_WORD = re.compile(r"\w+", re.UNICODE)


def context_of(result: RetrievalResult) -> tuple[tuple[RecordRef, ...], tuple[PinText, ...]]:
    """The records a retrieval placed in a prompt (``Draft.context``) and the pins it showed
    (``Draft.pins``), in placement order, each once."""
    refs: dict[tuple[str, str], RecordRef] = {}
    pins: dict[str, PinText] = {}
    for item in result.context.items:
        if item.kind in MEMORY_KINDS:
            refs.setdefault(("memories", item.item_id), RecordRef("memories", item.item_id))
        elif item.kind == CANON_KIND:
            refs.setdefault(("facts", item.item_id), RecordRef("facts", item.item_id))
        elif item.kind == PIN_KIND:
            pins.setdefault(item.item_id, PinText(item.item_id, item.text))
    return tuple(refs.values()), tuple(pins.values())


def draft_context(result: RetrievalResult, draft: Draft) -> Draft:
    """``draft`` carrying the records and pins ``result`` placed in its prompt."""
    refs, pins = context_of(result)
    return Draft(
        text=draft.text,
        model=draft.model,
        prompt_hash=draft.prompt_hash,
        finish_reason=draft.finish_reason,
        context=refs,
        pins=pins,
    )


def _runs(text: str, n: int) -> set[tuple[str, ...]]:
    words = [w.casefold() for w in _WORD.findall(text)]
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def pin_overlaps(pin: PinText, text: str, check: OutputCheck, policies: Sequence[Policy]) -> bool:
    """Whether ``pin`` overlaps the violation ``check`` found in ``text``: the violated
    deterministic policies flag the pin's own text, or the pin shares ``OVERLAP_WORDS``
    consecutive words with the text."""
    violated = {v.policy_id for v in check.violations}
    by_id = [p for p in policies if p.id in violated]
    if by_id and any(
        v.policy_id in violated for v in evaluate(pin.text, by_id, Target.TEXT_OUTPUT)
    ):
        return True
    return bool(_runs(pin.text, OVERLAP_WORDS) & _runs(text, OVERLAP_WORDS))


def threshold_of(settings: ProfileSettings) -> int:
    """The story's ``repair.implication_threshold`` (0: auto-quarantine off)."""
    return settings.repair.implication_threshold


@dataclass
class Implicator:
    """FR-GUARD-5's ``implicate`` hook for one turn: the story, the turn's operation, the
    threshold the turn captured, and the output policies its gate checks."""

    db: AsyncEngine
    story_id: str
    turn_operation_id: str
    threshold: int
    policies: Sequence[Policy] = ()

    async def __call__(self, draft: Draft, check: OutputCheck, attempt: int) -> Implicated:
        pins = [p for p in draft.pins if pin_overlaps(p, draft.text, check, self.policies)]
        refs = (*draft.context, *(RecordRef("memories", p.memory_id) for p in pins))
        unique = tuple(dict.fromkeys(refs))
        if not unique:
            return Implicated()
        # The implication's own control operation is no part of the request's idempotent call:
        # the request's key belongs to the turn (#150 found it bound to this one).
        with idempotency.detached():
            done = await mutation.implicate(
                self.db,
                self.story_id,
                unique,
                turn_operation_id=self.turn_operation_id,
                attempt=attempt,
                threshold=self.threshold,
                pins=[p.memory_id for p in pins],
            )
        return Implicated(refs=unique, quarantined=done.quarantined, pins=done.pins)


__all__ = [
    "CANON_KIND",
    "MEMORY_KINDS",
    "OVERLAP_WORDS",
    "PIN_KIND",
    "Implicator",
    "context_of",
    "draft_context",
    "pin_overlaps",
    "threshold_of",
]
