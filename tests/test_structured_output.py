"""Structured model results and provider failures without a database (#53; FR-MODEL-5,6,7,
NFR-REL-3): the json_schema path, the 400/prose fallback to parse-validate-retry, strict
validation, ``Uncertain`` and its reasons, the budget and the deadline, and the mapping of every
provider failure to ``ModelUnavailable``. The C-04 slices and X-05 run the same paths through a
real operation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx2
import openai
import pytest

from epistrel.core.errors import Unauthenticated
from epistrel.core.policies.model import Target
from epistrel.core.settings_schema import StructuredSettings
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import provider_schema
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
from epistrel.orchestrator.models.metrics import recording
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import example_instance
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    DirectiveParseResult,
    GuardrailCheckInputs,
    GuardrailVerdict,
    RewriteParseInputs,
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
from epistrel.orchestrator.models.stub import GARBAGE, RETRY_AFTER, FaultKind
from epistrel.orchestrator.policies.age import CastMember
from epistrel.orchestrator.policies.input_gate import (
    GateText,
    input_budget,
    input_gate,
    input_policies,
)
from epistrel.orchestrator.policies.seeds.policies import (
    MINOR_AGE_CONDITION,
    MINORS_POLICY,
    MINORS_POLICY_ID,
)
from epistrel.orchestrator.policies.store import GuardContext
from tests.auth_support import auth_settings
from tests.conftest import DUMMY_DSN
from tests.model_support import config_key, mock_openai
from tests.policy_support import policy
from tests.respond_support import WITHIN_S
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
    assert call.fields == ("external_data", "policy_prompt", "subjects", "text")
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
    # #685: a schema-error retry keeps the call's own temperature (here the route's: unset).
    assert second.params.temperature is None
    assert first.params.temperature is None


async def test_a_schema_error_retry_keeps_the_calls_temperature_a_prose_one_does_not() -> None:
    """#685: a retry after JSON that fails validation runs at the call's own params, so it is
    not a temperature-0 near-copy of the refused answer; a retry after an answer that is not
    JSON at all still runs at ``temperature=0``. The other params are the call's either way."""
    own = ModelParams(temperature=0.7, max_tokens=64)
    for answer, expected in (('{"violation": "yes", "confidence": 1}', 0.7), (PROSE, 0.0)):
        mal = models()
        mal.stub.script("guardrail_check", [answer])
        result = await mal.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            CHECK,
            params=own,
            deterministic_first=nothing_conclusive,
            budget=TurnBudget(),
        )
        assert result == GuardrailVerdict(violation=False, confidence=1.0)
        first, second = mal.stub.calls
        assert first.params.temperature == 0.7
        assert second.params.temperature == expected
        assert second.params.max_tokens == 64


# -- the fallback: 400 or prose ---------------------------------------------------------------


async def test_provider_400_falls_back_and_succeeds_on_the_fallback() -> None:
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
    assert budget.used == 1  # #759: the probe decided nothing, so it costs no budget call

    # Remembered per (base_url, model): the next call goes straight to the fallback.
    await check(mal)
    assert mal.stub.calls[-1].response_schema is None
    assert len(mal.stub.calls) == 3


async def test_the_probe_does_not_spend_a_retry() -> None:
    mal = models(retries=0)
    mal.stub.fault("guardrail_check", "response_format_400")
    assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
    assert len(mal.stub.calls) == 2


async def test_the_probe_leaves_a_gate_budgeted_at_g_its_every_check() -> None:
    """#759: story creation gates each character definition against the seeded minors policy
    under a budget of exactly one call per text. On an endpoint that refuses ``json_schema``, the
    probe spent a slot, the last definition's check could not be afforded, and the policy's
    ``on_uncertain`` refused three harmless definitions. Every check now runs, and all pass."""
    minors = replace(
        policy(MINORS_POLICY["rule"], policy_id=MINORS_POLICY_ID),
        applies_to=(Target.TEXT_INPUT, Target.TEXT_OUTPUT),
        age_condition=MINOR_AGE_CONDITION,
    )
    cast = tuple(
        CastMember(character_id=f"new:{n}", ordinal=n, true_age=age)
        for n, age in enumerate((30, 12, 40))
    )
    context = GuardContext(policies=(minors,), cast=cast, threshold=18)
    texts = [
        GateText(f"characters[{n}].definition", text)
        for n, text in enumerate(("The player.", "A boy.", "His aunt."))
    ]
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400")
    budget = input_budget(input_policies(context), texts)
    assert budget.max_calls == 3
    assert await input_gate(mal, context, texts, budget=budget) is None
    assert [c.response_schema for c in mal.stub.calls] == ["GuardrailVerdict", None, None, None]
    assert budget.used == 3


async def test_a_probe_on_a_one_call_budget_still_gets_its_answer() -> None:
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400")
    budget = TurnBudget(max_calls=1)
    assert await check(mal, budget) == GuardrailVerdict(violation=False, confidence=1.0)
    assert budget.used == 1


