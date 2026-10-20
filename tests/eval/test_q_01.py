"""Q-01: recall@10 and MRR (docs/04 §9; NFR-QUAL-6; #157). The ``q01`` suite builds the
S-THOSEGUYS-style 5,000-turn fixture (``thoseguys-v1``: 5,000 filler episodes, 100 planted
occasions) on a scratch story and asks its labeled queries (``evals/sets/retrieval/recall.jsonl``)
through ``Engine.retrieve`` on the stub (lexical and alias arms; no embedding model)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.authoring import retrieval
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
