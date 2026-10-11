"""Model-call accounting (FR-MODEL-12, #54): every attempt is recorded once with its outcome,
and the counters move with it. Stub-driven, no database; C-04's ``call_counts`` parts cover
both adapters against a database."""

from __future__ import annotations

import asyncio

import pytest
from openai.types import CompletionUsage

from epistrel.config import Settings
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import metrics
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.errors import (
    BudgetExhausted,
    EmptyCompletion,
    ModelUnavailable,
)
from epistrel.orchestrator.models.openai_compatible import reported_cost
from epistrel.orchestrator.models.registry import (
    GenerationInputs,
    GuardrailCheckInputs,
    GuardrailVerdict,
    PlayerInput,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.models.stub import FaultKind
from tests.auth_support import auth_settings

HELLO = GenerationInputs(player_input=PlayerInput(name="Player", say="hello"))
CHECK = GuardrailCheckInputs(text="a quiet road", policy_prompt="flag nothing")


@pytest.fixture
def models(dummy_dsn: str) -> ModelAccessLayer:
    settings: Settings = auth_settings(dummy_dsn, model_adapter="stub")
    layer = ModelAccessLayer(settings)
    layer.bind(None)
    return layer


def nothing() -> GuardrailVerdict | None:
    return None


@pytest.mark.parametrize(
    ("kind", "outcome", "error"),
    [
        ("timeout", "timeout", ModelUnavailable),
        ("connection", "http_error", ModelUnavailable),
        ("http_500", "http_error", ModelUnavailable),
        ("http_429", "http_error", ModelUnavailable),
        ("http_401", "http_error", ModelUnavailable),
        ("bad_body", "invalid_output", ModelUnavailable),
        ("empty", "invalid_output", EmptyCompletion),
    ],
)
async def test_failed_attempt_is_recorded_with_its_outcome(
    models: ModelAccessLayer, kind: FaultKind, outcome: str, error: type[Exception]
) -> None:
    """Complement asserted: the next, clean call on the same layer is recorded as ``ok``."""
    before = metrics.sample(
        "epistrel_model_calls_total", purpose="generation", model="stub", outcome=outcome
    )
    models.stub.fault("generation", kind)
    with metrics.recording() as log:
        with pytest.raises(error):
            await models.call("generation", HELLO)
        await models.call("generation", HELLO)
    assert [c.outcome for c in log.calls] == [outcome, "ok"]
    if kind != "empty":  # an empty answer arrived (with usage); the others never did
        assert (log.calls[0].tokens_in, log.calls[0].tokens_out) == (0, 0)
    after = metrics.sample(
        "epistrel_model_calls_total", purpose="generation", model="stub", outcome=outcome
    )
    assert after - before == 1


async def test_hung_call_is_a_timeout(models: ModelAccessLayer) -> None:
    models.stub.fault("generation", "hang")
    with metrics.recording() as log, pytest.raises(ModelUnavailable):
        await models.call("generation", HELLO, ModelParams(timeout=0.05))
    assert [c.outcome for c in log.calls] == ["timeout"]
    assert log.calls[0].latency_ms >= 40


async def test_cancelled_call_is_recorded(models: ModelAccessLayer) -> None:
    latch = models.stub.hold("generation")
    with metrics.recording() as log:
        task = asyncio.create_task(models.call("generation", HELLO))
        await latch.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [c.outcome for c in log.calls] == ["cancelled"]


async def test_each_structured_attempt_is_an_entry(models: ModelAccessLayer) -> None:
    """Garbage answers are ``invalid_output`` attempts numbered 1..3 (two retries by default);
    then a good answer on a fresh call is attempt 1, ``ok``."""
    models.stub.fault("guardrail_check", "garbage", n=3)
    with metrics.recording() as log:
        result = await models.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            CHECK,
            deterministic_first=nothing,
            budget=TurnBudget(),
        )
        assert isinstance(result, Uncertain)
        good = await models.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            CHECK,
            deterministic_first=nothing,
            budget=TurnBudget(),
        )
        assert isinstance(good, GuardrailVerdict)
    assert [(c.attempt, c.outcome) for c in log.calls] == [
        (1, "invalid_output"),
        (2, "invalid_output"),
        (3, "invalid_output"),
        (1, "ok"),
    ]


async def test_calls_not_made_are_not_recorded(models: ModelAccessLayer) -> None:
    with metrics.recording() as log, pytest.raises(BudgetExhausted):
        await models.call("generation", HELLO, budget=TurnBudget(max_calls=0))
    assert log.calls == []


async def test_recording_is_scoped(models: ModelAccessLayer) -> None:
    """Calls outside a ``recording()`` block are counted but logged nowhere."""
    await models.call("generation", HELLO)
    with metrics.recording() as log:
        await models.call("generation", HELLO)
    await models.call("generation", HELLO)
    assert len(log.calls) == 1
    assert metrics.CALL_LOG.get() is None


def test_model_label_is_sanitised() -> None:
    assert metrics.model_label("Cydonia-24B v4.3/Q4_K_M") == "cydonia-24b_v4.3_q4_k_m"
    assert metrics.model_label("") == "unknown"
    assert metrics.model_label(None) == "unknown"
    assert len(metrics.model_label("x" * 500)) == metrics.MAX_LABEL_CHARS


def test_outcome_of_unknown_errors() -> None:
    assert metrics.outcome_of(TimeoutError()) == "timeout"
    assert metrics.outcome_of(asyncio.CancelledError()) == "cancelled"
    assert metrics.outcome_of(ModelUnavailable("generation", "deadline")) == "timeout"
    assert metrics.outcome_of(ModelUnavailable("generation", "garbage")) == "invalid_output"
    assert metrics.outcome_of(ModelUnavailable("generation", "auth")) == "http_error"


def test_cost_is_omitted_when_unreported() -> None:
    call = metrics.ModelCall(
        purpose="generation",
        model="m",
        attempt=1,
        outcome="ok",
        latency_ms=5,
        tokens_in=1,
        tokens_out=1,
    )
    assert "cost" not in call.model_dump()
    assert call.model_copy(update={"cost": 0.0}).model_dump()["cost"] == 0.0


def test_reported_cost_reads_usage_cost_only() -> None:
    usage = CompletionUsage.model_validate(
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.002}
    )
    assert reported_cost(usage) == 0.002
    plain = CompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    assert reported_cost(plain) is None
    assert reported_cost(None) is None
    for bad in (-1, True, "0.1"):
        weird = CompletionUsage.model_validate(
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": bad}
        )
        assert reported_cost(weird) is None
