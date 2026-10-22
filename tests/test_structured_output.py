"""Structured model results and provider failures without a database (#53; FR-MODEL-5,6,7,
NFR-REL-3): the json_schema path, the 400/prose fallback to parse-validate-retry, strict
validation, ``Uncertain`` and its reasons, the budget and the deadline, and the mapping of every
provider failure to ``ModelUnavailable``. The C-04 slices and X-05 run the same paths through a
real operation."""

from __future__ import annotations

import asyncio
import json
import time

import httpx2
import openai
import pytest

from epistrel.core.errors import Unauthenticated
from epistrel.core.settings_schema import StructuredSettings
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import structured_settings
from epistrel.orchestrator.models.errors import (
    BudgetExhausted,
    EmptyCompletion,
    ModelUnavailable,
    ProviderGarbage,
    ResponseFormatRejected,
    map_provider_error,
)
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import example_instance
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    GuardrailCheckInputs,
    GuardrailVerdict,
)
from epistrel.orchestrator.models.structured import (
    SCHEMA_INSTRUCTION,
    MissingDeterministicStep,
    ResponseFormatMemo,
    Uncertain,
    guardrail_deterministic_first,
    parse,
    resolve_uncertain,
    uncertain_verdict,
    unfence,
)
from epistrel.orchestrator.models.stub import GARBAGE, RETRY_AFTER
from tests.auth_support import auth_settings
from tests.conftest import DUMMY_DSN
from tests.model_support import config_key, mock_openai
from tests.policy_support import policy
from tests.test_model_adapters import configured, fixed

CHECK = GuardrailCheckInputs(text="the text being checked", policy_prompt="no dragons")
GENERATION = example_instance(DELIVERED["generation"].input_schema)


def nothing_conclusive() -> GuardrailVerdict | None:
    return None


def models(retries: int = 2, uncertain_below: float = 0.5) -> ModelAccessLayer:
    """A stub Model Access Layer with no database and the given structured settings."""
    mal = ModelAccessLayer(auth_settings(DUMMY_DSN, model_adapter="stub"))
    chosen = StructuredSettings(structured_retry_budget=retries, uncertain_below=uncertain_below)

    async def settings() -> StructuredSettings:
        return chosen

    mal.structured_settings = settings  # type: ignore[method-assign]
    return mal


async def check(
    mal: ModelAccessLayer, budget: TurnBudget | None = None
) -> GuardrailVerdict | Uncertain:
    return await mal.structured_call(
        "guardrail_check",
        GuardrailVerdict,
        CHECK,
        deterministic_first=nothing_conclusive,
        budget=budget or TurnBudget(),
    )


def verdict(violation: bool, confidence: float) -> str:
    return json.dumps({"violation": violation, "confidence": confidence})


# -- the json_schema path ---------------------------------------------------------------------


async def test_json_schema_path_validates_in_one_call() -> None:
    mal = models()
    budget = TurnBudget()
    result = await check(mal, budget)
    assert result == GuardrailVerdict(violation=False, confidence=1.0)
    (call,) = mal.stub.calls
    assert call.response_schema == "GuardrailVerdict"
    assert all(SCHEMA_INSTRUCTION not in m.content for m in call.messages)  # not in the prompt
    assert call.fields == ("policy_prompt", "subjects", "text")
    assert budget.used == 1


async def test_schema_invalid_json_in_json_schema_mode_is_retried_in_that_mode() -> None:
    mal = models()
    mal.stub.script("guardrail_check", ['{"violation": "yes", "confidence": 1}'])
    result = await check(mal)
    assert result == GuardrailVerdict(violation=False, confidence=1.0)
    first, second = mal.stub.calls
    assert second.response_schema == "GuardrailVerdict"
    assert [m.role for m in second.messages] == ["system", "user", "assistant", "user"]
    assert second.messages[2].content == '{"violation": "yes", "confidence": 1}'
    assert "violation" in second.messages[3].content  # the validation error is fed back
    assert second.params.temperature == 0.0
    assert first.params.temperature is None


