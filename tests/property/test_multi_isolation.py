"""MULTI-ISOLATION: in a multi-character render, no character's output derives from another
character's private partition (docs/04 §6; FR-MULTI-1; #174).

In memory, over random sequential turns: casts of two to six primaries over two places (the
scene's authoritative placements), at perception ``none`` or ``co_location``. Each primary owns
secrets: its belief lines (its partition, ``## What you believe``), a nested belief, and in its
render a ``think`` and a ``latent`` beat beside its ``say``/``do`` beats; every text carries a
unique sentinel. The primaries render in order over one ``core.epistemic.stage.TurnStage``, as
the pipeline drives it: before primary A renders, its context is built the way
``core.epistemic.context`` builds it (``compose``; ``## Just now`` from
``TurnStage.perceived_for``, the routing ``working_memory(…, stage=)`` reads), then A's beats are
staged. For every primary A, against every other primary B:

- by row identity: no staged beat A's ``## Just now`` holds is B's ``think`` or anyone's
  ``latent``, and no belief record of B's is among A's lines;
- by text: no sentinel of B's ``think``, ``latent``, beliefs or nested beliefs occurs anywhere
  in A's context;
- a staged beat of B's is an antecedent for A (``antecedents_for``) only when B rendered before
  A, and never B's ``think`` or a ``latent``.

Complement: the narrator's omniscient context (everything observable, nothing private) does
contain every primary's public beats, and still none of their thoughts. 300 examples in CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from epistrel.core.epistemic import perception
from epistrel.core.epistemic.context import CharacterContext, compose
from epistrel.core.epistemic.stage import StagedBeat, TurnStage
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.retrieval.filtered import _seal

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 3_000 if NIGHTLY else 300

PLACES = ("P0", "P1")
PLACE_NAMES = {p: f"the place {p}" for p in PLACES}
PRIVATE = frozenset({"think", "latent"})
NARRATOR = "narrator"


@dataclass(frozen=True, slots=True)
class Turn:
    cast: tuple[str, ...]
    level: perception.Level
    placements: dict[str, str]
    renders: tuple[tuple[str, ...], ...]
    """Each primary's beat kinds, in render order."""


@st.composite
def turns(draw: Any) -> Turn:
    size = draw(st.integers(2, 6))
    cast = tuple(f"C{i}" for i in range(size))
    level = draw(st.sampled_from([perception.NONE, perception.CO_LOCATION]))
    placements = {c: draw(st.sampled_from(PLACES)) for c in cast}
    kinds = st.sampled_from(["say", "say", "do", "think", "latent"])
    renders = tuple(tuple(draw(st.lists(kinds, min_size=1, max_size=4))) for _ in cast)
    return Turn(cast, level, placements, renders)


def token(kind: str, owner: str, n: int) -> str:
    return f"S{kind.upper()}X{owner}X{n}Z"


def _belief_lines(owner: str) -> list[str]:
    return [f"you believe {token('belief', owner, 0)}"]


def _nested_lines(owner: str) -> list[str]:
    return [f"you think someone believes {token('nested', owner, 0)}"]


def _text(context: CharacterContext) -> str:
    return "\n".join(
        (
            *context.working_memory,
            *context.just_now,
            *context.retrieved,
            *context.canon,
            *context.belief_lines,
            *context.private_memories,
            context.scene,
            context.own_age,
        )
    )


def _ulid(prefix: str, n: int) -> str:
    return f"01{prefix}{n:0>23}"[:26]


def _claim(n: int) -> Proposition:
    return Proposition(
        subject_id=_ulid("E", n),
        predicate_id=_ulid("P", 0),
        object_id=_ulid("Q", 0),
        valid_time=Interval.of(None, None),
    )


