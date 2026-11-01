"""S-SIMULTANEOUS: concurrent reactions and adjudication (docs/04 §7; FR-MULTI-5,8; #175).

The key room (``tests.fixtures.key_room``): Cal (the player), Bobby and Jenni in the study, a
structured story with per-character renders. Cal asks; the request names Bobby then Jenni and
runs ``simultaneous`` (``interaction_mode``). Played end to end through ``respond`` with the stub
adapter, each primary's render scripted by its name in the prompt:

- neither render sees the other's same-turn reaction: no ``## Just now`` and none of the other's
  words in either captured prompt (the complement of S-SEQUENTIAL's);
- both declare actions on the brass key (Bobby pockets it, ``holds(Bobby, key)``; Jenni takes
  it to lock it away, ``holds(Jenni, key)``): two holders of one key under the functional
  ``held_by``. The conflict is detected deterministically and settled by request order: Bobby's
  action stands, Jenni's beat is repaired in character as an attempt ("Jenni reaches for the
  brass key, but it is already gone.") with her effect dropped (only Bobby's holding is canon),
  one ``render_rejected`` for her beat and no regeneration call; the turn report and the
  response list ``{object, winner, loser, rule: request_order}``; all beats commit as one turn,
  in request order;
- with ``simultaneous_tie_break = director`` one director call (``director_tiebreak``) picks
  Jenni, so Bobby's beat becomes the attempt (``rule: director``); a director naming someone who
  did not act falls back to request order with ``director_tiebreak_skipped``;
- the complement: two non-conflicting actions both stand and no adjudication is recorded;
- M6 (#188): with ``simultaneous_tie_break = state:initiative`` the higher initiative wins
  (``test_s_simultaneous__state_tie_break``), and two purchases one shared purse cannot cover
  are one conflict settled by the same tie-break.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import StateChangeSource
from epistrel.core.epistemic.conflicts import Effect, detect_conflicts
from epistrel.core.epistemic.perception import CO_LOCATION, RouteBeat, SceneState, route_turn
from epistrel.core.propositions import registry
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import HOLDS, LOCATED_AT, LOCATED_IN
from epistrel.core.states import StateDefinition
from epistrel.core.verify.rules import Options, Record
from epistrel.orchestrator.structured import simultaneous
from tests import multi_support as ms
from tests import perception_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.key_room import KeyRoom, key_room
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, turn_report

QUESTION = "Jenni, did you leave the lamp on in the hall?"
POCKETS = "Bobby slips the brass key into his pocket."
TAKES = "Jenni takes the brass key to lock it in the desk drawer."
JENNI_ATTEMPT = "Jenni reaches for the brass key, but it is already gone."
BOBBY_ATTEMPT = "Bobby reaches for the brass key, but it is already gone."
SITS = "Jenni sits down by the window."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _ask(room: KeyRoom, **fields: Any) -> dict[str, Any]:
    return body(
        room.cal,
        "Quick, the key!",
        render_character_ids=[room.bobby, room.jenni],
        interaction_mode="simultaneous",
        **fields,
    )


async def _play(client: Any, room: KeyRoom, **fields: Any) -> dict[str, Any]:
    answer = await respond(client, room.story_id, _ask(room, **fields))
    assert answer.status_code == 200, answer.text
    played: dict[str, Any] = answer.json()
    return played


async def _beats(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT e.operation_id, e.type, e.beat, e.character_id, e.payload->>'text' AS text "
        "FROM events e JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND o.kind = 'turn' AND o.status = 'committed' "
        "AND e.type LIKE 'beat_%' ORDER BY e.beat, e.seq",
        s=story,
    )


async def _holders(engine: AsyncEngine, room: KeyRoom) -> set[str]:
    """The characters canon says hold the key."""
    found = await rows(
        engine,
        "SELECT en.character_id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "JOIN entities en ON en.id = p.subject_id "
        "WHERE f.story_id = :s AND p.object_id = :k AND p.polarity = 'affirmed'",
        s=room.story_id,
        k=room.key,
    )
    return {str(r.character_id) for r in found}


def _script_conflict(stub: Any) -> None:
    ms.renders_for(
        stub,
        "Bobby",
        ms.render(
            beat("say", QUESTION, intent="sincere"),
            beat("do", POCKETS, propositions=[claim("Bobby", "holds", "the brass key")]),
        ),
    )
    ms.renders_for(
        stub,
        "Jenni",
        ms.render(beat("do", TAKES, propositions=[claim("Jenni", "holds", "the brass key")])),
    )


@pytest.mark.integration
@pytest.mark.part("M5", "FR-MULTI-5,8")
async def test_s_simultaneous(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (isolation, request order, one turn)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        stub.reset()
        _script_conflict(stub)
        played = await _play(client, room)
        assert played["interaction_mode"] == "simultaneous"
        assert played["order"] == [room.bobby, room.jenni]

        # Neither saw the other's reaction from this turn.
        (bobby_prompt,) = ms.render_prompts(stub, "Bobby")
        (jenni_prompt,) = ms.render_prompts(stub, "Jenni")
        for prompt in (bobby_prompt, jenni_prompt):
            assert "## Just now" not in prompt
            assert "Quick, the key!" in prompt  # the starting state: the player's input
        assert QUESTION not in jenni_prompt
        assert POCKETS not in jenni_prompt
        assert TAKES not in bobby_prompt

        # The conflict, settled by request order; the loser's action is an attempt.
        expected = [
            {"object": room.key, "winner": room.bobby, "loser": room.jenni, "rule": "request_order"}
        ]
        assert played["adjudications"] == expected
        report = await turn_report(engine, room.story_id)
        assert report["multi"]["mode"] == "simultaneous"
        assert report["multi"]["tie_break"] == "request_order"
        assert report["multi"]["adjudications"] == expected
        events = await _beats(engine, room.story_id)
        assert len({e.operation_id for e in events}) == 1  # one turn
        assert [(e.type, e.character_id) for e in events] == [
            ("beat_say", room.bobby),
            ("beat_do", room.bobby),
            ("beat_do", room.jenni),
        ]
        texts = {(e.character_id, e.type): e.text for e in events}
        assert texts[(room.bobby, "beat_do")] == POCKETS
        assert texts[(room.jenni, "beat_do")] == JENNI_ATTEMPT
        assert room.bobby not in JENNI_ATTEMPT  # the attempt never names the winner
        jenni_beat = next(e.beat for e in events if e.character_id == room.jenni)
        rejected = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'render_rejected'",
            s=room.story_id,
        )
        assert [(r.payload["beat"], r.payload["rung"], r.payload["text"]) for r in rejected] == [
            (jenni_beat, 2, TAKES)
        ]
        assert not [c for c in stub.calls if c.purpose == "regeneration"]
        assert {r["beat"] for r in report["repairs"]} == {jenni_beat}
        assert {r["class"] for r in report["repairs"]} == {"substitution"}
        assert await _holders(engine, room) == {room.bobby}  # her effect was dropped
        (message,) = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'message_rendered'",
            s=room.story_id,
        )
        assert message.payload["interaction_mode"] == "simultaneous"
        assert message.payload["render_character_ids"] == [room.bobby, room.jenni]


@pytest.mark.integration
@pytest.mark.part("M5", "FR-MULTI-5,8")
async def test_s_simultaneous__director(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """``simultaneous_tie_break = director``: one director call picks the winner among the
    conflicting characters; a winner who did not act falls back to request order."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        patched = await client.patch(
            f"/stories/{room.story_id}/settings",
            json={"simultaneous_tie_break": "director"},
            headers=WRITE,
        )
        assert patched.status_code == 200, patched.text
        stub.reset()
        _script_conflict(stub)
        stub.script(
            "director_tiebreak",
            [json.dumps({"adjudications": [{"object": room.key, "winner": room.jenni}]})],
        )
        played = await _play(client, room)
        assert played["adjudications"] == [
            {"object": room.key, "winner": room.jenni, "loser": room.bobby, "rule": "director"}
        ]
        (tiebreak,) = [
            c
            for c in stub.calls
            if c.purpose == "generation" and c.response_schema == "DirectorTiebreak"
        ]
        prompt = "\n".join(m.content for m in tiebreak.messages)
        assert "## Who got there first" in prompt
        assert POCKETS in prompt
        assert TAKES in prompt
        assert QUESTION not in prompt  # only the conflicting actions
        texts = {(e.character_id, e.type): e.text for e in await _beats(engine, room.story_id)}
        assert texts[(room.bobby, "beat_do")] == BOBBY_ATTEMPT
        assert texts[(room.jenni, "beat_do")] == TAKES
        assert await _holders(engine, room) == {room.jenni}
        report = await turn_report(engine, room.story_id)
        assert report["budget"]["used_by_kind"]["director_tiebreak"] == 1
        assert "director_tiebreak_skipped" not in played["degradation"]

        # A director naming someone who did not act on the key: request order stands.
        room = await key_room(client, engine)
        stub.reset()
        _script_conflict(stub)
        stub.script(
            "director_tiebreak",
            [json.dumps({"adjudications": [{"object": room.key, "winner": room.cal}]})],
        )
        played = await _play(client, room, simultaneous_tie_break="director")
        assert played["adjudications"] == [
            {"object": room.key, "winner": room.bobby, "loser": room.jenni, "rule": "request_order"}
        ]
        assert "director_tiebreak_skipped" in played["degradation"]