# -- the fallback: 400 or prose ---------------------------------------------------------------


async def test_provider_400_falls_back_and_succeeds_on_attempt_two() -> None:
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400")
    budget = TurnBudget()
    assert await check(mal, budget) == GuardrailVerdict(violation=False, confidence=1.0)
    probe, fallback = mal.stub.calls
    assert probe.response_schema == "GuardrailVerdict"
    assert fallback.response_schema is None
    assert SCHEMA_INSTRUCTION in fallback.messages[0].content
    assert json.dumps(GuardrailVerdict.model_json_schema(), sort_keys=True, separators=(",", ":"))[
        :40
    ] in (fallback.messages[0].content)
    assert budget.used == 2  # the probe debits the turn budget

    # Remembered per (base_url, model): the next call goes straight to the fallback.
    await check(mal)
    assert mal.stub.calls[-1].response_schema is None
    assert len(mal.stub.calls) == 3


async def test_the_probe_does_not_spend_a_retry() -> None:
    mal = models(retries=0)
    mal.stub.fault("guardrail_check", "response_format_400")
    assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
    assert len(mal.stub.calls) == 2


PROSE = "Sure! The text looks fine to me."


async def test_one_prose_answer_is_a_glitch_and_the_retry_stays_constrained() -> None:
    # #482: a single non-JSON 200 in json_schema mode is not proof the endpoint ignores it.
    mal = models()
    mal.stub.script("guardrail_check", [PROSE])
    assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
    first, second = mal.stub.calls
    assert first.response_schema == "GuardrailVerdict"
    assert second.response_schema == "GuardrailVerdict"  # the retry keeps response_format
    assert all(SCHEMA_INSTRUCTION not in m.content for m in second.messages)
    assert second.messages[-2].content == PROSE
    await check(mal)  # and the endpoint stays constrained for the next call
    assert mal.stub.calls[-1].response_schema == "GuardrailVerdict"


async def test_two_prose_answers_in_a_row_flip_to_the_fallback() -> None:
    mal = models()
    mal.stub.script("guardrail_check", [PROSE, PROSE])
    assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
    first, second, third = mal.stub.calls
    assert first.response_schema == second.response_schema == "GuardrailVerdict"
    assert third.response_schema is None
    assert SCHEMA_INSTRUCTION in third.messages[0].content
    await check(mal)  # remembered for the endpoint
    assert mal.stub.calls[-1].response_schema is None


async def test_prose_strikes_count_across_calls_to_the_endpoint() -> None:
    mal = models(retries=0)
    mal.stub.script("guardrail_check", [PROSE])
    assert await check(mal) == Uncertain("parse_error", 1)
    mal.stub.script("guardrail_check", [PROSE])
    assert await check(mal) == Uncertain("parse_error", 1)
    assert [c.response_schema for c in mal.stub.calls] == ["GuardrailVerdict"] * 2
    await check(mal)
    assert mal.stub.calls[-1].response_schema is None


@pytest.mark.parametrize("answer", [verdict(True, 0.9), '{"violation": "yes", "confidence": 1}'])
async def test_a_json_answer_resets_the_prose_run(answer: str) -> None:
    # Valid or schema-invalid, JSON means json_schema was obeyed.
    mal = models(retries=0)
    mal.stub.script("guardrail_check", [PROSE, answer, PROSE])
    for _ in range(3):
        await check(mal)
    await check(mal)
    assert [c.response_schema for c in mal.stub.calls] == ["GuardrailVerdict"] * 4


async def test_a_400_naming_response_format_still_rejects_at_once() -> None:
    mal = models(retries=0)
    mal.stub.script("guardrail_check", [PROSE])
    assert await check(mal) == Uncertain("parse_error", 1)  # one strike, still constrained
    mal.stub.fault("guardrail_check", "response_format_400")
    assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
    probe, fallback = mal.stub.calls[1:]
    assert probe.response_schema == "GuardrailVerdict"
    assert fallback.response_schema is None
    await check(mal)
    assert mal.stub.calls[-1].response_schema is None


