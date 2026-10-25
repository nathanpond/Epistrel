"""The speech-act classification of a first-person claim (FR-VERIFY-5,10, FR-TURN-6,7,8;
docs/02 §4.9; #166).

Pure: the Orchestrator's verifier (``orchestrator.verify.verifier``) gathers the inputs and asks
here. A character's ``say`` claim has three **independent** dimensions:

- the **objective relation** (``true``/``false``/``unresolved``): its canon verdict, as the
  speaker's viewpoint reads canon (#142);
- the **belief relation** (``consistent``/``inconsistent``/``unknown``): its verdict against the
  speaker's belief set at the claim's position (#159's ``belief(speaker)`` holder: the partition,
  and the canon the speaker may read when the partition says nothing). Deterministic only:
  ``uncertain`` reads as ``unknown``, and no entailment call is made;
- the **intent**, declared on the beat (#150's typed field) and then **checked**: a declared
  ``deceptive`` whose belief relation is ``consistent`` is no lie (relabelled ``sincere``,
  ``intent_corrected``); ``unspecified`` derives as sincere while the stored label stays
  ``unspecified``; ``joking`` needs no belief check.

**The classification** (``classify``) combines them with the claim's provenance (FR-VERIFY-10's
trichotomy):

- ``joke``: a joking claim (never canonized, never repaired).
- ``lie``: a deceptive claim against a legitimately formed **contrary** belief the speaker holds
  (``contrary``: a partition belief at ``suspected`` or above that the claim contradicts; it need
  not be true), whatever the objective relation, an attempted lie that happens to be true
  included. A deceptive claim whose canon verdict is ``novel`` (a lie about something nobody's
  record settles) is a lie too: it never canonizes and is not repaired. A declared lie with no
  underlying belief, about something canon settles, is a hallucination.
- ``sincere_error``: sincere, belief-consistent, objectively false: an **established** false
  belief (accepted, no change), or a **new** one licensed by an independently verified antecedent
  (``licensed``: the citation confirmed, or the engine's search found a path; the belief is then
  written with that provenance, ``write``).
- ``sincere``: everything else that is sincere and legitimately based.
- ``hallucination_repaired``: no legitimate basis. A sincere claim the speaker's beliefs
  contradict, or one about canon the speaker's view does not settle (``hidden``: what canon holds
  beyond it), with no licensing antecedent; a deceptive claim with no contrary belief. It is
  repaired (#151) and the belief set does not change.

A ``consistent`` claim needs no licence; an ``inconsistent`` or hidden one is accepted only when
``licensed``, which the caller answers lazily (``needs_licence``). The belief relation reported
for a licensed claim is ``consistent``: the belief it rests on is written with it.

**One utterance** aggregates its claims: any ``inconsistent`` → ``inconsistent``, else any
``unknown`` → ``unknown``, else ``consistent`` (no claim at all: ``unknown``); its
classification is the worst of its claims' (``hallucination_repaired > lie > sincere_error >
joke > sincere``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from epistrel.core.verify.rules import VerdictKind

BeliefRelation = Literal["consistent", "inconsistent", "unknown"]
Classification = Literal["sincere", "sincere_error", "lie", "joke", "hallucination_repaired"]
Write = Literal["acquire", "correct"]

SINCERE: Classification = "sincere"
SINCERE_ERROR: Classification = "sincere_error"
LIE: Classification = "lie"
JOKE: Classification = "joke"
HALLUCINATION: Classification = "hallucination_repaired"

CLASSIFICATIONS: tuple[Classification, ...] = (SINCERE, SINCERE_ERROR, LIE, JOKE, HALLUCINATION)
"""Every classification (``utterances.classification``'s CHECK)."""

WORST: tuple[Classification, ...] = (HALLUCINATION, LIE, SINCERE_ERROR, JOKE, SINCERE)
"""The classifications, worst first: an utterance takes its worst claim's."""

DECEPTIVE = "deceptive"
JOKING = "joking"


def belief_relation_of(kind: VerdictKind | None) -> BeliefRelation:
    """A belief verdict as a relation: supported → ``consistent``, contradicted →
    ``inconsistent``, anything else (novel, uncertain, not checked) → ``unknown``."""
    if kind is VerdictKind.SUPPORTED:
        return "consistent"
    if kind is VerdictKind.CONTRADICTED:
        return "inconsistent"
    return "unknown"


@dataclass(frozen=True, slots=True)
class Inputs:
    """What one claim's classification rests on: the declared ``intent``, the canon verdict
    (``canon``, as the speaker's view reads it), the belief verdict (``belief``), whether the
    speaker holds a ``contrary`` belief, and what canon holds beyond the speaker's view when that
    view leaves the claim open (``hidden``: the omniscient verdict, ``None`` when not asked)."""

    intent: str
    canon: VerdictKind
    belief: VerdictKind
    contrary: bool = False
    hidden: VerdictKind | None = None

    @property
    def belief_relation(self) -> BeliefRelation:
        return belief_relation_of(self.belief)

    @property
    def objectively_false(self) -> bool:
        return self.canon is VerdictKind.CONTRADICTED or (
            self.canon_open and self.hidden is VerdictKind.CONTRADICTED
        )

    @property
    def canon_open(self) -> bool:
        return self.canon not in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED)

    @property
    def hidden_settles(self) -> bool:
        """Whether canon settles the claim beyond the speaker's view (it holds a truth the
        speaker's view does not show)."""
        return self.canon_open and self.hidden in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED)


