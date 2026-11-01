"""The same request gives the same answer over REST and in-process (NFR-SEC-5's parity half,
#59): one story, read and played through both surfaces of one app."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.states.api import StateSetRequest
from epistrel.core.world_clock.scheduled import ScheduledEventCreate
from epistrel.core.world_clock.service import ClockAdvanceRequest
from tests import clock_support as clocks
from tests import provider_support as pv
from tests import state_support as st
from tests.conformance.conftest import Surface
from tests.respond_support import FIRST, PLAYER, SECOND, body
from tests.scenario.test_s_player_authority import TAKES, _knowledge_room, _narrate
from tests.story_support import story_body
from tests.structured_turn_support import structured_story


def _shared(result: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """A respond body without the per-story ids (the viewpoint names its character)."""
    viewpoint = result["output_viewpoint"]
    for name, character_id in ids.items():
        viewpoint = viewpoint.replace(character_id, name)
    return {
        "beats": [(b["beat"], b["kind"], b["channel"], b["text"]) for b in result["beats"]],
        "output_viewpoint": viewpoint,
        "audience_filter": result["audience_filter"],
        "message": result["message"]["text"],
    }


@pytest.mark.integration
async def test_rest_and_library_agree(both: dict[str, Surface]) -> None:
    """History reads under every viewpoint, story reads, and respond results are equal
    across the surfaces; a refusal has the same status and code on both."""
    rest, library = both["rest"], both["library"]
    played: dict[str, list[dict[str, Any]]] = {}
    for surface in (rest, library):
        created = (await surface.create_story(story_body(PLAYER, FIRST, SECOND))).json
        story = created["story_id"]
        ids = {c["name"]: c["id"] for c in created["characters"]}
        played[surface.name] = [
            _shared((await surface.respond(story, body(ids[PLAYER], line), viewpoint=vp)).json, ids)
            for line, vp in (("Same words.", None), ("More words.", "omniscient"))
        ]
    assert played["rest"] == played["library"]

    for params in (
        {},
        {"viewpoint": "omniscient", "include_audit": True},
        {"viewpoint": f"character:{ids[FIRST]}"},
        {"actor_character_id": ids[SECOND], "limit": 3},
        {"as_of": 1},
        {"viewpoint": "nobody"},
        {"viewpoint": f"character:{ids[FIRST]}", "include_audit": True},
    ):
        assert await rest.history(story, **params) == await library.history(story, **params)
    for vp in (None, "omniscient", "character:nobody"):
        over_rest, in_process = await rest.get_story(story, vp), await library.get_story(story, vp)
        assert (over_rest.status, over_rest.code) == (in_process.status, in_process.code)
        assert (over_rest.body or {}).get("characters") == (in_process.body or {}).get("characters")


@pytest.mark.integration
async def test_rest_and_library_agree__m6(
    both: dict[str, Surface], engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The M6 reads (#423) answer the same over REST and in-process, refusals included: the
    State list and named reads under every viewpoint (a hidden State, an unknown name), the
    world clock, the scheduled events (canon and a participant's view, an unresolved
    viewpoint), the snapshotted external facts (omniscient only), the reconciliation status,
    and a history read that carries a turn's ``implausible_knowledge`` notices (#598)."""
    rest, library = both["rest"], both["library"]
    author = RequestContext(principal="p-author")
    stub = library.engine.models.stub
    story, ids, _, _ = await _knowledge_room(rest.client, engine, canon="drawer")
    noticed = await _narrate(rest.client, stub, story, ids, TAKES)
    assert noticed.status_code == 200, noticed.text
    assert noticed.json()["notices"]
    cal, jenni, bobby = ids["Cal"], ids["Jenni"], ids["Bobby"]

    for params in (
        {},
        {"viewpoint": "omniscient"},
        {"viewpoint": f"character:{cal}"},
        {"viewpoint": f"character:{jenni}"},
        {"viewpoint": "omniscient", "include_audit": True},
    ):
        over_rest = await rest.history(story, **params)
        assert over_rest.status == 200, over_rest.code
        assert over_rest == await library.history(story, **params)
    omniscient = (await rest.history(story, viewpoint="omniscient")).json
    assert [t["notices"] for t in omniscient["turns"] if t.get("notices")]

    await st.set_level(engine, story, "on")
    await clocks.set_level(engine, story, "on")
    await st.declared(library.engine, story, name="gold", type="number", initial_value=10)
    await st.declared(
        library.engine,
        story,
        name="trust",
        type="number",
        scope="character",
        visibility="hidden",
        initial_value=0,
    )
    await library.engine.set_state(
        author, story, "trust", StateSetRequest(value=3, scope_key=jenni, reason="warming")
    )
    await library.engine.advance_clock(
        author, story, ClockAdvanceRequest(minutes=90, reason="they wait")
    )
    for event in (
        ScheduledEventCreate(proposition="the delivery", planned=600, participants=[jenni]),
        ScheduledEventCreate(proposition="the storm", planned=900),
    ):
        await library.engine.create_scheduled_event(author, story, event)

    viewpoints = (None, "omniscient", *(f"character:{c}" for c in (cal, jenni, bobby)), "nobody")
    for vp in viewpoints:
        keywords = {} if vp is None else {"viewpoint": vp}
        assert await rest.states(story, **keywords) == await library.states(story, **keywords), vp
        assert await rest.scheduled_events(story, vp) == await library.scheduled_events(story, vp)
        for name in ("gold", "trust", "unknown"):
            over_rest = await rest.state(story, name, **keywords)
            assert over_rest == await library.state(story, name, **keywords), (name, vp)
    seen = (await rest.states(story, viewpoint="omniscient")).json
    assert [s["name"] for s in seen["states"]] == ["gold", "trust"]
    assert seen["clock"]["offset"] == 90
    assert len((await rest.scheduled_events(story, "omniscient")).json["events"]) == 2
    held = (await rest.scheduled_events(story, f"character:{jenni}")).json["events"]
    assert [e["name"] for e in held] == ["the delivery"]
    assert (await rest.state(story, "trust", viewpoint=f"character:{cal}")).status == 404

    clock = await rest.clock(story)
    assert clock.status == 200, clock.code
    assert clock == await library.clock(story)
    unknown = await rest.clock("01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert unknown.status == 404
    assert unknown == await library.clock("01ARZ3NDEKTSV4RRFFQ69G5FAV")
    reconciliation = await rest.reconciliation(story)
    assert reconciliation.status == 200, reconciliation.code
    assert reconciliation == await library.reconciliation(story)

    name = pv.unique_name("ledger")
    await pv.register(engine, pv.in_process_spec(name, {"gold": "number"}))
    other, cast = await structured_story(rest.client, "Cal", "Mira")
    await st.set_level(engine, other, "on")
    gold = await pv.external_gold(engine, other, name)
    first, _ = await pv.record_read(engine, other, gold, cast["Cal"], 300)
    await pv.record_read(engine, other, gold, cast["Cal"], 250)
    reads: list[tuple[str | None, dict[str, Any]]] = [
        ("omniscient", {}),
        ("omniscient", {"limit": 1}),
        ("omniscient", {"as_of": first}),
        (f"character:{cast['Mira']}", {}),
        (None, {}),
    ]
    for vp, paging in reads:
        over_rest = await rest.inspect("external-facts", other, viewpoint=vp, **paging)
        assert over_rest == await library.inspect("external-facts", other, viewpoint=vp, **paging)
    facts = (await rest.inspect("external-facts", other, viewpoint="omniscient")).json
    assert [i["value"] for i in facts["items"]] == [300, 250]