@pytest.mark.integration
@pytest.mark.part("M5", "FR-MULTI-5,8")
async def test_s_simultaneous__no_conflict(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The complement: two non-conflicting actions both stand, nothing is adjudicated or
    repaired; a malformed tie-break is 422."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(beat("do", POCKETS, propositions=[claim("Bobby", "holds", "the brass key")])),
        )
        ms.renders_for(stub, "Jenni", ms.render(beat("do", SITS)))
        played = await _play(client, room)
        assert "adjudications" not in played
        texts = {(e.character_id, e.type): e.text for e in await _beats(engine, room.story_id)}
        assert texts == {(room.bobby, "beat_do"): POCKETS, (room.jenni, "beat_do"): SITS}
        report = await turn_report(engine, room.story_id)
        assert report["multi"]["adjudications"] == []
        assert "repairs" not in report
        assert await _holders(engine, room) == {room.bobby}

        malformed = await respond(
            client, room.story_id, _ask(room, simultaneous_tie_break="initiative")
        )
        assert malformed.status_code == 422, malformed.text


SETS = "Bobby sets the brass key on the study mantel."
HALL_TABLE = "Jenni sets the brass key on the hall table."


async def _placements(engine: AsyncEngine, room: KeyRoom) -> set[str]:
    """The places canon puts the key."""
    found = await rows(
        engine,
        "SELECT p.object_id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND p.subject_id = :k AND p.predicate_id = :at "
        "AND p.polarity = 'affirmed'",
        s=room.story_id,
        k=room.key,
        at=LOCATED_AT,
    )
    return {str(r.object_id) for r in found}


