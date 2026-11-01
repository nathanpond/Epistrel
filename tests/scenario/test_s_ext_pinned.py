"""S-EXT-PINNED: one value per turn (docs/04 §7; FR-EXT-2; #205).

Cal's gold is owned by a registered provider (an in-process ``ScriptedProvider``) that answers
300 at generation and 250 by verification time. The turn reads it once, at its input, and
pins it: the narrator's prompt shows 300, the narration "Cal has 300 gold left" is
``supported`` against the pin, and the commit records the one ``external_read`` (input category,
beat 0, step 0, value 300) and its ``external_values`` row. Verification issues no second live
read. The next turn is new work: it reads again (250).

Complement: a story whose gold is story-owned makes no adapter call at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _verdicts(report: dict[str, object]) -> list[str]:
    verify = report["verify"]
    assert isinstance(verify, dict)
    return [v["kind"] for v in verify["verdicts"] if v.get("kind") in ("supported", "contradicted")]


@pytest.mark.part("M6", "FR-EXT-2")
async def test_s_ext_pinned(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name, {"gold": "number", "marks": "number"}))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = ps.ScriptedProvider(name).attach(epistrel)
        fake.script("gold", 300, 250)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        gold = await ps.external_gold(engine, story, name)

        es.narrator(stub, es.render(beat("narration", "Rain taps. Cal has 300 gold left.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
        answer = await respond(client, story, body(cal, None, do="checks his purse"))
        assert answer.status_code == 200, answer.text

        # One live read, at the turn's input; verification read nothing more.
        assert fake.reads == [("gold", story, cal)]
        (narrator_prompt,) = es.prompts(stub, "NarratorRender")
        assert "300" in narrator_prompt
        assert "250" not in narrator_prompt
        report = await turn_report(engine, story)
        assert _verdicts(report) == ["supported"]
        assert "300 gold left" in answer.json()["message"]["text"]

        # The commit recorded the read and pinned row: 300 everywhere.
        (read,) = await ps.external_reads(engine, story)
        assert read.category == "input"
        assert (read.beat, read.step) == (0, 0)
        assert read.payload["value"] == 300
        assert read.payload["source"] == "live"
        assert (read.payload["provider"], read.payload["value_name"]) == (name, "gold")
        assert (read.payload["state_id"], read.payload["scope_key"]) == (gold.id, cal)
        assert len(read.payload["raw_hash"]) == len(read.payload["query_hash"]) == 64
        (row,) = await ps.external_value_rows(engine, story)
        assert (row["event_id"], row["value"], row["source"], row["version"]) == (
            read.id,
            300,
            "read",
            1,
        )
        assert (row["position_turn"], row["position_beat"], row["position_step"]) == (
            read.narrative_turn,
            0,
            0,
        )

        # The next turn is new work: it reads live again and pins 250.
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Wind rattles the door.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Close it.")))
        again = await respond(client, story, body(cal, None, do="looks around"))
        assert again.status_code == 200, again.text
        assert len(fake.reads) == 2
        assert [r.payload["value"] for r in await ps.external_reads(engine, story)] == [300, 250]

        # A hidden external State is not read at turn start and is absent from the narrator's
        # inputs; a claim that resolves to it reads it on demand, at the claim's beat.
        await ss.define_all(
            engine,
            story,
            ss.gold(
                name="marks",
                aliases=(),
                unit=None,
                visibility="hidden",
                owner="external",
                provider_id=name,
            ),
        )
        fake.script("marks", 40)
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Rain taps. Cal has 40 marks left.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
        hidden = await respond(client, story, body(cal, None, do="checks his purse"))
        assert hidden.status_code == 200, hidden.text
        (narrator_prompt,) = es.prompts(stub, "NarratorRender")
        assert "marks" not in narrator_prompt
        assert fake.reads[-2:] == [("gold", story, cal), ("marks", story, cal)]
        assert _verdicts(await turn_report(engine, story)) == ["supported"]
        *_, at_input, on_demand = await ps.external_reads(engine, story)
        assert (at_input.payload["value_name"], at_input.beat) == ("gold", 0)
        assert on_demand.payload["value_name"] == "marks"
        assert on_demand.payload["value"] == 40
        assert on_demand.beat > 0

        # Complement: a story-owned State makes no adapter call.
        calls = fake.calls
        owned, owned_ids = await structured_story(client, "Cal", "Mira")
        await ss.set_level(engine, owned, "on")
        await ss.define_all(engine, owned, ss.gold())
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Rain taps.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
        plain = await respond(client, owned, body(owned_ids["Cal"], None, do="checks his purse"))
        assert plain.status_code == 200, plain.text
        assert fake.calls == calls
        assert await ps.external_reads(engine, owned) == []
        await ss.set_level(engine, story, None)
        await ss.set_level(engine, owned, None)
