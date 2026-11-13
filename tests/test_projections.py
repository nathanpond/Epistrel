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


class _Counted(Rows):
    """``Rows`` that counts the row visits ``apply`` makes: rows read (``get``, iteration,
    ``keys_where``) and the entries of every dict or list value ``put`` stores that is not
    already the stored row's (a copied or rebuilt map)."""

    visits = 0

    def get(self, key: Any) -> Any:
        _Counted.visits += 1
        return super().get(key)

    def put(self, row: Any) -> None:
        stored = super().get(self.key(row))
        for name, value in row.items():
            if isinstance(value, dict | list) and (stored is None or value is not stored[name]):
                _Counted.visits += len(value)
        super().put(row)

    def __iter__(self) -> Any:
        for row in super().__iter__():
            _Counted.visits += 1
            yield row

    def keys_where(self, column: str, value: Any) -> Any:
        found = super().keys_where(column, value)
        _Counted.visits += len(found)
        return found


def _long_story(turns: int) -> list[LogEvent]:
    """A genesis, then ``turns`` turns of two beats each, the last turn regenerated."""
    events = [log_event("config_changed", {"setting": "s"}, kind=OperationKind.GENESIS, turn=None)]
    for turn in range(1, turns + 1):
        op = f"{turn:026d}"
        for text in ("said", "reply"):
            kind = "player_input" if text == "said" else "message_rendered"
            events.append(
                log_event(kind, {"text": text}, seq=len(events) + 1, operation_id=op, turn=turn)
            )
    events.append(
        log_event(
            "message_rendered",
            {"text": "again"},
            seq=len(events) + 1,
            operation_id="R" * 26,
            turn=turns,
            variant=2,
        )
    )
    return events


@pytest.mark.parametrize("projection", [TRANSCRIPT, HEADS], ids=lambda p: p.name)
def test_folding_a_long_story_visits_a_bounded_number_of_rows_per_event(
    projection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#887: folding a story visits a bounded number of rows per event, so a fold is linear in
    the story rather than quadratic (a turn's ``_shelve`` scanned every transcript row; the
    heads' map was copied and re-sorted for every event). The folded rows stay what they were."""
    turns = 400
    events = _long_story(turns)
    rows = _Counted(projection.table)
    monkeypatch.setattr(_Counted, "visits", 0)
    for event in events:
        projection.apply(rows, event)
    assert _Counted.visits <= 4 * len(events), (_Counted.visits, len(events))
    if projection is HEADS:
        (head,) = list(rows)
        expected = {str(t): 1 for t in range(1, turns)} | {str(turns): 2}
        assert (head["next_turn_index"], head["active_variants"]) == (turns + 1, expected)
        assert list(head["active_variants"]) == [str(t) for t in range(1, turns + 1)]
    else:
        membership = [(r["narrative_turn"], r["turn_variant"], r["membership"]) for r in rows]
        assert membership.count((turns, 1, "dormant")) == 2
        assert sum(1 for m in membership if m[2] == "active") == 2 * turns - 1