@pytest.mark.integration
@pytest.mark.part("M5", "FR-MULTI-5,8")
async def test_s_simultaneous__two_places(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """#549: Bobby leaves the brass key in the study while Jenni carries it to the hall, and no
    containment fact relates the two rooms. The placement conflict is detected and settled by
    request order before #177's reconcile, so exactly one placement (Bobby's) commits and
    Jenni's beat is the attempt."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        hall = await ps.place_entity(engine, room.story_id, "hall")
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(
                beat("do", SETS, propositions=[claim("the brass key", "located_at", "study")])
            ),
        )
        ms.renders_for(
            stub,
            "Jenni",
            ms.render(
                beat("do", HALL_TABLE, propositions=[claim("the brass key", "located_at", "hall")])
            ),
        )
        played = await _play(client, room)
        assert played["adjudications"] == [
            {"object": room.key, "winner": room.bobby, "loser": room.jenni, "rule": "request_order"}
        ]
        texts = {(e.character_id, e.type): e.text for e in await _beats(engine, room.story_id)}
        assert texts == {(room.bobby, "beat_do"): SETS, (room.jenni, "beat_do"): JENNI_ATTEMPT}
        placed = await _placements(engine, room)
        assert placed == {room.study}, placed
        assert hall not in placed


# -- the deterministic detection and the routing, in memory -------------------------------------

BOBBY_E, JENNI_E, CAL_E = (
    "01JE00000000000000000000B0",
    "01JE00000000000000000000J0",
    "01JE0000000000000000000C00",
)
KEY_E, COIN_E = "01JE00000000000000000000K0", "01JE0000000000000000000CN0"
STUDY_P, HALL_P = "01JE00000000000000000000S0", "01JE00000000000000000000H0"
HOUSE_P, DESK_P = "01JE0000000000000000000HS0", "01JE00000000000000000000D0"


def _p(subject: str, predicate: str, obj: str, *, negated: bool = False) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=Interval.of(None, None),
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
    )


def _effect(beat: int, who: str, rank: int, p: Proposition, *, player: bool = False) -> Effect:
    return Effect(beat, who, rank, p, f"d{beat}.0", player=player)


def _within(inner: str, outer: str) -> Record:
    """A containment fact: ``inner`` is ``located_in`` ``outer``."""
    return Record(_p(inner, LOCATED_IN, outer), f"in:{inner}")


@pytest.mark.part("M5", "FR-MULTI-5,8")
def test_s_simultaneous__detection() -> None:
    """``detect_conflicts`` is deterministic and model-free: two holders of one key (the
    functional ``held_by``), the possession/placement pair, and one key placed in two places no
    containment fact relates (#549: the location fluent is single-valued, whatever #140 answers
    a claim checked against canon) conflict over the key, whatever order the effects arrive in;
    two different items, a character's clash with itself, a negation, the same place twice and
    a place inside the other (``located_in``, either way, within the containment depth) do
    not. ``settle`` gives request order the earlier-named primary (the player first) and the
    director's choice where it judged; a beat that lost once takes no part later."""
    reg = registry.core_registry()
    bobby = _effect(2, "bobby", 1, _p(BOBBY_E, HOLDS, KEY_E))
    jenni = _effect(3, "jenni", 2, _p(JENNI_E, HOLDS, KEY_E))
    for effects in ([bobby, jenni], [jenni, bobby]):
        (found,) = detect_conflicts(effects, registry=reg)
        assert (found.object_id, found.beats, found.characters) == (
            KEY_E,
            (2, 3),
            ("bobby", "jenni"),
        )
    placed = _effect(3, "jenni", 2, _p(KEY_E, LOCATED_AT, STUDY_P))
    (found,) = detect_conflicts([placed, bobby], registry=reg)
    assert (found.object_id, found.rules) == (KEY_E, ("possession_placement",))
    in_study = _effect(2, "bobby", 1, _p(KEY_E, LOCATED_AT, STUDY_P))
    in_hall = _effect(3, "jenni", 2, _p(KEY_E, LOCATED_AT, HALL_P))
    for effects in ([in_study, in_hall], [in_hall, in_study]):
        (found,) = detect_conflicts(effects, registry=reg)
        assert (found.object_id, found.beats, found.characters, found.rules) == (
            KEY_E,
            (2, 3),
            ("bobby", "jenni"),
            ("placement",),
        )
    # Two places a containment fact does not relate: still two places.
    (found,) = detect_conflicts(
        [in_study, in_hall], registry=reg, containment=[_within(STUDY_P, HOUSE_P)]
    )
    assert found.rules == ("placement",)
    quiet = [
        [bobby, _effect(3, "jenni", 2, _p(JENNI_E, HOLDS, COIN_E))],  # two items
        [bobby, _effect(3, "bobby", 1, _p(BOBBY_E, HOLDS, COIN_E))],  # himself
        [bobby, _effect(3, "jenni", 2, _p(JENNI_E, HOLDS, KEY_E, negated=True))],
        [in_study, _effect(3, "jenni", 2, _p(KEY_E, LOCATED_AT, STUDY_P))],  # the same place
    ]
    for effects in quiet:
        assert detect_conflicts(effects, registry=reg) == [], effects
    # A place inside the other (the hall is in the house), either way and through two hops
    # (the desk in the hall in the house), is one placement: no conflict.
    in_house = _effect(3, "jenni", 2, _p(KEY_E, LOCATED_AT, HOUSE_P))
    at_desk = _effect(2, "bobby", 1, _p(KEY_E, LOCATED_AT, DESK_P))
    hierarchy = [_within(HALL_P, HOUSE_P), _within(DESK_P, HALL_P)]
    for effects in ([in_hall, in_house], [in_house, in_hall], [at_desk, in_house]):
        assert detect_conflicts(effects, registry=reg, containment=hierarchy) == [], effects
    # Beyond ``containment_max_depth`` the hops no longer relate the places.
    (found,) = detect_conflicts(
        [at_desk, in_house],
        registry=reg,
        containment=hierarchy,
        options=Options(containment_max_depth=1),
    )
    assert found.rules == ("placement",)
    # Request order: the earlier-named wins; the player (rank 0) before every primary.
    (conflict,) = detect_conflicts([jenni, bobby], registry=reg)
    (settled,) = simultaneous.settle([conflict])
    assert (settled.winner, settled.loser, settled.rule) == ("bobby", "jenni", "request_order")
    player = _effect(1, "cal", 0, _p(CAL_E, HOLDS, KEY_E), player=True)
    (conflict,) = detect_conflicts([bobby, player, jenni], registry=reg)
    settled_all = simultaneous.settle([conflict], {KEY_E: "jenni"})
    assert {(a.winner, a.loser, a.rule) for a in settled_all} == {
        ("cal", "bobby", "request_order"),
        ("cal", "jenni", "request_order"),
    }  # the player's action is never the director's to overrule
    (settled,) = simultaneous.settle(
        detect_conflicts([bobby, jenni], registry=reg), {KEY_E: "jenni"}
    )
    assert (settled.winner, settled.loser, settled.rule) == ("jenni", "bobby", "director")
    assert simultaneous.attempt_text("Jenni", "the brass key") == JENNI_ATTEMPT


def _study() -> SceneState:
    """Bobby and Jenni in the study."""
    return SceneState(
        location="study",
        placements={"bobby": "study", "jenni": "study"},
        characters=frozenset({"bobby", "jenni"}),
        places=frozenset({"study"}),
    )


@pytest.mark.part("M5", "FR-MULTI-5,8")
def test_s_simultaneous__routing() -> None:
    """A simultaneous turn is one moment (#175, carried from #161): every beat routes against
    the starting placements, so Bobby, who leaves the study in beat 2, still perceives Jenni's
    beat 3; in a sequential turn he does not (his move applies from the next beat). A move with
    no destination unplaces him."""
    scene = _study()
    beats = [
        RouteBeat(kind="do", beat=2, actor="bobby", moves=(("bobby", None),)),
        RouteBeat(kind="say", beat=3, actor="jenni"),
    ]
    sequential = route_turn(beats, scene, CO_LOCATION)
    assert sequential[1].of("bobby") is None
    together = route_turn(beats, scene, CO_LOCATION, mode="simultaneous")
    assert together[1].of("bobby") is not None
    assert together[0].of("jenni") is not None


# -- M6 (#188): the designated-State tie-break and shared-State conflicts ---------------------

INITIATIVE = {"name": "initiative", "type": "number", "scope": "character", "initial_value": 0}
PURSE = {
    "name": "purse",
    "type": "number",
    "scope": "story",
    "constraints": {"non_negative": True},
    "initial_value": 500,
    "unit": "gold",
}
MOOD = {"name": "mood", "type": "enum", "constraints": {"allowed_values": ["calm"]}}
BOBBY_PAYS = "Bobby pays 400 gold from the purse."
JENNI_PAYS = "Jenni pays 300 gold from the purse."


async def _initiative(engine: AsyncEngine, room: KeyRoom, bobby: int, jenni: int) -> None:
    for character, value in ((room.bobby, bobby), (room.jenni, jenni)):
        await ss.change(
            engine,
            room.story_id,
            ss.set_to(
                "initiative",
                value,
                scope_key=character,
                reason="rolled",
                source=StateChangeSource.CONSUMER,
            ),
        )


def _script_purse(stub: Any) -> None:
    for name, text, delta in (("Bobby", BOBBY_PAYS, -400), ("Jenni", JENNI_PAYS, -300)):
        effect = {"kind": "change", "state": "purse", "delta": delta, "reason": "a payment"}
        ms.renders_for(stub, name, ms.render(beat("do", text, effects=[effect])))


@pytest.mark.integration
@pytest.mark.part("M6", "FR-MULTI-8")
async def test_s_simultaneous__state_tie_break(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """With ``simultaneous_tie_break = state:initiative`` the higher initiative at the turn's
    start wins the key (``rule: state``), and two payments one shared purse cannot
    cover together are one conflict (``state:purse``) settled the same way: only the winner's
    effect commits, and the loser's beat is the attempt narrated as refused. The tie-break must
    name a numeric State of the story (422 ``tie_break_state_invalid`` on the settings PATCH);
    a request naming no such State falls back to request order with that warning.

    Complement: equal initiative falls back to request order, which picks the first-named
    character (Bobby)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        await ss.set_level(engine, room.story_id, "on")

        async def tie_break(value: str) -> Any:
            return await client.patch(
                f"/stories/{room.story_id}/settings",
                json={"simultaneous_tie_break": value},
                headers=WRITE,
            )

        refused = await tie_break("state:initiative")  # no such State yet
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["reason"] == "tie_break_state_invalid"
        await ss.define_all(
            engine,
            room.story_id,
            *(StateDefinition.model_validate(d) for d in (INITIATIVE, PURSE, MOOD)),
        )
        refused = await tie_break("state:mood")  # not a number
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["reason"] == "tie_break_state_invalid"
        accepted = await tie_break("state:initiative")
        assert accepted.status_code == 200, accepted.text
        await _initiative(engine, room, bobby=2, jenni=5)

        # -- The key: Jenni's higher initiative wins; Bobby's action is the attempt.
        stub.reset()
        _script_conflict(stub)
        played = await _play(client, room)
        rule = "state"
        assert played["adjudications"] == [
            {"object": room.key, "winner": room.jenni, "loser": room.bobby, "rule": rule}
        ]
        report = await turn_report(engine, room.story_id)
        assert report["multi"]["tie_break"] == "state:initiative"
        assert BOBBY_ATTEMPT in [b["text"] for b in played["beats"]]
        assert await _holders(engine, room) == {room.jenni}

        # -- A shared State: together the payments break the purse; one conflict group.
        stub.reset()
        _script_purse(stub)
        played = await _play(client, room)
        assert played["adjudications"] == [
            {"object": "state:purse", "winner": room.jenni, "loser": room.bobby, "rule": rule}
        ]
        texts = [b["text"] for b in played["beats"]]
        assert JENNI_PAYS in texts
        assert BOBBY_PAYS not in texts
        assert any(t.startswith("Bobby tries") for t in texts)
        assert [(e["state"], e["new_value"]) for e in played["state_effects"]] == [("purse", 200)]
        assert await ss.value(engine, room.story_id, "purse") == 200

        bobby_first = [
            {
                "object": "state:purse",
                "winner": room.bobby,
                "loser": room.jenni,
                "rule": "request_order",
            }
        ]

        # -- A request naming no numeric State: request order, with the warning.
        await _refill(engine, room)
        stub.reset()
        _script_purse(stub)
        played = await _play(client, room, simultaneous_tie_break="state:mood")
        assert "tie_break_state_invalid" in [w["kind"] for w in played["warnings"]]
        assert played["adjudications"] == bobby_first
        assert await ss.value(engine, room.story_id, "purse") == 100

        # -- Complement: equal initiative falls back to request order (Bobby, named first).
        await _initiative(engine, room, bobby=3, jenni=3)
        await _refill(engine, room)
        stub.reset()
        _script_purse(stub)
        played = await _play(client, room)
        assert played["adjudications"] == bobby_first
        assert BOBBY_PAYS in [b["text"] for b in played["beats"]]
        assert await ss.value(engine, room.story_id, "purse") == 100


async def _refill(engine: AsyncEngine, room: KeyRoom) -> None:
    """The shared purse back to 500."""
    await ss.change(
        engine,
        room.story_id,
        ss.set_to("purse", 500, reason="refilled", source=StateChangeSource.CONSUMER),
    )