@dataclass(frozen=True, slots=True)
class Decision:
    """One claim's classification: the classification, the intent as checked (``intent``: the
    stored label; ``unspecified`` stays), whether a declared lie was relabelled
    (``intent_corrected``), the belief relation reported, and whether a licensed belief is
    written (``write``: ``acquire`` a new one, ``correct`` the contradicting one)."""

    classification: Classification
    intent: str
    belief_relation: BeliefRelation
    intent_corrected: bool = False
    write: Write | None = None

    @property
    def hallucinated(self) -> bool:
        return self.classification == HALLUCINATION

    @property
    def stands(self) -> bool:
        """Whether the claim stands as said though canon contradicts it (a sincere error, a lie,
        a joke): the repair ladder leaves it."""
        return self.classification in (SINCERE_ERROR, LIE, JOKE)


def _sincere_path(inputs: Inputs) -> bool:
    """Whether the sincere branch needs a licensing antecedent (see the module docstring)."""
    if inputs.belief_relation == "consistent":
        return False
    if inputs.belief_relation == "inconsistent":
        return True
    return not inputs.canon_open or inputs.hidden_settles


def needs_licence(inputs: Inputs) -> bool:
    """Whether ``classify`` needs to know if a licensing antecedent exists: a sincere (or
    corrected) claim the speaker's beliefs contradict, or one whose truth canon holds beyond the
    speaker's view."""
    if inputs.intent == JOKING:
        return False
    if inputs.intent == DECEPTIVE and inputs.belief_relation != "consistent":
        return False
    return _sincere_path(inputs)


def classify(inputs: Inputs, *, licensed: bool = False) -> Decision:
    """The claim's classification (see the module docstring); ``licensed``: an independently
    verified antecedent licenses the belief the claim expresses (asked only when
    ``needs_licence``)."""
    relation = inputs.belief_relation
    if inputs.intent == JOKING:
        return Decision(JOKE, JOKING, relation)
    intent = inputs.intent
    corrected = False
    if intent == DECEPTIVE:
        if relation != "consistent":
            if inputs.contrary:
                return Decision(LIE, DECEPTIVE, relation)
            if inputs.canon_open and not inputs.hidden_settles:
                return Decision(LIE, DECEPTIVE, relation)
            return Decision(HALLUCINATION, DECEPTIVE, relation)
        intent, corrected = "sincere", True
    false = inputs.objectively_false
    if not _sincere_path(inputs):
        return Decision(SINCERE_ERROR if false else SINCERE, intent, relation, corrected)
    if not licensed:
        return Decision(HALLUCINATION, intent, relation, corrected)
    write: Write = "correct" if inputs.contrary else "acquire"
    return Decision(SINCERE_ERROR if false else SINCERE, intent, "consistent", corrected, write)


def aggregate_relation(relations: Iterable[BeliefRelation]) -> BeliefRelation:
    """An utterance's belief relation over its claims' (see the module docstring)."""
    found = list(relations)
    if "inconsistent" in found:
        return "inconsistent"
    if not found or "unknown" in found:
        return "unknown"
    return "consistent"


def worst(classifications: Iterable[Classification | None]) -> Classification | None:
    """An utterance's classification: its worst claim's (``None`` when none was classified)."""
    found = {c for c in classifications if c is not None}
    return next((c for c in WORST if c in found), None)


__all__ = [
    "CLASSIFICATIONS",
    "HALLUCINATION",
    "JOKE",
    "LIE",
    "SINCERE",
    "SINCERE_ERROR",
    "WORST",
    "BeliefRelation",
    "Classification",
    "Decision",
    "Inputs",
    "Write",
    "aggregate_relation",
    "belief_relation_of",
    "classify",
    "needs_licence",
    "worst",
]