async def test_the_refund_never_lets_the_call_pass_its_budget() -> None:
    """#763: the refund returns the probe's one slot and no more. With the turn's other slot
    already spent, the fallback takes the last one and its garbage answer gets no retry."""
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400")
    mal.stub.fault("guardrail_check", "garbage")
    budget = TurnBudget(max_calls=2)
    budget.debit()  # an earlier call of the turn
    assert await check(mal, budget) == Uncertain("budget_exhausted", 1)
    assert budget.used == 2
    assert len(mal.stub.calls) == 2  # the probe and the fallback


async def test_the_refunded_probe_is_still_recorded_as_a_failed_call() -> None:
    """#763: the probe costs no budget slot, yet it was sent, so ``model_calls`` keeps it,
    marked ``probe`` (#764) so that the calls counted equal the calls recorded without it."""
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400")
    budget = TurnBudget()
    with recording() as log:
        assert await check(mal, budget) == GuardrailVerdict(violation=False, confidence=1.0)
    assert [(c.outcome, c.probe) for c in log.calls] == [("http_error", True), ("ok", False)]
    assert budget.used == 1 == sum(not c.probe for c in log.calls)
    assert log.calls[0].model_dump()["probe"] is True
    assert "probe" not in log.calls[1].model_dump()  # absent on every other call


async def test_the_fallback_is_the_calls_first_attempt() -> None:
    mal = models(retries=0)
    mal.stub.fault("guardrail_check", "response_format_400")
    mal.stub.fault("guardrail_check", "garbage")
    with recording() as log:
        assert await check(mal) == Uncertain("parse_error", 1)
    assert [(c.attempt, c.outcome) for c in log.calls] == [
        (1, "http_error"),
        (1, "invalid_output"),
    ]


@pytest.mark.parametrize("probed", [False, True])
async def test_a_real_failure_is_charged_with_or_without_a_probe(probed: bool) -> None:
    """#763: only the rejected probe is refunded. A provider failure on the call itself (here
    a 500, on its own or on the fallback after a probe) keeps its budget slot."""
    mal = models()
    if probed:
        mal.stub.fault("guardrail_check", "response_format_400")
    mal.stub.fault("guardrail_check", "http_500")
    budget = TurnBudget()
    with pytest.raises(ModelUnavailable) as failed:
        await check(mal, budget)
    assert failed.value.details["attempts"] == 1
    assert budget.used == 1
    assert len(mal.stub.calls) == 1 + probed


async def test_a_second_format_rejection_on_the_fallback_is_charged() -> None:
    """#768: the fallback sends no ``response_format``, yet a misbehaving endpoint can still
    answer it with a 400 naming one. That rejection is the call's own failure: it raises, keeps
    its budget slot and is recorded unmarked, so no third, uncharged call is sent."""
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400", n=2)
    budget = TurnBudget()
    with recording() as log, pytest.raises(ModelUnavailable) as failed:
        await check(mal, budget)
    assert failed.value.details["attempts"] == 1
    assert budget.used == 1
    assert [c.response_schema for c in mal.stub.calls] == ["GuardrailVerdict", None]
    assert [(c.outcome, c.probe) for c in log.calls] == [
        ("http_error", True),
        ("http_error", False),
    ]


@pytest.mark.parametrize("fault", ["http_500", "timeout"])
async def test_a_real_failure_is_recorded_unmarked(fault: FaultKind) -> None:
    """#769: only a refunded format probe carries ``probe`` (ADR-180). A provider failure,
    after a probe or not, is counted and recorded, so it must not be marked."""
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400")
    mal.stub.fault("guardrail_check", fault)
    budget = TurnBudget()
    with recording() as log, pytest.raises(ModelUnavailable):
        await check(mal, budget)
    assert [c.probe for c in log.calls] == [True, False]
    assert budget.used == 1 == sum(not c.probe for c in log.calls)


async def test_a_format_rejection_nobody_refunds_is_never_marked() -> None:
    """#769: ``complete()`` sends the purpose's declared schema without ``format_probe``, so
    nothing refunds its rejection. The call is charged, and recorded as an ordinary failure."""
    mal = models()
    mal.stub.fault("guardrail_check", "response_format_400")
    budget = TurnBudget()
    with recording() as log, pytest.raises(ResponseFormatRejected):
        await mal.call("guardrail_check", CHECK, budget=budget)
    assert mal.stub.calls[0].response_schema == "GuardrailVerdict"
    assert [(c.outcome, c.probe) for c in log.calls] == [("http_error", False)]
    assert budget.used == 1


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
    # A hang never answers, so the timeout reason proves the timeout ended the call; the outer
    # bound only turns a regression into a failure rather than a hung run (#446).
    async with asyncio.timeout(WITHIN_S):
        with pytest.raises(ModelUnavailable) as failed:
            await mal.call("generation", GENERATION, ModelParams(timeout=0.2))
    assert failed.value.details == {"purpose": "generation", "reason": "timeout"}


