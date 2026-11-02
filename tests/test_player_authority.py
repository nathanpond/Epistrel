"""Unit checks of player authority's pure parts (FR-RESP-11; #180): the other-agent detector
(``core.verify.other_agent``) and the split of one input under each ``player_authority``
(``orchestrator.structured.player_authority``). The turn-level oracles are S-PLAYER-AUTHORITY
(``tests/scenario/test_s_player_authority.py``) and C-13 M5 (``tests/conformance/test_c_13.py``)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic.roles import Participation
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import HOLDS, LOCATED_AT, PRESENT_AT
from epistrel.core.settings_schema import StorySettings
from epistrel.core.turns import CastEntry
from epistrel.core.turns.beats import TurnBeat as Beat
from epistrel.core.verify import other_agent as oa
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.permission import Asserter
from epistrel.orchestrator.structured import player_authority as pa
from epistrel.orchestrator.verify.verifier import mover_of
from tests import partition_support as parts
from tests import structured_turn_support as st
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.scenario.test_s_player_authority import _forever, _viewpoint_room

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


# -- #598 (FR-RESP-14): co-author knowledge ----------------------------------------------------

T = 600
JEN, BOB, CAL_ID, WRN, KEY, DRAWER, PARLOR = (f"{'0' * 25}{n}" for n in range(1, 8))
NAMES = {CAL_ID: "Cal", JEN: "Jenni", BOB: "Bobby", WRN: "Wren"}


def _prop(subject: str, predicate: str, obj: str, *, negated: bool = False) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=Interval.of(T, None),
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
    )


def _claim(
    category: str,
    proposition: Proposition,
    span: tuple[int, int] = (0, 1),
    giver: str | None = None,
) -> SimpleNamespace:
    """A checked claim of beat 2 as ``presuppositions`` reads it."""
    detected = DetectedClaim(
        span=(2, *span),
        layer="detector",
        category=category,  # type: ignore[arg-type]
        channel="do",
        speaker=JEN,
        proposition=proposition,
        giver=giver,
    )
    return SimpleNamespace(claim=detected, result=None)


def _presupposed(text: str, *claims: SimpleNamespace) -> list[tuple[Any, ...]]:
    beat = Beat("do", 2, text, JEN, narrated_by=oa.PLAYER_NARRATOR)
    found = pa.presuppositions(beat, claims, exempt={JEN, CAL_ID}, names=NAMES)
    return [
        (p.subject_id, p.predicate_id, p.object_id, p.polarity.value, p.valid_time.definite())
        for p in found
    ]


def test_presuppositions() -> None:
    """The presupposition split (FR-RESP-14): what the narrated action brings about is never
    checked ("puts the key in the drawer": its mentions' scene participation; "slides the key
    to Bobby": the transfer she takes part in), and what it presupposes is ("takes the key from
    the drawer": the key was there, up to the beat; "nods at Bobby, who holds the key": a
    statement about another the action does not bring about)."""
    puts = "Jenni puts the key in the drawer."
    assert (
        _presupposed(
            puts,
            _claim("entity_mention", _prop(KEY, PRESENT_AT, PARLOR), (11, 18)),
            _claim("entity_mention", _prop(DRAWER, PRESENT_AT, PARLOR), (26, 32)),
        )
        == []
    )
    slides = "Jenni slides the key to Bobby."
    assert (
        _presupposed(
            slides,
            _claim("transfer", _prop(BOB, HOLDS, KEY), giver=JEN),
            _claim("transfer", _prop(JEN, HOLDS, KEY, negated=True)),
        )
        == []
    )
    takes = "Jenni takes the key from the drawer."
    assert _presupposed(
        takes,
        _claim("possession", _prop(JEN, HOLDS, KEY), (6, 11)),
        _claim("entity_mention", _prop(DRAWER, PRESENT_AT, PARLOR), (30, 36)),
    ) == [(KEY, LOCATED_AT, DRAWER, "affirmed", (None, T))]
    nods = "Jenni nods at Bobby, who holds the key."
    assert _presupposed(nods, _claim("possession", _prop(BOB, HOLDS, KEY))) == [
        (BOB, HOLDS, KEY, "affirmed", (T, None))
    ]


def test_presuppositions__complements() -> None:
    """Taking from a character presupposes it held the item; a take-transfer presupposes its
    giver held it (once, however the text names it); a transfer between others is checked
    as said; a protected hit (a hidden record the words match) is never a presupposition."""
    from_bobby = "Jenni takes the key from Bobby."
    assert _presupposed(from_bobby, _claim("possession", _prop(JEN, HOLDS, KEY))) == [
        (BOB, HOLDS, KEY, "affirmed", (None, T))
    ]
    steals = "Jenni steals the key from Bobby."
    assert _presupposed(
        steals,
        _claim("transfer", _prop(JEN, HOLDS, KEY)),
        _claim("transfer", _prop(BOB, HOLDS, KEY, negated=True)),
    ) == [(BOB, HOLDS, KEY, "affirmed", (None, T))]
    gave = "Jenni nods at Bobby, who gave Wren the key."
    assert _presupposed(
        gave,
        _claim("transfer", _prop(WRN, HOLDS, KEY), giver=BOB),
        _claim("transfer", _prop(BOB, HOLDS, KEY, negated=True)),
    ) == [(WRN, HOLDS, KEY, "affirmed", (T, None)), (BOB, HOLDS, KEY, "negated", (T, None))]
    hidden = "Jenni puts the key in the drawer."
    assert _presupposed(hidden, _claim("protected_content", _prop(KEY, LOCATED_AT, DRAWER))) == []
    # The character's own state and the player's are never checked.
    assert _presupposed("Jenni holds the key.", _claim("possession", _prop(JEN, HOLDS, KEY))) == []


def test_notices() -> None:
    """``co_author_knowledge`` (default ``confirm``): one notice per accepted presupposition,
    the player's words only; none under ``free``; a correction's notices are not correctable.
    The notice is built from the fiat alone, so it is the same whatever canon holds."""
    assert StorySettings().co_author_knowledge == "confirm"
    assert pa.knowledge_of(StorySettings(co_author_knowledge="free")) == pa.FREE
    found = _split(pa.CO_AUTHOR, "Jenni nods at Bobby, who holds the key.")
    (narrated,) = found.narrated
    fiat = pa.Fiat(narrated.beat, "j", _prop(BOB, HOLDS, KEY), narrated.claim.text)
    found.fiats.append(fiat)
    (notice,) = found.notices(3)
    assert notice == {
        "id": fiat.notice_id,
        "kind": pa.IMPLAUSIBLE_KNOWLEDGE,
        "character_id": "j",
        "claim_text": "Jenni nods at Bobby, who holds the key.",
        "turn": 3,
        "correctable": True,
    }
    assert KEY not in str(notice)
    assert HOLDS not in str(notice)
    found.corrected = True
    assert [n["correctable"] for n in found.notices(3)] == [False]
    found.knowledge = pa.FREE
    assert found.notices(3) == ()
    assert found.report()["fiats"] == [{"beat": 2, "character_id": "j"}]
    assert found.report()["notices"] == []


def test_mover_of_co_author() -> None:
    """#598 (from #518): the ``co_author`` player's narrated ``do`` moves the character it
    narrates, when the claim states a change; never its ``say`` nor an uncued claim."""
    narrating = Asserter("co_author", "c", narrates="b")
    assert mover_of(narrating, "do", change=True) == "b"
    assert mover_of(narrating, "say", change=True) is None
    assert mover_of(narrating, "do", change=False) is None
    assert mover_of(Asserter("co_author", "c"), "do", change=True) is None
    assert narrating == Asserter("co_author", "c")  # not part of who asserts


@pytest.mark.integration
async def test_narrated_say_contradicting_beliefs(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#564's regression (FR-RESP-14): under ``co_author`` with partitions on, Jenni believing
    Bobby holds the key, the player narrates her saying Cal does. Player authority: the say is
    a ``sincere_error`` (label and repair agree: it is not repaired), the words stand, nothing
    in the turn is a hallucination, and Jenni's beliefs are unchanged. The complement: the same
    say from Jenni's own render is ``hallucination_repaired`` and repaired."""

    said = 'Jenni says, "Cal holds the key."'
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, key = await _viewpoint_room(client, engine)
        jenni = ids["Jenni"]
        await parts.believe(engine, story, jenni, _forever(ids["Bobby"], HOLDS, key))
        before = await _beliefs(engine, story, jenni)
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm."))])
        answer = await respond(
            client, story, body(ids["Cal"], said, render_character_ids=[ids["Bobby"]])
        )
        assert answer.status_code == 200, answer.text
        report = await st.turn_report(engine, story)
        assert report["player_authority"]["narrated"] == [
            {"beat": 2, "character_id": jenni, "kind": "say"}
        ]
        assert "hallucination_repaired" not in json.dumps(report)
        assert not [r for r in report.get("repairs", []) if r["beat"] == 2]
        (utterance,) = [
            u for u in await st.utterances(engine, story) if u.speaker_character_id == jenni
        ]
        assert (utterance.event_beat, utterance.classification) == (2, "sincere_error")
        (event,) = await rows(
            engine,
            "SELECT payload->>'text' AS text FROM events WHERE story_id = :s "
            "AND character_id = :c AND type = 'beat_say'",
            s=story,
            c=jenni,
        )
        assert event.text == said
        assert "Cal holds the key" in answer.json()["message"]["text"]
        assert await _beliefs(engine, story, jenni) == before

        # The complement: her own render's say has no basis, so it is repaired.
        story, ids, key = await _viewpoint_room(client, engine)
        jenni = ids["Jenni"]
        await parts.believe(engine, story, jenni, _forever(ids["Bobby"], HOLDS, key))
        stub.reset()
        stub.script(
            "generation",
            [
                st.render(st.beat("say", "Cal holds the key.", intent="sincere")),
                st.render(st.beat("say", "I couldn't say.", intent="sincere")),
            ],
        )
        answer = await respond(
            client, story, body(ids["Cal"], "Who has it?", render_character_ids=[jenni])
        )
        assert answer.status_code == 200, answer.text
        report = await st.turn_report(engine, story)
        assert "hallucination_repaired" in json.dumps(report["verify"])
        assert [r for r in report.get("repairs", []) if r["beat"] == 2]
        assert "Cal holds the key" not in answer.json()["message"]["text"]


