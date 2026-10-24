"""U-17: perception qualifier validation (docs/04 §3; FR-TURN-13, FR-TURN-9,10; #161).

A widening qualifier is honored only with a plausible perception path in the recorded positions
and barriers (an eavesdrop) or an established channel (a remote call); an eavesdrop across a
sealed vault or from another city is rejected and repaired (dropped, the beat routes as
unqualified); narrowing qualifiers (a whisper, a foreign language) are accepted without
validation. Pure: ``core.epistemic.perception`` over hand-built scenes.
"""

from __future__ import annotations

import pytest

from epistrel.core.epistemic.perception import (
    CO_LOCATION,
    CONTENT,
    ENVELOPE,
    FULL,
    Qualifiers,
    RemoteChannel,
    RouteBeat,
    SceneState,
    pair,
    route_perception,
    validate_qualifiers,
)

JENNI, MARCUS, BOBBY, ADA, CLARA = "C-JENNI", "C-MARCUS", "C-BOBBY", "C-ADA", "C-CLARA"
PARLOR, HALL, VAULT, STUDY, CITY, HOUSE, ATTIC = (
    "P-PARLOR",
    "P-HALL",
    "P-VAULT",
    "P-STUDY",
    "P-CITY",
    "P-HOUSE",
    "P-ATTIC",
)
CAST = frozenset({JENNI, MARCUS, BOBBY, ADA, CLARA})


def scene(**placements: str) -> SceneState:
    """Jenni, Marcus and Bobby in the parlor; the hall adjoins it through a closed door, the
    vault through a wall; the study is in the house, which adjoins the attic through a thin
    wall; the city is elsewhere."""
    where = {JENNI: PARLOR, MARCUS: PARLOR, BOBBY: PARLOR, **placements}
    return SceneState(
        location=PARLOR,
        placements={k: v for k, v in where.items() if v},
        containment={STUDY: (HOUSE,), PARLOR: (HOUSE,)},
        adjacency={
            pair(PARLOR, HALL): "door_closed",
            pair(PARLOR, VAULT): "wall",
            pair(HOUSE, ATTIC): "thin_wall",
        },
        channels={"call-1": frozenset({JENNI, CLARA}), "radio": frozenset({MARCUS, ADA})},
        characters=CAST,
        places=frozenset({PARLOR, HALL, VAULT, STUDY, CITY, HOUSE, ATTIC}),
    )


def through_a_window() -> SceneState:
    """Jenni in the parlor, Ada in the hall, an open window between them."""
    return SceneState(
        location=PARLOR,
        placements={JENNI: PARLOR, ADA: HALL},
        adjacency={pair(PARLOR, HALL): "window"},
        characters=CAST,
        places=frozenset({PARLOR, HALL}),
    )


def say(**qualifiers: object) -> RouteBeat:
    return RouteBeat("say", 2, JENNI, Qualifiers(**qualifiers))  # type: ignore[arg-type]