def test_memo_strikes_reset_with_the_revision_and_clear() -> None:
    memo = ResponseFormatMemo()
    key = ("http://a/v1", "m")
    assert not memo.strike(key, 3)
    assert not memo.strike(key, 4)  # a configuration write starts the run again
    assert memo.strike(key, 4)
    assert not memo.supported(key, 4)
    memo.clear()
    assert not memo.strike(key, 4)
    memo.clear()  # E-07 clears after each injected case: no strike carries over
    assert not memo.strike(key, 4)
    memo.honoured(key)
    assert not memo.strike(key, 4)
    assert memo.supported(key, 4)


def test_memo_is_per_endpoint_and_resets_when_the_revision_moves() -> None:
    memo = ResponseFormatMemo()
    key = ("http://a/v1", "m")
    assert memo.supported(key, 3)
    memo.reject(key, 3)
    assert not memo.supported(key, 3)
    assert memo.supported(("http://a/v1", "other"), 3)
    assert memo.supported(key, 4)  # a configuration write: try json_schema again
    assert memo.supported(key, 3)
    memo.reject(key, 4)
    memo.clear()
    assert memo.supported(key, 4)


# -- parsing and Uncertain ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('```json\n{"violation": true, "confidence": 0.9}\n```', "ok"),
        ('```\n{"violation": true, "confidence": 0.9}\n```', "ok"),
        ('{"violation": true, "confidence": 1}', "ok"),
        (GARBAGE, "parse_error"),
        ("", "parse_error"),
        ("I think it is fine.", "parse_error"),
        ('{"violation": true, "confidence": 0.9, "reason": "x"}', "schema_error"),
        ('{"violation": "true", "confidence": 0.9}', "schema_error"),
        ('{"violation": true}', "schema_error"),
        ('{"violation": true, "confidence": 7}', "schema_error"),
        ("[true, 0.9]", "schema_error"),
    ],
)
def test_parse_is_strict(text: str, expected: str) -> None:
    outcome = parse(text, GuardrailVerdict)
    if expected == "ok":
        assert isinstance(outcome, GuardrailVerdict)
    else:
        assert not isinstance(outcome, GuardrailVerdict)
        assert outcome.reason == expected
        # The problem names what was wrong, never the answer's contents.
        assert "dragons" not in outcome.problem


def test_unfence_leaves_unfenced_text_alone() -> None:
    assert unfence('{"a": 1}') == '{"a": 1}'
    assert unfence('```JSON\n{"a": 1}\n```') == '{"a": 1}'


async def test_garbage_until_the_retries_run_out_is_uncertain() -> None:
    mal = models()
    for _ in range(3):
        mal.stub.fault("guardrail_check", "garbage")
    result = await check(mal)
    assert result == Uncertain("parse_error", 3)
    assert len(mal.stub.calls) == 3  # one attempt + structured_retry_budget (2)
    assert GARBAGE not in repr(result)
    conservative = resolve_uncertain(result, uncertain_verdict("violation"))
    assert conservative == GuardrailVerdict(violation=True, confidence=0.0)
    assert not resolve_uncertain(result, uncertain_verdict("pass")).violation


async def test_schema_errors_until_the_retries_run_out_are_uncertain() -> None:
    mal = models(retries=1)
    mal.stub.script("guardrail_check", ['{"violation": 1}', '{"violation": 1}'])
    assert await check(mal) == Uncertain("schema_error", 2)


async def test_a_retry_that_succeeds_is_accepted() -> None:
    mal = models()
    mal.stub.fault("guardrail_check", "garbage")
    mal.stub.script("guardrail_check", [verdict(True, 0.8)])
    assert await check(mal) == GuardrailVerdict(violation=True, confidence=0.8)


