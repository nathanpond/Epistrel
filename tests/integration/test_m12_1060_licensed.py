"""#1060's deferred half (the owner's decisions, 2026-11-28), played end to end through ``respond``
on the stub adapter. Maren is the actor and the render character, as a consumer's host is. Each
rendered claim is an extraction claim that cannot normalize ("holds" straw, a tracked relation,
so #978's ``passes`` never takes it; the directive cases' has no object at all): without a
licence it is repaired ("could not support").

- ``test_an_echo_of_the_actors_input_passes``: the host's rendered ``say`` "It's time to vote."
  restates its own input ``say``, and its ``do`` "Maren stands by the fire." its own input
  ``do``: the claim passes (disposition ``pass``), with no regeneration. Complement: the same
  beat after a different input is repaired as before.
- ``test_another_characters_echo_is_repaired``: Flynn renders the actor's words; they are not
  his input, so his claim is repaired.
- ``test_a_directive_licenses_the_actors_beat``: a pending ephemeral directive ("Maren takes her
  place by the fire") under ``directive_support`` ``support`` and ``selfonly``. Under both, the
  actor's claim about herself that the directive bears on passes; a claim about something else
  passes under ``support`` only. Complements: with no directive, or one whose words do not bear
  on the claim, it is repaired.
- ``test_directive_support_is_a_story_setting``: ``PATCH /stories/{id}/settings`` takes
  ``support`` and ``selfonly``, defaults to ``support``, and refuses anything else (422).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.detection_support import claims_answer
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, render, structured_story, turn_report

pytestmark = pytest.mark.integration

SAY = "It's time to vote."
DO = "Maren stands by the fire."
SETTLES = "Maren settles onto the log beside the fire."
OMNISCIENT = {"viewpoint": "omniscient"}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _unnormalizable(span: str, subject: str, obj: str | None = "straw") -> str:
    return claims_answer(
        {
            "beat": 0,
            "span": span,
            "subject": subject,
            "predicate": "holds",
            "object": obj,
            "time": None,
            "polarity": "affirmed",
            "confidence": 1.0,
        }
    )


def _regenerations(stub: Any) -> list[str]:
    return [
        "\n".join(m.content for m in c.messages) for c in stub.calls if c.purpose == "regeneration"
    ]


def _dispositions(report: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(v["kind"], v.get("disposition")) for v in report.get("verify", {}).get("verdicts", [])]


async def _turn(
    client: Any,
    epistrel: Any,
    engine: AsyncEngine,
    story: str,
    actor: str,
    render_id: str,
    rendered: Any,
    span: str,
    subject: str,
    *,
    say: str | None,
    do: str | None = None,
    obj: str | None = "straw",
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    stub = epistrel.models.stub
    stub.reset()
    stub.script("generation", [render(rendered)])
    stub.script("extraction_backstop", [_unnormalizable(span, subject, obj)] * 6)
    stub.script("regeneration", [json.dumps({"text": "Maren waits."})] * 6)
    r = await respond(client, story, body(actor, say, do=do, render_character_ids=[render_id]))
    assert r.status_code == 200, r.text
    return r.json(), await turn_report(engine, story), _regenerations(stub)


@pytest.mark.parametrize("channel", ["say", "do"])
@pytest.mark.parametrize("echo", [True, False])
async def test_an_echo_of_the_actors_input_passes(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    channel: str,
    echo: bool,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren", "Flynn")
        text = SAY if channel == "say" else DO
        span = "It's time to vote" if channel == "say" else "stands by the fire"
        if channel == "say":
            say, do = (SAY if echo else "Welcome."), None
        else:
            say, do = "Welcome.", (DO if echo else "Maren looks up.")
        answer, report, regenerations = await _turn(
            client,
            epistrel,
            engine,
            story,
            ids["Maren"],
            ids["Maren"],
            beat(channel, text),
            span,
            "the_stable",
            say=say,
            do=do,
        )
    if echo:
        assert _dispositions(report) == [("uncertain", "pass")], report.get("verify")
        assert regenerations == []
        assert text in [b["text"] for b in answer["beats"]]
    else:
        assert _dispositions(report)[0] == ("uncertain", "repair"), report.get("verify")
        assert regenerations, "a beat that is not the input's is repaired as before"


async def test_another_characters_echo_is_repaired(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren", "Flynn")
        _, report, regenerations = await _turn(
            client,
            epistrel,
            engine,
            story,
            ids["Maren"],
            ids["Flynn"],
            beat("say", SAY),
            "It's time to vote",
            "the_stable",
            say=SAY,
        )
    assert _dispositions(report)[0] == ("uncertain", "repair"), report.get("verify")
    assert regenerations


@pytest.mark.parametrize(
    ("mode", "direction", "subject", "passes"),
    [
        ("support", "Maren takes her place by the fire.", "Maren", True),
        ("support", "Maren takes her place by the fire.", "the_stable", True),
        ("selfonly", "Maren takes her place by the fire.", "Maren", True),
        ("selfonly", "Maren takes her place by the fire.", "the_stable", False),
        ("support", None, "Maren", False),
        ("support", "Keep the tone tense and quiet.", "Maren", False),
    ],
)
async def test_a_directive_licenses_the_actors_beat(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    mode: str,
    direction: str | None,
    subject: str,
    passes: bool,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren", "Flynn")
        patched = await client.patch(
            f"/stories/{story}/settings", json={"directive_support": mode}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        if direction is not None:
            queued = await client.post(
                f"/stories/{story}/directives",
                params=OMNISCIENT,
                json={"text": direction, "kind": "ephemeral"},
                headers=WRITE,
            )
            assert queued.status_code == 200, queued.text
            assert queued.json()["disposition"] == "steer_queued"
        _, report, regenerations = await _turn(
            client,
            epistrel,
            engine,
            story,
            ids["Maren"],
            ids["Maren"],
            beat("do", SETTLES),
            "settles onto the log beside the fire",
            subject,
            say=SAY,
            obj=None,  # "holds" with no object: uncertain (argument_type) whatever the subject
        )
    if passes:
        assert _dispositions(report) == [("uncertain", "pass")], report.get("verify")
        assert regenerations == []
    else:
        assert _dispositions(report)[0] == ("uncertain", "repair"), report.get("verify")
        assert regenerations


async def test_directive_support_is_a_story_setting(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await structured_story(client, "Wren", "Maren")
        read = await client.get(f"/stories/{story}", headers=WRITE)
        assert read.status_code == 200, read.text
        assert read.json()["settings"]["directive_support"] == "support"
        for mode in ("selfonly", "support"):
            r = await client.patch(
                f"/stories/{story}/settings", json={"directive_support": mode}, headers=WRITE
            )
            assert r.status_code == 200, r.text
        bad = await client.patch(
            f"/stories/{story}/settings", json={"directive_support": "everything"}, headers=WRITE
        )
        assert bad.status_code == 422, bad.text
