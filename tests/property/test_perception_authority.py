"""PERCEPTION-AUTHORITY: content reaches a character only through authoritative perception
(docs/04 §6; FR-TURN-9,10, NFR-PRIV-3; #161).

In memory (300 examples): random scenes (up to six characters, some unplaced, over up to four
places with random ``located_in`` containment, adjacency and barriers, open channels and
languages), a random beat (``say``, ``do`` or narration, with random qualifiers: whisper
targets, an aside, eavesdroppers, a remote channel, a language, a partial text) and a random
roster the consumer claims is present. ``route_perception`` takes no roster, so whatever the
roster says, an independent statement of FR-TURN-2,9,13 holds:

- every perceiver is the actor, authoritatively co-located with the beat, an eavesdropper with
  a plausible path, a participant of a channel the speaker is on, or (a sound) in an adjoining
  place not behind a wall: a character on the roster who is none of these perceives nothing;
- every character authoritatively co-located perceives the beat, whether the roster names
  them or not;
- at ``co_location`` everything perceived is content; at ``full`` a whisper's words reach only
  its targets (and whom an explicit widening names), the rest of the room the envelope.

On Postgres: a ``respond`` whose ``scene_context.characters_present`` names an absent
character and omits a present one writes the absent one nothing and the present one the beat.
``__exit`` (#175): a character the turn unplaces mid-turn with no destination perceives nothing
after his exit in a sequential turn, and everything in a simultaneous one (one moment).
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.epistemic.perception import (
    CO_LOCATION,
    CONTENT,
    FULL,
    Level,
    Qualifiers,
    RemoteChannel,
    RouteBeat,
    SceneState,
    pair,
    route_perception,
    route_turn,
    validate_qualifiers,
)
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import BARRIERS, LOCATED_AT
from tests import canon_support
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, render, structured_story

CHARACTERS = tuple(f"C{i}" for i in range(6))
PLACES = tuple(f"P{i}" for i in range(4))
SAFE = frozenset(BARRIERS) - {"wall"}


@st.composite
def scenes(draw: Any) -> tuple[SceneState, RouteBeat, Level, frozenset[str]]:
    cast = draw(st.lists(st.sampled_from(CHARACTERS), min_size=2, max_size=6, unique=True))
    places = draw(st.lists(st.sampled_from(PLACES), min_size=2, max_size=4, unique=True))
    placements = {
        c: p for c in cast if (p := draw(st.one_of(st.none(), st.sampled_from(places)))) is not None
    }
    adjacency = {
        pair(a, b): draw(st.sampled_from(BARRIERS))
        for a in places
        for b in places
        if a < b and draw(st.booleans())
    }
    containment = {
        p: (outer,)
        for p in places
        if (outer := draw(st.one_of(st.none(), st.sampled_from(places)))) not in (None, p)
    }
    channels = {
        "ch": frozenset(draw(st.lists(st.sampled_from(cast), max_size=4, unique=True))),
    }
    languages = {
        c: frozenset(draw(st.lists(st.sampled_from(("en", "fr")), max_size=2, unique=True)))
        for c in cast
        if draw(st.booleans())
    }
    kind = draw(st.sampled_from(("say", "do", "narration")))
    actor = None if kind == "narration" else draw(st.sampled_from(cast))
    names = st.lists(st.sampled_from(cast), max_size=3, unique=True)
    q = Qualifiers(
        addressee=draw(st.one_of(st.none(), st.sampled_from(cast))),
        whisper_to=tuple(draw(names)) if kind == "say" else (),
        aside=draw(st.booleans()) if kind == "say" else False,
        eavesdrop_by=tuple(draw(names)) if kind != "narration" else (),
        remote_channel=RemoteChannel("ch") if kind != "narration" and draw(st.booleans()) else None,
        language=draw(st.one_of(st.none(), st.sampled_from(("en", "fr"))))
        if kind == "say"
        else None,
        partial_text=draw(st.one_of(st.none(), st.just("a few words"))),
        location=draw(st.sampled_from(places)) if kind == "narration" else None,
    )
    sound = "a knock" if kind == "do" and draw(st.booleans()) else None
    scene = SceneState(
        location=draw(st.sampled_from(places)),
        placements=placements,
        containment=containment,
        adjacency=adjacency,
        channels=channels,
        languages=languages,
        characters=frozenset(cast),
        places=frozenset(places),
    )
    roster = frozenset(draw(st.lists(st.sampled_from(cast), unique=True)))
    level: Level = draw(st.sampled_from((CO_LOCATION, FULL)))
    return scene, RouteBeat(kind, 2, actor, q, sound=sound), level, roster


def _ancestry(scene: SceneState, place: str) -> list[str]:
    found, frontier = [place], [place]
    while frontier:
        frontier = [o for p in frontier for o in scene.containment.get(p, ()) if o not in found]
        found += frontier
    return found


def _path(scene: SceneState, listener: str, location: str) -> bool:
    """An independent statement of FR-TURN-13's plausible path."""
    return any(
        scene.adjacency.get(pair(a, b)) in SAFE
        for a in _ancestry(scene, listener)
        for b in _ancestry(scene, location)
        if a != b
    )


