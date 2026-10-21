"""Structured model results, validated and never trusted (FR-MODEL-5,6,7).

``structured_call(mal, purpose, schema, inputs, *, params, deterministic_first, budget)``
returns a ``schema`` instance or ``Uncertain(reason)``; never a partially parsed object, and
never the model's raw text.

1. **Deterministic first** (FR-MODEL-5). ``deterministic_first()`` runs before any model call;
   a non-``None`` answer is conclusive and is returned with no model call, no budget debit and
   nothing recorded. Every purpose except ``generation`` and ``lite_summary`` must pass one
   (``MissingDeterministicStep`` at call time otherwise); for guardrails it is
   ``guardrail_deterministic_first``, built on ``core.policies.evaluate``.
2. **Constrained decoding first** (FR-MODEL-7). The request carries ``response_format:
   {"type": "json_schema", ...}`` (``json_schema.name`` is the schema's class name, ``strict``
   false; the schema is not repeated in the prompt). An endpoint that answers it with a 400
   whose body names ``response_format``, or answers 200 with text that is not JSON at all,
   does not support it: that is remembered per ``(base_url, model)`` until the deployment's
   ``config_revision`` moves (``ResponseFormatMemo``). The 400 probe costs one call of the turn
   budget but no retry.
3. **Parse-validate-retry** otherwise. The JSON Schema goes into the system message. A fenced
   ```` ```json ```` block is unwrapped; the text must parse as JSON (else ``parse_error``) and
   validate strictly against the schema, which forbids extra keys (else ``schema_error``). A
   failed attempt is retried up to ``structured_retry_budget`` times (deployment setting,
   default 2): the invalid answer goes back as an ``assistant`` turn followed by a ``user``
   correction naming what was wrong, at ``temperature=0``. Schema-invalid JSON in
   ``json_schema`` mode is retried the same way.
4. **Uncertain** results: retries spent → the last failure's reason; a ``refusal`` or a
   ``length`` finish → ``parse_error`` at once; a schema with a ``confidence`` field below
   ``uncertain_below`` (deployment setting, default 0.5) → ``low_confidence`` at once; a call
   the budget cannot afford → ``budget_exhausted``. The caller resolves ``Uncertain``
   conservatively (``resolve_uncertain``); for a guardrail that is the policy's ``on_uncertain``.

Transport failures are not retried here: a timeout, connection error, 5xx, 429, auth failure or
garbage body raises ``ModelUnavailable`` (with ``details.attempts``), and so does the turn
deadline passing between attempts (``reason=deadline``). The turn then fails as a whole.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticCustomError

from epistrel.core.errors import nul_location, without_nul
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import Policy, Target
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.adapter import REQUEST_FIELDS, Message, ModelParams, ModelResult
from epistrel.orchestrator.models.errors import ModelUnavailable, ResponseFormatRejected
from epistrel.orchestrator.models.prompts import payload_fields
from epistrel.orchestrator.models.registry import GuardrailVerdict, InputSchema

if TYPE_CHECKING:
    from epistrel.orchestrator.models.access import ModelAccessLayer

type UncertainReason = Literal["parse_error", "schema_error", "low_confidence", "budget_exhausted"]
type DeterministicFirst[T] = Callable[[], T | None]

DETERMINISTIC_FIRST_EXEMPT = frozenset({"generation", "lite_summary"})
"""Purposes with no deterministic path (FR-MODEL-5's residual is the whole call)."""

SCHEMA_INSTRUCTION = (
    "Respond with only a JSON object, and nothing else, that validates against this JSON Schema:"
)
CORRECTION = (
    "Your previous answer could not be used: {problem}. "
    "Reply again with only the JSON object, and nothing else."
)
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(?P<body>.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)
MAX_FEEDBACK_ERRORS = 5


@dataclass(frozen=True, slots=True)
class Uncertain:
    """A structured call that produced no usable result. Carries no model text."""

    reason: UncertainReason
    attempts: int


class MissingDeterministicStep(TypeError):
    """A structured call for a purpose that has a deterministic path did not pass one."""


class ResponseFormatMemo:
    """``(base_url, model)`` pairs whose endpoint does not honour ``json_schema``, each valid
    only at the ``config_revision`` it was learned at."""

    def __init__(self) -> None:
        self._rejected: dict[tuple[str, str], int] = {}

    def supported(self, key: tuple[str, str], revision: int) -> bool:
        learned = self._rejected.get(key)
        if learned is not None and learned != revision:
            del self._rejected[key]
            return True
        return learned is None

    def reject(self, key: tuple[str, str], revision: int) -> None:
        self._rejected[key] = revision

    def clear(self) -> None:
        self._rejected.clear()


@dataclass(frozen=True, slots=True)
class _Failure:
    """A failed attempt: ``problem`` is the correction the model is sent, ``public`` what the
    caller's report keeps (the same text unless a validator gave a content-free form)."""

    reason: Literal["parse_error", "schema_error"]
    problem: str
    public: str = ""

    def reported(self) -> str:
        return self.public or self.problem


PUBLIC = "public"
"""The context key of a ``feedback`` error's content-free form."""


def feedback(correction: str, public: str) -> PydanticCustomError:
    """A validator's error whose ``correction`` may name story content (the names a call asked
    about, so the model can fix its answer; #480) while the failure the caller reports, and so
    ``details.structural_errors``, keeps only the content-free ``public`` form."""
    return PydanticCustomError(
        "feedback", "{correction}", {"correction": correction, PUBLIC: public}
    )


def unfence(text: str) -> str:
    """``text`` without a surrounding ```` ```json ```` fence."""
    match = _FENCE.match(text)
    return match.group("body") if match else text


def parse[T: BaseModel](text: str, schema: type[T]) -> T | _Failure:
    """``text`` as a strict ``schema`` instance, or why it is not one (never its contents)."""
    body = unfence(text).strip()
    try:
        document = json.loads(body)
    except ValueError as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else "not JSON"
        return _Failure("parse_error", f"it is not valid JSON ({detail})")
    if nul_location(document) is not None:
        # A JSON ``\u0000`` escape decodes to the NUL ``send`` removed from the raw text (#275).
        body = json.dumps(without_nul(document))
    try:
        return schema.model_validate_json(body, strict=True)
    except ValidationError as exc:
        problems: list[str] = []
        public: list[str] = []
        for error in exc.errors()[:MAX_FEEDBACK_ERRORS]:
            where = ".".join(str(p) for p in error["loc"]) or "the object"
            problems.append(f"{where}: {error['msg']}")
            context = error.get("ctx") or {}
            public.append(f"{where}: {context.get(PUBLIC, error['msg'])}")
        return _Failure("schema_error", "; ".join(problems), "; ".join(public))


def usable(result: ModelResult, schema: type[BaseModel]) -> bool:
    """Whether ``result`` is a usable ``schema`` instance (the attempt's recorded outcome)."""
    if result.refused or result.finish_reason == "length":
        return False
    return not isinstance(parse(result.text, schema), _Failure)


def with_schema(messages: Sequence[Message], schema: type[BaseModel]) -> list[Message]:
    """``messages`` with the JSON Schema appended to the system message (fallback mode)."""
    dump = json.dumps(schema.model_json_schema(), sort_keys=True, separators=(",", ":"))
    instruction = f"{SCHEMA_INSTRUCTION}\n{dump}"
    out = list(messages)
    for index, message in enumerate(out):
        if message.role == "system":
            out[index] = Message(role="system", content=f"{message.content}\n\n{instruction}")
            return out
    return [Message(role="system", content=instruction), *out]


def _low_confidence(result: BaseModel, threshold: float) -> bool:
    confidence = getattr(result, "confidence", None)
    return isinstance(confidence, int | float) and confidence < threshold


async def structured_call[T: BaseModel](
    mal: ModelAccessLayer,
    purpose: str,
    schema: type[T],
    inputs: InputSchema | Mapping[str, Any],
    *,
    params: ModelParams | None = None,
    deterministic_first: DeterministicFirst[T] | None = None,
    budget: TurnBudget,
    variant: str | None = None,
    failures: list[str] | None = None,
) -> T | Uncertain:
    """See the module docstring. ``variant`` names the purpose's template variant (#150);
    ``failures``, when given, collects each failed attempt's problem (what was wrong, never the
    answer's text; a ``feedback`` error's content-free form) for the caller's report."""
    if deterministic_first is None and purpose not in DETERMINISTIC_FIRST_EXEMPT:
        raise MissingDeterministicStep(
            f"a structured {purpose!r} call needs its deterministic_first step (FR-MODEL-5)"
        )
    _, payload, base = mal.prepare(purpose, inputs, variant=variant)
    if deterministic_first is not None:
        conclusive = deterministic_first()
        if conclusive is not None:
            return conclusive

    settings = await mal.structured_settings()
    base_url, model, revision = await mal.endpoint(purpose)
    key = (base_url, model)
    json_mode = mal.response_format.supported(key, revision)
    messages = list(base) if json_mode else with_schema(base, schema)
    call_params = params
    retries_left = settings.structured_retry_budget
    attempts = 0
    token = REQUEST_FIELDS.set(payload_fields(payload))
    try:
        while True:
            if budget.expired():
                raise ModelUnavailable(purpose, "deadline", attempts=attempts)
            # A retry comes from the unreserved remainder: it never spends a slot reserved
            # for another check or the generation (#69).
            if not budget.affordable(purpose, first=attempts == 0):
                return Uncertain("budget_exhausted", attempts)
            attempts += 1
            try:
                result = await mal.send(
                    purpose,
                    messages,
                    call_params,
                    schema if json_mode else None,
                    budget=budget,
                    attempt=attempts,
                    usable=lambda answer: usable(answer, schema),
                )
            except ResponseFormatRejected as exc:
                if not json_mode:
                    raise exc.with_attempts(attempts) from None
                mal.response_format.reject(key, revision)
                json_mode = False
                messages = with_schema(base, schema)
                continue
            except ModelUnavailable as exc:
                raise exc.with_attempts(attempts) from None
            if result.refused or result.finish_reason == "length":
                return Uncertain("parse_error", attempts)
            outcome = parse(result.text, schema)
            if isinstance(outcome, _Failure) and failures is not None:
                failures.append(outcome.reported())
            if not isinstance(outcome, _Failure):
                if _low_confidence(outcome, settings.uncertain_below):
                    return Uncertain("low_confidence", attempts)
                return outcome
            if outcome.reason == "parse_error" and json_mode:
                # A 200 of prose: the endpoint ignored json_schema.
                mal.response_format.reject(key, revision)
                json_mode = False
                messages = with_schema(base, schema)
            if retries_left == 0:
                return Uncertain(outcome.reason, attempts)
            retries_left -= 1
            messages = [
                *messages,
                Message(role="assistant", content=result.text),
                Message(role="user", content=CORRECTION.format(problem=outcome.problem)),
            ]
            call_params = (params or ModelParams()).merged(ModelParams(temperature=0.0))
    finally:
        REQUEST_FIELDS.reset(token)


def resolve_uncertain[T](result: T | Uncertain, conservative: T) -> T:
    """``result``, or the caller's ``conservative`` default when it is ``Uncertain``."""
    return conservative if isinstance(result, Uncertain) else result


def guardrail_deterministic_first(
    text: str, policies: Iterable[Policy], target: Target
) -> DeterministicFirst[GuardrailVerdict]:
    """The guardrail check's deterministic step (FR-MODEL-5): #49's ``evaluate`` over the
    deterministic policies. A violation is conclusive (no model call); otherwise the model
    check runs on the residual."""
    deterministic = tuple(policies)

    def step() -> GuardrailVerdict | None:
        if evaluate(text, deterministic, target):
            return GuardrailVerdict(violation=True, confidence=1.0)
        return None

    return step


def uncertain_verdict(on_uncertain: Literal["violation", "pass"]) -> GuardrailVerdict:
    """The conservative verdict for an ``Uncertain`` model-assisted check: the policy's
    ``on_uncertain`` (default ``violation``), never a silent pass unless the policy says so."""
    return GuardrailVerdict(violation=on_uncertain == "violation", confidence=0.0)


__all__ = [
    "DETERMINISTIC_FIRST_EXEMPT",
    "DeterministicFirst",
    "MissingDeterministicStep",
    "ResponseFormatMemo",
    "Uncertain",
    "UncertainReason",
    "feedback",
    "guardrail_deterministic_first",
    "parse",
    "resolve_uncertain",
    "structured_call",
    "uncertain_verdict",
    "unfence",
    "usable",
    "with_schema",
]
