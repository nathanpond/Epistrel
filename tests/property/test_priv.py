"""PRIV: private-thought non-leakage, the context half (docs/04 §6; NFR-PRIV-1,4, FR-MEM-11;
#170).

In memory, over random stories: casts of two to eight characters (a player and minors included),
up to 30 turns over three places, each turn with its own authoritative placements and up to
four beats (``say``, ``do``, ``think`` with density 0.3, narration at a declared place; a
``say`` may whisper to someone), at perception ``co_location`` or ``full``. Each beat's text
carries a unique sentinel token. The stories are routed and remembered as the engine does
(``perception.route_turn``; one memory per perceiver, the holder's alone, as
``records.write_perceived`` writes them; a ``think`` its thinker's only), and objective
(author) memories are added. Each character's context is then built by ``context.compose`` from:

- its working memory, re-routed from history by ``records.perceived_window`` (what
  ``records.working_memory`` reads at ``co_location`` and ``full``);
- the memories #159's viewpoint filter admits for it (``viewpoint.admits`` under
  ``scope_for(character:<id>, partitions=True)``), the only set the assembler takes
  (``ViewpointFiltered``).

An independent statement of FR-TURN-2,9 (the actor, or co-location at the beat's place, with a
whisper's words for its targets only at ``full``; a thought for its thinker only) gives what
each character may know, and for every character:

- no ``think`` sentinel of another character appears anywhere in its context, minors' included;
- no sentinel of a beat it did not perceive appears in its context;
- every beat it did perceive is in its working memory, which carries exactly those sentinels;
- no consumer-supplied recent message (a planted sentinel) is in its context: nothing that
  builds one can receive it.

Complement: the same stories read ``omniscient`` (no filter) do contain the private rows. 500
examples in CI. ``EPISTREL_HYPOTHESIS_PROFILE=nightly`` (the nightly red-team workflow) runs
5,000. The output half (player-facing disclosure) is #172's (R-03, S-MINOR-INTERIORITY) and
#177's. ``test_priv__minors`` (#172, FR-EPI-11) runs the generator with every non-player
character a minor, each context also carrying its own private memories.
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
from epistrel.core.memory import records
from epistrel.core.retrieval.filtered import _seal
from epistrel.core.retrieval.viewpoint import Viewpoint, admits, scope_for

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 5_000 if NIGHTLY else 500

PLACES = ("P0", "P1", "P2")
PLACE_NAMES = {p: f"the place {p}" for p in PLACES}
RECENT_MESSAGE = "RECENTSENTINEL"
OMNISCIENT = "omniscient"


@dataclass(frozen=True, slots=True)
class Planned:
    kind: str
    actor: str | None
    whisper: tuple[str, ...]
    place: str | None


@dataclass(frozen=True, slots=True)
class Story:
    cast: tuple[str, ...]
    player: str
    minors: frozenset[str]
    level: perception.Level
    turns: tuple[tuple[dict[str, str], tuple[Planned, ...]], ...]


@st.composite
def stories(draw: Any, all_minors: bool = False) -> Story:
    size = draw(st.integers(2, 8))
    cast = tuple(f"C{i}" for i in range(size))
    player = cast[0]
    minors = (
        frozenset(cast[1:])
        if all_minors
        else frozenset(draw(st.lists(st.sampled_from(cast[1:]), max_size=2, unique=True)))
    )
    level = draw(st.sampled_from([perception.CO_LOCATION, perception.FULL]))
    turns = []
    for _ in range(draw(st.integers(1, 30))):
        placements = {c: draw(st.sampled_from(PLACES)) for c in cast}
        beats = []
        for _ in range(draw(st.integers(1, 4))):
            roll = draw(st.floats(0, 1))
            kind = (
                "think" if roll < 0.3 else draw(st.sampled_from(["say", "say", "do", "narration"]))
            )
            actor = None if kind == "narration" else draw(st.sampled_from(cast))
            whisper: tuple[str, ...] = ()
            if kind == "say" and draw(st.booleans()):
                whisper = (draw(st.sampled_from(cast)),)
            place = draw(st.sampled_from(PLACES)) if kind == "narration" else None
            beats.append(Planned(kind, actor, whisper, place))
        turns.append((placements, tuple(beats)))
    return Story(cast, player, minors, level, tuple(turns))


def sentinel(turn: int, beat: int) -> str:
    return f"SENTINEL{turn}X{beat}Z"


def _history(
    story: Story,
) -> tuple[
    list[tuple[perception.SceneState, list[records.HistoryBeat]]],
    dict[str, Planned],
    dict[str, int],
]:
    """Each turn's scene and stored beats; each sentinel's plan and turn."""
    turns = []
    plans: dict[str, Planned] = {}
    when: dict[str, int] = {}
    for t, (placements, beats) in enumerate(story.turns, start=1):
        scene = perception.SceneState(
            location=placements[story.player],
            placements=placements,
            characters=frozenset(story.cast),
            places=frozenset(PLACES),
        )
        stored = []
        for b, plan in enumerate(beats, start=1):
            token = sentinel(t, b)
            plans[token] = plan
            when[token] = t
            text = f"{plan.kind} {token}"
            beat = records.WorkingBeat(
                event_id=token,
                narrative_turn=t,
                turn_variant=1,
                beat=b,
                kind=plan.kind,  # type: ignore[arg-type]
                character_id=plan.actor,
                text=text,
            )
            q = perception.Qualifiers(
                whisper_to=plan.whisper,
                addressee=plan.whisper[0] if plan.whisper else None,
                location=plan.place,
            )
            stored.append(records.HistoryBeat(beat, q))
        turns.append((scene, stored))
    return turns, plans, when


def _memories(
    story: Story,
    turns: list[tuple[perception.SceneState, list[records.HistoryBeat]]],
) -> list[tuple[str, dict[str, Any], str | None]]:
    """``(text, visibility, owner of a think)``: one routed memory per perceiver, the holder's
    alone, as the memory sink writes them, and two objective author memories."""
    names = {c: c for c in story.cast}
    found: list[tuple[str, dict[str, Any], str | None]] = [
        ("The house has three rooms.", {"scope": "objective"}, None),
        ("Everyone knows the river floods.", {"scope": "public"}, None),
    ]
    for scene, beats in turns:
        routed = perception.route_turn(
            [
                perception.RouteBeat(
                    kind=h.beat.kind,
                    beat=int(h.beat.beat or 0),
                    actor=h.beat.character_id,
                    qualifiers=h.qualifiers,
                    source=h.beat,
                )
                for h in beats
            ],
            scene,
            story.level,
        )
        for result in routed:
            source = result.beat.source
            assert isinstance(source, records.WorkingBeat)
            beat = records.Beat(
                source.kind,
                source.text,
                source.event_id,
                beat=source.beat,
                character_id=source.character_id,
            )
            for seen in result.perceived:
                line = records.perceived_line(beat, seen, names, PLACE_NAMES)
                visibility = {"scope": "characters", "characters": [seen.character_id]}
                owner = source.character_id if source.kind == "think" else None
                found.append((line, visibility, owner))
    return found


def _may_know(story: Story, plan: Planned, turn: int, character: str) -> bool:
    """FR-TURN-2,9 stated independently: who may know a beat's words."""
    placements = story.turns[turn - 1][0]
    if plan.kind == "think":
        return plan.actor == character
    if plan.actor == character:
        return True
    where = plan.place if plan.kind == "narration" else placements[str(plan.actor)]
    if placements[character] != where:
        return False
    if plan.kind == "say" and plan.whisper and story.level == perception.FULL:
        return character in plan.whisper
    return True