def _play(turn: Turn) -> None:
    scene = perception.SceneState(
        location=turn.placements[turn.cast[0]],
        placements=turn.placements,
        characters=frozenset(turn.cast),
        places=frozenset(PLACES),
    )
    names = {c: c for c in turn.cast}
    stage = TurnStage("op", 1, 0)
    owner_of: dict[int, str] = {}
    private_tokens: dict[str, set[str]] = {c: set() for c in turn.cast}
    public_tokens: dict[str, set[str]] = {c: set() for c in turn.cast}
    for c in turn.cast:
        private_tokens[c] |= {token("belief", c, 0), token("nested", c, 0)}
    number = 2
    for index, character in enumerate(turn.cast):
        stage.open_render(character, number)
        viewpoint = f"character:{character}"
        window = stage.perceived_for(
            character, level=turn.level, scene=scene, names=names, places=PLACE_NAMES
        )
        context = compose(
            character,
            working=_seal([], viewpoint),
            names=names,
            just_now=_seal(window, viewpoint),
            beliefs=_belief_lines(character),
            nested_lines=_nested_lines(character),
        )
        text = _text(context)
        for other in turn.cast:
            if other == character:
                continue
            # By row identity: no other primary's private beat, no latent at all.
            assert not [
                b
                for b in window
                if (b.kind == "think" and b.character_id != character) or b.kind == "latent"
            ], (character, window)
            # By text: none of the other's secrets.
            leaked = sorted(t for t in private_tokens[other] if t in text)
            assert not leaked, (character, other, leaked)
        for beat in stage.antecedents_for(character):
            assert owner_of[beat.beat] != character
            assert turn.cast.index(owner_of[beat.beat]) < index
            assert beat.kind not in PRIVATE
        assert all(b.staged for b in window)
        for n, kind in enumerate(turn.renders[index]):
            words = token(kind, character, n)
            (private_tokens if kind in PRIVATE else public_tokens)[character].add(words)
            stage.stage(
                StagedBeat(beat=number, kind=kind, character_id=character, text=f"{kind} {words}"),
                () if kind in PRIVATE else (_claim(number),),
            )
            owner_of[number] = character
            number += 1
    # The complement: the narrator's omniscient-minus-think context has every public beat.
    seen = stage.perceived_for(
        NARRATOR, level=perception.NONE, scene=None, names=names, places=PLACE_NAMES
    )
    narrated = "\n".join(b.text for b in seen)
    for character in turn.cast:
        assert all(t in narrated for t in public_tokens[character]), character
        thoughts = {t for t in private_tokens[character] if t.startswith(("STHINK", "SLATENT"))}
        assert not {t for t in thoughts if t in narrated}, character


@pytest.mark.part("M5", "FR-MULTI-1")
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(turn=turns())
def test_multi_isolation(turn: Turn) -> None:
    """See the module docstring."""
    _play(turn)


@pytest.mark.part("M5", "FR-MULTI-1")
def test_multi_isolation__stage_replace() -> None:
    """``TurnStage.replace`` swaps a beat for its regenerated version: the acceptances formed
    from the old one go, so a later render never rests on the rejected version; an abandoned
    turn's stage answers nothing (the oracle refuses every staged beat)."""
    stage = TurnStage("op", 3, 0)
    stage.open_render("B", 2)
    stage.stage(StagedBeat(beat=2, kind="say", character_id="B", text="claim"), (_claim(2),))
    stage.open_render("J", 3)
    stage.accept("J", 2, _claim(2), stage.position_of(stage.beats[0]))
    assert [a.beat for a in stage.accepted("J")] == [2]
    ref = {"operation_id": "op", "variant": 0, "beat": 2}
    found = stage.staged_beat(ref)
    assert found is not None
    assert found.position.beat == 2
    stage.replace(2, StagedBeat(beat=2, kind="say", character_id="B", text="no claim"))
    assert stage.accepted("J") == []
    assert stage.staged_beat(ref) is None  # the regenerated beat verified nothing
    assert stage.antecedents_for("J") == []
    stage.stage(StagedBeat(beat=2, kind="say", character_id="B", text="claim"), (_claim(2),))
    assert stage.staged_beat({**ref, "operation_id": "other"}) is None
    assert stage.staged_beat({**ref, "variant": 1}) is None
    stage.discard()
    assert stage.staged_beat(ref) is None
    assert stage.beats == []
