"""S-PLAYER-AUTHORITY: own_character vs co_author (FR-RESP-11; docs/04 §7; #180).

A structured story (Cal, the player; Jenni; Bobby) and a key. The player's input: "Jenni smiles
and hands me the key."

- A story created without the setting reads ``player_authority = own_character`` (``GET
  /stories/{id}`` echoes it). The input is a **request** Jenni's render resolves: it reaches her
  render (and no one else's) under ``## The player asks of you``; she refuses ("Not a chance.")
  and the report says ``refused``; no Jenni ``do`` is recorded from the input (the ``utterances``
  table holds none, the complement), the span is stored tagged ``request`` on the
  ``player_input`` event and is not part of the player's own text, and nothing about the key is
  canon. Asked to drop the key, she complies with a ``do`` of her own render (``complied``).
- Under ``co_author`` (``PATCH /stories/{id}/settings``), the same input is recorded as Jenni's
  observable action: her ``beat_do`` at beat 2 (``narrated_by = player``, first person rewritten
  to "Cal"), checked against her viewpoint (her own view, the co-author asserter: the hand-over is
  permitted, basis ``co_author``, and canonized with provenance ``player``), her render after it.
  "Jenni thinks about betraying me." is rejected ``player_cannot_write_think``: no ``think`` row
  is ever written from the input (the complement), and the rest of the input stands. Placed in the
  kitchen while Cal is in the parlor, Jenni's narrated action is dropped (``not_present``) and
  nothing of hers is recorded.
- ``test_s_player_authority__hand_over`` (#519): asked for the key under ``own_character``,
  Jenni complies with the obvious action, "Jenni hands Cal the key." in her own render. The
  hand-over is her action (the player's character only receives it), so it survives without a
  repair and is canon.
- ``test_s_player_authority__viewpoint`` (#561, revised by #598): under ``co_author`` with
  per-character partitions, a narration breaking Jenni's viewpoint (``contradicted``,
  ``not_perceived``) is rejected and nothing is recorded; one presupposing what she could not
  know is accepted with a notice (FR-RESP-14); a narrated ``say`` her beliefs hold and canon
  contradicts is a sincere error that stands.
- ``test_s_player_authority__knowledge`` (#598, FR-RESP-14): ``co_author_knowledge``, the
  presupposition split, the ``author_fiat`` acquisition, the notices and the correction.
- ``test_s_player_authority__knowledge_inert`` (#670): ``co_author_knowledge`` is accepted and
  has no effect on a Structured story without partitions or on a Lite story.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.epistemic import replay
from epistrel.core.membership import Position
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import HOLDS, LOCATED_AT, PRESENT_AT
from epistrel.core.visibility import Visibility
from tests import partition_support as parts
from tests import perception_support as ps
from tests import structured_turn_support as st
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.event_support import rows
from tests.respond_support import body, new_story, respond, respond_client
from tests.story_support import READ, WRITE

HANDS = "Jenni smiles and hands me the key."
SKETCH = "Jenni smiles and hands Cal the key."
THINKS = "Jenni thinks about betraying me."
DROPS = "Jenni drops the key on the table."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _authority(client: Any, story: str) -> str:
    answer = await client.get(f"/stories/{story}", headers=READ)
    assert answer.status_code == 200, answer.text
    return str(answer.json()["settings"]["player_authority"])


async def _input_events(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'player_input' ORDER BY seq",
        s=story,
    )


async def _key_facts(engine: AsyncEngine, story: str, key: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.provenance::text AS provenance FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND (p.subject_id = :k OR p.object_id = :k)",
        s=story,
        k=key,
    )


async def _thinks(engine: AsyncEngine, story: str, character: str) -> int:
    found = await rows(
        engine,
        "SELECT count(*) AS n FROM events WHERE story_id = :s AND character_id = :c "
        "AND type = 'beat_think'",
        s=story,
        c=character,
    )
    return int(found[0].n)


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-11")
async def test_s_player_authority(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await st.structured_story(client, "Cal", "Jenni", "Bobby")
        async with engine.begin() as conn:
            key = await store.add_entity(conn, story, "item", "the key")
        stub = epistrel.models.stub
        cal, jenni = ids["Cal"], ids["Jenni"]
        assert await _authority(client, story) == "own_character"

        # own_character: a request Jenni's render resolves; she refuses.
        stub.script("generation", [st.render(st.beat("say", "Not a chance."))])
        answer = await respond(client, story, body(cal, "Well?", do=HANDS))
        assert answer.status_code == 200, answer.text
        (call,) = st.schema_calls(stub, "CharacterRender")
        prompt = st.text_of(call)
        assert "## You are Jenni" in prompt
        assert "## The player asks of you" in prompt
        assert SKETCH in prompt
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["mode"] == "own_character"
        assert report["requests"] == [
            {"text": HANDS, "character_id": jenni, "kind": "do", "outcome": "refused"}
        ]
        assert not [d for d in (await st.turn_report(engine, story))["verify"]["denied"]]
        (event,) = await _input_events(engine, story)
        assert "hands" not in event.payload["text"]
        assert event.payload.get("do") is None
        (request,) = event.payload["requests"]
        assert (request["tag"], request["character_id"], request["text"]) == (
            "request",
            jenni,
            HANDS,
        )
        # The complement: no Jenni ``do`` recorded from the input, nothing about the key.
        assert [
            (u.speaker_character_id, u.event_type) for u in await st.utterances(engine, story)
        ] == [(cal, "player_input"), (jenni, "beat_say")]
        assert await _key_facts(engine, story, key) == []

        # Asked something else, she complies: the action is her own render's.
        stub.reset()
        stub.script("generation", [st.render(st.beat("do", "Jenni drops the key on the table."))])
        answer = await respond(client, story, body(cal, None, do=DROPS))
        assert answer.status_code == 200, answer.text
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["requests"][0]["outcome"] == "complied"
        narrated = await rows(
            engine,
            "SELECT payload->>'narrated_by' AS by FROM events WHERE story_id = :s "
            "AND type = 'beat_do' AND character_id = :c",
            s=story,
            c=jenni,
        )
        assert [n.by for n in narrated] == [None]

        # co_author: recorded as Jenni's observable action, never her ``think``.
        patched = await client.patch(
            f"/stories/{story}/settings", json={"player_authority": "co_author"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        assert await _authority(client, story) == "co_author"
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "There. Happy?"))])
        answer = await respond(
            client, story, body(cal, "Thanks.", do=f"I hold out my hand. {HANDS} {THINKS}")
        )
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        assert "## The player asks of you" not in st.text_of(
            st.schema_calls(stub, "CharacterRender")[0]
        )
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["narrated"] == [{"beat": 2, "character_id": jenni, "kind": "do"}]
        assert report["rejected"] == [
            {"text": THINKS, "character_id": jenni, "code": "player_cannot_write_think"}
        ]
        found = await rows(
            engine,
            "SELECT beat, payload FROM events WHERE story_id = :s AND type = 'beat_do' "
            "AND character_id = :c AND payload ? 'narrated_by' ORDER BY seq",
            s=story,
            c=jenni,
        )
        assert [(f.beat, f.payload["narrated_by"], f.payload["text"]) for f in found] == [
            (2, "player", SKETCH)
        ]
        assert [b["beat"] for b in turn["beats"] if b["kind"] == "beat_say"][-1] == 3
        verdicts = (await st.turn_report(engine, story))["verify"]
        assert not [d for d in verdicts["denied"] if d["beat"] == 2]
        assert "permitted:co_author" in {v.get("permission") for v in verdicts["verdicts"]}
        assert {f.provenance for f in await _key_facts(engine, story, key)} == {"player"}
        (*_, event) = await _input_events(engine, story)
        assert event.payload["do"] == "I hold out my hand."
        assert "requests" not in event.payload
        # The complement: player input never writes her ``think``.
        assert await _thinks(engine, story, jenni) == 0

        # Not in the room: her narrated action is dropped, nothing of hers recorded.
        parlor = await ps.place_entity(engine, story, "parlor")
        kitchen = await ps.place_entity(engine, story, "kitchen")
        await ps.put(engine, story, cal, parlor)
        await ps.put(engine, story, ids["Bobby"], parlor)
        await ps.put(engine, story, jenni, kitchen)
        before = len(await st.utterances(engine, story))
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm?"))])
        answer = await respond(
            client,
            story,
            body(cal, None, do=HANDS, render_character_ids=[ids["Bobby"]]),
        )
        assert answer.status_code == 200, answer.text
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["narrated"] == []
        assert report["rejected"] == [{"text": HANDS, "character_id": jenni, "code": "not_present"}]
        made = (await st.utterances(engine, story))[before:]
        assert jenni not in {u.speaker_character_id for u in made}


GIVES = "Jenni hands Cal the key."


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-11")
async def test_s_player_authority__hand_over(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#519: under ``own_character``, asked for the key, Jenni complies with the obvious action,
    handing it over in her own render. The hand-over is her observable action (the player's
    character only receives the key), so it survives without a repair: nothing is denied, the
    beat is recorded as rendered, and the transfer is canon (provenance ``render``)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await st.structured_story(client, "Cal", "Jenni", "Bobby")
        async with engine.begin() as conn:
            key = await store.add_entity(conn, story, "item", "the key")
        stub = epistrel.models.stub
        cal, jenni = ids["Cal"], ids["Jenni"]
        assert await _authority(client, story) == "own_character"
        stub.script("generation", [st.render(st.beat("do", GIVES))])
        answer = await respond(client, story, body(cal, None, do=HANDS))
        assert answer.status_code == 200, answer.text
        report = await st.turn_report(engine, story)
        assert report["player_authority"]["requests"][0]["outcome"] == "complied"
        assert report["verify"]["denied"] == []
        assert "repairs" not in report  # no repair ran (the key is absent then)
        assert {v["permission"] for v in report["verify"]["verdicts"]} == {"permitted:own"}
        done = await rows(
            engine,
            "SELECT payload->>'text' AS text FROM events WHERE story_id = :s "
            "AND type = 'beat_do' AND character_id = :c",
            s=story,
            c=jenni,
        )
        assert [d.text for d in done] == [GIVES]
        assert {f.provenance for f in await _key_facts(engine, story, key)} == {"render"}
        held = await rows(
            engine,
            "SELECT e.character_id, p.polarity::text AS polarity FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id "
            "JOIN entities e ON e.id = p.subject_id AND e.story_id = f.story_id "
            "WHERE f.story_id = :s AND p.object_id = :k",
            s=story,
            k=key,
        )
        assert {(h.character_id, h.polarity) for h in held} == {
            (cal, "affirmed"),
            (jenni, "negated"),
        }


NODS = "Jenni nods at Bobby, who holds the key."
CLAIMS = 'Jenni says, "Bobby holds the key."'


async def _viewpoint_room(
    client: Any, engine: AsyncEngine, *, holder: str | None = None, away: bool = False
) -> tuple[str, dict[str, str], str]:
    """A ``co_author`` story under per-character partitions at perception ``full``, everyone in
    the parlor (Jenni placed in the kitchen but ``present_at`` the parlor when ``away``: in the
    scene, out of sight), and the key, which objective canon says ``holder`` holds."""
    story, ids = await st.structured_story(client, "Cal", "Jenni", "Bobby")
    await ps.perceive(engine, story, "full")
    parlor = await ps.place_entity(engine, story, "parlor")
    kitchen = await ps.place_entity(engine, story, "kitchen")
    for name, cid in ids.items():
        await ps.put(engine, story, cid, kitchen if away and name == "Jenni" else parlor)
    if away:
        await commit(
            engine,
            story,
            _forever(ids["Jenni"], PRESENT_AT, parlor),
            kind=OperationKind.CONTROL,
            provenance="author",
        )
    async with engine.begin() as conn:
        key = await store.add_entity(conn, story, "item", "the key")
    if holder is not None:
        await commit(
            engine,
            story,
            _forever(ids[holder], HOLDS, key),
            kind=OperationKind.CONTROL,
            provenance="author",
        )
    patched = await client.patch(
        f"/stories/{story}/settings", json={"player_authority": "co_author"}, headers=WRITE
    )
    assert patched.status_code == 200, patched.text
    return story, ids, key


def _forever(subject: str, predicate: str, obj: str) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=Interval.of(None, None),
    )


async def _player_facts(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT id FROM facts WHERE story_id = :s AND provenance = 'player'",
        s=story,
    )


async def _rejected(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'render_rejected' ORDER BY seq",
        s=story,
    )


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-11")
async def test_s_player_authority__viewpoint(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#561, the complement of AC3: under ``co_author`` with per-character partitions, a
    narration that breaks Jenni's viewpoint is rejected (a rung-1 ``render_rejected``, no model
    call) and nothing of it is recorded (no Jenni beat or utterance, no ``player`` fact):

    - ``contradicted``: Jenni handing over a key canon says Bobby holds;
    - ``not_perceived``: Jenni in the scene (``present_at`` the parlor) but placed in the
      kitchen, out of sight of the player's input (#161).

    #598 (FR-RESP-14, ADR-174) revised the third row: Jenni nodding at Bobby "who holds the
    key", which nothing gave her a path to (#165), is no longer rejected ``could_not_know``. It
    commits, as her narrated ``do``, with one ``implausible_knowledge`` notice holding the
    player's words.

    The narrated ``say``'s sincere error: Jenni believes Bobby holds the key, canon says Cal
    does, and the player narrates her saying so. Her ``beat_say`` stands, labelled
    ``sincere_error`` by #166, and is neither rejected nor repaired."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for do, holder, away, code in (
            (HANDS, "Bobby", False, "contradicted"),
            (HANDS, None, True, "not_perceived"),
        ):
            story, ids, _ = await _viewpoint_room(client, engine, holder=holder, away=away)
            jenni = ids["Jenni"]
            stub.reset()
            stub.script("generation", [st.render(st.beat("say", "Hm."))])
            answer = await respond(
                client, story, body(ids["Cal"], None, do=do, render_character_ids=[ids["Bobby"]])
            )
            assert answer.status_code == 200, answer.text
            report = (await st.turn_report(engine, story))["player_authority"]
            assert report["narrated"] == [], code
            (rejected,) = report["rejected"]
            assert (rejected["character_id"], rejected["code"]) == (jenni, code)
            (event,) = await _rejected(engine, story)
            assert event.payload["rung"] == 1
            assert event.payload["constraints"] == [{"kind": "player_authority", "reason": code}]
            assert len(st.schema_calls(stub, "CharacterRender")) == 1  # Bobby's only
            assert jenni not in {u.speaker_character_id for u in await st.utterances(engine, story)}
            jenni_beats = await rows(
                engine,
                "SELECT type FROM events WHERE story_id = :s AND character_id = :c "
                "AND type LIKE 'beat_%'",
                s=story,
                c=jenni,
            )
            assert jenni_beats == [], code
            assert await _player_facts(engine, story) == [], code
            assert "the key" not in answer.json()["message"]["text"], code
            assert "notices" not in answer.json(), code

        # #598: what Jenni could not know is accepted, with a notice; nothing is rejected.
        story, ids, _ = await _viewpoint_room(client, engine)
        jenni = ids["Jenni"]
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm."))])
        answer = await respond(
            client, story, body(ids["Cal"], None, do=NODS, render_character_ids=[ids["Bobby"]])
        )
        assert answer.status_code == 200, answer.text
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["narrated"] == [{"beat": 2, "character_id": jenni, "kind": "do"}]
        assert report["rejected"] == []
        assert await _rejected(engine, story) == []
        (notice,) = answer.json()["notices"]
        assert (notice["kind"], notice["character_id"], notice["claim_text"]) == (
            "implausible_knowledge",
            jenni,
            NODS,
        )

        # The narrated say's sincere error stands, labelled.
        story, ids, key = await _viewpoint_room(client, engine, holder="Cal")
        jenni = ids["Jenni"]
        await parts.believe(engine, story, jenni, _forever(ids["Bobby"], HOLDS, key))
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm."))])
        answer = await respond(
            client, story, body(ids["Cal"], CLAIMS, render_character_ids=[ids["Bobby"]])
        )
        assert answer.status_code == 200, answer.text
        report = await st.turn_report(engine, story)
        assert report["player_authority"]["narrated"] == [
            {"beat": 2, "character_id": jenni, "kind": "say"}
        ]
        assert report["player_authority"]["rejected"] == []
        assert await _rejected(engine, story) == []
        assert not [r for r in report.get("repairs", []) if r["beat"] == 2]
        said = [
            (u.event_type, u.event_beat, u.classification)
            for u in await st.utterances(engine, story)
            if u.speaker_character_id == jenni
        ]
        assert said == [("beat_say", 2, "sincere_error")]
        (narrated,) = await rows(
            engine,
            "SELECT payload->>'narrated_by' AS by FROM events WHERE story_id = :s "
            "AND character_id = :c AND type = 'beat_say'",
            s=story,
            c=jenni,
        )
        assert narrated.by == "player"


PUTS = "Jenni puts the key in the drawer."
TAKES = "Jenni takes the key from the drawer."
SEARCHES = "Jenni searches the desk."
KNOWS = "Bobby holds the key."


async def _knowledge_room(
    client: Any, engine: AsyncEngine, *, canon: str | None = None, knowledge: str | None = None
) -> tuple[str, dict[str, str], str, str]:
    """``_viewpoint_room`` (partitions on, ``co_author``) with a drawer, and what only Bobby
    knows, if anything: the key in the drawer (``canon = "drawer"``, he hid it there) or in his
    hands (``"bobby"``), scoped to him, or in Cal's (``"cal"``), scoped to Cal. ``knowledge``
    sets ``co_author_knowledge``."""
    story, ids, key = await _viewpoint_room(client, engine)
    drawer = await ps.place_entity(engine, story, "drawer")
    hidden = Visibility(scope="characters", characters=(ids["Bobby"],))
    if canon == "drawer":
        await commit(
            engine,
            story,
            _forever(key, LOCATED_AT, drawer),
            kind=OperationKind.CONTROL,
            provenance="author",
            visibility=hidden,
        )
    elif canon in ("bobby", "cal"):
        # "cal": the key is Cal's, and only Cal knows it (a narration about Bobby holding it is
        # contradicted by canon hidden from Jenni, #169's hidden conflict).
        holder = ids["Bobby"] if canon == "bobby" else ids["Cal"]
        await commit(
            engine,
            story,
            _forever(holder, HOLDS, key),
            kind=OperationKind.CONTROL,
            provenance="author",
            visibility=Visibility(scope="characters", characters=(holder,)),
        )
    if knowledge is not None:
        patched = await client.patch(
            f"/stories/{story}/settings", json={"co_author_knowledge": knowledge}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
    return story, ids, key, drawer


async def _narrate(client: Any, stub: Any, story: str, ids: dict[str, str], do: str) -> Any:
    """The player (Cal) narrates ``do``; Bobby renders "Hm."."""
    stub.reset()
    stub.script("generation", [st.render(st.beat("say", "Hm."))])
    return await respond(
        client, story, body(ids["Cal"], None, do=do, render_character_ids=[ids["Bobby"]])
    )


async def _fiats(engine: AsyncEngine, story: str) -> list[tuple[str, str, str | None, str]]:
    """Every ``author_fiat`` acquisition: holder, subject, object and polarity."""
    found = await rows(
        engine,
        "SELECT t.holder_character_id AS holder, p.subject_id, p.object_id, "
        "p.polarity::text AS polarity FROM belief_transitions t "
        "JOIN beliefs b ON b.id = t.belief_id JOIN propositions p ON p.id = b.proposition_id "
        "WHERE t.story_id = :s AND t.kind = 'author_fiat' ORDER BY t.id",
        s=story,
    )
    return [(str(f.holder), str(f.subject_id), f.object_id, f.polarity) for f in found]


def _masked(notice: dict[str, Any]) -> dict[str, Any]:
    """A notice with its ids masked (each story has its own)."""
    return {**notice, "id": "<id>", "character_id": "<id>"}


async def _speech_of(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    report = await st.turn_report(engine, story)
    return [v["speech"] for v in report["verify"]["verdicts"] if "speech" in v]


async def _turn_of(engine: AsyncEngine, story: str) -> Any:
    (row,) = await rows(
        engine,
        "SELECT attempts, config_revisions FROM operations WHERE story_id = :s "
        "AND kind = 'turn' ORDER BY narrative_turn DESC LIMIT 1",
        s=story,
    )
    return row


@pytest.mark.integration
@pytest.mark.part("M6", "FR-RESP-14")
async def test_s_player_authority__knowledge(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#598, S-PLAYER-AUTHORITY's M6 part (ADR-174): partitions on, ``co_author``, Jenni never
    saw Bobby hide the key in the drawer.

    Under the default ``co_author_knowledge = confirm`` (``GET /stories/{id}`` echoes it):

    - "Jenni puts the key in the drawer." raises no notice: the action brings the key's place
      about, so nothing about it is checked against what she knew;
    - "Jenni takes the key from the drawer." commits, records ``located_at(key, drawer)`` as
      Jenni's ``author_fiat`` acquisition and returns one ``implausible_knowledge`` notice holding
      only the player's words, stored with the turn (the history read returns it). A story
      whose canon does not settle where the key was gives the same notice apart from ids (no
      oracle);
    - "Jenni nods at Bobby, who holds the key." raises a notice; Jenni's next render saying so
      is verified as knowing it (sincere and belief-consistent, so not repaired as a
      hallucination: only #144's permission check, Bobby's secret, may still steer it), where
      without the narration it is a hallucination;
    - correcting the "takes" turn with "Jenni searches the desk." replays it in one operation:
      the original is dormant and swaps back; the replacement cannot be corrected again
      (``already_corrected``); once a later turn commits, correcting is ``not_latest_turn``
      and changes nothing.

    Under ``free`` the same inputs commit with no notices (the fiats are written). Under both,
    narration contradicting author-locked canon is still rejected (``contradicted``) and player
    input never writes Jenni's ``think``. Under ``own_character`` the setting is inert.
    Changing ``co_author_knowledge`` while a turn is held makes it fail its re-check and
    regenerate under the new value."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- puts: the action's own result, no notice, no fiat.
        story, ids, key, drawer = await _knowledge_room(client, engine, canon="drawer")
        answer = await client.get(f"/stories/{story}", headers=READ)
        assert answer.json()["settings"]["co_author_knowledge"] == "confirm"
        answer = await _narrate(client, stub, story, ids, PUTS)
        assert answer.status_code == 200, answer.text
        assert "notices" not in answer.json()
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["narrated"] == [{"beat": 2, "character_id": ids["Jenni"], "kind": "do"}]
        assert report["rejected"] == []
        assert await _fiats(engine, story) == []

        # -- takes: accepted, the fiat written, one notice of the player's words.
        notices: list[dict[str, Any]] = []
        for canon in ("drawer", None):
            story, ids, key, drawer = await _knowledge_room(client, engine, canon=canon)
            jenni = ids["Jenni"]
            answer = await _narrate(client, stub, story, ids, TAKES)
            assert answer.status_code == 200, answer.text
            turn = answer.json()
            (notice,) = turn["notices"]
            assert notice == {
                "id": notice["id"],
                "kind": "implausible_knowledge",
                "character_id": jenni,
                "claim_text": TAKES,
                "turn": turn["turn"],
                "correctable": True,
                "external_effects": False,
            }
            notices.append(_masked(notice))
            report = (await st.turn_report(engine, story))["player_authority"]
            assert report["narrated"] == [{"beat": 2, "character_id": jenni, "kind": "do"}]
            assert report["rejected"] == []
            assert await _fiats(engine, story) == [(jenni, key, drawer, "affirmed")]
            (event,) = await _input_events(engine, story)
            assert event.payload["notices"] == [
                {k: v for k, v in notice.items() if k != "external_effects"}
            ]
            read = await client.get(
                f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
            )
            assert read.status_code == 200, read.text
            (summary,) = read.json()["turns"]
            assert summary["notices"] == [notice]
            # Her knowledge of where the key was, from the fiat on (#165's replay).
            async with engine.connect() as conn:
                known = await replay.could_have_known(
                    conn,
                    story,
                    jenni,
                    _before(key, LOCATED_AT, drawer),
                    Position(turn["turn"] + 1, 0, 0, 1),
                )
            assert isinstance(known, replay.Known), canon
        # No oracle: canon settling it or not, the same notice apart from ids.
        assert notices[0] == notices[1]

        # -- correct the "takes" turn (the canon-unsettled story's).
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm."))])
        corrected = await client.post(
            f"/stories/{story}/correct",
            json={"turn": 1, "player_input": {"do": SEARCHES}},
            headers=WRITE,
        )
        assert corrected.status_code == 200, corrected.text
        fixed = corrected.json()
        assert (fixed["turn"], fixed["variant"], fixed["previous_variant"]) == (1, 2, 1)
        assert "notices" not in fixed
        found = await client.get(f"/stories/{story}/variants", headers=READ)
        chains = {(c["turn"], c["variant"]): c["membership"] for c in found.json()["items"]}
        assert chains == {(1, 1): "dormant", (1, 2): "active"}
        (*_, replaced) = await _input_events(engine, story)
        assert replaced.payload["do"] is None  # the narration only
        assert replaced.payload["corrected_from"]
        # A correction cannot itself be corrected.
        again = await client.post(
            f"/stories/{story}/correct",
            json={"turn": 1, "player_input": {"do": PUTS}},
            headers=WRITE,
        )
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "already_corrected"
        # The original swaps back, with its notice (correctable again: it is no correction).
        swapped = await client.post(
            f"/stories/{story}/swap",
            json={"to_variant_chain": {"turn": 1, "variant": 1}},
            headers=WRITE,
        )
        assert swapped.status_code == 200, swapped.text
        read = await client.get(
            f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
        )
        (summary,) = read.json()["turns"]
        assert [_masked(n) for n in summary["notices"]] == [notices[1]]
        # A later turn: the turn is no longer the latest, and nothing changes.
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Well."))])
        later = await respond(
            client, story, body(ids["Cal"], "Well?", render_character_ids=[ids["Bobby"]])
        )
        assert later.status_code == 200, later.text
        revision = later.json()["timeline_revision"]
        refused = await client.post(
            f"/stories/{story}/correct",
            json={"turn": 1, "player_input": {"do": SEARCHES}},
            headers=WRITE,
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "not_latest_turn"
        found = await client.get(f"/stories/{story}", headers=READ)
        assert found.json()["timeline_revision"] == revision
        found = await client.get(f"/stories/{story}/variants", headers=READ)
        assert {(c["turn"], c["variant"]) for c in found.json()["items"]} == {(1, 1), (1, 2)}

        # -- nods: a notice; her next render is verified as knowing it.
        story, ids, key, _ = await _knowledge_room(client, engine, canon="bobby")
        jenni = ids["Jenni"]
        answer = await _narrate(client, stub, story, ids, NODS)
        assert answer.status_code == 200, answer.text
        (notice,) = answer.json()["notices"]
        assert (notice["character_id"], notice["claim_text"]) == (jenni, NODS)
        assert await _fiats(engine, story) == [(jenni, ids["Bobby"], key, "affirmed")]
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", KNOWS, intent="sincere"))])
        answer = await respond(
            client, story, body(ids["Cal"], "Who has it?", render_character_ids=[jenni])
        )
        assert answer.status_code == 200, answer.text
        speech = await _speech_of(engine, story)
        assert {(s["classification"], s["belief_relation"]) for s in speech} == {
            ("sincere", "consistent")
        }
        # Only the permission check (Bobby's secret, #144) may still steer it, never #166.
        report = await st.turn_report(engine, story)
        assert {r["tag"] for r in report.get("repairs", [])} <= {"unpermitted"}
        # The complement: with no narration, the same render has no basis.
        story, ids, key, _ = await _knowledge_room(client, engine, canon="bobby")
        stub.reset()
        stub.script(
            "generation",
            [
                st.render(st.beat("say", KNOWS, intent="sincere")),
                st.render(st.beat("say", "I couldn't say.", intent="sincere")),
            ],
        )
        answer = await respond(
            client, story, body(ids["Cal"], "Who has it?", render_character_ids=[ids["Jenni"]])
        )
        assert answer.status_code == 200, answer.text
        assert "hallucination_repaired" in {
            s["classification"] for s in await _speech_of(engine, story)
        }

        # -- free: the same inputs commit with no notices (the fiats are written).
        for do, canon in ((TAKES, "drawer"), (NODS, "bobby")):
            story, ids, _, _ = await _knowledge_room(client, engine, canon=canon, knowledge="free")
            answer = await _narrate(client, stub, story, ids, do)
            assert answer.status_code == 200, answer.text
            assert "notices" not in answer.json(), do
            report = (await st.turn_report(engine, story))["player_authority"]
            assert report["rejected"] == [], do
            assert len(await _fiats(engine, story)) == 1, do

        # -- under both: locked canon still wins, and no ``think`` is ever written.
        for knowledge in ("confirm", "free"):
            story, ids, key, _ = await _knowledge_room(client, engine, knowledge=knowledge)
            await commit(
                engine,
                story,
                _forever(ids["Bobby"], HOLDS, key),
                kind=OperationKind.CONTROL,
                provenance="author",
                author_locked=True,
            )
            answer = await _narrate(client, stub, story, ids, f"{HANDS} {THINKS}")
            assert answer.status_code == 200, answer.text
            assert "notices" not in answer.json(), knowledge
            report = (await st.turn_report(engine, story))["player_authority"]
            assert sorted(r["code"] for r in report["rejected"]) == [
                "contradicted",
                "player_cannot_write_think",
            ], knowledge
            assert await _thinks(engine, story, ids["Jenni"]) == 0
            assert await _fiats(engine, story) == []

        # -- own_character: the setting is inert (a request, no notice, no fiat).
        story, ids, _, _ = await _knowledge_room(client, engine, canon="bobby")
        patched = await client.patch(
            f"/stories/{story}/settings", json={"player_authority": "own_character"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        answer = await _narrate(client, stub, story, ids, NODS)
        assert answer.status_code == 200, answer.text
        assert "notices" not in answer.json()
        report = (await st.turn_report(engine, story))["player_authority"]
        assert [r["outcome"] for r in report["requests"]] == ["unresolved"]
        assert await _fiats(engine, story) == []

        # -- a change of the setting while the turn runs: re-checked, regenerated under it.
        story, ids, _, _ = await _knowledge_room(client, engine, canon="bobby")
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm."))] * 2)
        latch = stub.hold("generation")
        task = asyncio.ensure_future(
            respond(
                client,
                story,
                body(ids["Cal"], None, do=NODS, render_character_ids=[ids["Bobby"]]),
            )
        )
        await asyncio.wait_for(latch.entered.wait(), 5)
        patched = await client.patch(
            f"/stories/{story}/settings", json={"co_author_knowledge": "free"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        latch.release()
        answer = await task
        assert answer.status_code == 200, answer.text
        row = await _turn_of(engine, story)
        assert "notices" not in answer.json()
        assert [(a["outcome"], a.get("recheck")) for a in row.attempts] == [
            ("recheck", "failed"),
            ("commit", None),
        ]
        assert "co_author_knowledge" in row.attempts[0]["config_changed"]
        assert row.config_revisions["co_author_knowledge"] == "free"
        (event,) = await _input_events(engine, story)
        assert "notices" not in event.payload
        assert len(await _fiats(engine, story)) == 1


def _before(subject: str, predicate: str, obj: str) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=Interval.of(None, None),
    )


async def _facts_about(engine: AsyncEngine, story: str, key: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.provenance::text AS provenance, p.subject_id FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND p.object_id = :k ORDER BY f.id",
        s=story,
        k=key,
    )


@pytest.mark.integration
@pytest.mark.part("M6", "FR-RESP-14")
async def test_s_player_authority__knowledge_hidden(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#598 (FR-RESP-14, NFR-PRIV-4): no oracle on hidden canon. "Jenni nods at Bobby, who holds
    the key." in a story where nothing settles who holds it, and in one where canon hidden
    from Jenni says Cal does (only Cal knows): the outward result is identical, under
    ``confirm`` (the beat narrated, nothing rejected, one notice of the player's words, the
    same message, no rejection record) and under ``free`` (no notice), and Jenni's
    ``author_fiat`` is written in both. Where canon is hidden the claim is never canonized over
    it: Cal's fact stands alone."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for knowledge in ("confirm", "free"):
            outward: list[Any] = []
            for canon in (None, "cal"):
                story, ids, key, _ = await _knowledge_room(
                    client, engine, canon=canon, knowledge=knowledge
                )
                jenni = ids["Jenni"]
                answer = await _narrate(client, stub, story, ids, NODS)
                assert answer.status_code == 200, answer.text
                turn = answer.json()
                report = (await st.turn_report(engine, story))["player_authority"]
                outward.append(
                    (
                        [_masked(n) for n in turn.get("notices", [])],
                        [{**n, "character_id": "<id>"} for n in report["narrated"]],
                        report["rejected"],
                        turn["message"]["text"],
                        await _rejected(engine, story),
                    )
                )
                assert await _fiats(engine, story) == [(jenni, ids["Bobby"], key, "affirmed")]
                if canon == "cal":
                    found = await _facts_about(engine, story, key)
                    assert [(f.provenance, str(f.subject_id)) for f in found] == [
                        ("author", ids["Cal"])
                    ], knowledge
            assert outward[0] == outward[1], knowledge
            assert len(outward[0][0]) == (1 if knowledge == "confirm" else 0)
            assert outward[0][2] == []


@pytest.mark.integration
@pytest.mark.part("M6", "FR-RESP-14")
async def test_s_player_authority__knowledge_inert(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#670 (#598, FR-RESP-14): ``co_author_knowledge`` is accepted and inert where it does not
    apply. Under ``co_author``, "Jenni takes the key from the drawer." is narrated with the
    setting unset, ``confirm`` and ``free``: (a) on a Structured story without partitions and
    (b) on a Lite story. Each PATCH is 200, and every run returns no notice, writes no
    ``author_fiat`` acquisition and gives the same outcome as the unset run."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- (a) Structured, partitions off: the narrated action is recorded, nothing else.
        outcomes: list[Any] = []
        for knowledge in (None, "confirm", "free"):
            story, ids, _key, _drawer = await _knowledge_room(client, engine, knowledge=knowledge)
            await ps.perceive(engine, story, "none")
            answer = await _narrate(client, stub, story, ids, TAKES)
            assert answer.status_code == 200, answer.text
            assert "notices" not in answer.json(), knowledge
            assert await _fiats(engine, story) == [], knowledge
            (event,) = await _input_events(engine, story)
            assert not event.payload.get("notices"), knowledge
            report = (await st.turn_report(engine, story))["player_authority"]
            outcomes.append(
                (
                    report["narrated"] == [{"beat": 2, "character_id": ids["Jenni"], "kind": "do"}],
                    report["rejected"],
                    answer.json()["message"]["text"],
                )
            )
        assert outcomes[0][0] is True
        assert outcomes[1:] == [outcomes[0], outcomes[0]]

        # -- (b) Lite: the setting is accepted and never read.
        lite: list[Any] = []
        for knowledge in (None, "confirm", "free"):
            story, ids = await new_story(client, "Cal", "Jenni", "Bobby")
            settings: dict[str, Any] = {"player_authority": "co_author"}
            if knowledge is not None:
                settings["co_author_knowledge"] = knowledge
            patched = await client.patch(f"/stories/{story}/settings", json=settings, headers=WRITE)
            assert patched.status_code == 200, patched.text
            stub.reset()
            answer = await respond(client, story, body(ids["Cal"], None, do=TAKES))
            assert answer.status_code == 200, answer.text
            turn = answer.json()
            assert "notices" not in turn, knowledge
            assert await _fiats(engine, story) == [], knowledge
            lite.append((turn["message"]["text"], turn["call_counts"]))
        assert lite[1:] == [lite[0], lite[0]]