async def test_the_deadline_clamps_every_call() -> None:
    mal = models()
    mal.stub.fault("guardrail_check", "hang")
    async with asyncio.timeout(WITHIN_S):  # not hung; the reason proves the deadline (#446)
        with pytest.raises(ModelUnavailable) as failed:
            await check(mal, TurnBudget(deadline_s=0.2))
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


def _as_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local mock server (127.0.0.1) is api.anthropic.com for this test."""
    monkeypatch.setattr(provider_schema, "ANTHROPIC_HOST", "127.0.0.1")


async def test_anthropic_style_strict_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """#761 (the owner's option 2): an endpoint that refuses a ``json_schema`` unless it is
    ``strict: true`` with no bounds keywords (api.anthropic.com, emulated from its live 400s)
    accepts the structured call in ``json_schema`` mode: one request, ``strict: true``, and no
    ``probe`` call recorded. The reply is still validated against the full schema: a
    ``confidence`` of 1.5, which the wire schema no longer bounds, is invalid output and is
    retried. The ADR-180 probe of an endpoint that rejects ``json_schema`` outright is unchanged
    (``test_openai_endpoint_rejecting_response_format_falls_back``)."""
    _as_anthropic(monkeypatch)
    with mock_openai(require_strict=True, script=[verdict(False, 1.5)]) as server:
        mal = ModelAccessLayer(
            auth_settings(DUMMY_DSN, model_adapter="openai", config_key=config_key()),
            adapter=_fixed(f"{server.root}/v1"),
        )
        try:
            with recording() as log:
                assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
        finally:
            await mal.aclose()
    first, retry = server.requests
    for request in (first, retry):
        json_schema = request.body["response_format"]["json_schema"]
        assert json_schema["strict"] is True
        assert "maximum" not in json_schema["schema"]["properties"]["confidence"]
    assert [(c.outcome, c.probe) for c in log.calls] == [("invalid_output", False), ("ok", False)]


async def test_strict_required_endpoint_not_anthropic_still_probes() -> None:
    """#761 complement: the same endpoint behind any other host gets today's request (no
    ``strict``), so it 400s, probes once and falls back (ADR-180): other endpoints are
    unchanged, and the emulation does refuse the pre-#761 request."""
    with mock_openai(require_strict=True) as server:
        mal = ModelAccessLayer(
            auth_settings(DUMMY_DSN, model_adapter="openai", config_key=config_key()),
            adapter=_fixed(f"{server.root}/v1"),
        )
        try:
            with recording() as log:
                assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
        finally:
            await mal.aclose()
    assert ["response_format" in r.body for r in server.requests] == [True, False]
    assert "strict" not in server.requests[0].body["response_format"]["json_schema"]
    assert [(c.outcome, c.probe) for c in log.calls] == [("http_error", True), ("ok", False)]


async def test_anthropic_schema_without_wire_form_skips_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#761: on api.anthropic.com a schema with no strict wire form (``DirectiveParseResult``'s
    free-form ``qualifiers`` dict) is sent as parse-validate-retry at once: no
    ``response_format``, no probe, and nothing remembered, so the next guardrail check on the
    same endpoint and model still goes in ``json_schema`` mode."""
    _as_anthropic(monkeypatch)
    answer = '{"classification": "durable", "steer_text": ""}'
    with mock_openai(require_strict=True, script=[answer]) as server:
        mal = ModelAccessLayer(
            auth_settings(DUMMY_DSN, model_adapter="openai", config_key=config_key()),
            adapter=_fixed(f"{server.root}/v1"),
        )

        async def endpoint(_: str) -> tuple[str, str, int]:
            return f"{server.root}/v1", "m", 0  # what a configured deployment resolves

        # Without a database ``endpoint`` names the adapter, not the URL the call goes to.
        monkeypatch.setattr(mal, "endpoint", endpoint)
        try:
            with recording() as log:
                result = await mal.structured_call(
                    "directive_parse",
                    DirectiveParseResult,
                    RewriteParseInputs(instruction="Bobby holds a BA."),
                    deterministic_first=lambda: None,
                    budget=TurnBudget(),
                )
                assert await check(mal) == GuardrailVerdict(violation=False, confidence=1.0)
        finally:
            await mal.aclose()
    assert result == DirectiveParseResult(classification="durable")
    directive, guardrail = server.requests
    assert "response_format" not in directive.body
    assert '"title":"DirectiveParseResult"' in directive.body["messages"][0]["content"]
    assert guardrail.body["response_format"]["json_schema"]["strict"] is True
    assert [(c.outcome, c.probe) for c in log.calls] == [("ok", False), ("ok", False)]


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
