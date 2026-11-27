"""``RepairLadder.run(beat, violations, budget) -> RepairResult``: correct one structured beat
whose claims ``needs_repair`` (FR-REPAIR-1..4, FR-VERIFY-12, FR-GUARD-6, NFR-PERF-7; #151).

**What goes in.** A beat of a structured turn and its violations: every claim #142's verifier
marked ``needs_repair`` — ``contradicted``, ``uncertain`` under the ``repair`` safe disposition,
or a novel claim #144 denied (``unpermitted``). Several violations of one beat are repaired
together.

**The context** (FR-REPAIR-1). For each contradicted claim the facts its verdict cites (and,
for a closed set it cites, the canon facts of the set's predicate for its subject: "Bobby's only
enrollment is OSU" brings his enrollment at OSU) and their dependency cluster, the union of
``core.verify.neighborhood.fact_neighborhood(depth 2)`` seeded at each of them, as the beat's
speaker may read them (the narrator's: omniscient), through the claim's position; a claim that
cites no fact seeds it at its subject. An undecided or unpermitted claim has no neighborhood:
its constraint is "do not assert …" ("do not introduce facts about …", with the reason, for an
unpermitted one; #172: "do not assert what another character privately thinks or feels" for a
``think_leak``, naming none of it, and "do not claim to remember …" for a confabulation).
The neighborhood's size (its facts, memories excluded) classifies the repair.

**The rungs**, strictly in order, an inapplicable one skipped (the pure parts are
``core.repair.rungs``):

1. *narrowing*: the sentences that carry the violations are dropped (a ``say``, ``do`` or
   narration beat that keeps at least one sentence), model-free. #1019 (FR-REPAIR-2): **a
   name is not a line**: the narrowed text is accepted only when it keeps at least one word
   that is not part of a cast member's name or alias, a title or punctuation
   (``core.repair.rungs.name_only``; "Nathan Pond." fails, "Okay, Nathan." passes), else the
   next rung runs. The ladder's ``name_words`` are the whole cast's (the check is off when
   empty); a substitution, a regeneration's ``say``, ``do`` or narration candidate and the State
   deterministic rung's narrowing are held to it too, and a failing text is recorded as a
   ``RejectedRender`` with the constraint ``{"kind": "narrowing", "reason": "name_only"}``. An
   original beat that is only a name, and that nothing flagged, is left alone;
2. *substitution*: a functional predicate whose verdict cites exactly one canon fact has the
   claim's object replaced by the canon value (its canonical name or literal), model-free;
3. *regeneration*: the ``regeneration`` purpose rewrites the beat in place (the generation
   inputs of the render it came from, the rejected text, the typed constraints, the
   contradicted facts and the neighborhood) through the Model Access Layer's
   ``structured_call`` (``RegeneratedBeat``; #172: a span leaking another character's private
   thought is cut from the rejected text it reads, ``redact``). It starts only when the turn's
   budget can reserve, at once, the re-render, its extraction and one re-check per
   model-assisted output policy (``can_afford_unreserved`` + ``hold``), and while the beat's
   *render* has ``repair.max_attempts`` left (each regeneration is one attempt; #53's retries
   are inside it). #1014 (FR-REPAIR-5): the attempts are counted per render, not per turn: the
   ladder's ``render_of`` names the render a beat came from (the narrator's, each primary's
   with the reactions it narrates, an escalated reactor's, a sequential turn's follow-up), and
   each render has its own ``max_attempts``; the turn's budget and deadline still cap the
   total. Otherwise the ladder stays deterministic: ``repair_deterministic`` (cause ``budget``
   or ``max_attempts``);
4. *the safe fallback*: a ``say``, ``do`` or narration beat becomes the stored safe fallback
   line (the character's own, else the deployment's, else the last resort; the first that
   passes the output policies), ``safe_fallback`` (cause ``repair``); a ``think`` or ``latent``
   beat is dropped instead (reported, no fallback text). ``think`` and ``latent`` beats go
   straight to rung 3.

**State findings** (#187, FR-STATE-5,6, FR-VERIFY-12): a beat whose violations include a
State effect the dry-run refused (``state_constraint``; #188's ``state_unpermitted``, an
effect its State does not authorize, is repaired the same way), a change with no concrete amount
(``effect_unresolved``) or a narrated change with no effect (``state_undeclared``) skips rungs 1
and 2 (no rewording fixes a declared effect): the regeneration asks the render, with the
finding's line as its constraint, for the attempt narrated as failing with no effect or a
smaller one, a concrete amount, or the effect declared (``RegeneratedBeat.effects``; the
re-check dry-runs them again). The deterministic rung then answers instead of the fallback
line: a refused effect is narrated as refused (``attempt``, every effect dropped, nothing
changes), and an unresolved or undeclared change loses its sentence (narrowing) and its
effect, else the beat becomes the fallback line. A fallback line never keeps an effect. #190: a
declared effect nothing narrates (``state_unnarrated``) asks the regeneration to narrate the
change (the rewrite keeps the beat's effects when it declares none), and the deterministic rung
keeps the effect and appends one line built from its reason. #209: a sentence asserting the
outcome of a change asked of an external value's owner (``external_outcome_unconfirmed``) asks
the regeneration for the attempt only (the rewrite keeps the beat's effects: the request
stands); the deterministic rung removes the sentence (narrowing) and keeps the effects, else
narrates the attempt (``attempt``, effects kept).

**Acceptance** (FR-REPAIR-2, FR-GUARD-6). Every rung's output passes the output gate (the
story's output policies, model-assisted ones included) and is detected again (#148: the
declared structure and the deterministic detectors for rungs 1 and 2, every layer for rung 3)
and verified again (#142, over the turn's overlay, reusing the turn's validated entailment
answers) before it is accepted. A correction with any claim that still ``needs_repair``, or one
the gate rejects, is not accepted: the next rung runs. A regeneration whose extraction could not
run is accepted on its deterministic re-verification, with ``repair_unextracted``.

**The drop rule** (#1014, FR-REPAIR-5; ``drop_plan``, applied by the pipeline once a render's
beats have all been through the ladder, and again over the whole turn): a ``say``, ``do`` or
narration beat that became the fallback line (``RepairResult.fallback``) is dropped
(``repair_dropped``, cause ``speaker_has_line``) when its speaker (the character; the narrator
for narration) has another committed ``say``, ``do`` or narration beat in the turn that is not
a fallback line (a ``think``, a ``latent`` and a State finding's deterministic beat do not
count). When every beat of the speaker failed, its first fallback line is kept and the others
are dropped (``duplicate_fallback``).

**Copied lines** (#1013, FR-MULTI-8,11; ``orchestrator.structured.copies``): a ``say`` or ``do``
beat flagged ``copied_line`` (a sentence another speaker already said in the turn) skips rungs 1
and 2 and gets exactly **one** regeneration, whatever ``repair.max_attempts`` is, with every
constraint of the beat plus ``no_copy`` ("Do not repeat what {name} just said; respond in your
own words.", naming the speaker of the copied sentence from the render's cast names). The
regeneration draws on the render's attempts and the turn's budget: with no attempt left, no
budget, an unusable output, a model error, or a candidate that is still a copy or fails any
other check, the beat is **dropped** (``repair_dropped``, cause ``copied_line``; it replaces any
``repair_deterministic``), never the fallback line, and the drop rule's post-pass counts it
neither way. A copy with a State finding keeps the State deterministic rung's outcome for its
effect, built without the copied text (a refused attempt's line, an unnarrated change's line);
only the prose is dropped. A non-copy beat whose candidate copies is rejected like any failing
candidate, and its per-beat entry adds ``copied_line`` to its ``violations``
(``RepairResult.flagged``). ``copy_dropped`` drops a copied beat with no ladder (the repair
module off). ``epistrel_copied_lines_total{outcome}`` counts ``regenerated`` and ``dropped``.

**What comes out** (``RepairResult``): the accepted beat (``None`` for a dropped one) at the
original beat's position with its new claims (a claim rung 2 rewrote restates canon and is
never canonized, ``restates``), the rung, the attempts, every rejected text
(``RejectedRender``: the original beat and each failed candidate, written by the pipeline as
``render_rejected`` audit events at the commit), the FR-REPAIR-4 class
(``core.repair.rungs.classify``), the degradation and the violations still open (none once a
beat is accepted). Nothing from a rejected render is canonized: the pipeline drops its canon
requests. #1014: ``detail`` is the beat's ``degradation_details`` entries (its index, speaker,
the rung it ended at and the codes of what flagged it, ``violation_codes``); codes and ids only.

Log lines carry counts and codes only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    ValidationInfo,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon.facts import Fact
from epistrel.core.config_changes import read_section
from epistrel.core.events.types import RenderRejected
from epistrel.core.propositions.model import Cardinality, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.repair.rungs import (
    FALLBACK,
    NAME_ONLY,
    NARROWING,
    REGENERATION,
    SUBSTITUTION,
    Constraint,
    RepairClass,
    classify,
    locate,
    name_only,
    narrow,
    substitute,
)
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.settings_schema import NAME_PLACEHOLDER, OMNISCIENT, VerifySettings
from epistrel.core.states.effects import (
    EXTERNAL_OUTCOME_UNCONFIRMED,
    PROSE_TAGS,
    REFUSED_TAGS,
    STATE_UNNARRATED,
    TIME_STOP,
    Finding,
)
from epistrel.core.tables import entities
from epistrel.core.tables.propositions import closed_sets
from epistrel.core.turns.beats import BEAT_TEXT_MAX, BeatKind, TurnBeat
from epistrel.core.verify.neighborhood import Seed, fact_neighborhood
from epistrel.core.verify.protected import viewpoint_of
from epistrel.core.verify.rules import Rule, VerdictKind
from epistrel.observability.metrics import count_copied_line, count_regeneration
from epistrel.orchestrator.budget import REPAIR as BUDGET_REPAIR
from epistrel.orchestrator.budget import STOP as BUDGET_STOP
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.lite.prompt import prompt_hash
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    REGENERATION as REGENERATION_PURPOSE,
)
from epistrel.orchestrator.models.registry import (
    REGENERATION_MAX_RECORDS,
    GenerationInputs,
    RegenerationInputs,
    RepairConstraint,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.policies.output_gate import LAST_RESORT, FallbackLines
from epistrel.orchestrator.structured.render import (
    CLOCK_CORRECTION,
    CLOCK_EFFECT_DROPPED,
    MAX_EFFECTS,
    MAX_PROPOSITIONS,
    EffectDecl,
    IntentName,
    PropositionSketch,
    clock_offence,
    clock_refusal,
    declared_of,
    drop_clock_effects,
)
from epistrel.orchestrator.verify.extraction import EXTRACTION_DEGRADED, CheckedClaim
from epistrel.orchestrator.verify.verifier import (
    CONFABULATION,
    COPIED_LINE,
    HALLUCINATION,
    RESOLUTION_CONFLICT,
    THINK_LEAK,
    UNANCHORED,
    UNPERMITTED,
    TimeFrame,
    render_proposition,
    render_time,
)

log = logging.getLogger(__name__)

SAY, THINK, DO, LATENT, NARRATION = "say", "think", "do", "latent", "narration"
DETERMINISTIC_KINDS = frozenset({SAY, DO, NARRATION})
TOWARD_DENIALS = frozenset({"other_character_secret", "player_character"})
"""#1060: the denials that protect another character than the speaker, whose name an
``unpermitted`` constraint carries when the claim's subject is the speaker."""
"""The beat kinds rungs 1, 2 and 4's fallback line apply to; ``think`` and ``latent`` go
straight to rung 3, then are dropped."""
NEIGHBORHOOD_DEPTH = 2
REPAIR_DETERMINISTIC = "repair_deterministic"
"""Degradation: a beat needed a regeneration the turn could not reserve (``budget``) or had no
attempt left for (``max_attempts``), so only the deterministic rungs and the fallback ran."""
REPAIR_UNEXTRACTED = "repair_unextracted"
"""Degradation: a regeneration's extraction could not run; its deterministic re-verification
stood."""
NARRATOR_FALLBACK = "A moment passes."
"""#652: rung 4's line for a narrator beat (a narration, a staged turn's outcome step): the
narrator's voice, never a character's stored line."""
SAFE_FALLBACK = "safe_fallback"
"""Degradation: a beat became the stored safe fallback line (or, ``think``/``latent``, was
dropped)."""
REPAIR = "repair"
REPAIR_DROPPED = "repair_dropped"
"""Degradation (#1014): a beat that became the fallback line was dropped instead, because its
speaker has a real line in the turn (``SPEAKER_HAS_LINE``) or already keeps one fallback line
(``DUPLICATE_FALLBACK``); #1013: a copied line whose one regeneration failed or could not run
(``COPIED_LINE``)."""
SPEAKER_HAS_LINE = "speaker_has_line"
DUPLICATE_FALLBACK = "duplicate_fallback"
COPY_CONSTRAINT = "Do not repeat what {name} just said; respond in your own words."
"""#1013: the ``no_copy`` constraint of a copied line's regeneration."""
COPY_SOURCE_FALLBACK = "the other character"
"""#1013: how ``COPY_CONSTRAINT`` names a source speaker the render's cast does not name."""
REPAIR_STEPS = frozenset({REPAIR_DETERMINISTIC, REPAIR_UNEXTRACTED, SAFE_FALLBACK, REPAIR_DROPPED})
"""The degradation steps the ladder reports per beat (``RepairResult.detail``)."""
RUNG_NAMES: dict[int, str] = {
    NARROWING: "narrowing",
    SUBSTITUTION: "substitution",
    REGENERATION: "regeneration",
    FALLBACK: "safe_fallback",
}
STATE_SETTLED = "state_settled"
"""#1014: the rung name of a State finding's deterministic beat (#187), not a fallback line."""
DROPPED = "dropped"
"""#1014: the rung name of a beat the ladder left out of the turn (a ``think`` or ``latent``,
or a fallback line the drop rule removed)."""
UNCHECKED = "unchecked"
"""#1014: the violation code of a claim with no verification result."""
NARRATOR_RENDER = "narrator"
"""#1014: the render key of the narrator's beats (``RepairLadder.render_of``)."""
REGENERATION_OUTCOMES = ("accepted", "failed", "skipped_max_attempts", "skipped_budget")
"""``epistrel_repair_regenerations_total``'s ``outcome`` values (#1014)."""


