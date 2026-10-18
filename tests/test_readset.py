"""``core.readset`` without a database (#136): the empty-result sentinel and result hashes, query
keys' shape and identity, dedupe and the within-attempt consistency check, the stored document
and its counts, and the literals of the intersection query."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from epistrel.core.propositions.model import TypedLiteral
from epistrel.core.propositions.vocabulary import SAME_AS
from epistrel.core.readset import (
    EMPTY_RESULT,
    TOUCH_SQL,
    QueryKey,
    ReadSetInconsistent,
    StructuredReadSet,
    holder_of,
    literal_hash,
    read_set_counts,
    result_hash,
    stored_keys,
)
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT

T0 = datetime(2026, 10, 17, tzinfo=UTC)
A, B, P = "01A" + "0" * 23, "01B" + "0" * 23, "01P" + "0" * 23


def test_result_hashes() -> None:
    assert hashlib.sha256(b"").hexdigest() == EMPTY_RESULT
    assert result_hash([]) == EMPTY_RESULT
    one = result_hash([(A, T0), (B, T0)])
    assert one == result_hash([(B, T0), (A, T0)])  # sorted pairs
    assert one != result_hash([(A, T0)])
    assert result_hash([(A, T0)]) != result_hash([(A, T0.replace(microsecond=1))])


def test_literals_hash_by_value() -> None:
    forty = TypedLiteral(kind="int", value=40)
    assert literal_hash(forty) == literal_hash(TypedLiteral(kind="float", value=40.0))
    assert literal_hash(forty) != literal_hash(TypedLiteral(kind="str", value="40"))


def test_query_key_shape() -> None:
    key = QueryKey(subjects=(B, A, A), predicate_id=P)
    assert key.subjects == (A, B)
    assert key.holder == "canon"
    assert key.view()["subject"] == [A, B]
    assert key.view()["object"] == "*"
    assert holder_of(None) == "canon"
    assert holder_of(A) == f"utterance:{A}"
    with pytest.raises(ValueError, match="one object"):
        QueryKey(subjects=None, predicate_id=P, object_id=A, literal={"kind": "int", "value": 1})
    with pytest.raises(ValueError, match="holder"):
        QueryKey(subjects=None, predicate_id=P, holder="belief")
    with pytest.raises(ValueError, match="window"):
        QueryKey(subjects=None, predicate_id=P, valid_window=(5, 5))


def test_keys_dedupe_and_must_agree_within_an_attempt() -> None:
    read_set = StructuredReadSet()
    read_set.start_attempt()
    first = read_set.record(QueryKey(subjects=(A,), predicate_id=P))
    assert read_set.record(QueryKey(subjects=(A,), predicate_id=P)) is first
    assert len(read_set.query_keys) == 1
    with pytest.raises(ReadSetInconsistent):
        read_set.record(QueryKey(subjects=(A,), predicate_id=P, result_hash="0" * 64))
    read_set.record_retrieved("memories", B, T0)
    assert read_set.counts() == {"keys_count": 1, "retrieved_count": 1}
    document = read_set.document({"window": "h"})
    assert document["lite"] == {"window": "h"}
    assert read_set_counts(document) == {"keys_count": 1, "retrieved_count": 1}
    assert stored_keys(document) == [first]
    assert read_set_counts({"window": "h"}) == {"keys_count": 0, "retrieved_count": 0}
    # A new attempt reads afresh.
    read_set.start_attempt()
    assert read_set.counts() == {"keys_count": 0, "retrieved_count": 0}
    assert read_set.attempt == 2


def test_the_intersection_literals() -> None:
    """``TOUCH_SQL`` spells these constants out (it is one literal statement)."""
    sql = str(TOUCH_SQL)
    assert f"'{OMNISCIENT}'" in sql
    assert f"'{CHARACTER_VIEWPOINT_PREFIX}%'" in sql
    assert f"substr(k.viewpoint, {len(CHARACTER_VIEWPOINT_PREFIX) + 1})" in sql
    assert f"'{SAME_AS}'" in sql