@pytest.mark.parametrize(("confidence", "uncertain"), [(0.49, True), (0.5, False), (0.0, True)])
async def test_low_confidence_is_uncertain_at_once(confidence: float, uncertain: bool) -> None:
    mal = models()
    mal.stub.script("guardrail_check", [verdict(True, confidence)])
    result = await check(mal)
    if uncertain:
        assert result == Uncertain("low_confidence", 1)
    else:
        assert result == GuardrailVerdict(violation=True, confidence=confidence)
    assert len(mal.stub.calls) == 1


async def test_uncertain_below_is_a_deployment_setting() -> None:
    mal = models(uncertain_below=0.9)
    mal.stub.script("guardrail_check", [verdict(False, 0.8)])
    assert await check(mal) == Uncertain("low_confidence", 1)


@pytest.mark.parametrize("kind", ["refusal", "length"])
async def test_refusal_or_truncation_is_a_parse_error_at_once(kind: str) -> None:
    mal = models()
    mal.stub.fault("guardrail_check", kind)  # type: ignore[arg-type]
    assert await check(mal) == Uncertain("parse_error", 1)
    assert len(mal.stub.calls) == 1


def test_structured_settings_parse_with_conservative_fallbacks() -> None:
    assert structured_settings(None) == StructuredSettings()
    assert structured_settings({"structured_retry_budget": 1, "other": "kept elsewhere"}) == (
        StructuredSettings(structured_retry_budget=1)
    )
    assert structured_settings({"uncertain_below": 3}) == StructuredSettings()
    assert StructuredSettings().structured_retry_budget == 2
    assert StructuredSettings().uncertain_below == 0.5


# -- the budget and the deadline --------------------------------------------------------------


async def test_budget_exhausted_mid_retry_is_uncertain() -> None:
    mal = models()
    mal.stub.fault("guardrail_check", "garbage")
    budget = TurnBudget(max_calls=1)
    assert await check(mal, budget) == Uncertain("budget_exhausted", 1)
    assert budget.used == 1
    assert len(mal.stub.calls) == 1


async def test_no_budget_left_makes_no_call() -> None:
    mal = models()
    assert await check(mal, TurnBudget(max_calls=0)) == Uncertain("budget_exhausted", 0)
    assert mal.stub.calls == []
    with pytest.raises(BudgetExhausted):
        await mal.call("generation", GENERATION, budget=TurnBudget(max_calls=0))
    assert mal.stub.calls == []


async def test_a_hung_call_ends_at_its_timeout() -> None:
    mal = models()
    mal.stub.fault("generation", "hang")
    started = time.monotonic()
    with pytest.raises(ModelUnavailable) as failed:
        await mal.call("generation", GENERATION, ModelParams(timeout=0.2))
    assert time.monotonic() - started < 2
    assert failed.value.details == {"purpose": "generation", "reason": "timeout"}


async def test_the_deadline_clamps_every_call() -> None:
    mal = models()
    mal.stub.fault("guardrail_check", "hang")
    started = time.monotonic()
    with pytest.raises(ModelUnavailable) as failed:
        await check(mal, TurnBudget(deadline_s=0.2))
    assert time.monotonic() - started < 2
    assert failed.value.details == {
        "purpose": "guardrail_check",
        "reason": "deadline",
        "attempts": 1,
    }


async def test_a_held_call_is_released_by_the_deadline() -> None:
    mal = models()
    latch = mal.stub.hold("generation")
    with pytest.raises(ModelUnavailable) as failed:
        await mal.call("generation", GENERATION, budget=TurnBudget(deadline_s=0.2))
    assert failed.value.reason == "deadline"
    assert latch.entered.is_set()


async def test_deadline_expiry_mid_retry_is_unavailable_not_uncertain() -> None:
    mal = models()
    mal.stub.latency_s = 0.15
    mal.stub.fault("guardrail_check", "garbage")
    with pytest.raises(ModelUnavailable) as failed:
        await check(mal, TurnBudget(deadline_s=0.2))
    assert failed.value.reason == "deadline"


