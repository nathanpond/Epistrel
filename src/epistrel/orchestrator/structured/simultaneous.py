"""A ``simultaneous`` structured turn's adjudication: conflicting actions, the tie-break and the
loser's attempt (FR-MULTI-5,8, FR-MULTI-11; docs/02 §8; #175).

**The fan-out** (``orchestrator.structured.pipeline``). Under ``interaction_mode =
simultaneous`` every primary's context is built from the turn's starting state (#170's
``build_context`` with no staged beat of this turn: the player's input and the narrator's
narration only), each render is gated, detected and verified alone (#166: against its own
beliefs and canon, the same-turn overlay holding only the input's and the narration's claims)
and repaired alone (#151), then staged (``core.epistemic.stage.TurnStage``, ``mode =
simultaneous``: no staged beat is anyone's antecedent, and a ``staged_beat`` antecedent is
refused with ``antecedent_unavailable_in_mode``). The renders run one after another: the turn's
one budget and connection serve them, and none waits on another's output.

**Detect → tie-break → repair** (``adjudicate``), after every primary is verified and repaired
alone and before routing and composition:

1. ``effects_of``: the turn's effects (``core.epistemic.conflicts.Effect``): each staged
   ``do``'s and ``latent``'s claims that verified (``supported`` or novel and permitted; an
   ``uncertain``, contradicted or denied claim is no effect), and the player's input ``do``
   (rank 0: the player wins a request-order tie), each ranked by its primary's place in request
   order. Speech, thoughts and the narrator's beats never take part.
2. ``core.epistemic.conflicts.detect_conflicts``: deterministic and model-free.
3. The tie-break (``simultaneous_tie_break``, ``multi.tie_break_of``): ``request_order`` (the
   earlier-named primary's action stands) or ``director``: one ``generation`` call
   (``director_tiebreak.j2``, its answer ``{adjudications: [{object, winner}]}``) seeing only
   the conflicting ``say``/``do`` texts and the scene, optional work (``budget
   .DIRECTOR_TIEBREAK``) that falls back to request order with ``director_tiebreak_skipped``
   when it cannot be paid for, fails, or answers a winner who did not act on the object. A
   conflict the player's input takes part in is settled by request order (the player's action
   is never a model's to overrule). Conflicts are settled in the order of their first effect,
   one winner each; an effect whose beat already lost an earlier conflict takes no part in a
   later one.
4. Each losing beat is **repaired in character as an attempt** (``attempt_repair``, #151's
   rung 2, deterministic): a ``do`` becomes ``<actor> reaches for <object>, but it is already
   gone.`` (never naming the winner), flagged ``attempt`` so no detector reads it again; its
   claims, canon requests and mentions go with its old text (its effect is dropped before any
   reconciliation), and the rejected text is a ``render_rejected`` audit record (rung 2, class
   ``substitution``); a losing ``latent`` is dropped. The attempt keeps its beat's slot.

``turn_report.multi.adjudications`` and the response's ``adjudications`` list ``{object,
winner, loser, rule}`` (``rule`` ∈ ``request_order``, ``director``; ``object`` an entity id).
A non-conflicting turn records none. Log lines carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.epistemic.conflicts import Conflict, Effect, detect_conflicts
from epistrel.core.propositions.registry import Registry
from epistrel.core.repair.rungs import SUBSTITUTION, RepairClass
from epistrel.core.turns.beats import TurnBeat
from epistrel.orchestrator.budget import DIRECTOR_TIEBREAK, TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    GENERATION,
    GenerationInputs,
    PlayerInput,
    TiebreakBeat,
    TiebreakConflict,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.repair.ladder import Context, RejectedRender, RepairResult
from epistrel.orchestrator.verify.extraction import CheckedClaim

log = logging.getLogger(__name__)

SIMULTANEOUS = "simultaneous"
REQUEST_ORDER, DIRECTOR = "request_order", "director"
DIRECTOR_VARIANT = "director_tiebreak"
DIRECTOR_TIEBREAK_SKIPPED = "director_tiebreak_skipped"
"""Degradation: the director's tie-break could not be made or answered nothing usable, so
request order settled the turn's conflicts."""
EFFECT_KINDS = frozenset({"do", "latent"})
NOT_EFFECTS = frozenset({"contradicted", "uncertain", "denied"})
RECENT_BEATS = 5
MAX_CONFLICTS = 12


