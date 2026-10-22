"""Q-01: recall@10 and MRR (docs/04 §9; NFR-QUAL-6; #157). The ``q01`` suite builds the
S-THOSEGUYS-style 5,000-turn fixture (``thoseguys-v1``: 5,000 filler episodes, 100 planted
occasions) on a scratch story and asks its labeled queries (``evals/sets/retrieval/recall.jsonl``)
through ``Engine.retrieve`` on the stub (lexical and alias arms; no embedding model)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.authoring import retrieval
from epistrel.evals.authoring.common import case
from epistrel.evals.suites.q01_recall import MRR, RECALL, RecallCase
from tests.eval.suite_support import (
    SETS,
    assert_floors_pass,
    by_id,
    flipped,
    frozen,
    strict_verdict,
    stub_run,
)

pytestmark = pytest.mark.integration


@pytest.mark.part("M4", "NFR-QUAL-6")
async def test_q_01(pg_url: str, engine: AsyncEngine) -> None:
    """100 labeled queries, one per planted occasion, each naming its event, its day and one or
    two participants: every relevant beat is in the first ten the context receives (recall@10
    100 % on both seeds; the floor passes), first in every ranking (MRR 1.0), and the labels are
    the fixture's own builder keys at its version.

    Complement asserted: a copy of the set that labels another occasion's beats relevant fails
    the recall floor under the strict rule."""
    suite = SUITES["q01"]
    cases = [c for c in suite.load(SETS) if isinstance(c, RecallCase)]
    assert len(cases) >= 100
    keys = {b["key"] for o in retrieval.occasions() for b in o["beats"]}
    assert {k for c in cases for k in c.expected.relevant} <= keys
    assert {c.inputs.fixture for c in cases} == {retrieval.FIXTURE_VERSION}
    result = await stub_run(pg_url, engine, suite)
    assert_floors_pass(result)
    assert by_id(result)[MRR].mean == 1.0
    assert by_id(result)[RECALL].sample_size == sum(len(c.expected.relevant) for c in cases)

    first, second = cases[0], cases[1]
    wrong = flipped(cases, first.id, expected={"relevant": second.expected.relevant})
    complement = await stub_run(pg_url, engine, frozen(suite, cases), cases=wrong)
    assert strict_verdict(complement, RECALL) is Verdict.failed


TOPIC_QUERIES = (
    "What happened with {topic} at the {event}?",
    "Tell me again about {topic}.",
    "Who was there when {topic} turned up at the {event}?",
    "Remember {topic} at the {event}?",
    "Who remembers {topic} that turned up?",
)
"""#458's topic-only queries: the occasion's topic (with its event type, or none) and no time
or participant, as #157's first Q-01 templates asked them. "Tell me" names the speaker; "turned
up" shares a word with recent fillers."""


def topic_cases() -> list[RecallCase]:
    """One topic-only query per planted occasion (``TOPIC_QUERIES`` in turn); relevant = its
    beats. Not a labeled set: the committed ``recall.jsonl`` stays Q-01's resolution queries."""
    found = []
    for n, occasion in enumerate(retrieval.occasions(), start=1):
        query = TOPIC_QUERIES[(n - 1) % len(TOPIC_QUERIES)].format(
            topic=occasion["topic"], event=occasion["event"]
        )
        found.append(
            RecallCase.model_validate(
                case(
                    "recall",
                    "topic",
                    n,
                    origin="authored",
                    inputs={"fixture": retrieval.FIXTURE_VERSION, "query": query, "asker": "Cal"},
                    expected={"relevant": [b["key"] for b in occasion["beats"]]},
                )
            )
        )
    return found


async def test_q_01__topic_only_queries(pg_url: str, engine: AsyncEngine) -> None:
    """#458 (FR-RET-3,5): a query naming an older occasion by its topic, with or without its
    event type and with no time or participant, finds that occasion. Its beats each hold the
    topic's words, and they are not buried by the latest occasions of the type, by recent
    memories naming the speaker, or by recent memories sharing one common word with the query.
    Over the 100 occasions on the stub (lexical and alias arms), recall@10 is at least 0.95 on
    both seeds and the first relevant beat is ranked first almost always (MRR ≥ 0.9). Before
    the fix they were 0.47 and 0.15. This is a regression bound, not a floor: topic-only
    recall has none, and Q-01's labeled set stays its resolution queries.

    Complement asserted: the queries really are topic-only (no day, no participant named)."""
    suite = SUITES["q01"]
    cases = topic_cases()
    names = {*retrieval.EVERYONE}
    assert not any(" ago" in c.inputs.query or "yesterday" in c.inputs.query for c in cases)
    assert not any(n in c.inputs.query.split() for c in cases for n in names)
    result = await stub_run(pg_url, engine, frozen(suite, cases), cases=cases)
    recall, mrr = by_id(result)[RECALL], by_id(result)[MRR]
    assert min(recall.per_seed) >= 0.95, recall.per_seed
    assert mrr.mean is not None
    assert mrr.mean >= 0.9, mrr.mean