# -- the regeneration's answer -------------------------------------------------------------------


class RegeneratedBeat(BaseModel):
    """What one ``regeneration`` call returns (#151): the rewritten beat's text, the claims it
    declares and, for a ``say``, its intent. The beat keeps its kind, actor and position."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=BEAT_TEXT_MAX)
    propositions: list[PropositionSketch] = Field(default_factory=list, max_length=MAX_PROPOSITIONS)
    intent: IntentName | None = None
    effects: list[EffectDecl] = Field(default_factory=list, max_length=MAX_EFFECTS)
    """#187: the rewritten beat's State effects (a State finding's repair: none, a smaller one,
    a concrete amount or the declared change)."""
    _clock_drops: int = PrivateAttr(default=0)

    @model_validator(mode="wrap")
    @classmethod
    def _clock(
        cls, data: Any, handler: ModelWrapValidatorHandler[Self], info: ValidationInfo
    ) -> Self:
        """#996: a world-clock change dropped once its correction was sent in this call
        (``render.drop_clock_effects``)."""
        drops = 0
        if isinstance(data, dict) and "effects" in data:
            effects, drops = drop_clock_effects(data["effects"], info)
            if drops:
                data = {**data, "effects": effects}
        found = handler(data)
        found._clock_drops = drops
        return found

    @model_validator(mode="after")
    def _text(self) -> Self:
        if not self.text.strip():
            raise ValueError("text: empty")
        # #996: the render's rule (``render.effect_errors``): a turn never changes the clock.
        clocked = [
            f"effects.{m}.state: {CLOCK_CORRECTION}"
            for m, e in enumerate(self.effects)
            if clock_offence(e)
        ]
        if clocked:
            raise clock_refusal(clocked, self.effects)
        return self

    @property
    def clock_effects_dropped(self) -> int:
        """#996: the world-clock changes dropped after their correction."""
        return self._clock_drops

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Message]) -> dict[str, Any]:
        """The stub's default (FR-MODEL-11): a text from the request's hash, no claims."""
        del messages
        return {"text": f"[stub regeneration {digest[:12]}]", "propositions": []}