def effects_of(
    claims: Sequence[CheckedClaim],
    beats: Sequence[TurnBeat],
    order: Sequence[str],
    *,
    actor_id: str,
) -> list[Effect]:
    """The turn's effects (see the module docstring): ``order`` the primaries in request
    order, ``actor_id`` whose input beat 1 is."""
    by_beat = {b.beat: b for b in beats}
    rank = {c: n + 1 for n, c in enumerate(order)}
    found: list[Effect] = []
    for c in claims:
        proposition, number = c.claim.proposition, c.claim.beat
        if proposition is None or number is None or c.verdict in NOT_EFFECTS:
            continue
        if c.result is not None and c.result.needs_repair:
            continue
        if number == 1:
            if c.claim.channel == "do":
                found.append(Effect(1, actor_id, 0, proposition, c.claim_id, player=True))
            continue
        beat = by_beat.get(number)
        if (
            beat is None
            or beat.kind not in EFFECT_KINDS
            or beat.narrator
            or beat.attempt
            or beat.character_id not in rank
        ):
            continue
        found.append(
            Effect(
                number,
                str(beat.character_id),
                rank[str(beat.character_id)],
                proposition,
                c.claim_id,
            )
        )
    return found


@dataclass(frozen=True, slots=True)
class Adjudication:
    """One settled conflict over ``object_id``: the winner's and the loser's characters and
    beats, and the rule that chose."""

    object_id: str
    winner: str
    loser: str
    rule: str
    winner_beat: int
    loser_beat: int

    def report(self) -> dict[str, Any]:
        return {
            "object": self.object_id,
            "winner": self.winner,
            "loser": self.loser,
            "rule": self.rule,
        }


# -- the director's tie-break -------------------------------------------------------------------


class TiebreakAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: str = Field(max_length=64)
    winner: str = Field(max_length=64)


class DirectorTiebreak(BaseModel):
    """The director's answer (``response_schema``): one winner per contested object."""

    model_config = ConfigDict(extra="forbid")

    stub_script: ClassVar[str] = DIRECTOR_VARIANT
    """The stub adapter answers this schema's calls from scripts keyed ``director_tiebreak``
    (``stub.script("director_tiebreak", …)``), never from the ``generation`` queue."""

    adjudications: list[TiebreakAnswer] = Field(default_factory=list, max_length=MAX_CONFLICTS * 2)

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Any]) -> dict[str, Any]:
        """The stub's default: no judgment (request order stands)."""
        del digest, messages
        return {"adjudications": []}


def validated_winners(
    answer: Sequence[TiebreakAnswer], conflicts: Sequence[Conflict]
) -> dict[str, str] | None:
    """The director's winners by object; ``None`` when an answer names an object no conflict
    contests, a winner who did not act on it, or one object twice. An object it leaves out is
    settled by request order."""
    acted = {c.object_id: set(c.characters) for c in conflicts}
    found: dict[str, str] = {}
    for a in answer:
        if a.object not in acted or a.winner not in acted[a.object] or a.object in found:
            return None
        found[a.object] = a.winner
    return found


async def director_winners(
    models: ModelAccessLayer,
    conflicts: Sequence[Conflict],
    beats: Mapping[int, TurnBeat],
    *,
    names: Mapping[str, str],
    object_names: Mapping[str, str],
    actor: Any,
    say: str | None,
    do: str | None,
    guidance: str,
    instructions: str,
    scenario_text: str,
    director_notes: str,
    scene: str,
    recent: Sequence[str],
    budget: TurnBudget,
    params: ModelParams,
) -> tuple[dict[str, str] | None, str | None]:
    """Ask the director who got there first (see the module docstring): the winners by object
    (``None`` with the skip's cause when request order must stand); never raises for the
    model."""
    if not conflicts:
        return {}, None
    if not budget.try_optional(DIRECTOR_TIEBREAK, 1):
        return None, "budget"
    held = budget.held
    inputs = GenerationInputs(
        rating_guidance=guidance,
        custom_instructions=instructions,
        scenario_text=scenario_text,
        director_notes=director_notes,
        scene=scene,
        working_memory=list(recent[-RECENT_BEATS:]),
        player_input=PlayerInput(name=actor.name, say=say, do=do),
        tiebreak_conflicts=[
            TiebreakConflict(
                object=c.object_id,
                name=object_names.get(c.object_id, ""),
                actions=[
                    TiebreakBeat(
                        character_id=e.character_id,
                        name=names.get(e.character_id, e.character_id),
                        kind="do",
                        text=beats[e.beat].text[:2_000],
                    )
                    for e in c.effects
                    if e.beat in beats and beats[e.beat].kind == "do"
                ][:6],
            )
            for c in conflicts[:MAX_CONFLICTS]
        ],
    )
    try:
        with budget.charging(DIRECTOR_TIEBREAK):
            found = await models.structured_call(
                GENERATION.name,
                DirectorTiebreak,
                inputs,
                params=params.merged(ModelParams(temperature=0.0)),
                budget=budget,
                variant=DIRECTOR_VARIANT,
            )
    except ModelError:
        return None, "model"
    finally:
        budget.release_to(min(budget.held, max(0, held - 1)))
    if isinstance(found, Uncertain):
        return None, "model"
    winners = validated_winners(found.adjudications, conflicts)
    if winners is None:
        return None, "invalid_answer"
    log.debug("director tie-break: %d conflicts, %d judged", len(conflicts), len(winners))
    return winners, None