async def test_an_expired_budget_makes_no_call() -> None:
    mal = models()
    budget = TurnBudget(deadline_s=0.01)
    await asyncio.sleep(0.02)
    with pytest.raises(ModelUnavailable) as failed:
        await mal.call("generation", GENERATION, budget=budget)
    assert failed.value.reason == "deadline"
    assert mal.stub.calls == []


def test_turn_budget_bounds() -> None:
    budget = TurnBudget(max_calls=2)
    assert budget.can_afford(2)
    assert not budget.can_afford(3)
    budget.debit()
    assert budget.remaining_calls == 1
    assert TurnBudget().remaining_calls is None
    assert TurnBudget().remaining_s() is None
    assert not TurnBudget().expired()
    with pytest.raises(ValueError, match="max_calls"):
        TurnBudget(max_calls=-1)
    with pytest.raises(ValueError, match="deadline_s"):
        TurnBudget(deadline_s=0)


# -- provider failures ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "details"),
    [
        ("timeout", {"reason": "timeout"}),
        ("connection", {"reason": "connection"}),
        ("http_500", {"reason": "http_error", "provider_status": 500}),
        ("http_429", {"reason": "rate_limited", "provider_status": 429}),
        ("http_401", {"reason": "auth", "provider_status": 401}),
        ("bad_body", {"reason": "garbage"}),
    ],
)
async def test_transport_faults_are_unavailable_and_never_retried(
    kind: str, details: dict[str, object]
) -> None:
    mal = models()
    mal.stub.fault("guardrail_check", kind)  # type: ignore[arg-type]
    with pytest.raises(ModelUnavailable) as failed:
        await check(mal)
    assert failed.value.details == {"purpose": "guardrail_check", **details, "attempts": 1}
    assert failed.value.code == "model_unavailable"
    assert failed.value.http_status == 503
    assert failed.value.__cause__ is None  # the provider's text does not ride along
    assert len(mal.stub.calls) == 1
    headers = failed.value.response_headers()
    assert headers == ({"Retry-After": RETRY_AFTER} if kind == "http_429" else {})


async def test_unstructured_calls_map_faults_too() -> None:
    mal = models()
    mal.stub.fault("generation", "http_500")
    with pytest.raises(ModelUnavailable) as failed:
        await mal.call("generation", GENERATION)
    assert failed.value.details == {
        "purpose": "generation",
        "reason": "http_error",
        "provider_status": 500,
    }


async def test_an_empty_unstructured_completion_raises_empty_completion() -> None:
    mal = models()
    mal.stub.fault("generation", "empty")
    with pytest.raises(EmptyCompletion) as empty:
        await mal.call("generation", GENERATION)
    assert (empty.value.code, empty.value.http_status) == ("model_unavailable", 503)
    assert empty.value.details == {"purpose": "generation", "reason": "empty"}
    assert not isinstance(empty.value, ModelUnavailable)  # the caller may regenerate once (#57)


def _status_error(cls: type[openai.APIStatusError], status: int, message: str) -> Exception:
    request = httpx2.Request("POST", "http://x/v1/chat/completions")
    response = httpx2.Response(status, request=request)
    return cls(message, response=response, body={"error": {"message": message}})


def test_map_provider_error() -> None:
    rejected = map_provider_error(
        "guardrail_check",
        _status_error(openai.BadRequestError, 400, "response_format is not supported"),
    )
    assert isinstance(rejected, ResponseFormatRejected)
    assert rejected.details["provider_status"] == 400
    other = map_provider_error("generation", _status_error(openai.BadRequestError, 400, "nope"))
    assert other is not None
    assert not isinstance(other, ResponseFormatRejected)
    assert other.details["reason"] == "http_error"
    forbidden = map_provider_error(
        "generation", _status_error(openai.PermissionDeniedError, 403, "no")
    )
    assert forbidden is not None
    assert forbidden.details["reason"] == "auth"
    for garbage in (json.JSONDecodeError("x", "{", 1), ProviderGarbage("x")):
        mapped = map_provider_error("generation", garbage)
        assert mapped is not None
        assert mapped.details["reason"] == "garbage"
    assert map_provider_error("generation", KeyError("an engine bug")) is None
    assert map_provider_error("generation", ValueError("an engine bug")) is None
    already = ModelUnavailable("generation", "no_api_key")
    assert map_provider_error("generation", already) is already
    # Only a 429 passes Retry-After through.
    assert ModelUnavailable("g", "http_error", retry_after="5").response_headers() == {}
    assert Unauthenticated("x").response_headers() == {"WWW-Authenticate": "Bearer"}


