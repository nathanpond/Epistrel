"""Pure rules of the idempotency store (#61): the key rule, the request hash, the result
codec, the reference body, and the injectable clock."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from epistrel.core import clock
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import InvalidRequest
from epistrel.core.idempotency import (
    ReferenceReplay,
    ResultCodec,
    StoredResult,
    mark_replayed,
    request_hash,
    revision_of,
    validate_key,
)
from epistrel.core.stories import StatusChange
from epistrel.testing import faults


def test_key_rule() -> None:
    for good in ("a", "A-z.0_9:x", "k" * 255):
        assert validate_key(good) == good
    for bad in ("", "k" * 256, "white space", "comma,key", "ä"):
        with pytest.raises(InvalidRequest) as refused:
            validate_key(bad)
        assert refused.value.details == {"field": "headers.Idempotency-Key"}


def test_request_hash_is_canonical() -> None:
    """Key order never matters; every value and the operation name do."""

    def material(body: object, viewpoint: str = "omniscient") -> dict[str, object]:
        return {"body": body, "viewpoint": viewpoint}

    one = request_hash("respond", material({"a": 1, "b": [1, 2]}))
    two = request_hash("respond", {"viewpoint": "omniscient", "body": {"b": [1, 2], "a": 1}})
    assert one == two
    assert len(one) == 64
    assert request_hash("respond", material({"a": 2, "b": [1, 2]})) != one
    assert request_hash("ingest", material({"a": 1, "b": [1, 2]})) != one
    assert request_hash("respond", material({"a": 1, "b": [2, 1]})) != one
    assert request_hash("respond", material({"a": 1, "b": [1, 2]}, "character:x")) != one


def test_codec_round_trip_marks_the_replay() -> None:
    codec: ResultCodec[StatusChange] = ResultCodec.of(
        StatusChange, status=lambda change: 200 if change.changed else 204
    )
    change = StatusChange(StoryStatus.ARCHIVED, 3, changed=True)
    stored = codec.store(change)
    assert stored == StoredResult(
        200, {"status": "archived", "timeline_revision": 3, "changed": True, "replayed": False}
    )
    assert StoredResult.from_json(stored.to_json()) == stored
    loaded = codec.load(stored)
    assert loaded == dataclasses.replace(change, replayed=True)
    assert codec.store(dataclasses.replace(change, changed=False)).status == 204
    created: ResultCodec[StatusChange] = ResultCodec.of(StatusChange, status=201)
    assert created.store(change).status == 201
    assert mark_replayed({"plain": 1}) == {"plain": 1}
    assert revision_of(change) == 3
    assert revision_of(object()) is None


def test_reference_body() -> None:
    reference = ReferenceReplay(
        key="k-1", operation_id="0" * 26, revision=7, revision_field="config_revision"
    )
    assert reference.body() == {
        "operation_id": "0" * 26,
        "reference": f"/operations/{'0' * 26}",
        "config_revision": 7,
        "idempotency_key": "k-1",
    }
    nothing = ReferenceReplay(
        key="k-2", operation_id=None, revision=None, revision_field="timeline_revision"
    )
    assert nothing.body()["reference"] is None


def test_clock_is_injectable() -> None:
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    with clock.frozen_at(moment):
        assert clock.now() == moment
    assert clock.now() != moment
    clock.set_clock(lambda: moment)
    try:
        assert clock.now() == moment
    finally:
        clock.set_clock(None)
    assert clock.now().tzinfo is not None


def test_faults_entry_point_usage() -> None:
    assert faults.main([]) == 2
    assert faults.main(["other"]) == 2
    assert 1024 < faults.free_port() < 65536
