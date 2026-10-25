"""S-REACTIVE: narrated reactions and escalation (docs/04 §7; FR-MULTI-7,9,10, NFR-PERF-7;
#176).

The bar (``tests.reactive_support``): Cal (the player) asks; Bobby and Jenni are named; Ada, Sam
(the bartender) and Dale are present and default-``reactive``. Played end to end through
``respond`` with the stub adapter, each primary's render scripted by its name in the prompt.

- ``test_s_reactive``: five present, two primary, three reactive → exactly two renders; Bobby's
  render declares Ada's glance (``reactions``), Jenni's ``do`` prose narrates Dale's flinch; both
  are recorded as the reactors' ``beat_do`` with ``narrated_by``, and neither writes a ``think``,
  ``latent`` or belief row for its reactor, who remembers its own reaction and still gets an
  exposure record of Bobby's claim. Caught: a reaction to Bobby's ``think`` (unperceived) and a
  narrated thought of Dale's are dropped (``render_rejected`` for the first).
- ``test_s_reactive__fixed_roles``: the bartender fixed ``reactive``: the player asks him for a
  drink and Jenni narrates him sliding a beer across: no render for him, the beat is repaired to
  a non-verbal reaction, nothing about serving is recorded, and the response lists him in
  ``escalation_deferred`` (``fixed_role``) without an error. The same request with a default
  bartender escalates him to a primary render; a request naming the fixed bartender renders him
  as primary. A fixed ``silent`` Dale gets no narrated reaction (listed ``fixed_role``).
- ``test_s_reactive__interjections``: interjections are refused by default (a fixed reactor's is
  limited and deferred), allowed and viewpoint-checked when the story enables them (a ``say``
  with ``narrated_by``), and one over the word cap escalates.
- ``test_s_reactive__overrides``: per-request ``roles`` apply (``silent`` drops a reaction,
  ``primary`` renders the character), and their validation (an unknown character, the player,
  a named character overridden down) is 422; the PATCH route's own rules (the player 422, an
  unknown character 404, a config change recorded).
- ``test_s_reactive__budget``: a budget that cannot fit another primary plus its reservation
  defers a default-reactive character's escalation with reason ``budget``.

M6's State clauses are M6's.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import multi_support as ms
from tests import perception_support as ps
from tests import reactive_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ
from tests.structured_turn_support import beat, turn_report

pytestmark = pytest.mark.integration

GLANCE = "Ada glances at the door."
FLINCH = "Dale flinches at the clink."
BEER = "Sam slides a beer across the bar."
BOBBY_THINKS = "Bobby thinks about the money."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _ask(b: rs.Bar, *named: str, say: str = "Well?", **fields: Any) -> dict[str, Any]:
    return body(b["Cal"], say, render_character_ids=[b[n] for n in named], **fields)


async def _play(client: Any, b: rs.Bar, *named: str, **fields: Any) -> dict[str, Any]:
    answer = await respond(client, b.story_id, _ask(b, *named, **fields))
    assert answer.status_code == 200, answer.text
    played: dict[str, Any] = answer.json()
    return played


@pytest.mark.part("M5", "FR-MULTI-7,9,10; NFR-PERF-7")
async def test_s_reactive(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (two renders, the narrated reactions, the bounds, caught)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine)
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            rs.render(
                beat("think", BOBBY_THINKS),
                beat("say", "Olivia sent the text.", intent="sincere"),
                reactions=[
                    rs.reaction(b["Ada"], GLANCE),
                    # Ada reacts to Bobby's thought: she never perceived it.
                    rs.reaction("Sam", "Sam winces.", reacts_to=0),
                ],
            ),
        )
        ms.renders_for(
            stub,
            "Jenni",
            rs.render(
                beat("do", f"Jenni sets down her glass. {FLINCH} Dale thinks Jenni is lying.")
            ),
        )
        turn = await _play(client, b, "Bobby", "Jenni")
        assert rs.character_renders(stub) == ["Bobby", "Jenni"]  # exactly two renders
        assert turn["roles"] == {
            b["Bobby"]: {"role": "primary", "source": "default"},
            b["Jenni"]: {"role": "primary", "source": "default"},
            **{b[n]: {"role": "reactive", "source": "default"} for n in ("Ada", "Sam", "Dale")},
        }
        narrated = await rs.narrated(engine, b.story_id)
        assert [(n.type, n.character_id, n.narrated_by, n.text) for n in narrated] == [
            ("beat_do", b["Ada"], b["Bobby"], GLANCE),
            ("beat_do", b["Dale"], b["Jenni"], FLINCH),
        ]
        # Jenni's own beat keeps her action only: the flinch is Dale's, the thought dropped.
        (jenni_do,) = await rows(
            engine,
            "SELECT payload->>'text' AS text FROM events WHERE story_id = :s "
            "AND character_id = :c AND type = 'beat_do'",
            s=b.story_id,
            c=b["Jenni"],
        )
        assert jenni_do.text == "Jenni sets down her glass."
        for name in ("Ada", "Dale"):
            found = await rs.reactor_rows(engine, b.story_id, b[name])
            assert found["private_beats"] == 0
            assert found["beliefs_at_reaction"] == 0
            # Being reactive costs no memory: an exposure of what they witnessed (#164).
            assert found["exposures"] >= 1
        assert any(GLANCE in m.text for m in await ps.memories_of(engine, b.story_id, b["Ada"]))
        report = (await turn_report(engine, b.story_id))["roles"]
        assert [r["reason"] for r in report["rejected"]] == ["unperceived"]
        assert report["dropped"] == {"inner_state": 1}
        (dropped,) = await rs.rejected(engine, b.story_id)
        assert dropped.payload["text"] == "Sam winces."
        assert "Sam winces" not in turn["message"]["text"]
        assert "Dale thinks" not in turn["message"]["text"]
        assert "escalation_deferred" not in turn


@pytest.mark.part("M5", "FR-MULTI-7,9,10; NFR-PERF-7")
async def test_s_reactive__fixed_roles(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (the bartender, fixed and default; the fixed-silent Dale)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- The fixed bartender: no render, a non-verbal reaction, fixed_role, no error.
        b = await rs.bar(client, engine)
        await rs.set_role(client, b.story_id, b["Sam"], "reactive", fixed=True)
        await rs.set_role(client, b.story_id, b["Dale"], "silent", fixed=True)
        about_sam = (
            "SELECT count(*) FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.subject_id = :c"
        )
        sam_facts = await scalar(engine, about_sam, s=b.story_id, c=b["Sam"])
        stub.reset()
        ms.renders_for(
            stub,
            "Jenni",
            rs.render(
                beat("do", f"Jenni waves at the bar. {BEER}"),
                reactions=[rs.reaction(b["Dale"], "Dale shrugs.")],
            ),
        )
        turn = await _play(client, b, "Jenni", say="Sam, a beer please.")
        assert rs.character_renders(stub) == ["Jenni"]
        assert turn["roles"][b["Sam"]] == {"role": "reactive", "source": "fixed"}
        assert turn["roles"][b["Dale"]] == {"role": "silent", "source": "fixed"}
        assert turn["escalation_deferred"] == [
            {"character_id": b["Dale"], "reason": "fixed_role"},
            {"character_id": b["Sam"], "reason": "fixed_role", "needed": "decision"},
        ]
        narrated = await rs.narrated(engine, b.story_id)
        assert [(n.type, n.character_id) for n in narrated] == [("beat_do", b["Sam"])]
        assert narrated[0].text == "Sam looks up but says nothing."  # repaired to non-verbal
        assert BEER not in turn["message"]["text"]
        # Whether he serves stays open: nothing about Sam is canonized, nothing he said.
        assert await scalar(engine, about_sam, s=b.story_id, c=b["Sam"]) == sam_facts
        assert not await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND character_id = :c AND type <> 'beat_do' "
            "AND type LIKE 'beat_%'",
            s=b.story_id,
            c=b["Sam"],
        )

        # -- A request naming the fixed bartender renders him as primary.
        stub.reset()
        ms.renders_for(stub, "Sam", rs.render(beat("say", "Coming right up.", intent="sincere")))
        turn = await _play(client, b, "Sam", say="Sam, a beer please.")
        assert rs.character_renders(stub) == ["Sam"]
        assert turn["roles"][b["Sam"]]["role"] == "primary"

        # -- A default bartender escalates to a primary render (after Jenni's).
        b = await rs.bar(client, engine)
        stub.reset()
        ms.renders_for(stub, "Jenni", rs.render(beat("do", f"Jenni waves at the bar. {BEER}")))
        ms.renders_for(stub, "Sam", rs.render(beat("say", "Here you go.", intent="sincere")))
        turn = await _play(client, b, "Jenni", say="Sam, a beer please.")
        assert rs.character_renders(stub) == ["Jenni", "Sam"]
        assert turn["roles"][b["Sam"]] == {"role": "primary", "source": "escalated"}
        assert turn["order"] == [b["Jenni"], b["Sam"]]
        assert "escalation_deferred" not in turn
        report = (await turn_report(engine, b.story_id))["roles"]
        assert report["escalated"] == [b["Sam"]]


@pytest.mark.part("M5", "FR-MULTI-7,9,10; NFR-PERF-7")
async def test_s_reactive__interjections(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (off by default; on, checked; over the cap)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine)
        await rs.set_role(client, b.story_id, b["Ada"], "reactive", fixed=True)
        quip = rs.reaction(b["Ada"], '"Not again," Ada mutters.', kind="interjection")

        # Off by default: the fixed Ada's interjection is limited, never a say of hers.
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            rs.render(beat("say", "I lost again.", intent="sincere"), reactions=[quip]),
        )
        turn = await _play(client, b, "Bobby")
        assert turn["escalation_deferred"] == [
            {"character_id": b["Ada"], "reason": "fixed_role", "needed": "speech"}
        ]
        assert [n.type for n in await rs.narrated(engine, b.story_id)] == ["beat_do"]

        # On: a brief interjection is Ada's say, narrated by Bobby, checked like any say.
        await rs.interjections(client, b.story_id, True)
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            rs.render(beat("say", "I lost again.", intent="sincere"), reactions=[quip]),
        )
        turn = await _play(client, b, "Bobby")
        assert "escalation_deferred" not in turn
        last = (await rs.narrated(engine, b.story_id))[-1]
        assert (last.type, last.character_id, last.narrated_by) == (
            "beat_say",
            b["Ada"],
            b["Bobby"],
        )
        assert "Not again" in turn["message"]["text"]

        # Over the cap (six words): speech beyond an interjection escalates a default Ada.
        await rs.set_role(client, b.story_id, b["Ada"], "reactive")
        stub.reset()
        long = rs.reaction(
            b["Ada"], '"I told you this would happen, Bobby, every time."', kind="interjection"
        )
        ms.renders_for(
            stub,
            "Bobby",
            rs.render(beat("say", "I lost again.", intent="sincere"), reactions=[long]),
        )
        ms.renders_for(stub, "Ada", rs.render(beat("say", "I told you.", intent="sincere")))
        turn = await _play(client, b, "Bobby")
        assert rs.character_renders(stub) == ["Bobby", "Ada"]
        assert turn["roles"][b["Ada"]] == {"role": "primary", "source": "escalated"}


@pytest.mark.part("M5", "FR-MULTI-7,9,10; NFR-PERF-7")
async def test_s_reactive__overrides(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (per-request roles and their validation; the PATCH route)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine)

        # The PATCH route: a config change, the player 422, an unknown character 404.
        answer = await rs.participation(client, b.story_id, b["Ada"], "primary", fixed=True)
        assert answer.status_code == 200, answer.text
        found = answer.json()
        assert found["character"]["participation"] == {"role": "primary", "fixed": True}
        changes = await client.get(
            f"/stories/{b.story_id}/config-changes",
            params={"setting": f"participation:{b['Ada']}"},
            headers=READ,
        )
        assert changes.status_code == 200, changes.text
        (change,) = changes.json()["items"]
        assert change["new"] == {"role": "primary", "fixed": True}
        assert change["class"] == "prospective"
        story = await client.get(f"/stories/{b.story_id}", headers=READ)
        by_id = {c["id"]: c for c in story.json()["characters"]}
        assert by_id[b["Ada"]]["participation"] == {"role": "primary", "fixed": True}
        assert "participation" not in by_id[b["Cal"]]
        refused = await rs.participation(client, b.story_id, b["Cal"], "primary")
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["reason"] == "is_player"
        missing = await rs.participation(client, b.story_id, "01JA00000000000000000000ZZ", "silent")
        assert missing.status_code == 404, missing.text
        # A stored primary the request does not name is reactive for the turn.
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            rs.render(
                beat("say", "Evening.", intent="sincere"),
                reactions=[rs.reaction(b["Ada"], GLANCE), rs.reaction(b["Dale"], FLINCH)],
            ),
        )
        turn = await _play(client, b, "Bobby", roles={b["Dale"]: "silent"})
        assert turn["roles"][b["Ada"]] == {"role": "reactive", "source": "fixed"}
        assert turn["roles"][b["Dale"]] == {"role": "silent", "source": "request"}
        assert [n.character_id for n in await rs.narrated(engine, b.story_id)] == [b["Ada"]]

        # ``primary`` names the character (rendered after the named ones).
        stub.reset()
        ms.renders_for(stub, "Bobby", rs.render(beat("say", "Evening.", intent="sincere")))
        ms.renders_for(stub, "Dale", rs.render(beat("say", "Hi.", intent="sincere")))
        turn = await _play(client, b, "Bobby", roles={b["Dale"]: "primary"})
        assert rs.character_renders(stub) == ["Bobby", "Dale"]
        assert turn["roles"][b["Dale"]] == {"role": "primary", "source": "request"}

        # Validation: 422 before any model call.
        stub.reset()
        for roles, reason in (
            ({"01JA00000000000000000000ZZ": "silent"}, "unknown"),
            ({b["Cal"]: "reactive"}, "no_role"),
            ({b["Bobby"]: "silent"}, "named_overridden"),
        ):
            answer = await respond(client, b.story_id, _ask(b, "Bobby", roles=roles))
            assert answer.status_code == 422, answer.text
            assert answer.json()["error"]["details"]["reason"] == reason
        assert stub.calls == []


@pytest.mark.part("M5", "FR-MULTI-7,9,10; NFR-PERF-7")
async def test_s_reactive__budget(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (an escalation the budget cannot fit is deferred)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine)
        refused = await respond(client, b.story_id, _ask(b, "Jenni", budget={"max_calls": 1}))
        assert refused.status_code == 422, refused.text
        minimum = refused.json()["error"]["details"]["minimum_required"]
        stub.reset()
        ms.renders_for(stub, "Jenni", rs.render(beat("do", f"Jenni waves at the bar. {BEER}")))
        turn = await _play(client, b, "Jenni", budget={"max_calls": minimum})
        assert rs.character_renders(stub) == ["Jenni"]
        assert turn["escalation_deferred"] == [
            {"character_id": b["Sam"], "reason": "budget", "needed": "decision"}
        ]
        assert turn["roles"][b["Sam"]] == {"role": "reactive", "source": "default"}
        assert turn["budget"]["used"] <= minimum
