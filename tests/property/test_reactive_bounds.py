"""REACTIVE-BOUNDS: narrated reactions stay inside their bounds (docs/04 §6; FR-MULTI-7,9,10;
#176).

In memory, over random turns as the pipeline drives them (``orchestrator.structured.roles``):
casts of two to six non-player characters beside the player, random stored roles (default or
fixed ``primary``/``reactive``/``silent``), random requests (who is named, per-request ``roles``
overrides, interjections on or off) and random stub reactions narrated by the named primaries
(non-verbal, an interjection of random length, speech, a thought, a decision, an effect;
declared or in ``do`` prose; for a present, absent, primary or player subject), with a budget
that affords a random number of escalations. The oracle is the turn's roles
(``TurnRoles.roles``, the report's ``turn_report.roles``) and the beats ``apply`` lays down:

- a narrated reaction never creates a ``think`` or ``latent`` beat (nor, being a reactor's
  ``do``/``say`` with ``narrated_by``, any belief: the observation sink skips it, asserted end to
  end by S-REACTIVE's row counts);
- every narrated beat is a reactive character's that is present and not a primary, never the
  player's, never a ``silent`` one's, never a thought;
- no reactive character has speech beyond the story's interjection setting: a narrated ``say``
  exists only with interjections on and within the word cap;
- no character with a fixed role is rendered in another role unless the request names it or
  overrides its role: a fixed character is never ``escalated``, a fixed role is reported as
  stored unless the request set it, and at most ``MAX_ESCALATIONS_PER_TURN`` escalate.

Complement: with every role a default and the budget open, a reaction needing speech or a
decision escalates. The perception and could-have-known checks of every narrated reaction run in
the database (``roles.verify``) and are S-REACTIVE's and S-EXTRACT-ADVERSARIAL (f)'s. 300 examples
in CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from epistrel.core.epistemic import roles as rules
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.settings_schema import StorySettings
from epistrel.core.turns import CastEntry
from epistrel.core.turns.beats import TurnBeat
from epistrel.orchestrator.structured import roles as roles_rt

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 3_000 if NIGHTLY else 300

NAMES = ("Ada", "Bobby", "Dale", "Jenni", "Marcus", "Sam")
PLAYER = "Cal"
TEXTS = {
    "nonverbal": "{n} glances at the door.",
    "speech": '"Not tonight," {n} says.',
    "inner": "{n} thinks it is a lie.",
    "decision": "{n} decides to leave.",
    "effect": "{n} pockets the coin.",
}


@dataclass(frozen=True, slots=True)
class Request:
    roles: dict[str, str] | None
    reactive_interjections: bool | None


@dataclass(frozen=True, slots=True)
class Turn:
    cast: tuple[CastEntry, ...]
    present: tuple[str, ...]
    named: tuple[str, ...]
    overrides: dict[str, str]
    interjections: bool
    reactions: tuple[tuple[str, str, str, int, bool], ...]
    """(narrator, subject name, category, interjection words, in prose)."""
    affordable: int


@st.composite
def turns(draw: Any) -> Turn:
    count = draw(st.integers(2, 6))
    names = NAMES[:count]
    cast = [CastEntry(id=PLAYER, name=PLAYER, definition="", is_player=True)]
    for name in names:
        cast.append(
            CastEntry(
                id=name,
                name=name,
                definition="",
                is_player=False,
                participation_role=draw(st.sampled_from(rules.ROLES)),
                participation_fixed=draw(st.booleans()),
            )
        )
    present = tuple(n for n in names if draw(st.integers(0, 4)))
    named = tuple(draw(st.lists(st.sampled_from(names), min_size=1, max_size=2, unique=True)))
    overrides: dict[str, str] = {}
    for name in names:
        if name not in named and draw(st.integers(0, 4)) == 0:
            overrides[name] = draw(st.sampled_from((rules.REACTIVE, rules.SILENT)))
    reactions = tuple(
        (
            draw(st.sampled_from(named)),
            draw(st.sampled_from((*names, PLAYER))),
            draw(st.sampled_from(tuple(TEXTS))),
            draw(st.integers(1, 12)),
            draw(st.booleans()),
        )
        for _ in range(draw(st.integers(0, 6)))
    )
    return Turn(
        cast=tuple(cast),
        present=present,
        named=named,
        overrides=overrides,
        interjections=draw(st.booleans()),
        reactions=reactions,
        affordable=draw(st.integers(0, 3)),
    )


@dataclass(frozen=True, slots=True)
class Decl:
    character_id: str
    text: str
    kind: str
    reacts_to: int | None = None


def _play(turn: Turn) -> tuple[roles_rt.TurnRoles, list[TurnBeat]]:
    request = Request(turn.overrides or None, turn.interjections)
    found = roles_rt.resolve(
        turn.cast,
        present_names=turn.present or tuple(c.name for c in turn.cast if not c.is_player),
        rendering=list(turn.named),
        actor_id=PLAYER,
        request=request,
        story=StorySettings(),
        settings=ProfileSettings(),
    )
    budget = [turn.affordable]

    def afford() -> bool:
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        return True

    laid: list[TurnBeat] = []
    start = 2
    for primary in turn.named:
        beats = [TurnBeat(kind="say", beat=start, text="Evening.", character_id=primary)]
        declared: list[Decl] = []
        prose: list[str] = []
        for narrator, subject, category, length, in_prose in turn.reactions:
            if narrator != primary:
                continue
            if category == "speech" and not in_prose:
                text = '"' + " ".join(["word"] * length) + f'," {subject} says.'
                declared.append(Decl(subject, text, rules.INTERJECTION))
                continue
            text = TEXTS[category].format(n=subject)
            if in_prose:
                prose.append(text)
            else:
                declared.append(Decl(subject, text, rules.NONVERBAL))
        if prose:
            beats.append(
                TurnBeat(
                    kind="do",
                    beat=start + 1,
                    text=" ".join([f"{primary} nods.", *prose]),
                    character_id=primary,
                )
            )
        reactions = roles_rt.collect(beats, declared, primary=primary, start=start, roles=found)
        made, escalate = roles_rt.apply(
            beats, {}, reactions, found, afford=afford, escalating=primary not in found.escalated
        )
        for cid in escalate:
            found.escalate(cid)
        laid += made
        start = max(b.beat for b in made) + 1
    return found, laid


def _stored(turn: Turn) -> dict[str, CastEntry]:
    return {c.id: c for c in turn.cast if not c.is_player}


@pytest.mark.part("M5", "FR-MULTI-7,9,10")
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(turn=turns())
def test_reactive_bounds(turn: Turn) -> None:
    """See the module docstring."""
    found, beats = _play(turn)
    stored = _stored(turn)
    narrated = [b for b in beats if b.narrated_by is not None]
    for b in narrated:
        reactor = str(b.character_id)
        assert b.kind in ("do", "say")  # never a think or a latent
        assert reactor != PLAYER
        assert reactor in turn.present or not turn.present
        role = found.roles[reactor]
        assert role.role != rules.SILENT
        assert reactor not in turn.named
        assert rules.classify(b.text) != "inner"
        if b.kind == "say":
            assert found.interjections
            assert rules.words(b.text) <= found.max_words
    assert len(found.escalated) <= rules.MAX_ESCALATIONS_PER_TURN
    for cid, role in found.roles.items():
        entry = stored[cid]
        named = cid in turn.named
        overridden = cid in turn.overrides
        if entry.participation_fixed and not named and not overridden:
            # A fixed role is a bound: never escalated, reported as stored (a stored primary
            # the request did not name narrates as reactive, FR-MULTI-7's naming rule).
            assert cid not in found.escalated
            expected = (
                rules.REACTIVE
                if entry.participation_role == rules.PRIMARY
                else (entry.participation_role)
            )
            assert (role.role, role.source) == (expected, rules.FIXED)
        if named:
            assert role.role == rules.PRIMARY


@pytest.mark.part("M5", "FR-MULTI-7,9,10")
def test_reactive_bounds__escalates() -> None:
    """The complement: default roles and an open budget, speech and a decision escalate."""
    cast = (
        CastEntry(id=PLAYER, name=PLAYER, definition="", is_player=True),
        CastEntry(id="Jenni", name="Jenni", definition="", is_player=False),
        CastEntry(id="Sam", name="Sam", definition="", is_player=False),
        CastEntry(id="Ada", name="Ada", definition="", is_player=False),
    )
    turn = Turn(
        cast=cast,
        present=("Jenni", "Sam", "Ada"),
        named=("Jenni",),
        overrides={},
        interjections=False,
        reactions=(("Jenni", "Sam", "decision", 1, True), ("Jenni", "Ada", "speech", 3, False)),
        affordable=5,
    )
    found, _ = _play(turn)
    assert found.escalated == ["Ada", "Sam"]  # declared reactions first, then prose
    assert found.roles["Sam"].source == rules.ESCALATED
    assert not found.escalation_deferred
