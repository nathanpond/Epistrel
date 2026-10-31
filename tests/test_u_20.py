"""U-20: the pacing rule (docs/04 §3; FR-CLOCK-6,7), its M6 part (#194).

Every U-20 (M6) function lives in this file (traceability counts a test ID per milestone in one
file).

- ``test_u_20`` (pure, ``core.world_clock.pacing`` and ``core.world_clock.timing``): renders of
  0, 4, 5, 50, 51 and 300 words in ``say``+``do`` advance 0, 0, 1, 1, 2 and 2 minutes through
  the default table; ``think``, ``latent``, a ``flashback`` line and effect declarations add
  nothing, bracketed stage directions are not words and a hyphenated word is one; the player's
  input counts (``orchestrator.structured.clocking``: the input is a render of its own);
  sequential renders sum, simultaneous renders take the largest; a render with a declared skip
  uses the skip instead of its pacing increment; a custom table (0-9 → 0, 10-99 → 1, 100+ → 3)
  is applied exactly and validated at definition (contiguous, ascending, from 0, the last row
  open-ended). Complements: a ``think``-only render advances 0; ``manual`` advances 0 for 300
  words; no model call is made (a stub adapter's call count is unchanged by pacing and timing).
- ``test_u_20__turns`` (Postgres, the structured pipeline with the stub): a turn of a paced
  story advances the clock by its renders' pacing, one ``pacing`` advance per render that moves
  it, and ``respond.clock.after`` is the fold; the same turn on a ``manual`` story makes exactly
  the same model calls and moves nothing. Ingest (owner decision 2026-10-30): an ingested
  300-word ``narration`` and a ``player_turn`` are not paced and leave the clock unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import ClockMode, StateChangeSource
from epistrel.core.errors import InvalidRequest
from epistrel.core.turns.beats import DeclaredAdvance, TurnBeat
from epistrel.core.world_clock.anchor import Anchor
from epistrel.core.world_clock.pacing import (
    pacing_increment,
    render_words,
    validate_pacing_table,
    word_count,
)
from epistrel.core.world_clock.timing import (
    SEQUENTIAL,
    SIMULTANEOUS,
    TimedAdvance,
    TimedBeat,
    time_turn,
)
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.orchestrator.structured import clocking
from tests import clock_support as ck
from tests import multi_support as ms
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ, WRITE
from tests.structured_turn_support import beat, render, structured_story

CUSTOM = ((0, 10, 0), (10, 100, 1), (100, None, 3))


def words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def line(kind: str, n: int, **fields: Any) -> TurnBeat:
    return TurnBeat(kind=kind, beat=2, text=words(n), character_id="c", **fields)  # type: ignore[arg-type]


def clock(mode: ClockMode = ClockMode.PACED, table: Any = None) -> clocking.ClockRead:
    found = validate_pacing_table(table or ((0, 5, 0), (5, 51, 1), (51, None, 2)))
    unanchored = Anchor.unanchored()
    return clocking.ClockRead("on", mode, found, 0, unanchored, unanchored.clockwork, None)


@pytest.mark.part("M6", "FR-CLOCK-6,7")
def test_u_20() -> None:
    # The default table: 0, 4, 5, 50, 51 and 300 words in say + do.
    for n, minutes in ((0, 0), (4, 0), (5, 1), (50, 1), (51, 2), (300, 2)):
        say, do = n // 2, n - n // 2
        lines = [line("say", say), line("do", do)]
        assert render_words(lines) == n
        assert pacing_increment(lines) == minutes, n
    # think, latent, a flashback line and effects add nothing; stage directions are no words.
    quiet = [
        line("say", 4),
        line("think", 300),
        line("latent", 300),
        line("do", 300, flashback=True),
        TurnBeat(
            kind="do",
            beat=3,
            text="[she shrugs, slowly and pointedly, at length] Fine-tuned.",
            character_id="c",
            advances=(DeclaredAdvance("waits", minutes=0),),
        ),
    ]
    assert word_count("[she shrugs, slowly and pointedly, at length] Fine-tuned.") == 1
    assert render_words(quiet) == 5
    assert pacing_increment(quiet) == 1
    # Complement: a think-only render advances 0; manual mode advances 0 for 300 words.
    assert pacing_increment([line("think", 300)]) == 0
    assert pacing_increment([line("say", 300)], mode=ClockMode.MANUAL) == 0
    assert pacing_increment([line("say", 300)], mode=ClockMode.EXTERNAL) == 0
    # A custom table, applied exactly.
    custom = validate_pacing_table([list(r) for r in CUSTOM])
    for n, minutes in ((0, 0), (9, 0), (10, 1), (99, 1), (100, 3), (1000, 3)):
        assert pacing_increment([line("say", n)], custom) == minutes, n
    bads: tuple[list[list[int | None]], ...] = (
        [[1, 10, 0], [10, None, 1]],  # not from 0
        [[0, 10, 0], [11, None, 1]],  # a gap
        [[0, 10, 0], [5, None, 1]],  # overlapping, not ascending
        [[0, 10, 0], [10, 20, 1]],  # the last row closed
        [[0, None, 0], [10, None, 1]],  # an open row before the last
        [[0, 10, -1], [10, None, 1]],  # negative minutes
    )
    for bad in bads:
        with pytest.raises(InvalidRequest):
            validate_pacing_table(bad)

    # The player's input counts: its own render (beat 1).
    paced = clock()
    character = [line("say", 3)]
    timing = clocking.timed_turn(paced, character, say=words(30), do=words(30))
    assert [(t.start, t.end) for t in timing.beat_times] == [(0, 2), (2, 2)]
    assert [(a.source, a.minutes) for a in timing.advances] == [(StateChangeSource.PACING, 2)]
    assert clocking.timed_turn(paced, character, say=words(2)).turn_end == 0

    # Sequential renders sum, simultaneous take the largest.
    renders = [TimedBeat(1, 1, 1), TimedBeat(2, 3, 2), TimedBeat(4, 4, 1)]
    assert time_turn(renders, {}, SEQUENTIAL, 100).turn_end == 104
    assert time_turn(renders, {}, SIMULTANEOUS, 100).turn_end == 102
    # A declared skip replaces the render's pacing increment.
    skipped = [TimedBeat(1, 1, 1), TimedBeat(2, 3, 2, (TimedAdvance(0, minutes=20),))]
    found = time_turn(skipped, {}, SEQUENTIAL, 100)
    assert found.turn_end == 121
    assert [(a.source, a.minutes) for a in found.advances] == [
        (StateChangeSource.PACING, 1),
        (StateChangeSource.DECLARED_SKIP, 20),
    ]
    # A custom table through the turn's timing.
    assert clocking.timed_turn(clock(table=CUSTOM), [line("do", 100)]).turn_end == 3

    # No model call: a stub adapter's call count is unchanged by pacing and timing.
    stub = StubAdapter()
    before = len(stub.calls)
    pacing_increment([line("say", 300)])
    time_turn(renders, {}, SIMULTANEOUS, 0)
    clocking.timed_turn(paced, character, say=words(300))
    assert len(stub.calls) == before


# -- through the pipeline ------------------------------------------------------------------------


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _clock_rows(engine: AsyncEngine, story: str) -> list[tuple[str, int]]:
    found = await rows(
        engine,
        "SELECT c.source, c.delta FROM state_changes c JOIN state_definitions d "
        "ON d.id = c.state_id WHERE c.story_id = :s AND d.name = 'world_clock' "
        "AND c.membership = 'active' ORDER BY c.narrative_turn, c.beat, c.step",
        s=story,
    )
    return [(str(r.source), int(r.delta)) for r in found]


async def _turn(client: Any, engine: AsyncEngine, stub: StubAdapter, mode: str) -> dict[str, Any]:
    story, ids = await structured_story(client, "Cal", "Mira")
    await ck.set_level(engine, story, "on")
    if mode != "paced":
        patched = await client.patch(f"/stories/{story}/clock", json={"mode": mode}, headers=WRITE)
        assert patched.status_code == 200, patched.text
    stub.reset()
    ms.renders_for(
        stub, "Mira", render(beat("think", words(80)), beat("say", words(60)), beat("do", words(3)))
    )
    answer = await respond(client, story, body(ids["Cal"], words(7), do="nods"))
    assert answer.status_code == 200, answer.text
    return {"story": story, "ids": ids, "answer": answer.json(), "calls": len(stub.calls)}


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-6")
async def test_u_20__turns(
    stub_settings: Settings,
    engine: AsyncEngine,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        paced = await _turn(client, engine, stub, "paced")
        # The player's 8 words: 1 minute; Mira's 63 spoken and acted words: 2 (her think 0).
        assert await _clock_rows(engine, paced["story"]) == [("pacing", 1), ("pacing", 2)]
        assert paced["answer"]["clock"]["before"]["offset"] == 0
        assert paced["answer"]["clock"]["after"]["offset"] == 3
        assert await ck.offset(engine, paced["story"]) == 3
        manual = await _turn(client, engine, stub, "manual")
        assert await _clock_rows(engine, manual["story"]) == []
        assert manual["answer"]["clock"]["after"]["offset"] == 0
        # Pacing and timing make no model call: the same turn, the same calls.
        assert manual["calls"] == paced["calls"]
        assert manual["answer"]["call_counts"] == paced["answer"]["call_counts"]

        # Ingest is not paced: a 300-word narration and a player turn leave the clock as it is.
        story, ids = paced["story"], paced["ids"]
        ingested = await client.post(
            f"/stories/{story}/ingest",
            json={
                "events": [
                    {"client_event_id": "u20-narration", "kind": "narration", "text": words(300)},
                    {
                        "client_event_id": "u20-player",
                        "kind": "player_turn",
                        "actor_character_id": ids["Cal"],
                        "say": words(60),
                    },
                ]
            },
            headers=WRITE,
        )
        assert ingested.status_code in (200, 201), ingested.text
        assert await ck.offset(engine, story) == 3
        read = await client.get(f"/stories/{story}/clock", headers=READ)
        assert read.json()["offset"] == 3
