"""C-12: the output viewpoint contract (FR-RESP-12, FR-AUTH-2) [M2] (#59); the structured
pipeline's part (FR-RESP-8,12) [M4] (#152); the audience filter's output clauses and the
``player_view`` setting (FR-RESP-12,13) [M5] (#177), and the M5 read rules on the inspection reads
and history (``test_c_12__m5_reads``, #178).

Every respond and history read is produced for one output viewpoint, resolved the same way
over REST and in the library: ``test_c_12`` and ``test_c_12__structured`` run every check
through both surfaces (the ``surface`` fixture in ``conftest.py``; NFR-SEC-5's parity half).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.settings_schema import StorySettings
from epistrel.core.viewpoint import PRIVATE_CHANNELS, validate_default_viewpoint
from tests import perception_support as ps
from tests.conformance.conftest import Surface
from tests.respond_support import FIRST, PLAYER, SECOND, body
from tests.story_support import WRITE, character, count, story_body
from tests.structured_turn_support import beat, render, structured_story, turn_report

PRIVATE_TEXT = "C12-PRIVATE-BEAT"
AUDIT_REASON = "c12-audit-record"
OPERATIONS = "SELECT count(*) FROM operations WHERE story_id = :s"


async def _story(surface: Surface, **fields: Any) -> tuple[str, dict[str, str]]:
    created = (await surface.create_story(story_body(PLAYER, FIRST, SECOND, **fields))).json
    return created["story_id"], {c["name"]: c["id"] for c in created["characters"]}


async def _private_records(engine: AsyncEngine, story_id: str, character_id: str) -> None:
    """A ``think`` beat and an audit record at turn 1, inserted directly (Lite never writes
    either; the filter's code path is exercised by this fixture)."""
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.MAINTENANCE, principal="p-test", turn=1
    ) as op:
        op.append(
            "think", {"character_id": character_id, "text": PRIVATE_TEXT}, character_id=character_id
        )
        op.append("guardrail_output_rejected", {"text": "withheld", "reason": AUDIT_REASON})
        await op.commit()


def _vp(character_id: str) -> str:
    return f"character:{character_id}"


def _no_filtering(result: dict[str, Any]) -> bool:
    """The result states perception ``none``: no audience filtering applies (M2)."""
    return bool(result["audience_filter"] == "none")


def _private_items(result: dict[str, Any]) -> list[tuple[str, str | None, Any]]:
    """The private-channel items of an omniscient read: text, character, true ages."""
    return [
        (i["text"], i["actor_character_id"], i["true_ages"])
        for i in result["items"]
        if i["channel"] in PRIVATE_CHANNELS
    ]


