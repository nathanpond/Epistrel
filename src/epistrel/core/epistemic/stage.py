"""A sequential turn's staged beats: verified, not yet committed (FR-MULTI-5,11, FR-EPI-14 (b),
FR-STORE-31; docs/02 §4.4; #174).

In ``sequential`` mode the primaries render one after another. Each render's beats are detected,
verified and repaired before the next primary renders; the beats that come out are **staged**
here (``TurnStage.stage``) and only staged beats reach a later primary. Nothing is written to the
database until the turn's single commit (#150's rule): a turn that is abandoned (a conflict, a
failed later render) discards its stage, and every belief formed from it with it.

What the stage answers:

- ``perceived_for(character_id, …)``: the staged beats as that character perceived them, routed
  over the turn's scene at the story's perception level by the same routing as committed history
  (``core.memory.records.perceived_window``, #161, #170): content in full, an envelope as its
  envelope, an eavesdropper's part as that part; no ``latent``, and no other character's
  ``think``. ``core.memory.records.working_memory(…, stage=)`` appends them after the committed
  window (flagged ``staged``), and ``core.epistemic.context`` shows them under ``## Just now``.
- ``antecedents_for(character_id)``: the staged beats that may be a later render's antecedent
  (FR-EPI-14 (b)): beats rendered before the character's own render, by someone else, whose
  claims verified (``StagedBeat.verified``). A beat whose claims stayed uncertain, or none of
  whose claims verified, is no antecedent.
- ``staged_beat(ref)``: #160's ``AntecedentOracle`` for ``antecedent = staged_beat`` (``{
  operation_id, variant, beat}``): a verified staged beat of this turn at ``(turn, variant, 0,
  beat, step)``. ``render_step`` is #168's (``None`` here). ``bind`` names the variant once the
  commit knows it.
- ``accept`` / ``accepted``: a later primary's acceptance of an earlier beat's claim (#162's
  testimony default, formed once at staging), which joins that primary's belief record set for
  its own render's verification (``BeliefSource.stage``, ``replay.stage_path``).

``replace(index, beat)`` swaps a staged beat for its regenerated version: the acceptances formed
from the old one go, so a later render never rests on the rejected version. Pure; no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from epistrel.core.epistemic import perception
from epistrel.core.epistemic.transitions import StagedRecord
from epistrel.core.membership import Position
from epistrel.core.memory import records
from epistrel.core.memory.records import HistoryBeat, WorkingBeat
from epistrel.core.propositions.model import Proposition

OBSERVABLE = frozenset({"say", "do", "narration"})
"""The kinds a later render may perceive (and rest a belief on)."""


@dataclass(frozen=True, slots=True)
class StagedBeat:
    """One staged beat: its number, kind, speaker (``None``: narration), text, perception
    qualifiers, the propositions its claims verified (``supported`` or canonized: what makes it
    an antecedent) and its declared step (#168)."""

    beat: int
    kind: str
    character_id: str | None
    text: str
    qualifiers: perception.Qualifiers = field(default_factory=perception.Qualifiers)
    narrator: bool = False
    verified: tuple[Proposition, ...] = ()
    step: int = 0

    def working(self, turn: int) -> WorkingBeat:
        return WorkingBeat(
            event_id="",
            narrative_turn=turn,
            turn_variant=None,
            beat=self.beat,
            kind=self.kind,  # type: ignore[arg-type]
            character_id=None if self.kind == "narration" else self.character_id,
            text=self.text,
            staged=True,
        )


@dataclass(frozen=True, slots=True)
class Acceptance:
    """A later primary's staged acceptance of a beat's claim (#162's default at staging)."""

    witness: str
    beat: int
    proposition: Proposition
    position: Position


class TurnStage:
    """The turn's staged beats and the acceptances formed from them (see the module docstring).
    Mutable until the commit."""

    def __init__(self, operation_id: str, turn: int, variant: int | None = None) -> None:
        self.operation_id = operation_id
        self.turn = turn
        self.variant = variant
        self._beats: dict[int, StagedBeat] = {}
        self._renders: dict[str, int] = {}
        """Each primary's first beat (its render's place in the order)."""
        self._accepted: list[Acceptance] = []
        self.discarded = False

    # -- staging -----------------------------------------------------------------------------

    def open_render(self, character_id: str, first_beat: int) -> None:
        """``character_id``'s render starts at ``first_beat`` (staged beats before it are its
        candidate antecedents)."""
        self._renders.setdefault(character_id, first_beat)

    def stage(self, beat: StagedBeat, verified_props: Sequence[Proposition] = ()) -> None:
        """Stage ``beat`` (verified), with the propositions its claims verified."""
        if verified_props:
            beat = replace(beat, verified=tuple(verified_props))
        self._beats[beat.beat] = beat

    def replace(self, index: int, beat: StagedBeat) -> None:
        """Swap staged beat ``index`` for ``beat`` (its regenerated version): every acceptance
        formed from the old one is dropped."""
        self._beats.pop(index, None)
        self._accepted = [a for a in self._accepted if a.beat != index]
        self._beats[beat.beat] = beat

    def discard(self) -> None:
        """The turn was abandoned: nothing staged survives."""
        self._beats.clear()
        self._accepted.clear()
        self._renders.clear()
        self.discarded = True

    def bind(self, variant: int) -> None:
        """The turn's variant, known at its commit."""
        self.variant = variant

    # -- reads -------------------------------------------------------------------------------

    @property
    def beats(self) -> list[StagedBeat]:
        return [self._beats[n] for n in sorted(self._beats)]

    def get(self, index: int) -> StagedBeat | None:
        return self._beats.get(index)

    def history(self) -> list[HistoryBeat]:
        """The staged beats as routing re-reads history (``records.HistoryBeat``)."""
        return [HistoryBeat(b.working(self.turn), b.qualifiers) for b in self.beats]

    def perceived_for(
        self,
        character_id: str,
        *,
        level: perception.Level,
        scene: perception.SceneState | None,
        names: Mapping[str, str],
        places: Mapping[str, str],
    ) -> list[WorkingBeat]:
        """The staged beats as ``character_id`` perceived them (see the module docstring); at
        perception ``none`` (or with no scene), every observable beat and its own thoughts."""
        if level == perception.NONE or scene is None:
            return [
                b.working(self.turn)
                for b in self.beats
                if b.kind in OBSERVABLE or (b.kind == "think" and b.character_id == character_id)
            ]
        found = records.perceived_window(
            [(scene, self.history())], character_id, level, names, places
        )
        return [replace(b, staged=True) for b in found]

    def render_of(self, character_id: str) -> int | None:
        return self._renders.get(character_id)

    def antecedents_for(self, character_id: str) -> list[StagedBeat]:
        """The staged beats ``character_id``'s render may rest a belief on (FR-EPI-14 (b))."""
        first = self._renders.get(character_id)
        return [
            b
            for b in self.beats
            if b.kind in OBSERVABLE
            and b.verified
            and b.character_id != character_id
            and (first is None or b.beat < first)
        ]

    def is_antecedent_for(self, character_id: str, beat: int) -> bool:
        return any(b.beat == beat for b in self.antecedents_for(character_id))

    def accept(self, witness: str, beat: int, proposition: Proposition, position: Position) -> None:
        """``witness`` accepted ``proposition`` from staged beat ``beat`` (#162's default)."""
        if self.is_antecedent_for(witness, beat):
            self._accepted.append(Acceptance(witness, beat, proposition, position))

    def accepted(self, witness: str) -> list[Acceptance]:
        return [a for a in self._accepted if a.witness == witness and a.beat in self._beats]

    # -- the antecedent oracle (#160) ---------------------------------------------------------

    def position_of(self, beat: StagedBeat) -> Position:
        return Position(self.turn, int(self.variant or 0), 0, beat.beat, beat.step)

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        if self.discarded or str(ref.get("operation_id")) != self.operation_id:
            return None
        if self.variant is not None and int(ref.get("variant", -1)) != self.variant:
            return None
        found = self._beats.get(int(ref.get("beat", -1)))
        if found is None or not found.verified or found.kind not in OBSERVABLE:
            return None
        return StagedRecord(
            self.position_of(found),
            frozenset(str(p.normalized_key()) for p in found.verified),
        )

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        del ref
        return None

    def report(self) -> dict[str, Any]:
        return {"staged": len(self._beats), "accepted": len(self._accepted)}


@dataclass(frozen=True, slots=True)
class Oracles:
    """Several ``AntecedentOracle``s as one: the first that answers wins (the stage's staged
    beats and a render's steps)."""

    found: tuple[Any, ...]

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        for oracle in self.found:
            answer = oracle.staged_beat(ref)
            if answer is not None:
                return answer  # type: ignore[no-any-return]
        return None

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        for oracle in self.found:
            answer = oracle.render_step(ref)
            if answer is not None:
                return answer  # type: ignore[no-any-return]
        return None


__all__ = ["OBSERVABLE", "Acceptance", "Oracles", "StagedBeat", "TurnStage"]
