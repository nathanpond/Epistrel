"""A multi-character turn's concurrent commitments, reconciled on one shared timeline before
composition (FR-MULTI-3, FR-COMMIT-1; docs/02 §8; #177).

Several renders (and the narrator's) can each commit the world to something in one turn: a
``latent`` effect, a permitted novel claim canonized at the commit (#144), a hidden commit
(#169). ``reconcile_turn`` gathers every such canon write of the turn (``Commitment``: the
turn's canon requests and its ``latent`` claims) onto one timeline, in beat order, and checks
every pair of them from two different beats, those inside one render included:

- **A contradiction** is a conclusive ``contradicted`` from #140's deterministic rules read
  either way (the functional-key rule #175's conflict detector uses, closed sets, max
  cardinality, exclusions, implications) or the core possession/placement pair
  (``core.epistemic.conflicts.clash``). ADR-160's ``uncertain(world_knowledge)`` is never one.
  Only affirmed commitments take part.
- **A legitimate supersession** is not a contradiction: in ``sequential`` mode a later
  ``latent`` whose render saw the earlier beat as a staged beat (#174: it was rendered before
  that render began) moves the world on ("the key on the table" → "the key in his pocket").
- **Identical commitments** of two beats (the same canon write, ``CanonRequest.identity``) are
  one fact: the commit canonizes it once (the first beat keeps its position) and the pair is
  recorded with the rule ``dedupe``.
- ``simultaneous`` (#175): the pairs adjudication already settled are skipped; their losers'
  effects were dropped before reconciliation.

**Who stands** (the authority hierarchy, #146's order): the **author** (a narrator beat, or the
player's input: the ``co_author`` player) over a character; then, in ``sequential`` mode, the
**earlier verified beat** over a later one (``earlier_beat``: a later render can never
override what an earlier one committed); in ``simultaneous`` mode **request order**
(``request_order``: the earlier-named primary). Two author commitments: the earlier stands.

**The loser** is rewritten, not committed: a losing ``do`` becomes an attempt (#151's rung 2,
``simultaneous.attempt_repair``: its claims, canon requests and mentions go, a
``render_rejected`` audit record keeps its text); a losing ``latent`` is dropped
(``latent_dropped``: its canon write goes, no belief is formed from it); any other loser (a
``say``'s or a narration's canonized claim) keeps its words and loses its canon write. A pair
whose loser lost already is not recorded twice.

``Reconciliation.report()`` is ``turn_report.reconcile`` and the response's ``reconcile``:
``{pairs: [{proposition, winner_beat, loser_beat, rule, action}]}``, proposition keys, beat
numbers and codes only (invariant 3); the pipeline records the pairs as a ``turn_reconciled``
audit event. Pure: no I/O, and this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from epistrel.core.canon.canonize import CanonRequest
from epistrel.core.epistemic.conflicts import Effect, clash
from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.turns.beats import TurnBeat

Rule = Literal["author", "earlier_beat", "request_order", "dedupe"]
Action = Literal["attempt", "latent_dropped", "canon_dropped", "kept_once"]
AUTHOR: Rule = "author"
EARLIER_BEAT: Rule = "earlier_beat"
REQUEST_ORDER: Rule = "request_order"
DEDUPE: Rule = "dedupe"
SEQUENTIAL, SIMULTANEOUS = "sequential", "simultaneous"
PLAYER_BEAT = 1


@dataclass(frozen=True, slots=True)
class Commitment:
    """One canon write of the turn: its beat, kind and actor, whether it is the author's (a
    narrator beat or the player's input), the render it came from (its place in render order;
    0 for the player's input and the narrator's opening), its proposition, its claim id, and the
    canon request it is (``None`` for a ``latent``, which the commit canonizes apart)."""

    beat: int
    kind: str
    character_id: str | None
    author: bool
    render: int
    proposition: Proposition
    claim_id: str | None = None
    request: CanonRequest | None = None

    def key(self) -> tuple[int, str]:
        return (self.beat, self.claim_id or "")

    @property
    def identity(self) -> str:
        return self.proposition.model_dump_json()


@dataclass(frozen=True, slots=True)
class Pair:
    """One reconciled pair: the proposition key, the beat that stood, the beat that lost, the
    rule, and what became of the loser."""

    proposition: str
    winner_beat: int
    loser_beat: int
    rule: Rule
    action: Action

    def report(self) -> dict[str, Any]:
        return {
            "proposition": self.proposition,
            "winner_beat": self.winner_beat,
            "loser_beat": self.loser_beat,
            "rule": self.rule,
            "action": self.action,
        }

    def body(self) -> dict[str, Any]:
        """The response's ``reconcile`` entry (no ``action``)."""
        return {k: v for k, v in self.report().items() if k != "action"}


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What ``reconcile_turn`` decided: the pairs, the beats rewritten as attempts, the
    ``latent`` claims dropped and the canon requests dropped (by claim id, or by beat)."""

    pairs: tuple[Pair, ...] = ()
    attempts: tuple[int, ...] = ()
    dropped_latents: frozenset[tuple[int, str | None]] = frozenset()
    dropped_requests: frozenset[tuple[int | None, str | None]] = frozenset()
    contested: dict[int, str] = field(default_factory=dict)
    """Each attempt beat's contested object (the entity its attempt text names)."""

    def report(self) -> dict[str, Any]:
        return {"pairs": [p.report() for p in self.pairs]}

    @property
    def contradictions(self) -> list[Pair]:
        return [p for p in self.pairs if p.rule != DEDUPE]