@pytest.mark.part("M5", "FR-TURN-9,10; NFR-PRIV-3")
@settings(max_examples=300, deadline=None)
@given(case=scenes())
def test_perception_authority(case: tuple[SceneState, RouteBeat, Level, frozenset[str]]) -> None:
    """See the module docstring."""
    scene, routed, level, roster = case
    q, _ = validate_qualifiers(routed, scene, level)
    found = route_perception(routed, scene, q, level)
    assert found == route_perception(routed, scene, q, level)  # deterministic
    perceived = {p.character_id: p for p in found}
    actor = routed.actor
    location = (q.location or scene.location) if routed.kind == "narration" else scene.at(actor)
    room = {c for c, p in scene.placements.items() if location is not None and p == location}
    channel = scene.channels["ch"]
    widened: set[str] = set()
    if level == FULL and location is not None:
        widened |= {
            c
            for c in routed.qualifiers.eavesdrop_by
            if c in scene.placements and _path(scene, scene.placements[c], location)
        }
        if routed.sound is not None:
            widened |= {
                c
                for c, p in scene.placements.items()
                if scene.adjacency.get(pair(p, location)) not in (None, "wall")
            }
    if level == FULL and routed.qualifiers.remote_channel is not None and actor in channel:
        widened |= channel
    allowed = room | widened | ({actor} if actor else set())
    # Nobody beyond the authoritative audience, whatever the roster claims.
    assert set(perceived) <= allowed, (set(perceived) - allowed, roster)
    assert not {c for c in roster - allowed if c in perceived}
    # Everyone authoritatively co-located perceives, on the roster or not.
    assert room <= set(perceived)
    if actor is not None:
        assert perceived[actor].fidelity == CONTENT
    if level == CO_LOCATION:
        assert all(p.fidelity == CONTENT for p in found)
        assert set(perceived) == room | ({actor} if actor else set())
    elif routed.kind == "say" and q.whisper_to and not q.eavesdrop_by and not q.remote_channel:
        words = {c for c, p in perceived.items() if p.fidelity == CONTENT} - {actor}
        assert words <= set(q.whisper_to)
        assert all(perceived[c].fidelity != CONTENT for c in room - set(q.whisper_to) - {actor})


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.integration
@pytest.mark.part("M5", "FR-TURN-9,10; NFR-PRIV-3")
async def test_perception_authority__roster(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """A manipulated roster changes nothing: Bobby, co-located but left off it, gets Ada's
    words; Clara, in another room but named on it, gets nothing. Complement: the roster was
    accepted (it names cast members only) and the turn committed."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada", "Bobby", "Clara")
        await ps.perceive(engine, story, "full")
        kitchen = await ps.place_entity(engine, story, "kitchen")
        cellar = await ps.place_entity(engine, story, "cellar")
        for name in ("Wren", "Ada", "Bobby"):
            await ps.put(engine, story, ids[name], kitchen)
        await ps.put(engine, story, ids["Clara"], cellar)
        epistrel.models.stub.script(
            "generation", [render(beat("say", "The soup is ready.", intent="sincere"))]
        )
        roster = {"characters_present": [ids["Wren"], ids["Ada"], ids["Clara"]]}
        response = await respond(
            client, story, body(ids["Wren"], "Anything to eat?", scene_context=roster)
        )
        assert response.status_code == 200, response.text
        bobby = [m.text for m in await ps.memories_of(engine, story, ids["Bobby"])]
        assert "Ada says: The soup is ready." in bobby
        assert await ps.memories_of(engine, story, ids["Clara"]) == []


@pytest.mark.integration
@pytest.mark.part("M5", "FR-TURN-9,10; NFR-PRIV-3")
async def test_perception_authority__exit(
    stub_settings: Settings,
    engine: AsyncEngine,
) -> None:
    """A mid-turn exit with no destination (#175, carried from #161): the turn's own negated
    ``located_at`` closing Bobby's placement at beat 2 is a move to nowhere at beat 2
    (``scene.load_scene``), so in a sequential turn routing unplaces him from beat 3 (he still
    perceives his own exit) and he never hears what Cal says after he left; in a
    ``simultaneous`` turn every beat routes against the starting placements, so he does.
    Complement: a turn that closes nothing moves no one."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await structured_story(client, "Cal", "Bobby")
        study = await ps.place_entity(engine, story, "study")
        await ps.put(engine, story, ids["Cal"], study)
        placed = await ps.put(engine, story, ids["Bobby"], study)
        async with engine.connect() as conn:
            before, none = await scene_loader.load_scene(conn, story, 1)
        assert none == {}
        assert before.at(ids["Bobby"]) == study
        left = await canon_support.supersede(
            engine,
            story,
            placed.id,
            Proposition(
                subject_id=ids["Bobby"],
                predicate_id=LOCATED_AT,
                object_id=study,
                valid_time=Interval.of(None, None),
                polarity=Polarity.NEGATED,
            ),
            provenance="render",
            beat=2,
        )
        turn = int(left.narrative_turn or 0)
        async with engine.connect() as conn:
            scene, moves = await scene_loader.load_scene(conn, story, turn)
        assert scene.at(ids["Bobby"]) == study  # placed at the turn's start
        assert moves == {2: ((ids["Bobby"], None),)}
        beats = [
            RouteBeat(kind="do", beat=2, actor=ids["Bobby"], moves=moves[2]),
            RouteBeat(kind="say", beat=3, actor=ids["Cal"]),
        ]
        exit_, after = route_turn(beats, scene, CO_LOCATION)
        assert exit_.of(ids["Bobby"]) is not None
        assert after.of(ids["Bobby"]) is None
        _, together = route_turn(beats, scene, CO_LOCATION, mode="simultaneous")
        assert together.of(ids["Bobby"]) is not None