# -- what the ladder works with ------------------------------------------------------------------


@dataclass(slots=True)
class Recheck:
    """A candidate beat detected and verified again (the pipeline's ``check_turn`` over that
    beat alone): the beat with its claims, the checked claims, the degradation and the
    pipeline's own record of the check (``value``), which it merges when the candidate is
    accepted."""

    beat: TurnBeat
    claims: list[CheckedClaim]
    degradation: list[str] = field(default_factory=list)
    value: Any = None

    @property
    def violations(self) -> list[CheckedClaim]:
        return [c for c in self.claims if c.result is not None and c.result.needs_repair]


Rechecker = Callable[[TurnBeat, Sequence[PropositionSketch], bool, int], Awaitable[Recheck]]
"""``recheck(candidate, sketches, backstop, rung)``: detect and verify the candidate beat
again; ``backstop`` ``False`` runs only the declared structure and the deterministic detectors;
``rung`` is the rung that made the candidate (#155 records its re-verification as the pass
``repair_<rung>``)."""
OutputCheck = Callable[[str], Awaitable[bool]]
"""Whether a text passes the story's output policies (the output gate's check)."""
InputsOf = Callable[[TurnBeat], GenerationInputs | None]
"""The generation inputs of the render a beat came from (``None``: no regeneration)."""
RenderOf = Callable[[TurnBeat], Hashable]
"""#1014: the render a beat came from (its key); each render has its own
``repair.max_attempts``."""


@dataclass(frozen=True, slots=True)
class RejectedRender:
    """One text the ladder rejected (FR-REPAIR-3): ``render_rejected``'s payload."""

    beat: int
    attempt: int
    rung: int
    text: str
    constraints: tuple[Constraint, ...] = ()
    verdicts: tuple[dict[str, Any], ...] = ()
    detected_claims: tuple[dict[str, Any], ...] = ()
    prompt_hash: str | None = None
    reasons: tuple[dict[str, Any], ...] = ()
    """#1019: untyped constraint payloads after the typed ones (``NAME_ONLY``)."""

    def event(self) -> RenderRejected:
        return RenderRejected(
            beat=self.beat,
            attempt=self.attempt,
            rung=self.rung,
            text=self.text,
            constraints=(*(c.as_payload() for c in self.constraints), *self.reasons),
            verdicts=self.verdicts,
            detected_claims=self.detected_claims,
            prompt_hash=self.prompt_hash,
        )


@dataclass(frozen=True, slots=True)
class Context:
    """FR-REPAIR-1's exposure for one beat: each violation's contradicted facts and the union
    of their neighborhoods (facts, ids and the lines a regeneration reads)."""

    contradicted: tuple[Fact, ...] = ()
    neighborhood: tuple[Fact, ...] = ()
    payloads: tuple[dict[str, Any], ...] = ()
    names: Mapping[str, str] = field(default_factory=dict)
    registry: Registry | None = None
    constraints: tuple[Constraint, ...] = ()
    frame: TimeFrame = UNANCHORED
    """How the facts' valid-times read in the regeneration prompt (#460)."""
    redactions: tuple[tuple[int, int], ...] = ()
    """#172: the beat's spans that leak another character's private thought, cut from the
    rejected text a regeneration reads (``REDACTED``)."""

    @property
    def size(self) -> int:
        """The neighborhood's size: its facts (memories excluded)."""
        return len({f.id for f in self.neighborhood})


@dataclass(slots=True)
class RepairResult:
    """What ``RepairLadder.run`` made of one beat (see the module docstring)."""

    beat: TurnBeat | None
    rung: int
    attempts: int
    rejected: list[RejectedRender]
    repair_class: RepairClass
    degradation: list[tuple[str, str]]
    remaining_violations: list[CheckedClaim]
    original: TurnBeat
    violations: list[CheckedClaim]
    context: Context
    recheck: Recheck | None = None
    sketches: list[PropositionSketch] = field(default_factory=list)
    fallback: bool = False
    """#1014: the beat became the stored fallback line (``_fallback_line`` made its text)."""
    drop_cause: str | None = None
    """#1014: why the drop rule removed the fallback line (``SPEAKER_HAS_LINE`` or
    ``DUPLICATE_FALLBACK``); #1013: ``COPIED_LINE`` for a dropped copy; ``None`` when it was not
    dropped."""
    flagged: set[str] = field(default_factory=set)
    """#1013: codes a rejected candidate added to what flagged the beat (``copied_line``)."""

    @property
    def copied(self) -> bool:
        """#1013: whether the beat, or a candidate for it, was flagged ``copied_line``."""
        return COPIED_LINE in self.codes

    @property
    def codes(self) -> list[str]:
        """#1014: the codes of what flagged the beat (``violation_codes``), with #1013's
        ``flagged``."""
        return sorted({*violation_codes(self.violations), *self.flagged})

    def restates(self, claim: Proposition) -> bool:
        """Whether ``claim`` is one rung 2 rewrote to canon's value: it restates canon, so it is
        never canonized again (its time may be the turn's, not canon's)."""
        if self.rung != SUBSTITUTION:
            return False
        key = (claim.subject_id, claim.predicate_id, claim.qualifier_key())
        return any(
            (p.subject_id, p.predicate_id, p.qualifier_key()) == key
            for v in self.violations
            if (p := v.claim.proposition) is not None
        )

    @property
    def accepted(self) -> bool:
        """Whether a correction was accepted (rungs 1 to 3), not the fallback or a drop."""
        return self.rung < FALLBACK

    @property
    def dropped(self) -> bool:
        return self.beat is None

    @property
    def speaker(self) -> str | None:
        """Whose beat it is: the character's id, ``None`` for the narrator (#1014)."""
        return speaker_of(self.original)

    @property
    def rung_name(self) -> str:
        """The rung the beat ended at, by name (#1014): ``narrowing``, ``substitution``,
        ``regeneration``, ``safe_fallback``, ``state_settled`` (a State finding's deterministic
        beat) or ``dropped`` (a ``think``/``latent``, or a fallback line the drop rule
        removed)."""
        if self.drop_cause is not None or self.beat is None:
            return DROPPED
        if self.rung >= FALLBACK and not self.fallback:
            return STATE_SETTLED
        return RUNG_NAMES.get(self.rung, str(self.rung))

    def drop(self, cause: str) -> None:
        """#1014: the drop rule removed the fallback line (``cause``); its ``safe_fallback``
        degradation becomes ``repair_dropped``."""
        self.beat = None
        self.drop_cause = cause
        self.degradation = [
            d for d in self.degradation if d[0] not in (SAFE_FALLBACK, REPAIR_DROPPED)
        ] + [(REPAIR_DROPPED, cause)]

    def detail(self) -> list[dict[str, Any]]:
        """#1014: the beat's ``degradation_details`` entries, one per degradation step it took
        (a dropped fallback line's ``safe_fallback`` replaced by ``repair_dropped``; a
        ``repair_deterministic`` that led to it stays), each with the beat's index, its speaker
        (``None``: the narrator), the rung it ended at and the codes of what flagged it. Its
        ``kind`` is for the viewpoint filter only. Codes and ids only, never text."""
        steps = [d for d in self.degradation if d[0] in REPAIR_STEPS]
        codes = self.codes
        return [
            {
                "step": step,
                "cause": cause,
                "beat": self.original.beat,
                "character_id": self.speaker,
                "rung": self.rung_name,
                "violations": codes,
                "kind": self.original.kind,
            }
            for step, cause in dict.fromkeys(steps)
        ]

    def report(self) -> list[dict[str, Any]]:
        """``turn_report.repairs`` entries, one per violation: ids, codes and counts only."""
        return [
            {
                "beat": self.original.beat,
                "claim": v.claim_id,
                "verdict": v.verdict,
                "tag": None if v.result is None else v.result.repair_tag,
                "rung": self.rung,
                "class": self.repair_class.value,
                "attempts": self.attempts,
                "neighborhood_size": self.context.size,
                "neighborhood": list(self.context.payloads),
                "contradicted_facts": [f.id for f in self.context.contradicted],
                "accepted": self.accepted,
                "dropped": self.dropped,
                "rejected": len(self.rejected),
            }
            for v in self.violations
        ]


