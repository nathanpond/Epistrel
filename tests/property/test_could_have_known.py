"""COULD-HAVE-KNOWN: acquisition replay (docs/04 §6; FR-EPI-4; #165).

Over random acquisition histories (300 examples, in memory): beliefs (lineages) about the target
proposition or another one, of either polarity, each a sequence of dated transitions of random
kinds and levels at random narrative positions, some closed at a later position, and a random
reference position T. ``replay`` answers ``Known`` exactly when an independent fold of the same
history up to T leaves the holder holding the target, with its polarity, at the caller's level or
above: a reference at T is accepted iff the replay yields the belief. A ``Known`` path is a record
of the history at or before T; a ``NoPath``'s nearest path lies after T.

Complement: a path created **after** T never licenses a reference at T. Adding any number of
acquisitions dated after T leaves the answer at T unchanged, and a history whose only licensing
record is after T is ``NoPath`` with that record as ``nearest``. Observation auto-satisfies
(FR-BELIEF-2): one observation at or before T is ``Known``, its path the observation. A belief in
¬P licenses no assertion of P.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from epistrel.core.enums import BeliefConfidenceLevel, BeliefTransitionKind
from epistrel.core.epistemic.replay import Acquisition, Known, NoPath, PathRef, replay
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Polarity

Level = BeliefConfidenceLevel
LEVELS = list(Level)
TARGET = ("bobby", "knocked_at", ("entity", "porch"), ())
OTHER = ("bobby", "knocked_at", ("entity", "school"), ())

positions = st.builds(
    Position,
    turn=st.integers(1, 6),
    variant=st.just(0),
    slot_k=st.just(0),
    beat=st.integers(0, 3),
    step=st.integers(0, 1),
)


@dataclass(frozen=True)
class Lineage:
    """One belief: its key, polarity, transitions ``(position, kind, level)`` and closure."""

    key: tuple[object, ...]
    polarity: Polarity
    steps: tuple[tuple[Position, BeliefTransitionKind, Level], ...]
    until: Position | None


lineages = st.builds(
    Lineage,
    key=st.sampled_from([TARGET, OTHER]),
    polarity=st.sampled_from(list(Polarity)),
    steps=st.lists(
        st.tuples(positions, st.sampled_from(list(BeliefTransitionKind)), st.sampled_from(LEVELS)),
        min_size=1,
        max_size=4,
    ).map(tuple),
    until=st.one_of(st.none(), positions),
)


def acquisitions(history: list[Lineage]) -> list[Acquisition]:
    found = []
    for n, lineage in enumerate(history):
        for m, (at, kind, level) in enumerate(lineage.steps):
            found.append(
                Acquisition(
                    key=lineage.key,
                    polarity=lineage.polarity,
                    level=level,
                    since=at,
                    until=lineage.until,
                    lineage=f"belief-{n}",
                    ref=PathRef("transition", f"t-{n}-{m:02d}", at, kind.value),
                )
            )
    return found


def rank(level: Level) -> int:
    return LEVELS.index(level)


def oracle(history: list[Lineage], at: Position, minimum: Level) -> bool:
    """The independent fold: replay every lineage's transitions in narrative order up to ``at``,
    and ask whether one holds the target, affirmed, at ``minimum`` or above, unclosed."""
    for lineage in history:
        if lineage.key != TARGET or lineage.polarity is not Polarity.AFFIRMED:
            continue
        if lineage.until is not None and lineage.until.key() <= at.key():
            continue
        state: Level | None = None
        for position, _kind, level in sorted(lineage.steps, key=lambda s: (s[0].key(), s[2].value)):
            if position.key() <= at.key():
                state = level
        if state is not None and rank(state) >= rank(minimum):
            return True
    return False


def _sorted_ties(history: list[Lineage]) -> list[Lineage]:
    """The history without two transitions of one lineage at the same position (the stored
    order breaks such ties by transaction time, which the fold does not model)."""
    kept = []
    for lineage in history:
        seen: dict[tuple[int, ...], tuple[Position, BeliefTransitionKind, Level]] = {}
        for step in lineage.steps:
            seen.setdefault(step[0].key(), step)
        kept.append(Lineage(lineage.key, lineage.polarity, tuple(seen.values()), lineage.until))
    return kept


@pytest.mark.part("M5", "FR-EPI-4")
@settings(max_examples=300, deadline=None)
@given(
    history=st.lists(lineages, max_size=5),
    at=positions,
    minimum=st.sampled_from([Level.SUSPECTED, Level.BELIEVED]),
    later=st.lists(st.tuples(st.integers(1, 3), st.sampled_from(LEVELS)), min_size=1, max_size=3),
)
def test_could_have_known(
    history: list[Lineage], at: Position, minimum: Level, later: list[tuple[int, Level]]
) -> None:
    history = _sorted_ties(history)
    records = acquisitions(history)
    answer = replay(records, TARGET, Polarity.AFFIRMED, at, min_level=minimum)
    assert isinstance(answer, Known) == oracle(history, at, minimum)
    refs = {a.ref for a in records}
    if isinstance(answer, Known):
        assert answer.path in refs
        assert answer.path.position is not None
        assert answer.path.position.at_or_before(at)
    else:
        assert isinstance(answer, NoPath)
        if answer.nearest is not None:
            assert answer.nearest.position is not None
            assert at.before(answer.nearest.position)

    # Complement: paths created after T never license a reference at T.
    after = [
        Acquisition(
            key=TARGET,
            polarity=Polarity.AFFIRMED,
            level=level,
            since=Position(at.turn + gap),
            lineage=f"later-{n}",
            ref=PathRef("transition", f"later-{n}", Position(at.turn + gap), "testimony"),
        )
        for n, (gap, level) in enumerate(later)
    ]
    again = replay([*records, *after], TARGET, Polarity.AFFIRMED, at, min_level=minimum)
    assert isinstance(again, Known) == isinstance(answer, Known)
    if isinstance(answer, Known):
        assert again == answer
    alone = replay(after, TARGET, Polarity.AFFIRMED, at, min_level=minimum)
    assert isinstance(alone, NoPath)
    licensing = [a for a in after if rank(a.level) >= rank(minimum)]
    if licensing:
        assert alone.nearest is not None
        assert alone.nearest.position is not None
        assert at.before(alone.nearest.position)
    else:
        assert alone.nearest is None


@pytest.mark.part("M5", "FR-EPI-4")
def test_could_have_known__observation_and_negation() -> None:
    """Observation auto-satisfies (FR-BELIEF-2), and a belief in ¬P licenses no assertion of
    P (only asking about it)."""
    seen = Position(2, beat=1, step=1)
    observed = Acquisition(
        key=TARGET,
        polarity=Polarity.AFFIRMED,
        level=Level.KNOWN,
        since=seen,
        lineage="b",
        ref=PathRef("transition", "t-1", seen, BeliefTransitionKind.OBSERVATION.value),
    )
    assert replay([observed], TARGET, Polarity.AFFIRMED, Position(3)) == Known(observed.ref)
    assert isinstance(replay([observed], TARGET, Polarity.AFFIRMED, Position(2)), NoPath)
    denied = Acquisition(
        key=TARGET,
        polarity=Polarity.NEGATED,
        level=Level.KNOWN,
        since=seen,
        lineage="n",
        ref=PathRef("transition", "t-2", seen, BeliefTransitionKind.TESTIMONY.value),
    )
    assert isinstance(replay([denied], TARGET, Polarity.AFFIRMED, Position(3)), NoPath)
    assert replay([denied], TARGET, Polarity.NEGATED, Position(3)) == Known(denied.ref)
