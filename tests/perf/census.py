"""The turn census P-05 gates on (NFR-COST-1; #69): every Lite turn played in the test session,
with its calls by purpose and the guardrail calls G its budget reserved.

``tests/conftest.py`` installs the census for the whole session (it wraps the pipeline's
``_play``, through which every respond, regenerate and queued turn passes) and orders
``test_p_05`` last, so the census covers the scenario suite (``tests/scenario``) when it ran.
Only test ids, purposes, outcomes and counts are kept: never story text."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from epistrel.orchestrator.lite import pipeline
from epistrel.orchestrator.lite.schemas import RespondResult

SCENARIO_PREFIX = "tests/scenario/"


@dataclass(frozen=True, slots=True)
class CensusTurn:
    """One played turn: which test played it, its calls and what its budget reserved."""

    test: str
    call_counts: dict[str, int]
    outcomes: tuple[str, ...]
    reserved: int
    used: int
    max_calls: int | None

    @property
    def calls(self) -> int:
        return sum(self.call_counts.values())

    @property
    def clean(self) -> bool:
        """One generation, every call answered: no regeneration, retry or failure."""
        return self.call_counts.get("generation", 0) == 1 and all(
            outcome == "ok" for outcome in self.outcomes
        )

    @property
    def s_test(self) -> bool:
        """Played by a scenario test (``tests/scenario``, docs/04's S-tests)."""
        return self.test.startswith(SCENARIO_PREFIX)


@dataclass
class Census:
    turns: list[CensusTurn] = field(default_factory=list)
    s_tests_collected: bool = False
    """Whether the session collected any scenario test (P-05 then requires their turns)."""

    def record(self, result: RespondResult) -> None:
        budget = result.budget
        self.turns.append(
            CensusTurn(
                test=os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0],
                call_counts=dict(result.call_counts),
                outcomes=tuple(call.outcome for call in result.model_calls),
                reserved=sum(budget.reserved.values()) if budget is not None else 0,
                used=budget.used if budget is not None else len(result.model_calls),
                max_calls=budget.max_calls if budget is not None else None,
            )
        )


CENSUS = Census()


@contextmanager
def installed() -> Iterator[Census]:
    """Record every turn the pipeline plays while the block runs."""
    original: Callable[..., Any] = pipeline._play

    async def counted(*args: Any, **kwargs: Any) -> RespondResult:
        result: RespondResult = await original(*args, **kwargs)
        CENSUS.record(result)
        return result

    pipeline._play = counted
    try:
        yield CENSUS
    finally:
        pipeline._play = original
