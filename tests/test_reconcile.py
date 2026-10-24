"""``orchestrator.structured.reconcile`` (#177, FR-MULTI-3): a turn's concurrent commitments on
one timeline. Pure: commitments in, pairs and rewrites out (the scenario half is S-DUET)."""

from __future__ import annotations

from epistrel.core.canon.canonize import CanonRequest
from epistrel.core.enums import RecordProvenance
from epistrel.core.propositions import registry
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import HOLDS, LOCATED_AT
from epistrel.core.turns.beats import TurnBeat
from epistrel.orchestrator.structured.reconcile import (
    Commitment,
    commitments_of,
    reconcile_turn,
)

BOBBY, JENNI = "01JE00000000000000000000B0", "01JE00000000000000000000J0"
KEY, STUDY, HALL = (
    "01JE00000000000000000000K0",
    "01JE00000000000000000000S0",
    "01JE00000000000000000000H0",
)


def _p(subject: str, predicate: str, obj: str, *, negated: bool = False) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=Interval.of(None, None),
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
    )


def _c(
    beat: int,
    kind: str,
    who: str | None,
    p: Proposition,
    *,
    author: bool = False,
    render: int = 0,
) -> Commitment:
    return Commitment(beat, kind, who, author, render, p, f"c{beat}")


def test_an_earlier_beat_stands_and_a_later_do_is_an_attempt() -> None:
    """Sequential: a later render's ``do`` can never override an earlier verified beat's
    commitment; the possession/placement pair contradicts."""
    reg = registry.core_registry()
    placed = _c(2, "latent", "jenni", _p(KEY, LOCATED_AT, STUDY), render=0)
    pocketed = _c(4, "do", "bobby", _p(BOBBY, HOLDS, KEY), render=1)
    for given in ([placed, pocketed], [pocketed, placed]):
        found = reconcile_turn(given, registry=reg)
        (pair,) = found.pairs
        assert (pair.winner_beat, pair.loser_beat, pair.rule, pair.action) == (
            2,
            4,
            "earlier_beat",
            "attempt",
        )
        assert found.attempts == (4,)
        assert found.contested == {4: KEY}


def test_a_later_latent_that_saw_the_earlier_beat_supersedes_it() -> None:
    """Sequential supersession: the key on the table, then (a render that saw it) in his
    pocket; not a contradiction. Without the sight, the latent is dropped."""
    reg = registry.core_registry()
    placed = _c(2, "do", "jenni", _p(KEY, LOCATED_AT, STUDY), render=0)
    pocketed = _c(4, "latent", "bobby", _p(BOBBY, HOLDS, KEY), render=1)
    assert reconcile_turn([placed, pocketed], registry=reg, saw={(2, 4)}).pairs == ()
    found = reconcile_turn([placed, pocketed], registry=reg)
    (pair,) = found.pairs
    assert (pair.loser_beat, pair.action) == (4, "latent_dropped")
    assert found.dropped_latents == frozenset({(4, "c4")})


def test_the_author_stands_over_a_character() -> None:
    """A narrator beat (or the player's input) is the author: it stands even when later."""
    reg = registry.core_registry()
    pocketed = _c(2, "do", "bobby", _p(BOBBY, HOLDS, KEY))
    narrated = _c(5, "narration", None, _p(KEY, LOCATED_AT, HALL), author=True)
    (pair,) = reconcile_turn([pocketed, narrated], registry=reg).pairs
    assert (pair.winner_beat, pair.loser_beat, pair.rule) == (5, 2, "author")


def test_simultaneous_request_order_and_settled_pairs() -> None:
    """Simultaneous: the earlier-named primary stands (``request_order``), whatever the beat
    numbers; a pair #175's adjudication settled is skipped."""
    reg = registry.core_registry()
    first = _c(2, "do", "jenni", _p(JENNI, HOLDS, KEY))
    second = _c(3, "do", "bobby", _p(BOBBY, HOLDS, KEY))
    found = reconcile_turn(
        [first, second], registry=reg, mode="simultaneous", order=["bobby", "jenni"]
    )
    (pair,) = found.pairs
    assert (pair.winner_beat, pair.loser_beat, pair.rule) == (3, 2, "request_order")
    assert (
        reconcile_turn([first, second], registry=reg, mode="simultaneous", settled=[(2, 3)]).pairs
        == ()
    )


def test_uncertain_and_negated_pairs_never_contradict() -> None:
    """ADR-160: two places no containment relates are ``uncertain``, never a contradiction;
    a negation commits no value."""
    reg = registry.core_registry()
    study = _c(2, "latent", "jenni", _p(KEY, LOCATED_AT, STUDY))
    hall = _c(3, "latent", "bobby", _p(KEY, LOCATED_AT, HALL))
    gone = _c(4, "do", "bobby", _p(BOBBY, HOLDS, KEY, negated=True))
    assert reconcile_turn([study, hall, gone], registry=reg).pairs == ()


def test_identical_canon_writes_are_one_fact() -> None:
    """Two beats' identical canon writes are kept once (``dedupe``), a ``latent`` of an
    attempt beat is no commitment, and the player's input is the author."""
    p = _p(KEY, LOCATED_AT, STUDY)
    beats = [
        TurnBeat(kind="do", beat=2, text="a", character_id="jenni"),
        TurnBeat(kind="do", beat=3, text="b", character_id="bobby"),
        TurnBeat(kind="do", beat=4, text="c", character_id="bobby", attempt=True),
    ]
    requests = [
        CanonRequest(claim=p, provenance=RecordProvenance.RENDER, beat=n, claim_id=f"c{n}")
        for n in (1, 2, 3, 4)
    ]
    found = commitments_of(requests, [], beats)
    assert [(c.beat, c.author) for c in found] == [(1, True), (2, False), (3, False)]
    pairs = reconcile_turn(found, registry=registry.core_registry()).pairs
    assert {(x.winner_beat, x.loser_beat, x.rule) for x in pairs} == {
        (1, 2, "dedupe"),
        (1, 3, "dedupe"),
        (2, 3, "dedupe"),
    }