# -- settling ------------------------------------------------------------------------------------


def settle(
    conflicts: Sequence[Conflict], winners: Mapping[str, str] | None = None
) -> list[Adjudication]:
    """One winner per conflict, in order (see the module docstring): the director's when it
    judged the object and the player took no part, else the earliest in request order."""
    lost: set[int] = set()
    found: list[Adjudication] = []
    for conflict in conflicts:
        live = [e for e in conflict.effects if e.beat not in lost]
        if len({e.character_id for e in live}) < 2:
            continue
        chosen = None if winners is None else winners.get(conflict.object_id)
        player = any(e.player for e in live)
        if chosen is not None and not player and any(e.character_id == chosen for e in live):
            rule, winner = DIRECTOR, next(e for e in live if e.character_id == chosen)
        else:
            rule, winner = REQUEST_ORDER, live[0]
        seen: set[str] = set()
        for effect in live:
            if effect.character_id == winner.character_id or effect.player:
                continue
            lost.add(effect.beat)
            if effect.character_id in seen:
                continue
            seen.add(effect.character_id)
            found.append(
                Adjudication(
                    conflict.object_id,
                    winner.character_id,
                    effect.character_id,
                    rule,
                    winner.beat,
                    effect.beat,
                )
            )
    return found


def attempt_text(actor: str, obj: str) -> str:
    """The loser's attempt (rung 2's deterministic rewrite): it never names the winner."""
    return f"{actor} reaches for {obj}, but it is already gone."


def attempt_repair(
    beat: TurnBeat, violations: Sequence[CheckedClaim], *, actor: str, obj: str
) -> RepairResult:
    """The losing ``beat`` repaired in character as an attempt (see the module docstring): the
    repair ladder's result shape, so the pipeline merges it like any repair and writes its
    ``render_rejected``."""
    rejected = RejectedRender(
        beat=beat.beat,
        attempt=0,
        rung=SUBSTITUTION,
        text=beat.text,
        verdicts=tuple(c.result.as_payload(c.claim_id) for c in violations if c.result),
        detected_claims=tuple(c.claim.payload() for c in violations),
    )
    accepted = (
        None
        if beat.kind != "do"
        else replace(
            beat,
            text=attempt_text(actor, obj),
            claims=(),
            mentions=(),
            attempt=True,
            repaired_from=(SUBSTITUTION, RepairClass.SUBSTITUTION.value),
        )
    )
    return RepairResult(
        beat=accepted,
        rung=SUBSTITUTION,
        attempts=1,
        rejected=[rejected],
        repair_class=RepairClass.SUBSTITUTION,
        degradation=[],
        remaining_violations=[],
        original=beat,
        violations=list(violations),
        context=Context(),
    )


def losing_claims(
    claims: Sequence[CheckedClaim], adjudications: Sequence[Adjudication]
) -> dict[int, list[CheckedClaim]]:
    """Each losing beat's claims (the violations its attempt repair reports)."""
    beats = {a.loser_beat for a in adjudications}
    found: dict[int, list[CheckedClaim]] = {b: [] for b in beats}
    for c in claims:
        if c.claim.beat in found and c.claim.proposition is not None:
            found[int(c.claim.beat)].append(c)
    return found


def conflicts_of(
    claims: Sequence[CheckedClaim],
    beats: Sequence[TurnBeat],
    order: Sequence[str],
    *,
    actor_id: str,
    registry: Registry,
) -> list[Conflict]:
    """The turn's conflicts over its effects (``effects_of`` then ``detect_conflicts``)."""
    return detect_conflicts(effects_of(claims, beats, order, actor_id=actor_id), registry=registry)


__all__ = [
    "DIRECTOR",
    "DIRECTOR_TIEBREAK_SKIPPED",
    "DIRECTOR_VARIANT",
    "REQUEST_ORDER",
    "SIMULTANEOUS",
    "Adjudication",
    "DirectorTiebreak",
    "TiebreakAnswer",
    "attempt_repair",
    "attempt_text",
    "conflicts_of",
    "director_winners",
    "effects_of",
    "losing_claims",
    "settle",
    "validated_winners",
]
