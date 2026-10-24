"""Unit checks of player authority's pure parts (FR-RESP-11; #180): the other-agent detector
(``core.verify.other_agent``) and the split of one input under each ``player_authority``
(``orchestrator.structured.player_authority``). The turn-level oracles are S-PLAYER-AUTHORITY
(``tests/scenario/test_s_player_authority.py``) and C-13 M5 (``tests/conformance/test_c_13.py``)."""

from __future__ import annotations

from epistrel.core.epistemic.roles import Participation
from epistrel.core.settings_schema import StorySettings
from epistrel.core.turns import CastEntry
from epistrel.core.verify import other_agent as oa
from epistrel.orchestrator.structured import player_authority as pa

CAL = CastEntry("c", "Cal", "", True)
JENNI = CastEntry("j", "Jenni", "", False)
BOBBY = CastEntry("b", "Bobby", "", False)
WREN = CastEntry("w", "Wren", "", True)
CAST = (CAL, JENNI, BOBBY, WREN)
OTHERS = {"Jenni": "j", "Bobby": "b", "Wren": "w"}
HANDS = "Jenni smiles and hands me the key."


def test_detect_do() -> None:
    """A ``do`` sentence whose subject is another modeled character is that character's claim:
    a thought by its inner-state cue, speech by its verb or quotation, else an action; a
    pronoun takes the last-mentioned character; the player's own sentences, a joint subject and
    an unmodeled name are left alone."""
    text = f'I wave. {HANDS} She thinks about betraying me. Then Bobby says "Run." Jenni and I sit.'
    found = oa.detect_do(text, OTHERS)
    assert [(c.kind, c.character_id, c.text) for c in found] == [
        ("do", "j", HANDS),
        ("think", "j", "She thinks about betraying me."),
        ("say", "b", 'Then Bobby says "Run."'),
    ]
    assert all(text[c.start : c.end] == c.text for c in found)
    assert oa.detect_do("Marcus waves. I hand Jenni the key.", OTHERS) == []


def test_detect_say() -> None:
    """In the player's ``say`` only quoted speech attributed to another character counts; an
    attribution of a belief is the player's own speech (#167)."""
    say = 'Listen. Jenni says, "I will come." "Go away," said Bobby.'
    found = oa.detect_say(say, OTHERS)
    assert [(c.character_id, c.text) for c in found] == [
        ("j", 'Jenni says, "I will come."'),
        ("b", '"Go away," said Bobby.'),
    ]
    assert oa.detect_say("Jenni thinks it was Marcus.", OTHERS) == []


def test_first_person() -> None:
    assert oa.first_person(HANDS, "Cal") == "Jenni smiles and hands Cal the key."
    assert oa.first_person("I am glad, my friend.", "Cal") == "Cal is glad, Cal's friend."


def _split(mode: str, do: str, **kw: object) -> pa.Split:
    return pa.split(None, do, mode=mode, cast_=CAST, actor_id=CAL.id, **kw)  # type: ignore[arg-type]


def test_split_own_character() -> None:
    """``own_character``: the span is a request, out of the player's own text; a thought is
    dropped (``player_cannot_write_think``); another human's character is never asked
    (``other_player_character``); someone not in the scene is dropped (``not_present``)."""
    found = _split(
        pa.OWN_CHARACTER,
        f"I sit. {HANDS} Jenni thinks I'm lying. Wren nods. Bobby waves.",
        present=frozenset({"j", "w"}),
    )
    assert found.do == "I sit."
    assert [(r.character_id, r.sketch) for r in found.requests] == [
        ("j", "Jenni smiles and hands Cal the key.")
    ]
    assert found.requests_for("j") == ["Jenni smiles and hands Cal the key."]
    assert [(r.character_id, r.code) for r in found.rejected] == [
        ("j", pa.PLAYER_CANNOT_WRITE_THINK),
        ("w", pa.OTHER_PLAYER_CHARACTER),
        ("b", "not_present"),
    ]
    assert found.payloads()[0]["tag"] == "request"
    assert found.payloads()[0]["span"] == ["do", 7, 7 + len(HANDS)]


def test_split_co_author() -> None:
    """``co_author``: the span is the character's narrated beat (beats 2..k in input order,
    first person rewritten), a thought is never one, and a fixed ``silent`` character or a
    fixed ``reactive`` one given speech is not narrated (``role_fixed``)."""
    found = _split(
        pa.CO_AUTHOR,
        f'{HANDS} Bobby says "Hi." Jenni thinks about it.',
        roles={"b": Participation("reactive", fixed=True)},
    )
    assert found.do is None  # nothing of the player's own is left
    (beat,) = pa.narrated_beats(found)
    assert (beat.beat, beat.kind, beat.character_id, beat.narrated_by, beat.text) == (
        2,
        "do",
        "j",
        oa.PLAYER_NARRATOR,
        "Jenni smiles and hands Cal the key.",
    )
    assert [r.code for r in found.rejected] == [pa.ROLE_FIXED, pa.PLAYER_CANNOT_WRITE_THINK]


def test_split_only_for_the_players_character() -> None:
    """Input for a character the platform plays is that character's (FR-AUTH-3): no split."""
    found = pa.split(None, HANDS, mode=pa.CO_AUTHOR, cast_=CAST, actor_id=BOBBY.id)
    assert (found.do, found.active) == (HANDS, False)


def test_requested_renders_and_outcomes() -> None:
    """A requested character with no render gets one (never under ``co_author``); a verified
    ``do`` sharing the request's words is compliance, any other beat a refusal, none
    unresolved."""
    story = StorySettings()
    assert story.player_authority == pa.OWN_CHARACTER
    asked = pa.requested_renders(CAST, None, HANDS, story=story, actor_id=CAL.id, rendering=[])
    assert asked == [JENNI]
    assert (
        pa.requested_renders(
            CAST,
            None,
            HANDS,
            story=StorySettings(player_authority="co_author"),
            actor_id=CAL.id,
            rendering=[],
        )
        == []
    )
    from epistrel.core.turns.beats import TurnBeat

    found = _split(pa.OWN_CHARACTER, HANDS)
    names = ["Cal", "Jenni", "Bobby"]
    pa.outcomes(found, [TurnBeat("do", 2, "Jenni hands Cal the key.", "j")], names)
    assert found.outcomes == [pa.COMPLIED]
    pa.outcomes(found, [TurnBeat("say", 2, "Get it yourself.", "j")], names)
    assert found.outcomes == [pa.REFUSED]
    pa.outcomes(found, [], names)
    assert found.outcomes == [pa.UNRESOLVED]
    assert found.report()["requests"][0]["outcome"] == pa.UNRESOLVED