@pytest.mark.part("M5", "FR-TURN-9,10,13")
def test_u_17() -> None:
    """Eavesdrops through a closed door, a thin wall (via ``located_in``) and a window are
    accepted; through a wall (a sealed vault) or from another city they are dropped as
    ``sealed_barrier``/``no_path`` and the beat routes as unqualified (the would-be
    eavesdropper perceives nothing). A remote call needs the speaker on an established
    channel: accepted for its participants (a co-located non-participant gets the phone
    envelope), dropped as ``no_channel`` otherwise. Whispers and languages pass unvalidated.
    Complement: below ``full`` nothing is validated or repaired."""
    # A closed door: plausible, Ada catches the words.
    state = scene(**{ADA: HALL})
    q, repairs = validate_qualifiers(say(eavesdrop_by=(ADA,)), state)
    assert (q.eavesdrop_by, repairs) == ((ADA,), ())
    seen = {p.character_id: p for p in route_perception(say(eavesdrop_by=(ADA,)), state)}
    assert (seen[ADA].fidelity, seen[ADA].channel) == (CONTENT, "eavesdrop")

    # Through a thin wall, the path found through the parlor's ``located_in`` house.
    state = scene(**{ADA: ATTIC})
    assert validate_qualifiers(say(eavesdrop_by=(ADA,)), state) == (
        Qualifiers(eavesdrop_by=(ADA,)),
        (),
    )

    # A window: plausible; a render-limited eavesdrop keeps only the words it caught.
    state = through_a_window()
    beat = say(eavesdrop_by=(ADA,), partial_text="the vault code")
    (ada,) = [p for p in route_perception(beat, state) if p.character_id == ADA]
    assert (ada.fidelity, ada.partial_text) == (CONTENT, "the vault code")

    # A sealed vault: rejected and repaired; the beat routes as unqualified.
    state = scene(**{ADA: VAULT})
    q, repairs = validate_qualifiers(say(eavesdrop_by=(ADA,)), state)
    assert q.eavesdrop_by == ()
    assert [(r.kind, r.reason, r.character_id) for r in repairs] == [
        ("eavesdrop_by", "sealed_barrier", ADA)
    ]
    assert ADA not in {p.character_id for p in route_perception(say(eavesdrop_by=(ADA,)), state)}

    # Another city: no path at all.
    state = scene(**{ADA: CITY})
    _, repairs = validate_qualifiers(say(eavesdrop_by=(ADA,)), state)
    assert [r.reason for r in repairs] == ["no_path"]
    # Unplaced, or no character of the story: repaired too.
    _, repairs = validate_qualifiers(say(eavesdrop_by=(ADA, "C-NOBODY")), scene())
    assert [r.reason for r in repairs] == ["no_path", "unknown_character"]
    # Already in the room: ignored silently (they perceive anyway).
    assert validate_qualifiers(say(eavesdrop_by=(BOBBY,)), scene()) == (Qualifiers(), ())

    # An established call: Clara, in the city, hears Jenni; Marcus and Bobby, in the room but
    # not on the call, get the phone envelope.
    state = scene(**{CLARA: CITY})
    beat = say(remote_channel=RemoteChannel("call-1"))
    q, repairs = validate_qualifiers(beat, state)
    assert (q.remote_channel, repairs) == (RemoteChannel("call-1", (CLARA,)), ())
    seen = {p.character_id: p for p in route_perception(beat, state)}
    assert (seen[CLARA].fidelity, seen[CLARA].channel) == (CONTENT, "remote")
    assert seen[MARCUS].fidelity == ENVELOPE
    envelope = seen[MARCUS].envelope
    assert envelope is not None
    assert envelope.kind == "phone"
    # A channel Jenni is not on, or none: no channel; participants beyond the channel's are cut.
    for bad in ("radio", "nothing"):
        _, repairs = validate_qualifiers(say(remote_channel=RemoteChannel(bad)), state)
        assert [r.reason for r in repairs] == ["no_channel"]
    q, _ = validate_qualifiers(say(remote_channel=RemoteChannel("call-1", (CLARA, ADA))), state)
    assert q.remote_channel == RemoteChannel("call-1", (CLARA,))

    # Narrowing needs no validation: a whisper to a co-located target and a language pass.
    assert validate_qualifiers(say(whisper_to=(MARCUS,), language="fr"), scene()) == (
        Qualifiers(whisper_to=(MARCUS,), language="fr"),
        (),
    )
    # ... but a whisper target who is not in the room is dropped, and with none left the beat
    # is an aside (the speaker alone hears the words).
    q, repairs = validate_qualifiers(say(whisper_to=(ADA,)), scene(**{ADA: HALL}))
    assert (q.whisper_to, q.aside) == ((), True)
    assert [r.reason for r in repairs] == ["target_absent"]

    # Complement: at ``co_location`` qualifiers are ignored, never validated or repaired.
    state = scene(**{ADA: VAULT})
    assert validate_qualifiers(say(eavesdrop_by=(ADA,)), state, CO_LOCATION)[1] == ()
    assert validate_qualifiers(say(eavesdrop_by=(ADA,)), state, FULL)[1] != ()