@pytest.mark.integration
@pytest.mark.part("M2", "FR-RESP-12; FR-AUTH-2")
async def test_c_12(surface: Surface, engine: AsyncEngine) -> None:
    """Echo; defaults (actor, then the story's default); an ``omniscient`` default refused;
    ``omniscient`` only when named; a key reused for another viewpoint is a conflict with no
    cached result; audit reads without ``omniscient`` refused; two principals get the same
    result; a Lite character viewpoint gets the whole message. Run over REST and the library.

    Complement asserted: with ``omniscient`` named, the audit records **are** returned (and the
    private beat, labelled by its channel, and the true ages)."""
    story, ids = await _story(surface)
    player, first, second = ids[PLAYER], ids[FIRST], ids[SECOND]

    # Echo and the actor default; a Lite character viewpoint returns the whole message.
    played = (await surface.respond(story, body(player, "Who's there?", do="knocks"))).json
    assert played["output_viewpoint"] == _vp(player)
    assert _no_filtering(played)
    assert [(b["channel"], b["text"]) for b in played["beats"]] == [
        ("say", "Who's there?"),
        ("do", "knocks"),
        ("message", played["message"]["text"]),
    ]
    assert all("true_ages" not in b for b in played["beats"])
    as_first = (await surface.respond(story, body(first, "Only me."))).json
    assert as_first["output_viewpoint"] == _vp(first)
    named = (await surface.respond(story, body(player, "Hm.", output_viewpoint=_vp(second)))).json
    assert named["output_viewpoint"] == _vp(second)
    via_query = (await surface.respond(story, body(player, "Hm?"), viewpoint="omniscient")).json
    assert via_query["output_viewpoint"] == "omniscient"
    assert {b["true_ages"][player] for b in via_query["beats"] if b["channel"] != "message"} == {20}

    # History: the story's default (the sole player), the actor, the named viewpoint.
    history = (await surface.history(story)).json
    assert history["output_viewpoint"] == _vp(player)
    assert _no_filtering(history)
    assert (await surface.history(story, actor_character_id=first)).json["output_viewpoint"] == _vp(
        first
    )

    # Unresolved: no name, no actor, no default -> 422 with nothing written.
    cast = [character(n, 30) for n in (PLAYER, FIRST)]
    bare = (await surface.create_story({"name": "No default", "characters": cast})).json
    bare_id = bare["story_id"]
    assert (await surface.history(bare_id)).code == "viewpoint_unresolved"
    before = await count(engine, OPERATIONS, s=bare_id)
    actor = bare["characters"][0]["id"]
    wrong = await surface.respond(bare_id, body(actor, output_viewpoint="character:nobody"))
    assert (wrong.status, wrong.code) == (422, "viewpoint_unresolved")
    malformed = await surface.respond(bare_id, body(actor), viewpoint="everyone")
    assert malformed.code == "viewpoint_unresolved"
    assert await count(engine, OPERATIONS, s=bare_id) == before

    # An omniscient default is refused at creation and by every settings write (#63's PATCH).
    refused = await surface.create_story(
        story_body(PLAYER, settings={"default_output_viewpoint": "omniscient"})
    )
    assert (refused.status, refused.code) == (422, "validation_error")
    with pytest.raises(ValueError, match="must be a character"):
        StorySettings.model_validate({"default_output_viewpoint": "omniscient"})
    with pytest.raises(InvalidRequest):
        validate_default_viewpoint("omniscient", ids.values())

    # Omniscient only when named: the private beat and the audit records.
    await _private_records(engine, story, first)
    for read in (await surface.history(story), await surface.history(story, viewpoint=_vp(first))):
        assert PRIVATE_TEXT not in str(read.json)
        assert "audit" not in read.json
        assert all("true_ages" not in i for i in read.json["items"])
    denied = await surface.history(story, viewpoint=_vp(first), include_audit=True)
    assert (denied.status, denied.code) == (403, "omniscient_required")
    assert (await surface.history(story, viewpoint="bogus", include_audit=True)).code == (
        "viewpoint_unresolved"
    )
    omniscient = (await surface.history(story, viewpoint="omniscient", include_audit=True)).json
    assert _private_items(omniscient) == [(PRIVATE_TEXT, first, {first: 21})]
    assert [(a["kind"], AUDIT_REASON in a["text"]) for a in omniscient["audit"]] == [
        ("guardrail_output_rejected", True)
    ]

    # A key reused for another viewpoint: 409, no cached result, nothing written.
    first_try = await surface.respond(story, body(player, "Again?"), key="c12-key")
    assert first_try.status == 200
    before = await count(engine, OPERATIONS, s=story)
    conflict = await surface.respond(
        story, body(player, "Again?"), key="c12-key", viewpoint="omniscient"
    )
    assert (conflict.status, conflict.code, conflict.body) == (409, "idempotency_conflict", None)
    assert await count(engine, OPERATIONS, s=story) == before
    same = await surface.respond(
        story, body(player, "Again?"), key="c12-key", viewpoint=_vp(player)
    )
    assert same.code != "idempotency_conflict"

    # No outcome depends on the principal.
    one = await surface.history(story, principal="u-one", viewpoint="omniscient")
    two = await surface.history(story, principal="u-two", viewpoint="omniscient")
    assert one == two

    # True ages only for an omniscient story read.
    characters = (await surface.get_story(story, viewpoint="omniscient")).json["characters"]
    assert [c["true_age"] for c in characters] == [20, 21, 22]
    for viewpoint in (None, _vp(first)):
        shown = (await surface.get_story(story, viewpoint=viewpoint)).json["characters"]
        assert all("true_age" not in c for c in shown)
    assert (await surface.get_story(story, viewpoint="character:nobody")).code == (
        "viewpoint_unresolved"
    )