def _text(context: CharacterContext) -> str:
    return "\n".join(
        (
            *context.working_memory,
            *context.retrieved,
            *context.canon,
            *context.belief_lines,
            *context.private_memories,
            context.scene,
            context.own_age,
        )
    )


def _context(
    story: Story,
    character: str,
    viewpoint: str,
    turns: list[tuple[perception.SceneState, list[records.HistoryBeat]]],
    memories: list[tuple[str, dict[str, Any], str | None]],
) -> CharacterContext:
    window = records.perceived_window(
        turns, character, story.level, {c: c for c in story.cast}, PLACE_NAMES
    )
    seen_as = Viewpoint.parse(viewpoint)
    scope = scope_for(seen_as, partitions=True, player=False)
    retrieved = [text for text, visibility, _ in memories if admits(scope, visibility)]
    # #172: its own thoughts, as ``context._private_lines`` reads them (the holder's alone).
    own = [
        text
        for text, visibility, owner in memories
        if owner == character and admits(scope, visibility)
    ]
    return compose(
        character,
        working=_seal(window, f"character:{character}"),
        names={c: c for c in story.cast},
        retrieved=_seal(retrieved, viewpoint),
        private_memories=[f"you thought: {t}" for t in own[-8:]],
    )


@pytest.mark.part("M5", "NFR-PRIV-1,4; FR-MEM-11")
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(story=stories())
def test_priv(story: Story) -> None:
    """See the module docstring."""
    _check(story)


@pytest.mark.part("M5", "NFR-PRIV-1")
@settings(
    max_examples=EXAMPLES // 5,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(story=stories(all_minors=True))
def test_priv__minors(story: Story) -> None:
    """#172 (FR-EPI-11): PRIV's generator with every non-player character a minor. "Minor"
    limits weight and sparsity, never privacy: the same assertions hold, and each minor's
    own thoughts are first-class (its ``## Your private memories`` holds only its own)."""
    _check(story)
    turns, plans, _ = _history(story)
    memories = _memories(story, turns)
    for character in story.minors:
        context = _context(story, character, f"character:{character}", turns, memories)
        held = {t for t, p in plans.items() if p.kind == "think" and p.actor == character}
        shown = {t for t in plans if any(t in line for line in context.private_memories)}
        assert shown <= held, (character, sorted(shown - held))
        assert len(shown) == min(len(held), 8), (character, len(shown), len(held))


def _check(story: Story) -> None:
    turns, plans, when = _history(story)
    memories = _memories(story, turns)
    consumer_recent = [{"name": story.player, "say": RECENT_MESSAGE}]
    private = {token for token, plan in plans.items() if plan.kind == "think"}
    for character in story.cast:
        context = _context(story, character, f"character:{character}", turns, memories)
        text = _text(context)
        allowed = {
            token for token, plan in plans.items() if _may_know(story, plan, when[token], character)
        }
        present = {token for token in plans if token in text}
        # No other character's think (minors' included), no unperceived beat.
        others = {t for t in private if plans[t].actor != character}
        assert not present & others, (character, sorted(present & others))
        assert present <= allowed, (character, sorted(present - allowed))
        # Every perceived beat is in the working memory, which carries exactly those.
        remembered = {t for t in plans if any(t in line for line in context.working_memory)}
        assert remembered == allowed, (character, sorted(remembered ^ allowed))
        # Consumer-supplied recent messages never reach a character's context.
        assert consumer_recent
        assert RECENT_MESSAGE not in text
    # Complement: read omniscient (no filter), the private rows are there.
    thoughts = [m for m in memories if m[2] is not None]
    if thoughts:
        thinker = str(thoughts[0][2])
        reader = next((c for c in story.cast if c != thinker), thinker)
        everything = _context(story, reader, OMNISCIENT, turns, memories)
        assert any(line == thoughts[0][0] for line in everything.retrieved)