def commitments_of(
    requests: Sequence[CanonRequest],
    latent: Sequence[tuple[TurnBeat, Any]],
    beats: Sequence[TurnBeat],
    *,
    render_of: dict[int, int] | None = None,
) -> list[Commitment]:
    """The turn's canon writes (see the module docstring) from its canon requests and its
    ``latent`` claims; ``render_of`` gives each beat's render index (default 0)."""
    by_beat = {b.beat: b for b in beats}
    renders = render_of or {}
    found: list[Commitment] = []

    def made(
        number: int | None,
        proposition: Proposition | None,
        claim_id: str | None,
        request: CanonRequest | None,
    ) -> None:
        if number is None or proposition is None:
            return
        beat = by_beat.get(number)
        if number == PLAYER_BEAT:
            kind, actor, author = "input", None, True
        elif beat is None:
            return
        else:
            kind, actor, author = beat.kind, beat.character_id, beat.narrator
            if beat.attempt:
                return
        found.append(
            Commitment(
                beat=number,
                kind=kind,
                character_id=actor,
                author=author,
                render=renders.get(number, 0),
                proposition=proposition,
                claim_id=claim_id,
                request=request,
            )
        )

    for r in requests:
        made(r.beat, r.claim, r.claim_id, r)
    for b, c in latent:
        made(b.beat, getattr(c, "proposition", None), getattr(c, "claim_id", None), None)
    return sorted(found, key=Commitment.key)


def _contradicts(a: Commitment, b: Commitment, registry: Registry) -> tuple[str, str] | None:
    if a.proposition.polarity is not Polarity.AFFIRMED:
        return None
    if b.proposition.polarity is not Polarity.AFFIRMED:
        return None
    # ``clash`` compares two characters' effects; each beat counts as its own actor here, so a
    # character's two beats (and the narrator's) are reconciled too.
    return clash(
        Effect(a.beat, f"beat:{a.beat}", a.render, a.proposition, a.claim_id),
        Effect(b.beat, f"beat:{b.beat}", b.render, b.proposition, b.claim_id),
        registry,
    )


def _winner(
    a: Commitment, b: Commitment, mode: str, rank: dict[str | None, int]
) -> tuple[Commitment, Commitment, Rule]:
    """``(winner, loser, rule)`` of a contradictory pair, ``a`` earlier than ``b``."""
    if a.author != b.author:
        return (a, b, AUTHOR) if a.author else (b, a, AUTHOR)
    if a.author:
        return a, b, AUTHOR
    if mode == SIMULTANEOUS and a.character_id != b.character_id:
        ra, rb = rank.get(a.character_id, 1_000), rank.get(b.character_id, 1_000)
        if rb < ra:
            return b, a, REQUEST_ORDER
        return a, b, REQUEST_ORDER
    return a, b, EARLIER_BEAT


def _action(loser: Commitment) -> Action:
    if loser.kind == "latent":
        return "latent_dropped"
    if loser.kind == "do":
        return "attempt"
    return "canon_dropped"


def reconcile_turn(
    commitments: Sequence[Commitment],
    *,
    mode: str = SEQUENTIAL,
    registry: Registry,
    order: Sequence[str] = (),
    saw: Collection[tuple[int, int]] = (),
    settled: Collection[tuple[int, int]] = (),
) -> Reconciliation:
    """The turn's reconciliation (see the module docstring). ``order``: the primaries in
    request order (``simultaneous`` ranks); ``saw``: ``(earlier, later)`` beat pairs where the
    later beat's render saw the earlier beat staged (``sequential`` supersession); ``settled``:
    the ``(winner, loser)`` beat pairs #175's adjudication settled. Pure."""
    rank: dict[str | None, int] = {c: n for n, c in enumerate(order)}
    ordered = sorted(commitments, key=Commitment.key)
    pairs: list[Pair] = []
    lost: set[tuple[int, str | None]] = set()
    lost_beats: set[int] = set()
    attempts: list[int] = []
    latents: set[tuple[int, str | None]] = set()
    requests: set[tuple[int | None, str | None]] = set()
    contested: dict[int, str] = {}
    skip = {frozenset(p) for p in settled}
    for n, a in enumerate(ordered):
        for b in ordered[n + 1 :]:
            if a.beat == b.beat or frozenset((a.beat, b.beat)) in skip:
                continue
            if (a.beat, a.claim_id) in lost or (b.beat, b.claim_id) in lost:
                continue
            key = str(a.proposition.normalized_key())
            if a.identity == b.identity:
                if (
                    a.request is not None
                    and b.request is not None
                    and a.request.identity() == b.request.identity()
                ):
                    pairs.append(Pair(key, a.beat, b.beat, DEDUPE, "kept_once"))
                continue
            found = _contradicts(a, b, registry)
            if found is None:
                continue
            if (
                mode == SEQUENTIAL
                and b.kind == "latent"
                and not a.author
                and (a.beat, b.beat) in saw
            ):
                continue  # a legitimate supersession: the later render saw the earlier value
            winner, loser, rule = _winner(a, b, mode, rank)
            action = _action(loser)
            pairs.append(Pair(key, winner.beat, loser.beat, rule, action))
            lost.add((loser.beat, loser.claim_id))
            if action == "attempt":
                if loser.beat not in lost_beats:
                    attempts.append(loser.beat)
                    contested[loser.beat] = found[0]
                lost_beats.add(loser.beat)
            elif action == "latent_dropped":
                latents.add((loser.beat, loser.claim_id))
            else:
                requests.add((loser.beat, loser.claim_id))
    return Reconciliation(
        pairs=tuple(pairs),
        attempts=tuple(sorted(attempts)),
        dropped_latents=frozenset(latents),
        dropped_requests=frozenset(requests),
        contested=contested,
    )


__all__ = [
    "AUTHOR",
    "DEDUPE",
    "EARLIER_BEAT",
    "REQUEST_ORDER",
    "Commitment",
    "Pair",
    "Reconciliation",
    "commitments_of",
    "reconcile_turn",
]