# -- the per-beat cause and the drop rule (#1014) -------------------------------------------------


def speaker_of(beat: TurnBeat) -> str | None:
    """Whose line ``beat`` is: the character's id; ``None`` for the narrator's."""
    if beat.narrator or beat.kind == NARRATION:
        return None
    return beat.character_id


def violation_code(claim: CheckedClaim) -> str:
    """#1014: what flagged one claim, as a code: its repair tag (``unpermitted:<reason>`` for a
    denied novel claim, bare ``unpermitted`` when no reason was recorded; ``think_leak``,
    ``confabulation``, ``resolution_conflict``, ``hallucinated``, a State finding's tag), else
    ``hidden_conflict``, else its verdict (``contradicted``, ``uncertain``); ``unchecked`` for a
    claim with no result."""
    result = claim.result
    if result is None:
        return UNCHECKED
    tag = result.repair_tag
    if tag == UNPERMITTED:
        denied = result.denied
        return UNPERMITTED if denied is None else f"{UNPERMITTED}:{denied.reason.value}"
    if tag == HALLUCINATION:
        return "hallucinated"
    if tag is not None:
        return tag
    if result.hidden_conflict:
        return "hidden_conflict"
    return result.kind.value


def violation_codes(claims: Sequence[CheckedClaim]) -> list[str]:
    """#1014: the distinct codes of what flagged a beat (``violation_code``), sorted."""
    return sorted({violation_code(c) for c in claims})


def real_line(beat: TurnBeat) -> bool:
    """#1014: whether ``beat`` counts as its speaker's line for the drop rule: a ``say``,
    ``do`` or narration beat rendered by the engine (not the player's input, beat 1)."""
    return beat.beat > 1 and beat.kind in DETERMINISTIC_KINDS


def drop_plan(
    beats: Sequence[TurnBeat],
    results: Sequence[RepairResult],
    *,
    earlier: Sequence[TurnBeat] = (),
    earlier_results: Sequence[RepairResult] = (),
) -> list[tuple[RepairResult, str]]:
    """#1014 (FR-REPAIR-5): which fallback lines among ``results`` to drop, with the cause.
    ``beats`` are the beats so far (``results``' beats among them), ``results`` their repairs;
    ``earlier`` and ``earlier_results`` an earlier stage of the same turn (a staged turn's
    intent, for its outcome step), counted but never dropped. A fallback line
    (``RepairResult.fallback``, not dropped yet) is dropped when its speaker has another beat
    that is a real line (``real_line``) and not the outcome of a rung-4 repair (a fallback line
    or a State finding's deterministic beat): ``speaker_has_line``. Otherwise the speaker's
    first fallback line (by beat; an earlier stage's first) stays and the rest go:
    ``duplicate_fallback``."""
    every = [*earlier_results, *results]
    settled = {r.original.beat for r in every if r.rung >= FALLBACK and r.beat is not None}
    speaking = {speaker_of(b) for b in (*earlier, *beats) if real_line(b) and b.beat not in settled}
    present = {b.beat for b in beats}
    before = {b.beat for b in earlier}
    kept: set[str | None] = {
        r.speaker
        for r in earlier_results
        if r.fallback and r.drop_cause is None and r.original.beat in before
    }
    lines = sorted(
        (r for r in results if r.fallback and r.drop_cause is None and r.original.beat in present),
        key=lambda r: r.original.beat,
    )
    found: list[tuple[RepairResult, str]] = []
    for r in lines:
        if r.speaker in speaking:
            found.append((r, SPEAKER_HAS_LINE))
        elif r.speaker in kept:
            found.append((r, DUPLICATE_FALLBACK))
        else:
            kept.add(r.speaker)
    return found


# -- the ladder ----------------------------------------------------------------------------------


def _verdicts(claims: Sequence[CheckedClaim]) -> tuple[dict[str, Any], ...]:
    return tuple(c.result.as_payload(c.claim_id) for c in claims if c.result is not None)


def _detected(claims: Sequence[CheckedClaim]) -> tuple[dict[str, Any], ...]:
    return tuple(c.claim.payload() for c in claims)


def _fact_ids(claim: CheckedClaim) -> list[str]:
    """The canon facts a claim's verdict cites, its remainders' included, in order."""
    result = claim.result
    if result is None:
        return []
    found: list[str] = []
    stack = [result]
    while stack:
        current = stack.pop(0)
        found += [e.fact_id for e in current.verdict.evidence if e.fact_id is not None]
        stack += list(current.sub_results)
    return list(dict.fromkeys(found))


def _closed_set_ids(claim: CheckedClaim) -> list[str]:
    result = claim.result
    if result is None:
        return []
    found: list[str] = []
    stack = [result]
    while stack:
        current = stack.pop(0)
        found += [e.closed_set_id for e in current.verdict.evidence if e.closed_set_id]
        stack += list(current.sub_results)
    return list(dict.fromkeys(found))


async def _closed_set_members(
    conn: AsyncConnection, lookup: KeyedLookup, story_id: str, claim: CheckedClaim
) -> list[str]:
    """The canon facts behind each closed set a verdict cites: the facts of its predicate for
    its subject (Bobby's enrollment at OSU, for "Bobby's only enrollment is OSU"), which the
    claim contradicts as much as the set does."""
    ids = _closed_set_ids(claim)
    if not ids:
        return []
    rows = await conn.execute(
        select(closed_sets.c.predicate_id, closed_sets.c.key).where(
            closed_sets.c.story_id == story_id, closed_sets.c.id.in_(ids)
        )
    )
    found: list[str] = []
    for row in rows:
        subject = (row.key or {}).get("subject_id")
        if subject is None:
            continue
        facts = await lookup.canon_rows(
            story_id, subject=str(subject), predicate=str(row.predicate_id)
        )
        found += [f.id for f in facts]
    return found


def state_findings(violations: Sequence[CheckedClaim]) -> list[Finding]:
    """#187: the State findings among ``violations``."""
    return [
        f for v in violations if v.result is not None and (f := v.result.state_finding) is not None
    ]


def _declared_index(claim: CheckedClaim) -> int | None:
    found = claim.claim.matched_declared_id
    if found is None or "." not in found:
        return None
    try:
        return int(found.rsplit(".", 1)[1])
    except ValueError:
        return None


REDACTED = "[private content removed]"
"""What replaces a span leaking another character's thought in a regeneration's rejected text
(#172): the private content is never echoed into a prompt."""


