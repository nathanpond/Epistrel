"""docs/04 I-02, M2 part: a rebuilt projection equals the incrementally maintained one."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.projections import rebuild
from epistrel.core.views import corrected_history
from tests.auth_support import auth_settings
from tests.concurrency_support import set_instructions, set_pins
from tests.event_support import (
    execute_in_tx,
    new_story,
    play_turn,
    record,
    scalar,
    segment_payload,
)
from tests.projection_support import snapshot, swap, truncate, untruncate

BEAT_TYPES = frozenset({"player_input", "message_rendered", "think", "latent"})
BOGUS_ID = "0" * 26
CHARACTER_ID = "01J00000000000000000000CHR"

Action = tuple[str, int]

OCCURRED: Counter[str] = Counter()
"""Timeline actions that changed membership, across the examples of one run."""

actions = st.lists(
    st.tuples(
        st.sampled_from(
            [
                "turn",
                "turn",
                "turn",
                "turn_with_rejection",
                "private_beat",
                "narration",
                "control",
                "consolidation",
                "truncate",
                "untruncate",
                "swap",
                "rewind_and_restore",
                "rewind_play_and_swap",
                "regenerate",
                "pins",
                "instructions",
            ]
        ),
        st.integers(min_value=0, max_value=20),
    ),
    max_size=14,
)


async def _turn_with_beats(engine: AsyncEngine, story_id: str, variant: str) -> None:
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal="p-test"
    ) as op:
        if variant == "narration":
            op.append(
                "message_rendered", {"text": "the rain begins"}, provenance=Provenance.CONSUMER
            )
        else:
            op.append("player_input", {"text": "hello"}, provenance=Provenance.PLAYER)
            op.append("think", {"text": "who is this?"}, character_id=CHARACTER_ID)
            op.append("message_rendered", {"text": "hi"}, character_id=CHARACTER_ID)
        await op.commit()


async def _perform(engine: AsyncEngine, story_id: str, action: Action, n: int) -> None:
    name, number = action
    head = int(
        await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id)
    )
    if name == "turn":
        await play_turn(engine, story_id, f"line {n}")
    elif name == "turn_with_rejection":
        await play_turn(engine, story_id, f"line {n}", rejected=f"draft {n}")
    elif name in ("private_beat", "narration"):
        await _turn_with_beats(engine, story_id, name)
    elif name == "control":
        await record(
            engine, story_id, OperationKind.CONTROL, "config_changed", {"setting": "s", "new": n}
        )
    elif name == "consolidation" and head > 1:
        through = 1 + number % (head - 1)
        await record(
            engine,
            story_id,
            OperationKind.CONSOLIDATION,
            "lite_summary_segment",
            segment_payload(f"summary {n}", through),
            turn=through,
        )
    elif name == "truncate":
        done = await truncate(engine, story_id, number % head)
        OCCURRED["truncate"] += done is not None and done.discarded_operations > 0
    elif name in ("rewind_and_restore", "rewind_play_and_swap"):
        # Play a turn, then truncate to before it, so the rewind always discards something.
        await play_turn(engine, story_id, f"line {n}")
        await _perform(engine, story_id, ("truncate", number % head), n)
        if name == "rewind_and_restore":
            await _perform(engine, story_id, ("untruncate", number), n)
        elif name == "rewind_play_and_swap":
            await play_turn(engine, story_id, f"line {n}'")
            await _perform(engine, story_id, ("swap", number), n)
    elif name == "regenerate" and head > 1:
        # A new variant of the head turn; the old one goes dormant in the same commit (#47).
        await play_turn(engine, story_id, f"line {n}, again", turn=head - 1)
        OCCURRED["regenerate"] += 1
    elif name == "pins":
        # #60's story-level text: the latest snapshot wins in ``story_texts``.
        await set_pins(engine, story_id, *(f"pin {n}.{i}" for i in range(number % 4)))
        OCCURRED["pins"] += 1
    elif name == "instructions":
        await set_instructions(engine, story_id, f"instructions {n}" if number % 3 else "")
        OCCURRED["instructions"] += 1
    elif name == "untruncate":
        OCCURRED["untruncate"] += await untruncate(engine, story_id) is not None
    elif name == "swap":
        OCCURRED["swap"] += await swap(engine, story_id) is not None


async def _corrupt(engine: AsyncEngine, story_id: str) -> None:
    """Change a stored transcript row, add a row the log never produced, break the head, and
    corrupt any summary segment rows (#72)."""
    await execute_in_tx(
        engine,
        (
            "UPDATE transcript_messages SET text = 'corrupted', membership = 'discarded' "
            "WHERE message_id = (SELECT min(message_id) FROM transcript_messages "
            "WHERE story_id = :s)",
            {"s": story_id},
        ),
        (
            "INSERT INTO transcript_messages (message_id, story_id, seq, operation_id, kind, "
            "channel, role, text, membership) VALUES (:id, :s, 0, :id, 'player_input', "
            "'message', 'player', 'never said', 'active')",
            {"id": BOGUS_ID, "s": story_id},
        ),
        (
            "UPDATE story_heads SET next_turn_index = 99, active_variants = '{\"7\": 3}' "
            "WHERE story_id = :s",
            {"s": story_id},
        ),
        (
            "UPDATE story_texts SET custom_instructions = 'corrupted', "
            'pinned = \'[{"id": "x", "text": "never pinned"}]\' WHERE story_id = :s',
            {"s": story_id},
        ),
        (
            "UPDATE lite_summary_segments SET text = 'corrupted', membership = 'dormant' "
            "WHERE story_id = :s",
            {"s": story_id},
        ),
    )


async def _scenario(url: str, steps: list[Action]) -> None:
    engine = make_engine(auth_settings(url))
    try:
        story_id = await new_story(engine)
        for n, action in enumerate(steps):
            await _perform(engine, story_id, action, n)
        incremental = await snapshot(engine, story_id)

        # Independent of `apply`: the active rows are exactly the beats of the corrected history,
        # and the head mirrors the story's authoritative next turn index.
        history = await corrected_history(engine, story_id)
        expected_active = sorted(e.id for e in history if e.type in BEAT_TYPES)
        active = await scalar(
            engine,
            "SELECT coalesce(array_agg(message_id ORDER BY message_id), '{}') "
            "FROM transcript_messages WHERE story_id = :s AND membership = 'active'",
            s=story_id,
        )
        assert list(active) == expected_active
        assert await scalar(
            engine,
            "SELECT h.next_turn_index = s.next_turn_index FROM story_heads h "
            "JOIN stories s ON s.id = h.story_id WHERE h.story_id = :s",
            s=story_id,
        )

        await _corrupt(engine, story_id)
        assert await snapshot(engine, story_id) != incremental
        result = await rebuild(engine, story_id, principal="p-operator")
        rebuilt = await snapshot(engine, story_id)

        assert rebuilt == incremental  # byte for byte, as canonical JSON in primary-key order
        assert result.counts == {
            "transcript_messages": len(incremental.transcript),
            "story_heads": 1,
            "story_texts": 1,
            "lite_summary_segments": len(incremental.segments),
            # The structured projections over canon (#137): a Lite story has none.
            "relationships": 0,
            "placement": 0,
            "scene_state": 0,
            # The belief projections over the partitions (#159, #167): a Lite story has none.
            "belief_state": 0,
            "nested_belief_state": 0,
            "state_values": 0,
            "external_values": 0,
        }
        # The corrupted row is replaced and the invented row is gone, not kept.
        assert all("corrupted" not in row and BOGUS_ID not in row for row in rebuilt.transcript)
        assert all("corrupted" not in row for row in rebuilt.texts)
    finally:
        await engine.dispose()


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=actions)
# Always run once: random draws favour the list's first entries, and a run whose draws never
# reached the later ones would fail the occurrence check for want of trying (#60). The example
# alone makes every kind the occurrence check names occur, whatever the random draws do (#276):
# turns 1-3 are truncated away, restored, truncated again, written over and swapped back.
@example(
    steps=[
        ("turn", 0),
        ("turn", 1),
        ("turn", 2),
        ("truncate", 1),
        ("untruncate", 0),
        ("truncate", 1),
        ("turn", 3),
        ("swap", 0),
        ("pins", 3),
        ("instructions", 1),
        ("regenerate", 0),
        ("pins", 0),
        ("instructions", 0),
    ]
)
def _every_sequence(pg_url: str, steps: list[Action]) -> None:
    asyncio.run(_scenario(pg_url, steps))


@pytest.mark.part("M2", "FR-STORE-4; FR-ADMIN-3")
def test_i_02(pg_url: str) -> None:
    """For any sequence of turns, background and control work, truncations, untruncations,
    swaps, regenerations and story-level text edits (#60), the projection rebuilt by folding
    the log equals the incrementally maintained one. Complement: a projection corrupted before
    the rebuild differs from it, and the rebuild replaces the corrupted rows rather than keeping
    them. The run must have exercised every membership change and text edit, or the equality
    says nothing about them."""
    OCCURRED.clear()
    _every_sequence(pg_url)
    kinds = ("truncate", "untruncate", "swap", "regenerate", "pins", "instructions")
    assert all(OCCURRED[kind] > 0 for kind in kinds), OCCURRED
