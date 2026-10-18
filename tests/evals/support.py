"""Shared pieces of the eval harness tests (#134): a stub ``Engine`` over the session database
and the synthetic zero-tolerance suite E-08's counterexamples run."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.evals import CaseModel, Direction, Metric, Outcome, Rule, Suite
from epistrel.evals.runner import CaseContext
from epistrel.orchestrator.models.adapter import Message
from epistrel.orchestrator.models.registry import GENERATION
from tests.auth_support import auth_settings

LEAK = "LEAKED-SECRET"
ZERO_METRIC = "synthetic.leak"


@asynccontextmanager
async def stub_engine(pg_url: str, engine: AsyncEngine) -> AsyncIterator[Engine]:
    """An in-process ``Engine`` on the stub adapter over the session database."""
    lib = Engine(auth_settings(pg_url, model_adapter="stub"), db=engine)
    lib.models.stub.reset()
    try:
        yield lib
    finally:
        lib.models.stub.reset()


class SyntheticZeroSuite(Suite):
    """A zero-tolerance metric over in-memory cases: each case makes one ``generation`` call
    through the Model Access Layer, and the answer leaks when it carries ``LEAK`` (the test
    scripts the stub to leak on one case of one seed)."""

    name = "synthetic_zero"
    title = "Synthetic zero-tolerance suite (tests only)"
    metrics = (
        Metric(ZERO_METRIC, Rule.zero, 0.0, Direction.at_most, "the answer leaked the secret"),
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        result = await ctx.models.complete(
            GENERATION.name, [Message(role="user", content="Say something harmless.")]
        )
        return Outcome({ZERO_METRIC: LEAK not in result.text}, output=result.text)


def synthetic_cases(n: int) -> list[CaseModel]:
    return [CaseModel(id=f"syn-{i:04d}") for i in range(1, n + 1)]


class FloorlessSuite(Suite):
    """A rate metric declared without a floor: ``--check`` must refuse it."""

    name = "floorless"
    title = "A suite whose rate metric has no floor (tests only)"
    metrics = (Metric("floorless.rate", Rule.rate, None),)