def redact(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """``text`` with each of ``spans`` replaced by ``REDACTED`` (overlaps merged)."""
    merged: list[list[int]] = []
    for start, end in sorted(s for s in spans if 0 <= s[0] < s[1] <= len(text)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out = text
    for start, end in reversed(merged):
        out = out[:start] + REDACTED + out[end:]
    return out


class RepairLadder:
    """One repair pass's ladder (see the module docstring). #1014: its ``repair.max_attempts``
    counts the regenerations of each render (``render_of``) separately; ``renders``,
    ``regenerated`` and ``regenerations`` say what it ran, for ``turn_report.repair`` and
    ``epistrel_repair_renders_total``."""

    name_words: frozenset[str] = frozenset()
    """#1019: the case-folded words of the whole cast's names and aliases (``name_only``)."""

    def __init__(
        self,
        db: AsyncEngine,
        models: ModelAccessLayer,
        story_id: str,
        *,
        turn: int,
        read_set: StructuredReadSet,
        max_attempts: int,
        recheck: Rechecker,
        output_ok: OutputCheck,
        fallback: FallbackLines,
        inputs_of: InputsOf,
        sketches_of: Mapping[int, Sequence[PropositionSketch]],
        params: ModelParams,
        extraction_calls: int,
        check_calls: int,
        render_of: RenderOf | None = None,
        cast_names: Mapping[str, str] | None = None,
        name_words: frozenset[str] | None = None,
        introduced: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self.db = db
        self.models = models
        self.story_id = story_id
        self.turn = turn
        self.read_set = read_set
        self.max_attempts = max_attempts
        self.recheck = recheck
        self.output_ok = output_ok
        self.fallback = fallback
        self.inputs_of = inputs_of
        self.sketches_of = sketches_of
        self.params = params
        self.extraction_calls = extraction_calls
        self.check_calls = check_calls
        self.render_of: RenderOf = render_of or (lambda _: None)
        self.cast_names: Mapping[str, str] = cast_names or {}
        """#1013: the render's cast names, for a copied line's ``no_copy`` constraint."""
        self.name_words = name_words or frozenset()
        self.introduced: Callable[[], Mapping[str, str]] = introduced or dict
        """#1024: the names of the places and objects the turn minted (``Introductions.names``),
        not yet entities, so a constraint names them, never by id."""
        self.used: dict[Hashable, int] = {}
        """#1014: the regenerations each render has used."""
        self.renders: list[Hashable] = []
        """#1014: every render that ran the ladder at least once, in order."""
        self.cascade_threshold = VerifySettings().cascade_threshold
        self.max_facts = VerifySettings().neighborhood_max_facts

    @property
    def attempts_used(self) -> int:
        """The regenerations this ladder ran, over every render."""
        return sum(self.used.values())

    @property
    def regenerated(self) -> list[Hashable]:
        """#1014: the renders with at least one regeneration attempt."""
        return [r for r in self.renders if self.used.get(r, 0)]

    @property
    def regeneration_cost(self) -> int:
        """What one regeneration reserves: the re-render, its extraction and one re-check per
        model-assisted output policy."""
        return 1 + self.extraction_calls + self.check_calls

    # -- the context ----------------------------------------------------------------------

    async def context(self, beat: TurnBeat, violations: Sequence[CheckedClaim]) -> Context:
        """FR-REPAIR-1's exposure (see the module docstring), read as the beat's speaker may."""
        async with self.db.connect() as conn:
            settings = await read_section(conn, VerifySettings)
            self.cascade_threshold = settings.cascade_threshold
            self.max_facts = settings.neighborhood_max_facts
            registry = await load_registry(conn, self.story_id)
            lookup = KeyedLookup(conn, self.read_set)
            viewpoint = self._viewpoint(beat)
            position = (self.turn, beat.beat)
            contradicted: dict[str, Fact] = {}
            neighbors: dict[str, Fact] = {}
            payloads: list[dict[str, Any]] = []
            for v in violations:
                if v.result is None or v.result.kind is not VerdictKind.CONTRADICTED:
                    continue
                cited = _fact_ids(v)
                members = await _closed_set_members(conn, lookup, self.story_id, v)
                found_facts = await lookup.canon_rows(
                    self.story_id,
                    viewpoint=viewpoint,
                    through_position=position,
                    ids=list(dict.fromkeys([*cited, *members])),
                )
                for fact in found_facts:
                    contradicted.setdefault(fact.id, fact)
                seeds = [Seed.fact(f.id) for f in found_facts]
                if not seeds and v.claim.proposition is not None:
                    seeds = [Seed.entity(v.claim.proposition.subject_id)]
                for seed in seeds:
                    found = await fact_neighborhood(
                        lookup,
                        self.story_id,
                        seed,
                        depth=NEIGHBORHOOD_DEPTH,
                        viewpoint=viewpoint,
                        through_position=position,
                        max_facts=self.max_facts,
                        registry=registry,
                    )
                    payloads.append(found.as_payload())
                    for fact in found.facts():
                        neighbors.setdefault(fact.id, fact)
            props = [f.proposition for f in (*contradicted.values(), *neighbors.values())]
            props += [v.claim.proposition for v in violations if v.claim.proposition is not None]
            names = await _names(conn, self.story_id, props)
            for minted, name in self.introduced().items():
                names.setdefault(minted, name)  # #1024
            speaker = None if beat.character_id is None else self.cast_names.get(beat.character_id)
            made = [await self._constraint(v, registry, names, speaker) for v in violations]
            # #1013: one ``no_copy`` line per source speaker, however many sentences it copied.
            copies = list(dict.fromkeys(c for c in made if c.kind == "no_copy"))
            constraints = tuple(
                [*(c for c in made if c.kind != "no_copy"), *copies] if copies else made
            )
            redactions = tuple(
                (v.claim.span[1], v.claim.span[2])
                for v in violations
                if v.result is not None and v.result.repair_tag == THINK_LEAK
            )
            frame = TimeFrame.of(await load_time_basis(conn, self.story_id))
        return Context(
            contradicted=tuple(contradicted.values()),
            neighborhood=tuple(neighbors.values()),
            payloads=tuple(payloads),
            names=names,
            registry=registry,
            constraints=constraints,
            frame=frame,
            redactions=redactions,
        )

    def _viewpoint(self, beat: TurnBeat) -> str:
        if beat.narrator or beat.kind == NARRATION or beat.character_id is None:
            return OMNISCIENT
        return viewpoint_of(beat.character_id)

    async def _constraint(
        self,
        v: CheckedClaim,
        registry: Registry,
        names: Mapping[str, str],
        speaker: str | None = None,
    ) -> Constraint:
        claim = v.claim.proposition
        cites = tuple(v.result.evidence_ids()) if v.result is not None else ()
        if v.result is not None and v.result.copied_from is not None:
            # #1013: names the source speaker only; the rejected text already carries the line.
            source = self.cast_names.get(v.result.copied_from[1]) or COPY_SOURCE_FALLBACK
            return Constraint("no_copy", COPY_CONSTRAINT.format(name=source))
        if v.result is not None and v.result.state_finding is not None:
            # #187: the finding's own line (a character's names no value it may not know).
            return Constraint("do_not_assert", v.result.state_finding.line)
        if v.result is not None and v.result.repair_tag == THINK_LEAK:
            # #172: the constraint names no private content (it is in the speaker's prompt).
            return Constraint(
                "do_not_assert",
                "Do not assert what another character privately thinks or feels; show only "
                "what can be seen or heard.",
            )
        if v.result is not None and v.result.repair_tag == CONFABULATION:
            recalled = (v.claim.recalled or "that").strip()[:200]
            return Constraint("do_not_assert", f"Do not claim to remember {recalled}.")
        if claim is None:
            return Constraint("do_not_assert", "Do not assert what this beat could not support.")
        if v.result is not None and v.result.repair_tag == RESOLUTION_CONFLICT:
            # #154: the beat bound an open thunk against recorded history; steer the binding
            # elsewhere (or leave it open).
            avoid = v.result.avoid
            name = names.get(avoid, avoid) if avoid is not None else None
            rendered = render_proposition(claim, registry, names)
            target = f"{name}" if name else "that value"
            return Constraint(
                "avoid_binding",
                f"avoid_binding: {target}. Recorded history rules out that {rendered}: settle "
                "it as something else, insignificant and with no history here, or leave it "
                "open.",
                cites,
            )
        if v.result is not None and v.result.hidden_conflict:
            # #169: hidden canon settles it; the constraint names nothing of the hidden value.
            rendered = render_proposition(claim, registry, names)
            return Constraint(
                "do_not_assert",
                f"Do not settle that {rendered}: this speaker does not know it; leave it open.",
                cites,
            )
        if v.result is not None and v.result.repair_tag == UNPERMITTED:
            denied = v.result.denied
            reason = "" if denied is None else f" ({denied.reason.value.replace('_', ' ')})"
            subject = names.get(claim.subject_id, claim.subject_id)
            toward = denied is not None and denied.reason.value in TOWARD_DENIALS
            if toward and speaker is not None and subject == speaker and claim.object_id:
                # #1060: the speaker's own action toward another character: name whose facts
                # they are (a locked-canon denial of the speaker's own fact still names it).
                subject = names.get(claim.object_id, claim.object_id)
            return Constraint(
                "do_not_assert",
                f"Do not introduce facts about {subject}: it is not this speaker's to tell"
                f"{reason}.",
                cites,
            )
        rendered = render_proposition(claim, registry, names)
        return Constraint("do_not_assert", f"Do not assert that {rendered}.", cites)

    # -- the run --------------------------------------------------------------------------

    async def run(
        self, beat: TurnBeat, violations: Sequence[CheckedClaim], budget: TurnBudget
    ) -> RepairResult:
        """Correct ``beat`` (see the module docstring)."""
        context = await self.context(beat, violations)
        sketches = list(self.sketches_of.get(beat.beat, ()))
        rejected: list[RejectedRender] = []
        degradation: list[tuple[str, str]] = []
        attempts = 0
        violating = {i for v in violations if (i := _declared_index(v)) is not None}
        stating = state_findings(violations)
        copied = [v for v in violations if v.result is not None and v.result.copied_from]
        flagged: set[str] = set()
        render = self.render_of(beat)
        if render not in self.renders:
            self.renders.append(render)

        def result(
            rung: int,
            accepted: TurnBeat | None,
            recheck: Recheck | None = None,
            chosen: Sequence[PropositionSketch] = (),
            *,
            fallback: bool = False,
        ) -> RepairResult:
            original = RejectedRender(
                beat=beat.beat,
                attempt=0,
                rung=rung,
                text=beat.text,
                constraints=context.constraints,
                verdicts=_verdicts(violations),
                detected_claims=_detected(violations),
            )
            repair_class = classify(rung, context.size, self.cascade_threshold)
            final = (
                None
                if accepted is None
                else replace(accepted, repaired_from=(rung, repair_class.value))
            )
            log.debug(
                "repair: beat %d accepted at rung %d (%s), %d rejected",
                beat.beat,
                rung,
                repair_class.value,
                len(rejected) + 1,
            )
            return RepairResult(
                beat=final,
                rung=rung,
                attempts=attempts,
                rejected=[original, *rejected],
                repair_class=repair_class,
                degradation=degradation,
                remaining_violations=[],
                original=beat,
                violations=list(violations),
                context=context,
                recheck=recheck,
                sketches=list(chosen),
                fallback=fallback,
                flagged=flagged,
            )

        async def attempt(
            rung: int,
            candidate: TurnBeat,
            chosen: Sequence[PropositionSketch],
            *,
            backstop: bool,
            number: int = 0,
            hash_: str | None = None,
        ) -> Recheck | None:
            def reject(claims: Sequence[CheckedClaim] = ()) -> None:
                rejected.append(
                    RejectedRender(
                        beat=beat.beat,
                        attempt=number,
                        rung=rung,
                        text=candidate.text,
                        constraints=context.constraints,
                        verdicts=_verdicts(claims),
                        detected_claims=_detected(claims),
                        prompt_hash=hash_,
                    )
                )

            if candidate.kind in DETERMINISTIC_KINDS and name_only(candidate.text, self.name_words):
                # #1019: a name is not a line.
                rejected.append(
                    RejectedRender(
                        beat=beat.beat,
                        attempt=number,
                        rung=rung,
                        text=candidate.text,
                        prompt_hash=hash_,
                        reasons=(NAME_ONLY,),
                    )
                )
                return None
            if not await self.output_ok(candidate.text):
                reject()
                return None
            found = await self.recheck(candidate, chosen, backstop, rung)
            if found.violations:
                if any(
                    c.result is not None and c.result.copied_from is not None
                    for c in found.violations
                ):
                    flagged.add(COPIED_LINE)  # #1013
                reject(found.violations)
                return None
            return found

        # Rung 1: narrowing (#187: never for a State finding, which no rewording fixes; #1013:
        # nor for a copied line).
        if beat.kind in DETERMINISTIC_KINDS and not stating and not copied:
            spans = self._spans(beat, violations, sketches)
            text = None if spans is None else narrow(beat.text, spans)
            if text is not None:
                kept = [
                    s
                    for n, s in enumerate(sketches)
                    if n not in violating and locate(text, [s.object, s.subject]) is not None
                ]
                found = await attempt(
                    NARROWING, replace(beat, text=text, claims=()), kept, backstop=False
                )
                if found is not None:
                    return result(NARROWING, found.beat, found, kept)
        # Rung 2: substitution.
        if beat.kind in DETERMINISTIC_KINDS and not stating and not copied:
            swapped = self._substitution(beat, violations, sketches, context)
            if swapped is not None:
                text, chosen = swapped
                found = await attempt(
                    SUBSTITUTION, replace(beat, text=text, claims=()), chosen, backstop=False
                )
                if found is not None:
                    return result(SUBSTITUTION, found.beat, found, chosen)
        # Rung 3: regeneration, while the turn can reserve it and the render has attempts left.
        inputs = self.inputs_of(beat)
        while inputs is not None:
            if self.used.get(render, 0) >= self.max_attempts:
                degradation.append((REPAIR_DETERMINISTIC, "max_attempts"))
                count_regeneration("skipped_max_attempts")
                break
            held = budget.held
            if not self._reserve(budget, stating):
                degradation.append((REPAIR_DETERMINISTIC, "budget"))
                count_regeneration("skipped_budget")
                break
            self.used[render] = self.used.get(render, 0) + 1
            attempts += 1
            found = None
            clock_drops = 0
            try:
                made = await self._regenerate(
                    beat,
                    inputs,
                    context,
                    budget,
                    # #190, #209: asked only to rewrite the prose, the rewrite keeps the
                    # beat's effects.
                    keep_effects=not stating or all(f.tag in PROSE_TAGS for f in stating),
                )
                if made is not None:
                    candidate, chosen, hash_, clock_drops = made
                    found = await attempt(
                        REGENERATION, candidate, chosen, backstop=True, number=attempts, hash_=hash_
                    )
            finally:
                budget.release_to(min(budget.held, held))
            # #1014: unusable output, a model error or a rejected candidate all fail.
            count_regeneration("failed" if found is None else "accepted")
            if found is not None:
                if EXTRACTION_DEGRADED in found.degradation:
                    degradation.append((REPAIR_UNEXTRACTED, "budget"))
                if clock_drops:
                    degradation.append((CLOCK_EFFECT_DROPPED, "state"))  # #996
                if copied:
                    count_copied_line("regenerated")
                return result(REGENERATION, found.beat, found, chosen)
            if copied:
                break  # #1013: exactly one regeneration for a copied line
        if copied:
            # #1013: no regeneration ran, or it failed: the beat is dropped, never the fallback.
            count_copied_line("dropped")
            degradation[:] = [(REPAIR_DROPPED, COPIED_LINE)]
            if stating and beat.kind in DETERMINISTIC_KINDS:
                # The State deterministic rung for the effect, built without the copied text.
                outcome = state_deterministic(
                    replace(beat, text=""), stating, words=self.name_words
                )
                if outcome is not None and outcome.text.strip():
                    return result(FALLBACK, outcome)
            gone = result(FALLBACK, None)
            gone.drop_cause = COPIED_LINE
            return gone
        # Rung 4: the safe fallback (a think or latent beat is dropped).
        degradation.append((SAFE_FALLBACK, REPAIR))
        if beat.kind not in DETERMINISTIC_KINDS:
            return result(FALLBACK, None)
        if stating and len(stating) == len(violations):
            # #187: the deterministic rung of a State finding (see the module docstring).
            settled = state_deterministic(beat, stating, words=self.name_words)
            if settled is not None:
                return result(FALLBACK, settled)
        line = await self._fallback_line(narrator=beat.narrator or beat.kind == NARRATION)
        # #1060: a character's fallback line is spoken, as the render's own fallback is
        # (``structured.pipeline``): a failing ``do`` beat falls back to a ``say``.
        kind: BeatKind = "say" if beat.kind == DO else beat.kind
        return result(
            FALLBACK,
            replace(beat, kind=kind, text=line, claims=(), mentions=(), effects=()),
            fallback=True,
        )

    def _reserve(self, budget: TurnBudget, stating: Sequence[Finding]) -> bool:
        """Whether one regeneration's calls could be reserved now (and they are held)."""
        cost = self.regeneration_cost
        if any(f.tag == TIME_STOP for f in stating) and budget.named.get(BUDGET_STOP, 0) >= cost:
            # #636: a due-event stop's regeneration is planned work (its reserved slot).
            budget.draw(BUDGET_STOP)
            budget.hold(cost)
            return True
        # Optional work (#156): never a reserved call, and only while it could start.
        return budget.try_optional(BUDGET_REPAIR, cost)

    # -- the rungs ------------------------------------------------------------------------

    def _spans(
        self,
        beat: TurnBeat,
        violations: Sequence[CheckedClaim],
        sketches: Sequence[PropositionSketch],
    ) -> list[tuple[int, int]] | None:
        """Where each violation sits in the beat's text; ``None`` when one cannot be found."""
        found: list[tuple[int, int]] = []
        for v in violations:
            _, start, end = v.claim.span
            whole = (start, end) == (0, len(beat.text))
            if v.claim.layer != "declared" and 0 <= start < end <= len(beat.text) and not whole:
                found.append((start, end))
                continue
            surfaces: list[str | None] = []
            index = _declared_index(v)
            if index is not None and index < len(sketches):
                surfaces += [sketches[index].object, sketches[index].subject]
            if v.claim.sketch is not None:
                surfaces += [v.claim.sketch.object_text, v.claim.sketch.subject_text]
            span = locate(beat.text, surfaces)
            if span is None:
                return None
            found.append(span)
        return found

    def _substitution(
        self,
        beat: TurnBeat,
        violations: Sequence[CheckedClaim],
        sketches: Sequence[PropositionSketch],
        context: Context,
    ) -> tuple[str, list[PropositionSketch]] | None:
        """Rung 2's text and declared sketches, or ``None`` when it does not apply."""
        if context.registry is None:
            return None
        text = beat.text
        chosen = list(sketches)
        by_id = {f.id: f for f in context.contradicted}
        for v in violations:
            result = v.result
            claim = v.claim.proposition
            if result is None or claim is None or result.kind is not VerdictKind.CONTRADICTED:
                return None
            if result.source != "deterministic" or not result.verdict.rules <= {
                Rule.FUNCTIONAL_KEY
            }:
                return None
            predicate = context.registry.get(claim.predicate_id)
            if predicate is None or predicate.cardinality is not Cardinality.FUNCTIONAL:
                return None
            cited = _fact_ids(v)
            if len(cited) != 1 or cited[0] not in by_id:
                return None
            replacement = _surface(by_id[cited[0]].proposition, context.names)
            index = _declared_index(v)
            surfaces: list[str] = []
            if index is not None and index < len(chosen) and chosen[index].object:
                surfaces.append(str(chosen[index].object))
            if v.claim.sketch is not None and v.claim.sketch.object_text:
                surfaces.append(v.claim.sketch.object_text)
            surfaces.append(_surface(claim, context.names))
            swapped = None
            for surface in dict.fromkeys(s for s in surfaces if s):
                swapped = substitute(text, surface, replacement)
                if swapped is not None:
                    break
            if swapped is None or replacement is None:
                return None
            text = swapped
            if index is not None and index < len(chosen):
                chosen[index] = chosen[index].model_copy(update={"object": replacement})
        return text, chosen

    async def _regenerate(
        self,
        beat: TurnBeat,
        base: GenerationInputs,
        context: Context,
        budget: TurnBudget,
        *,
        keep_effects: bool = True,
    ) -> tuple[TurnBeat, list[PropositionSketch], str, int] | None:
        """One ``regeneration`` call: the candidate beat, its declared sketches, the prompt
        hash and how many world-clock changes it dropped (#996), or ``None`` when the model
        gave nothing usable. #187: the candidate takes the effects the rewrite declares; with
        none declared it keeps the beat's own unless the repair is a State finding's
        (``keep_effects`` off)."""
        registry = context.registry
        assert registry is not None  # noqa: S101 - context() loads it

        def line(fact: Fact) -> str:
            p = fact.proposition
            rendered = render_proposition(p, registry, context.names)
            return f"{rendered} [{render_time(p, registry, context.frame)}]"[:1_000]

        contradicted = {f.id for f in context.contradicted}
        inputs = RegenerationInputs.model_validate(
            {
                **base.model_dump(mode="json"),
                "rejected_text": redact(beat.text, context.redactions),
                "beat_kind": beat.kind,
                "constraints": [
                    RepairConstraint(kind=c.kind, text=c.text[:1_000], cites=list(c.cites)[:50])
                    for c in context.constraints
                ],
                "contradicted_facts": [line(f) for f in context.contradicted][
                    :REGENERATION_MAX_RECORDS
                ],
                "neighborhood": [line(f) for f in context.neighborhood if f.id not in contradicted][
                    :REGENERATION_MAX_RECORDS
                ],
            }
        )
        _, _, messages = self.models.prepare(REGENERATION_PURPOSE.name, inputs)
        hash_ = prompt_hash(messages)
        try:
            found = await self.models.structured_call(
                REGENERATION_PURPOSE.name,
                RegeneratedBeat,
                inputs,
                params=self.params,
                # The deterministic rungs (1 and 2) ran before this call.
                deterministic_first=lambda: None,
                budget=budget,
            )
        except ModelError:
            return None
        if isinstance(found, Uncertain):
            return None
        intent = beat.intent
        if beat.kind == SAY and found.intent is not None:
            intent = found.intent
        effects = declared_of(found.effects)
        if not effects and keep_effects:
            effects = beat.effects
        candidate = replace(
            beat,
            text=found.text.strip(),
            intent=intent,
            claims=(),
            mentions=(),
            effects=effects,
        )
        return candidate, list(found.propositions), hash_, found.clock_effects_dropped

    async def _fallback_line(self, *, narrator: bool = False) -> str:
        """The first stored fallback line that passes the output policies (the last resort
        always stands). #652: a narrator beat's is the narrator's own line
        (``NARRATOR_FALLBACK``), never a character's."""
        if narrator and await self.output_ok(NARRATOR_FALLBACK):
            return NARRATOR_FALLBACK
        for source, text in self.fallback.candidates():
            if source == "last_resort" or await self.output_ok(text):
                return text
        return LAST_RESORT.replace(NAME_PLACEHOLDER, self.fallback.name)  # pragma: no cover


def state_deterministic(
    beat: TurnBeat, findings: Sequence[Finding], *, words: frozenset[str] = frozenset()
) -> TurnBeat | None:
    """#187: the deterministic rung of a beat whose only violations are State findings: a
    refused effect narrated as refused (every effect dropped: nothing changes); an unresolved or
    undeclared change without its sentence and its effect; ``None`` when nothing of the beat
    would be left (the fallback line then). #190: an unnarrated effect keeps its effect and the
    beat gains one line built from its reason (``Finding.narration``). #1019: a narrowing that
    leaves only a name (``words``, ``core.repair.rungs.name_only``) leaves nothing."""
    if any(f.tag == TIME_STOP for f in findings):
        # #196 (FR-CLOCK-16): nothing narrated past a due event's stop is committed.
        return None
    outcomes = [f for f in findings if f.tag == EXTERNAL_OUTCOME_UNCONFIRMED]
    if outcomes:
        # #209: the asserted outcome goes; the requested effects stay.
        found = _outcome_settled(beat, outcomes, words)
        findings = [f for f in findings if f.tag != EXTERNAL_OUTCOME_UNCONFIRMED]
        if found is None or not findings:
            return found
        beat = found
    unnarrated = [f for f in findings if f.tag == STATE_UNNARRATED and f.narration]
    rest = [f for f in findings if f.tag != STATE_UNNARRATED]
    settled = _state_settled(beat, rest, words) if rest else beat
    if not unnarrated or (settled is not None and settled.attempt):
        return settled  # a refusal drops every effect: nothing is left to narrate
    base = settled
    if base is None:
        gone = {f.state for f in rest}
        base = replace(beat, text="", effects=tuple(e for e in beat.effects if e.state not in gone))
    kept = {e.state for e in base.effects}
    lines = [f.narration for f in unnarrated if f.state in kept]
    if not lines:
        return settled
    text = " ".join(t for t in [base.text.strip(), *lines] if t)
    return replace(base, text=text, claims=(), mentions=())


def _outcome_settled(
    beat: TurnBeat, findings: Sequence[Finding], words: frozenset[str] = frozenset()
) -> TurnBeat | None:
    """#209: ``beat`` without the sentences asserting an unconfirmed external outcome, its
    effects kept; when nothing else is left (#1019: or only a name), the attempt narrated
    (``Finding.refusal``)."""
    spans = [f.span for f in findings if f.span is not None]
    text = narrow(beat.text, spans) if spans else None
    if text is not None and not name_only(text, words):
        return replace(beat, text=text, claims=(), mentions=())
    attempt = next((f.refusal for f in findings if f.refusal), "")
    if not attempt:
        return None
    return replace(beat, text=attempt, claims=(), mentions=(), attempt=True)


def _state_settled(
    beat: TurnBeat, findings: Sequence[Finding], words: frozenset[str] = frozenset()
) -> TurnBeat | None:
    refused = [f for f in findings if f.tag in REFUSED_TAGS and f.refusal]
    if refused:
        return replace(
            beat,
            text=refused[0].refusal,
            claims=(),
            mentions=(),
            effects=(),
            attempt=True,
        )
    bad = {f.effect for f in findings if f.effect is not None}
    effects = tuple(e for n, e in enumerate(beat.effects) if n not in bad)
    spans = [f.span for f in findings if f.span is not None]
    if not spans:
        return replace(beat, claims=(), mentions=(), effects=effects)
    text = narrow(beat.text, spans)
    if text is None or name_only(text, words):
        return None
    gone = {f.state for f in findings if f.span is not None}
    effects = tuple(e for e in effects if e.state not in gone)
    return replace(beat, text=text, claims=(), mentions=(), effects=effects)


def settled(beat: TurnBeat, violations: Sequence[CheckedClaim]) -> RepairResult:
    """#187: ``beat``'s State findings settled without the ladder (its repair module off, the
    turn the safe fallback, or a finding the ladder never saw): ``state_deterministic``'s beat
    (``None``: the beat is dropped), as the deterministic rung's result, so the pipeline merges
    it like any repair and writes its audit records."""
    found = state_findings(violations)
    accepted = None if beat.kind not in DETERMINISTIC_KINDS else state_deterministic(beat, found)
    repair_class = classify(FALLBACK, 0)
    return RepairResult(
        beat=None
        if accepted is None
        else replace(accepted, repaired_from=(FALLBACK, repair_class.value)),
        rung=FALLBACK,
        attempts=0,
        rejected=[
            RejectedRender(
                beat=beat.beat,
                attempt=0,
                rung=FALLBACK,
                text=beat.text,
                constraints=tuple(Constraint("do_not_assert", f.line) for f in found),
                verdicts=_verdicts(violations),
                detected_claims=_detected(violations),
            )
        ],
        repair_class=repair_class,
        degradation=[],
        remaining_violations=[],
        original=beat,
        violations=list(violations),
        context=Context(),
    )


def copy_dropped(beat: TurnBeat, violations: Sequence[CheckedClaim]) -> RepairResult:
    """#1013: ``beat``, flagged ``copied_line``, dropped without the ladder (the story's repair
    module off): ``repair_dropped`` (cause ``copied_line``), its rejected text recorded."""
    count_copied_line("dropped")
    repair_class = classify(FALLBACK, 0)
    return RepairResult(
        beat=None,
        rung=FALLBACK,
        attempts=0,
        rejected=[
            RejectedRender(
                beat=beat.beat,
                attempt=0,
                rung=FALLBACK,
                text=beat.text,
                verdicts=_verdicts(violations),
                detected_claims=_detected(violations),
            )
        ],
        repair_class=repair_class,
        degradation=[(REPAIR_DROPPED, COPIED_LINE)],
        remaining_violations=[],
        original=beat,
        violations=list(violations),
        context=Context(),
        drop_cause=COPIED_LINE,
    )


def _surface(p: Proposition, names: Mapping[str, str]) -> str:
    """How a proposition's object reads in prose: the entity's canonical name, the set's names
    joined, or the literal's text."""
    if p.object_id is not None:
        return names.get(p.object_id, p.object_id)
    if p.object_set:
        found = [names.get(o, o) for o in p.object_set]
        return found[0] if len(found) == 1 else ", ".join(found[:-1]) + " and " + found[-1]
    if p.literal is not None:
        value = p.literal.value
        text = str(value).lower() if isinstance(value, bool) else str(value)
        return f"{text} {p.literal.unit}" if p.literal.unit else text
    return ""


async def _names(
    conn: AsyncConnection, story_id: str, props: Sequence[Proposition]
) -> dict[str, str]:
    ids: list[str] = []
    for p in props:
        ids.append(p.subject_id)
        if p.object_id is not None:
            ids.append(p.object_id)
        ids += list(p.object_set or ())
        ids += [v for v in p.qualifiers.values() if isinstance(v, str)]
    if not ids:
        return {}
    rows = await conn.execute(
        select(entities.c.id, entities.c.canonical_name).where(
            entities.c.story_id == story_id, entities.c.id.in_(list(dict.fromkeys(ids)))
        )
    )
    return {str(r.id): str(r.canonical_name) for r in rows}


__all__ = [
    "COPIED_LINE",
    "COPY_CONSTRAINT",
    "DETERMINISTIC_KINDS",
    "DROPPED",
    "DUPLICATE_FALLBACK",
    "NAME_ONLY",
    "NARRATOR_FALLBACK",
    "NARRATOR_RENDER",
    "REGENERATION_OUTCOMES",
    "REPAIR_DETERMINISTIC",
    "REPAIR_DROPPED",
    "REPAIR_STEPS",
    "REPAIR_UNEXTRACTED",
    "RUNG_NAMES",
    "SAFE_FALLBACK",
    "SPEAKER_HAS_LINE",
    "STATE_SETTLED",
    "UNCHECKED",
    "Context",
    "InputsOf",
    "OutputCheck",
    "Recheck",
    "Rechecker",
    "RegeneratedBeat",
    "RejectedRender",
    "RenderOf",
    "RepairLadder",
    "RepairResult",
    "copy_dropped",
    "drop_plan",
    "real_line",
    "speaker_of",
    "state_deterministic",
    "state_findings",
    "violation_code",
    "violation_codes",
]
