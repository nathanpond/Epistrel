"""Projection ``apply`` as a pure function (no database)."""

from __future__ import annotations

from typing import Any

import pytest

from epistrel.core.enums import Membership, OperationKind, Provenance
from epistrel.core.events.registry import EventPayload, lookup, validate
from epistrel.core.projections import (
    HEADS,
    LITE_SUMMARY,
    TRANSCRIPT,
    LogEvent,
    Rows,
    register_projection,
    registered_projections,
)
from epistrel.core.projections.heads import HeadsProjection

STORY = "S" * 26
CHARACTER = "C" * 26


def log_event(
    event_type: str,
    payload: dict[str, Any] | EventPayload,
    *,
    seq: int = 1,
    kind: OperationKind = OperationKind.TURN,
    operation_id: str = "O" * 26,
    turn: int | None = 1,
    variant: int | None = 1,
    provenance: Provenance = Provenance.ENGINE,
    character_id: str | None = None,
    membership: Membership = Membership.ACTIVE,
) -> LogEvent:
    return LogEvent(
        id=f"{seq:026d}",
        story_id=STORY,
        seq=seq,
        operation_id=operation_id,
        kind=kind,
        membership=membership,
        category=lookup(event_type).category,
        narrative_turn=turn,
        turn_variant=variant,
        beat=None,
        character_id=character_id,
        provenance=provenance,
        type=event_type,
        payload=validate(event_type, payload),
    )


def test_m2_projections_are_registered_once() -> None:
    assert [p.name for p in registered_projections()] == [
        "transcript_messages",
        "story_heads",
        "story_texts",
        "lite_summary_segments",
        # The structured projections over canon (#137).
        "relationships",
        "placement",
        "scene_state",
        # The belief projections over the partitions (#159, #167).
        "belief_state",
        "nested_belief_state",
        # The State values over the State changes (#185).
        "state_values",
        # The external values over the external_read events (#205).
        "external_values",
    ]
    with pytest.raises(ValueError, match="already registered"):
        register_projection(HeadsProjection())


@pytest.mark.parametrize(
    ("event_type", "provenance", "character_id", "role", "channel"),
    [
        ("player_input", Provenance.PLAYER, None, "player", "message"),
        ("message_rendered", Provenance.ENGINE, None, "character", "message"),
        ("message_rendered", Provenance.ENGINE, CHARACTER, "character", "message"),
        ("message_rendered", Provenance.CONSUMER, None, "narration", "narration"),
        ("message_rendered", Provenance.CONSUMER, CHARACTER, "character", "message"),
        ("think", Provenance.ENGINE, CHARACTER, "character", "think"),
        ("latent", Provenance.ENGINE, CHARACTER, "character", "latent"),
    ],
)
def test_beats_become_one_row_each(
    event_type: str,
    provenance: Provenance,
    character_id: str | None,
    role: str,
    channel: str,
) -> None:
    rows = Rows(TRANSCRIPT.table)
    event = log_event(
        event_type, {"text": "words"}, provenance=provenance, character_id=character_id
    )
    TRANSCRIPT.apply(rows, event)
    TRANSCRIPT.apply(rows, event)  # applying the same event again changes nothing
    (row,) = list(rows)
    assert (row["role"], row["channel"], row["actor_character_id"]) == (role, channel, character_id)
    assert (row["message_id"], row["kind"], row["text"]) == (event.id, event_type, "words")
    assert row["membership"] == "active"


def test_events_a_projection_does_not_know_are_no_ops() -> None:
    for projection in registered_projections():
        rows = Rows(projection.table)
        for event in (
            log_event("config_changed", {"setting": "s"}, kind=OperationKind.CONTROL, turn=None),
            log_event("guardrail_output_rejected", {"text": "x", "reason": "r"}),
            log_event(
                "lite_summary_segment",
                {"text": "x", "covers_from": 1, "covers_to": 1},
                kind=OperationKind.CONSOLIDATION,
                turn=None,
            ),
        ):
            if projection is HEADS and event.kind is OperationKind.TURN:
                continue  # any event of a turn operation positions the head
            if projection is LITE_SUMMARY and event.type == "lite_summary_segment":
                continue  # its own event type (#72)
            projection.apply(rows, event)
        assert len(rows) == 0, projection.name


def test_membership_events_flip_only_the_named_operations() -> None:
    rows = Rows(TRANSCRIPT.table)
    for seq, op in enumerate(("A" * 26, "B" * 26, "C" * 26), start=1):
        TRANSCRIPT.apply(rows, log_event("player_input", {"text": op}, seq=seq, operation_id=op))
    head = {"next_turn_index": 2, "active_variants": {"1": 1}}
    TRANSCRIPT.apply(
        rows,
        log_event(
            "swap",
            {
                "turn": 1,
                "activated_operation_ids": ["A" * 26],
                "dormant_operation_ids": ["B" * 26],
                "active_head": head,
            },
            seq=4,
            kind=OperationKind.CONTROL,
            turn=None,
            variant=None,
        ),
    )
    by_op = {row["operation_id"][0]: row["membership"] for row in rows}
    assert by_op == {"A": "active", "B": "dormant", "C": "active"}


def test_heads_install_the_recorded_head() -> None:
    rows = Rows(HEADS.table)
    head = {"next_turn_index": 4, "active_variants": {"3": 2, "1": 1, "2": 1}}
    HEADS.apply(
        rows,
        log_event(
            "untruncate",
            {"truncate_operation_id": "T" * 26, "operation_ids": [], "restored_head": head},
            kind=OperationKind.CONTROL,
            turn=None,
            variant=None,
        ),
    )
    (row,) = list(rows)
    assert row["next_turn_index"] == 4
    assert list(row["active_variants"].items()) == [("1", 1), ("2", 1), ("3", 2)]