async def test_an_engine_bug_is_not_masked() -> None:
    mal = models()

    async def broken(*_: object, **__: object) -> None:
        raise KeyError("bug")

    mal.stub.complete = broken  # type: ignore[assignment,method-assign]
    with pytest.raises(KeyError):
        await mal.call("generation", GENERATION)


# -- deterministic first ----------------------------------------------------------------------


async def test_guardrail_check_without_a_deterministic_step_raises_at_call_time() -> None:
    mal = models()
    with pytest.raises(MissingDeterministicStep):
        await mal.structured_call("guardrail_check", GuardrailVerdict, CHECK, budget=TurnBudget())
    assert mal.stub.calls == []


async def test_a_conclusive_deterministic_step_makes_no_model_call() -> None:
    """Complement asserted: the same call with an inconclusive step does call the model."""
    mal = models()
    words = [policy({"type": "wordlist", "words": ["dragon"]})]
    budget = TurnBudget()
    conclusive = await mal.structured_call(
        "guardrail_check",
        GuardrailVerdict,
        GuardrailCheckInputs(text="a dragon appears", policy_prompt="p"),
        deterministic_first=guardrail_deterministic_first(
            "a dragon appears", words, words[0].applies_to[0]
        ),
        budget=budget,
    )
    assert conclusive == GuardrailVerdict(violation=True, confidence=1.0)
    assert (mal.stub.calls, budget.used) == ([], 0)
    residual = await mal.structured_call(
        "guardrail_check",
        GuardrailVerdict,
        GuardrailCheckInputs(text="a knight appears", policy_prompt="p"),
        deterministic_first=guardrail_deterministic_first(
            "a knight appears", words, words[0].applies_to[0]
        ),
        budget=budget,
    )
    assert residual == GuardrailVerdict(violation=False, confidence=1.0)
    assert (len(mal.stub.calls), budget.used) == (1, 1)


async def test_generation_needs_no_deterministic_step() -> None:
    mal = models()
    result = await mal.structured_call(
        "generation", GuardrailVerdict, GENERATION, budget=TurnBudget()
    )
    assert result == GuardrailVerdict(violation=False, confidence=1.0)


# -- the openai SDK adapter -------------------------------------------------------------------


async def test_openai_endpoint_rejecting_response_format_falls_back() -> None:
    with mock_openai(reject_response_format=True) as server:
        mal = ModelAccessLayer(
            auth_settings(DUMMY_DSN, model_adapter="openai", config_key=config_key()),
            adapter=_fixed(f"{server.root}/v1"),
        )
        try:
            assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
            assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
        finally:
            await mal.aclose()
    bodies = [r.body for r in server.requests]
    assert ["response_format" in b for b in bodies] == [True, False, False]
    assert len(server.requests) == 3  # no legacy max_tokens retry for a response_format 400


async def test_openai_html_body_is_garbage() -> None:
    with mock_openai(raw_body=b"<html>bad gateway</html>") as server:
        mal = ModelAccessLayer(
            auth_settings(DUMMY_DSN, model_adapter="openai", config_key=config_key()),
            adapter=_fixed(f"{server.root}/v1"),
        )
        try:
            with pytest.raises(ModelUnavailable) as failed:
                await mal.call("generation", GENERATION)
        finally:
            await mal.aclose()
    assert failed.value.details == {"purpose": "generation", "reason": "garbage"}


def _fixed(base_url: str) -> OpenAICompatibleAdapter:
    return fixed(configured(base_url, "m", "k"))