async def _beliefs(engine: AsyncEngine, story: str, holder: str) -> list[Any]:
    found = await rows(
        engine,
        "SELECT id FROM beliefs WHERE story_id = :s AND holder_character_id = :h ORDER BY id",
        s=story,
        h=holder,
    )
    return [f.id for f in found]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _extracted(proposition: Proposition, span: tuple[int, int]) -> SimpleNamespace:
    """An extraction-layer claim of beat 2 (#651)."""
    detected = DetectedClaim(
        span=(2, *span),
        layer="extraction",
        category="extraction",  # type: ignore[arg-type]
        channel="do",
        speaker=JEN,
        proposition=proposition,
    )
    return SimpleNamespace(claim=detected, result=None)


def test_presuppositions__extraction_results() -> None:
    """#651: an extraction claim that is the action's own result is exempt like a detector's
    ("puts the key in the drawer" → the key in the drawer). Complement asserted: an extracted
    statement about another character's possession is still presupposed."""
    puts = "Jenni puts the key in the drawer."
    assert _presupposed(puts, _extracted(_prop(KEY, LOCATED_AT, DRAWER), (6, 32))) == []
    nods = "Jenni nods at Bobby, who holds the key."
    assert _presupposed(nods, _extracted(_prop(BOB, HOLDS, KEY), (14, 38))) == [
        (BOB, HOLDS, KEY, "affirmed", (T, None))
    ]
