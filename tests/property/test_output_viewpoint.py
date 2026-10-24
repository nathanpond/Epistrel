"""OUTPUT-VIEWPOINT: what a viewpoint's output holds of a multi-character turn (docs/04 §6;
FR-RESP-8,12,13, FR-MULTI-4, FR-AUTH-2; #177).

In memory, over random multi-character turns: casts of two to five characters over two places
(an authoritative placement each, or none), at perception ``co_location`` or ``full``, in
``sequential`` or ``simultaneous`` mode; random beats (``say``, whispered or aside or not,
``do``, ``think``, ``latent`` and narration), some of which move their actor; a random viewpoint
V (a character or ``omniscient``) under a random ``player_view``. Every beat's text is made of
words unique to it (its sentinels). The output is ``core.epistemic.audience
.filter_for_viewpoint`` and the narrative its deterministic composition
(``core.turns.assembly``), checked by #152's ``trace`` with the withheld beats.

The oracle is independent of the filter: it walks the beats itself, calling
``perception.route_perception`` beat by beat over the placements the earlier beats' moves left
(the starting ones in ``simultaneous`` mode), and re-detects the narrative's content by its
sentinels (layers a and b of detection are deterministic, and a sentinel is the one claim each
beat makes). For a character viewpoint under ``player_view = character``:

- every returned beat is perceivable by V at the returned fidelity: ``content`` only where V
  perceived the content (its text the beat's own), ``envelope`` only where V perceived only the
  envelope (its text the envelope, none of the beat's sentinels), and nothing where V perceived
  nothing;
- no ``think`` or ``latent`` is returned, V's own included;
- the narrative and the beat list agree: the narrative holds exactly the returned beats'
  sentinels, in order, and passes the composition check.

Under ``player_view = story`` every non-private beat is returned in full, with the fidelity V
would have had, and still no ``think`` or ``latent``. ``omniscient`` (only when named) returns
every beat. Complement: a narrative that adds a withheld beat's words fails the check with
``fidelity_leak``, and ``omniscient`` does return the omitted beats and the ``think``.
300 examples in CI.

``test_output_viewpoint__respond`` is the one Postgres check: a whispered turn played through
``respond`` for Bobby's viewpoint, replayed, refused for another viewpoint under the same key,
and read back for each viewpoint.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import audience, perception
from epistrel.core.propositions.registry import core_registry
from epistrel.core.turns.assembly import AssemblyBeat, deterministic_assembly
from epistrel.core.viewpoint import PRIVATE_CHANNELS
from epistrel.orchestrator.compose import compose as composing
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, history, render, structured_story

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 3_000 if NIGHTLY else 300

PLACES = ("P0", "P1")
KINDS = ("say", "say", "do", "think", "latent", "narration")


@dataclass(frozen=True, slots=True)
class Beat:
    """A beat as ``trace`` reads it (``compose.VerifiedBeat``)."""

    kind: str
    text: str
    character_id: str | None
    beat: int
    claims: tuple[Any, ...] = ()
    fidelity: str = "content"


@dataclass(frozen=True, slots=True)
class Turn:
    cast: tuple[str, ...]
    placements: dict[str, str]
    level: perception.Level
    mode: str
    beats: tuple[audience.AudienceBeat, ...]
    viewpoint: str
    player_view: audience.PlayerView
    names: dict[str, str] = field(default_factory=dict)


def _text(number: int) -> str:
    return " ".join(f"s{number}w{k}" for k in range(5))


@st.composite
def turns(draw: st.DrawFn) -> Turn:
    size = draw(st.integers(2, 5))
    cast = tuple(f"c{n}" for n in range(size))
    placements = {c: p for c in cast if (p := draw(st.sampled_from((*PLACES, None)))) is not None}
    count = draw(st.integers(1, 8))
    beats: list[audience.AudienceBeat] = []
    for n in range(2, 2 + count):
        kind = draw(st.sampled_from(KINDS))
        actor = None if kind == "narration" else draw(st.sampled_from(cast))
        qualifiers = perception.Qualifiers()
        if kind == "say":
            style = draw(st.sampled_from(("room", "whisper", "aside")))
            if style == "whisper":
                targets = draw(st.lists(st.sampled_from(cast), min_size=1, max_size=2))
                qualifiers = perception.Qualifiers(whisper_to=tuple(dict.fromkeys(targets)))
            elif style == "aside":
                qualifiers = perception.Qualifiers(aside=True)
        moves: tuple[tuple[str, str | None], ...] = ()
        if actor is not None and draw(st.integers(0, 4)) == 0:
            moves = ((actor, draw(st.sampled_from((*PLACES, None)))),)
        beats.append(audience.AudienceBeat(kind, n, actor, _text(n), qualifiers, moves, source=n))
    viewpoint = draw(st.sampled_from(("omniscient", *(f"character:{c}" for c in cast))))
    return Turn(
        cast=cast,
        placements=placements,
        level=draw(st.sampled_from((perception.CO_LOCATION, perception.FULL))),
        mode=draw(st.sampled_from((perception.SEQUENTIAL, perception.SIMULTANEOUS))),
        beats=tuple(beats),
        viewpoint=viewpoint,
        player_view=draw(st.sampled_from(audience.PLAYER_VIEWS)),
        names={c: f"Name{c[1:]}" for c in cast},
    )


def _scene(turn: Turn) -> perception.SceneState:
    return perception.SceneState(
        location=None,
        placements=dict(turn.placements),
        adjacency={perception.pair(*PLACES): "door_closed"},
        characters=frozenset(turn.cast),
        places=frozenset(PLACES),
    )


def _oracle(turn: Turn, viewer: str) -> dict[int, tuple[str, str]]:
    """Each beat's fidelity for ``viewer`` and the text an output may carry, recomputed beat by
    beat through ``route_perception`` (see the module docstring)."""
    current = _scene(turn)
    found: dict[int, tuple[str, str]] = {}
    for b in sorted(turn.beats, key=lambda b: b.beat):
        routed = perception.RouteBeat(
            kind=b.kind,  # type: ignore[arg-type]
            beat=b.beat,
            actor=b.character_id,
            qualifiers=b.qualifiers,
            sound=perception.sound_of(b.text) if b.kind == "do" else None,
        )
        qualifiers, _ = perception.validate_qualifiers(routed, current, turn.level)
        seen = next(
            (
                p
                for p in perception.route_perception(routed, current, qualifiers, turn.level)
                if p.character_id == viewer
            ),
            None,
        )
        if seen is None:
            found[b.beat] = ("none", "")
        elif seen.fidelity == perception.ENVELOPE and seen.envelope is not None:
            found[b.beat] = ("envelope", perception.envelope_text(seen.envelope, turn.names))
        else:
            found[b.beat] = ("content", b.text)
        if b.moves and turn.mode != perception.SIMULTANEOUS:
            current = current.moved(b.moves)
    return found


def _sentinels(text: str) -> set[str]:
    return set(re.findall(r"\bs\d+w\d+\b", text))


def _shown(filtered: audience.Filtered) -> list[Beat]:
    return [
        Beat(
            "do" if s.envelope else s.beat.kind,
            s.text,
            s.beat.character_id,
            s.beat.beat,
        )
        for s in filtered.seen
    ]


def _narrative(turn: Turn, filtered: audience.Filtered) -> str:
    return deterministic_assembly(
        [
            AssemblyBeat(
                s.beat.kind,
                s.text,
                s.beat.character_id,
                s.beat.beat,
                s.fidelity if s.envelope else "content",
            )
            for s in filtered.seen
        ],
        turn.names,
        turn.viewpoint,
    )


@pytest.mark.part("M5", "FR-RESP-8,12,13; FR-MULTI-4; FR-AUTH-2")
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(turns())
def test_output_viewpoint(turn: Turn) -> None:
    """See the module docstring."""
    filtered = audience.filter_for_viewpoint(
        turn.beats,
        turn.viewpoint,
        scene=_scene(turn),
        level=turn.level,
        names=turn.names,
        player_view=turn.player_view,
        mode=turn.mode,
    )
    narrative = _narrative(turn, filtered)
    returned = {s.beat.beat: s for s in filtered.seen}
    private = {b.beat for b in turn.beats if b.kind in PRIVATE_CHANNELS}
    if turn.viewpoint == "omniscient":
        # Only when named: everything, ``think`` and ``latent`` included, in full.
        assert filtered.audience_filter == "none"
        assert set(returned) == {b.beat for b in turn.beats}
        assert all(s.text == s.beat.text for s in filtered.seen)
        return
    viewer = turn.viewpoint.removeprefix("character:")
    expected = _oracle(turn, viewer)
    assert not set(returned) & private  # no think or latent, under every player view
    if turn.player_view == audience.STORY_VIEW:
        assert filtered.audience_filter == "bypassed_by_player_view"
        assert set(returned) == {b.beat for b in turn.beats} - private
        for number, s in returned.items():
            assert s.text == s.beat.text
            assert s.fidelity == expected[number][0]
        return
    for b in turn.beats:
        if b.beat in private:
            continue
        fidelity, text = expected[b.beat]
        if fidelity == "none":
            assert b.beat not in returned
            assert b in filtered.withheld
            continue
        s = returned[b.beat]
        assert (s.fidelity, s.text) == (fidelity, text)
        if fidelity == "envelope":
            assert not _sentinels(s.text)
            assert b in filtered.withheld
    # The narrative and the beat list agree, and the narrative holds only what V perceived.
    content = [s for s in filtered.seen if s.fidelity == "content"]
    assert _sentinels(narrative) == {w for s in content for w in _sentinels(s.text)}
    for s in filtered.seen:
        if s.fidelity == "envelope":
            assert s.text in narrative
    perceived = {b for b, (f, _) in expected.items() if f != "none" and b not in private}
    if not perceived:
        assert filtered.audience_filter == "nothing_perceived"
        assert narrative == ""
    else:
        assert filtered.audience_filter == turn.level
    # The composition check passes the narrative composed from exactly the returned beats ...
    shown = _shown(filtered)
    withheld = [Beat(b.kind, b.text, b.character_id, b.beat) for b in filtered.withheld]
    hidden = [Beat(b.kind, b.text, b.character_id, b.beat) for b in turn.beats if b.beat in private]
    found = composing.trace(
        narrative,
        shown,
        [],
        viewpoint=turn.viewpoint,
        names=turn.names,
        hidden=hidden,
        withheld=withheld,
        registry=core_registry(),
    )
    assert found.passed, found.failures
    # ... and fails one that adds a withheld beat's words (complement).
    if withheld:
        leaky = f"{narrative}\n\n{withheld[0].text}." if narrative else f"{withheld[0].text}."
        failed = composing.trace(
            leaky,
            shown,
            [],
            viewpoint=turn.viewpoint,
            names=turn.names,
            hidden=hidden,
            withheld=withheld,
            registry=core_registry(),
        )
        assert "fidelity_leak" in {f.kind for f in failed.failures}


WORDS = "OV-WORDS the ledger hides in the stove."
LEAN = "She leans toward Marcus."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-8,12,13; FR-MULTI-4; FR-AUTH-2")
async def test_output_viewpoint__respond(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The Postgres check (see the module docstring): a stored result is returned only for its
    viewpoint (generation, idempotent replay, history read), each filtered by the audience
    filter, and the narrative and the beats agree."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Jenni", "Marcus", "Bobby")
        await ps.perceive(engine, story, "full")
        parlor = await ps.place_entity(engine, story, "parlor")
        for name in ("Wren", "Jenni", "Marcus", "Bobby"):
            await ps.put(engine, story, ids[name], parlor)
        epistrel.models.stub.script(
            "generation",
            [
                render(
                    beat("say", WORDS, intent="sincere", whisper_to=[ids["Marcus"]]),
                    beat("do", LEAN),
                )
            ],
        )
        bobby = f"character:{ids['Bobby']}"
        sent = body(ids["Wren"], "Well?", output_viewpoint=bobby)
        key = {"Idempotency-Key": "ov-respond-1"}
        first = await respond(client, story, sent, headers=key)
        assert first.status_code == 200, first.text
        played = first.json()
        assert "OV-WORDS" not in str(played)
        envelope = "Jenni whispers something to Marcus"
        assert envelope in played["message"]["text"]
        assert [b["text"] for b in played["beats"] if b.get("fidelity") == "envelope"] == [envelope]
        # A replay is the stored result, for its viewpoint only.
        again = await respond(client, story, sent, headers=key)
        assert again.status_code == 200
        assert again.json() == played
        other = body(ids["Wren"], "Well?", output_viewpoint=f"character:{ids['Marcus']}")
        refused = await respond(client, story, other, headers=key)
        assert refused.status_code == 409, refused.text
        # A history read is computed for the viewpoint it names.
        marcus = await history(client, story, f"character:{ids['Marcus']}")
        assert WORDS in [i["text"] for i in marcus]
        for_bobby = await history(client, story, bobby)
        assert "OV-WORDS" not in str(for_bobby)
        (message,) = [i["text"] for i in for_bobby if i["channel"] == "message"]
        assert message == played["message"]["text"]