THOUGHT = "He must never learn about the lake."
LATENT = "The boathouse key is under the third plank."
LINE = "Hello, Bobby. Sit with me."
ACTION = "She sits down by the fire and warms her hands."
COMPOSITION = "composition.j2"


def _jenni_turn() -> str:
    return render(
        beat("think", THOUGHT),
        beat("say", LINE, intent="sincere"),
        beat("do", ACTION),
        beat("latent", LATENT),
    )


@pytest.mark.integration
@pytest.mark.part("M4", "FR-RESP-8,12")
async def test_c_12__structured(
    surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A structured story with shared character knowledge: a
    character-viewpoint response and history read omit every ``think`` and ``latent`` beat and
    return every other beat in full; the composed message is checked against exactly those
    beats; an ``omniscient`` read returns the thought labelled (``[Jenni thinks] …``) and an
    omniscient composition is checked the same way (attribution: a thought without its label
    fails). Run over REST and the library.

    Complement asserted: the ``omniscient`` read **does** return the ``think`` and ``latent``
    beats, and a history read for the other viewpoint recomposes the message for it."""
    story, ids = await structured_story(surface.client, "Bobby", "Jenni")
    bobby, jenni = ids["Bobby"], ids["Jenni"]
    stub = surface.engine.models.stub
    stub.script("generation", [_jenni_turn()])

    played = (await surface.respond(story, body(bobby, "Jenni?"))).json
    assert played["output_viewpoint"] == _vp(bobby)
    assert _no_filtering(played)
    assert [(b["channel"], b["text"]) for b in played["beats"]] == [
        ("say", "Jenni?"),
        ("say", LINE),
        ("do", ACTION),
        ("message", played["message"]["text"]),
    ]
    message = played["message"]["text"]
    assert played["message"]["mode"] == "polished"
    assert message == f'Jenni: "{LINE}"\n\n*{ACTION}*'
    assert THOUGHT not in message
    assert LATENT not in message
    report = (await turn_report(engine, story))["composition"]
    assert report["check"]["failures"] == []
    assert report["check"]["traced"] == report["check"]["claims"]

    # The character read: the same beats, the same message.
    read = (await surface.history(story, viewpoint=_vp(bobby))).json
    assert _no_filtering(read)
    assert all(i["channel"] not in PRIVATE_CHANNELS for i in read["items"])
    texts = [(i["channel"], i["text"]) for i in read["items"]]
    assert ("say", LINE) in texts
    assert ("do", ACTION) in texts
    assert ("message", message) in texts

    # Complement: the omniscient read returns the private beats and the labelled thought.
    omniscient = (await surface.history(story, viewpoint="omniscient")).json
    private = {(i["channel"], i["text"]) for i in omniscient["items"]}
    assert {("think", THOUGHT), ("latent", LATENT)} <= private
    (recomposed,) = [i["text"] for i in omniscient["items"] if i["channel"] == "message"]
    assert recomposed == (
        f'[Jenni thinks] {THOUGHT}\n\nJenni: "{LINE}"\n\n*{ACTION}*\n\n[latent] {LATENT}'
    )

    # An omniscient turn: the thought may appear, labelled, and the other checks still apply.
    stub.script("generation", [_jenni_turn()])
    seen = (await surface.respond(story, body(bobby, "Jenni?"), viewpoint="omniscient")).json
    assert seen["message"]["mode"] == "polished"
    assert f"[Jenni thinks] {THOUGHT}" in seen["message"]["text"]
    assert {b["channel"] for b in seen["beats"]} >= {"think", "latent"}
    for_bobby = (await surface.history(story, viewpoint=_vp(bobby))).json
    turns = [i for i in for_bobby["items"] if i["channel"] == "message"]
    assert THOUGHT not in turns[-1]["text"]
    assert turns[-1]["text"] == message
    unlabelled = f'{THOUGHT} "{LINE}" Jenni says. {ACTION} [latent] {LATENT}'
    stub.script("generation", [_jenni_turn()])
    stub.script(COMPOSITION, [json.dumps({"text": unlabelled})])
    fallen = (await surface.respond(story, body(bobby, "Jenni?"), viewpoint="omniscient")).json
    assert fallen["message"]["mode"] == "deterministic"
    assert "composition_fallback" in fallen["degradation"]
    rejected = (await turn_report(engine, story))["composition"]["rejected"]
    assert "attribution" in {f["kind"] for f in rejected["failures"]}
    assert jenni in {b["actor_character_id"] for b in fallen["beats"]}


WHISPER = "C12-WHISPER the ledger is in the stove."
WHISPERED = "Jenni whispers something to Wren"
GLANCE = "She glances at the window."


async def _rooms(
    surface: Surface, engine: AsyncEngine, level: str
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Wren (the player), Jenni and Bobby in the parlor, Clara in the hall (through a closed
    door), at perception ``level``."""
    story, ids = await structured_story(surface.client, "Wren", "Jenni", "Bobby", "Clara")
    await ps.perceive(engine, story, level)
    places = {n: await ps.place_entity(engine, story, n) for n in ("parlor", "hall")}
    await ps.adjoin(engine, story, places["parlor"], places["hall"], "door_closed")
    for name in ("Wren", "Jenni", "Bobby"):
        await ps.put(engine, story, ids[name], places["parlor"])
    await ps.put(engine, story, ids["Clara"], places["hall"])
    return story, ids, places


def _whisper_turn(ids: dict[str, str]) -> str:
    return render(
        beat("think", THOUGHT),
        beat("say", WHISPER, intent="sincere", whisper_to=[ids["Wren"]]),
        beat("do", GLANCE),
    )


def _channels(found: dict[str, Any], key: str = "beats") -> list[tuple[str, str]]:
    return [(b["channel"], b["text"]) for b in found[key] if b["channel"] != "message"]


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-12,13")
async def test_c_12__m5_output(
    surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """With perception at ``co_location`` a beat in another room is omitted for a character's
    viewpoint and every beat in its room is returned in full, words included; at ``full``
    envelopes are returned as S-WHISPER describes. The response's beats and narrative and the
    history read agree. Run over REST and the library.

    Complement: the viewpoint in the room does receive what the one in the hall does not, and
    ``omniscient`` receives everything."""
    stub = surface.engine.models.stub
    story, ids, _ = await _rooms(surface, engine, "co_location")
    stub.script("generation", [_whisper_turn(ids)])
    bobby = (
        await surface.respond(story, body(ids["Wren"], "Well?"), viewpoint=_vp(ids["Bobby"]))
    ).json
    assert bobby["audience_filter"] == "co_location"
    assert ("say", WHISPER) in _channels(bobby)
    assert ("do", GLANCE) in _channels(bobby)
    assert WHISPER in bobby["message"]["text"]
    assert THOUGHT not in str(bobby)
    clara = (await surface.history(story, viewpoint=_vp(ids["Clara"]))).json
    assert clara["audience_filter"] == "co_location"
    assert _channels(clara, "items") == []
    assert WHISPER not in str(clara)
    assert GLANCE not in str(clara)
    for_bobby = (await surface.history(story, viewpoint=_vp(ids["Bobby"]))).json
    assert ("say", WHISPER) in _channels(for_bobby, "items")
    everything = (await surface.history(story, viewpoint="omniscient")).json
    assert everything["audience_filter"] == "none"
    assert ("think", THOUGHT) in _channels(everything, "items")

    story, ids, _ = await _rooms(surface, engine, "full")
    stub.script("generation", [_whisper_turn(ids)])
    bobby = (
        await surface.respond(story, body(ids["Wren"], "Well?"), viewpoint=_vp(ids["Bobby"]))
    ).json
    assert bobby["audience_filter"] == "full"
    assert ("say", WHISPERED) in _channels(bobby)
    (enveloped,) = [b for b in bobby["beats"] if b["text"] == WHISPERED]
    assert enveloped["fidelity"] == "envelope"
    assert enveloped["propositions"] == []
    assert WHISPER not in str(bobby)
    assert WHISPERED in bobby["message"]["text"]
    read = (await surface.history(story, viewpoint=_vp(ids["Bobby"]))).json
    assert ("say", WHISPERED) in _channels(read, "items")
    assert WHISPER not in str(read)
    wren = (await surface.history(story, viewpoint=_vp(ids["Wren"]))).json
    assert ("say", WHISPER) in _channels(wren, "items")
    (message,) = [i["text"] for i in wren["items"] if i["channel"] == "message"]
    assert WHISPER in message
    assert THOUGHT not in message
    # A character read carries no verdicts or detected claims: they could disclose the truth.
    for item in read["items"]:
        assert "detected" not in item
        assert "effects" not in item
        assert all(p["verdict"] is None for p in item.get("propositions") or [])


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-12,13")
async def test_c_12__player_view(
    surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """``player_view`` (#177): under ``character`` (the default) the viewpoint in the hall
    perceives nothing (``nothing_perceived``, an empty narrative); under ``story`` (the story's
    setting, a request override or a history parameter) every beat of the turn is returned in
    full whatever the character perceived, each with the fidelity it would have had, still never
    a ``think`` or ``latent``, and the character's partition is untouched; both echo
    ``player_view``. The whisper's words reach Bobby's response under ``story`` while his
    partition still holds only the envelope. Run over REST and the library."""
    stub = surface.engine.models.stub
    story, ids, _ = await _rooms(surface, engine, "full")
    stub.script("generation", [_whisper_turn(ids)])
    clara = (
        await surface.respond(story, body(ids["Wren"], "Well?"), viewpoint=_vp(ids["Clara"]))
    ).json
    assert (clara["player_view"], clara["audience_filter"]) == ("character", "nothing_perceived")
    assert clara["message"]["text"] == ""
    assert _channels(clara) == []

    # The request's override: everything, at the fidelity Clara would have had.
    stub.script("generation", [_whisper_turn(ids)])
    asked = body(ids["Wren"], "Well?", player_view="story")
    open_ = (await surface.respond(story, asked, viewpoint=_vp(ids["Clara"]))).json
    assert (open_["player_view"], open_["audience_filter"]) == ("story", "bypassed_by_player_view")
    assert ("say", WHISPER) in _channels(open_)
    assert ("do", GLANCE) in _channels(open_)
    assert {b["fidelity"] for b in open_["beats"] if b["channel"] != "message"} == {"none"}
    assert THOUGHT not in str(open_)
    assert WHISPER in open_["message"]["text"]
    assert [m.text for m in await ps.memories_of(engine, story, ids["Clara"])] == []

    # A history read's parameter, over the default.
    read = (await surface.history(story, viewpoint=_vp(ids["Clara"]), player_view="story")).json
    assert read["player_view"] == "story"
    assert ("say", WHISPER) in _channels(read, "items")
    assert THOUGHT not in str(read)
    closed = (await surface.history(story, viewpoint=_vp(ids["Clara"]))).json
    assert closed["player_view"] == "character"
    assert _channels(closed, "items") == []

    # The story's setting: Bobby reads the words; his partition holds the envelope only.
    patched = await surface.client.patch(
        f"/stories/{story}/settings", json={"player_view": "story"}, headers=WRITE
    )
    assert patched.status_code == 200, patched.text
    stub.script("generation", [_whisper_turn(ids)])
    bobby = (
        await surface.respond(story, body(ids["Wren"], "Well?"), viewpoint=_vp(ids["Bobby"]))
    ).json
    assert bobby["player_view"] == "story"
    (heard,) = [b for b in bobby["beats"] if b["text"] == WHISPER]
    assert heard["fidelity"] == "envelope"
    held = [m.text for m in await ps.memories_of(engine, story, ids["Bobby"])]
    assert WHISPERED in held
    assert not [t for t in held if "ledger" in t]
    # ``omniscient`` still only when named, the thought labelled there.
    seen = (await surface.history(story, viewpoint="omniscient")).json
    assert ("think", THOUGHT) in _channels(seen, "items")
    assert seen["audience_filter"] == "none"


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-12,13")
async def test_c_12__m5_reads(
    surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The read rules (#178): reads of a character's partition, its beliefs or the story's
    divergences (and exposures) that do not name ``omniscient`` are rejected, a character
    viewpoint with 403 ``omniscient_required`` and no viewpoint with 422
    ``viewpoint_unresolved``. History reads at perception ``co_location``: Clara, in the hall,
    is given none of the parlor's beats; Bobby, in the parlor, every one in full, words included.
    At ``full`` Bobby's history read holds the whisper's envelope only, as S-WHISPER describes,
    and Wren's (whispered to) the words. Run over REST and the library.

    Complement: the same inspection reads naming ``omniscient`` answer 200, labelled so."""
    stub = surface.engine.models.stub
    story, ids, _ = await _rooms(surface, engine, "co_location")
    stub.script("generation", [_whisper_turn(ids)])
    played = await surface.respond(story, body(ids["Wren"], "Well?"), viewpoint="omniscient")
    assert played.status == 200, played.code
    for kind, params in (
        ("beliefs", {"character": ids["Bobby"]}),
        ("divergences", {}),
        ("exposures", {}),
    ):
        refused = await surface.inspect(kind, story, viewpoint=_vp(ids["Bobby"]), **params)
        assert (refused.status, refused.code) == (403, "omniscient_required"), kind
        unnamed = await surface.inspect(kind, story, **params)
        assert (unnamed.status, unnamed.code) == (422, "viewpoint_unresolved"), kind
        seen = await surface.inspect(kind, story, viewpoint="omniscient", **params)
        assert seen.status == 200, (kind, seen.code)
        assert seen.json["output_viewpoint"] == "omniscient"
    clara = (await surface.history(story, viewpoint=_vp(ids["Clara"]))).json
    assert _channels(clara, "items") == []
    assert WHISPER not in str(clara)
    bobby = (await surface.history(story, viewpoint=_vp(ids["Bobby"]))).json
    assert ("say", WHISPER) in _channels(bobby, "items")
    assert ("do", GLANCE) in _channels(bobby, "items")
    assert THOUGHT not in str(bobby)

    story, ids, _ = await _rooms(surface, engine, "full")
    stub.script("generation", [_whisper_turn(ids)])
    played = await surface.respond(story, body(ids["Wren"], "Well?"), viewpoint="omniscient")
    assert played.status == 200, played.code
    bobby = (await surface.history(story, viewpoint=_vp(ids["Bobby"]))).json
    assert ("say", WHISPERED) in _channels(bobby, "items")
    assert WHISPER not in str(bobby)
    wren = (await surface.history(story, viewpoint=_vp(ids["Wren"]))).json
    assert ("say", WHISPER) in _channels(wren, "items")
