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
   {"type": "json_schema", ...}`` (``json_schema.name`` is the schema's class name; the schema
   is not repeated in the prompt). No ``strict`` key is sent, except to api.anthropic.com, which
   refuses a ``json_schema`` without ``strict: true``: there the schema is its strict wire form,
   with the bounds strict mode refuses dropped (``provider_schema``, #761). The reply is still
   validated against the full schema. A schema with no wire form there (a free-form ``dict``
   field) goes straight to parse-validate-retry for that call, and nothing is remembered.
   An endpoint that answers it with a 400
   whose body names ``response_format`` does not support it, at once. A 200 with text that is
   not JSON at all is one strike: the call retries still in ``json_schema`` mode, and only
   ``PROSE_STRIKES`` (2) such answers in a row, across calls to the same ``(base_url, model)``,
   prove the endpoint ignores it; any JSON answer in ``json_schema`` mode resets the run
   (#482). Either proof is remembered per ``(base_url, model)`` until the deployment's
   ``config_revision`` moves (``ResponseFormatMemo``). The 400 probe is recorded as a failed
   attempt but costs neither a retry nor a call of the turn budget: it decided nothing, and the
   fallback is the call's first attempt (#759: a gate budgeted at exactly G calls spent a slot
   on the probe, starved its last check, and its fail-closed policy refused a harmless text).
3. **Parse-validate-retry** otherwise. The JSON Schema goes into the system message. A fenced
   ```` ```json ```` block is unwrapped; the text must parse as JSON (else ``parse_error``) and
   validate strictly against the schema, which forbids extra keys (else ``schema_error``). A
   failed attempt is retried up to ``structured_retry_budget`` times (deployment setting,
   default 2): the invalid answer goes back as an ``assistant`` turn followed by a ``user``
   correction naming what was wrong. A ``parse_error`` retry runs at ``temperature=0``; a
   ``schema_error`` retry (JSON that fails validation) runs at the call's own params (#685:
   at ``temperature=0`` a model given a correction that names the fix repeated its answer on
   every attempt). Schema-invalid JSON in ``json_schema`` mode is retried the same way.
   #996: each attempt is validated with the codes of the corrections already sent in this
   call as its validation context (``corrected``): a validator that corrects a mistake only
   once (``correct_once``) relaxes on a later attempt, and its schema repairs the answer
   instead of refusing it again (a render's change to the world clock is dropped).
4. **Uncertain** results: retries spent → the last failure's reason; a ``refusal`` or a
   ``length`` finish → ``parse_error`` at once (each attempt's ``max_tokens`` carries
   ``STRUCTURED_HEADROOM_TOKENS`` above the caller's, so hidden reasoning does not end it
   before the JSON closes, #735); a schema with a ``confidence`` field below
   ``uncertain_below`` (deployment setting, default 0.5) → ``low_confidence`` at once; a call
   the budget cannot afford → ``budget_exhausted``. The caller resolves ``Uncertain``
   conservatively (``resolve_uncertain``); for a guardrail that is the policy's ``on_uncertain``.

Transport failures are not retried here: a timeout, connection error, 5xx, 429, auth failure or
garbage body raises ``ModelUnavailable`` (with ``details.attempts``), and so does the turn
deadline passing between attempts (``reason=deadline``). The turn then fails as a whole.
"""

from __future__ import annotations

import hashlib
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
from epistrel.orchestrator.models.adapter import (
    MAX_COMPLETION_TOKENS,
    REQUEST_FIELDS,
    Message,
    ModelParams,
    ModelResult,
)
from epistrel.orchestrator.models.call_trace import STRUCTURED_MODEL, STRUCTURED_SCHEMA
from epistrel.orchestrator.models.errors import (
    ModelUnavailable,
    ResponseFormatRejected,
    SchemaTooLarge,
)
from epistrel.orchestrator.models.prompts import payload_fields
from epistrel.orchestrator.models.provider_schema import json_schema_mode
from epistrel.orchestrator.models.registry import GuardrailVerdict, InputSchema

if TYPE_CHECKING:
    from epistrel.orchestrator.models.access import ModelAccessLayer

type UncertainReason = Literal["parse_error", "schema_error", "low_confidence", "budget_exhausted"]
type DeterministicFirst[T] = Callable[[], T | None]

DETERMINISTIC_FIRST_EXEMPT = frozenset(
    {
        "generation",
        "lite_summary",
        "rewrite_parse",
        "rewrite_record",
        "directive_parse",
        "lite_rewrite_reach",
        "lite_rewrite_parse",
        "image_prompt",
    }
)
"""Purposes with no deterministic path (FR-MODEL-5's residual is the whole call).
``rewrite_parse`` (#223) reads the author's free-text instruction; everything after it
(normalization, the questions, the plan) is deterministic code. ``rewrite_record`` (#224) is
called only for a record the deterministic substitution cannot repair. ``image_prompt`` (#259)
writes prose from the perceived inputs; the negative prompt, seed, references and size are
deterministic and never pass through it."""

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


STRUCTURED_HEADROOM_TOKENS = 1_024
"""Tokens every structured call's ``max_tokens`` gets on top of what its caller asks for
(#735, ADR-201): a model that reasons spends hidden tokens against the same cap, and a
length-sized cap ran out before the JSON closed. The caller's figure stays the length target
(``max_chars`` still enforces it, #464); this is a ceiling, clamped to
``MAX_COMPLETION_TOKENS``. A call that sends no ``max_tokens`` gets none."""


def with_headroom(params: ModelParams | None) -> ModelParams | None:
    """``params`` with ``STRUCTURED_HEADROOM_TOKENS`` added to a ``max_tokens`` it sets."""
    if params is None or params.max_tokens is None:
        return params
    ceiling = min(params.max_tokens + STRUCTURED_HEADROOM_TOKENS, MAX_COMPLETION_TOKENS)
    return params.model_copy(update={"max_tokens": ceiling})


PROSE_STRIKES = 2
"""Consecutive non-JSON ``200`` answers in ``json_schema`` mode, across calls to one
``(base_url, model)``, that prove the endpoint ignores ``response_format`` (#482). One is a
glitch, not evidence; two in a row still leave a call with the default retry budget an attempt
in the fallback."""


class ResponseFormatMemo:
    """``(base_url, model)`` pairs whose endpoint does not honour ``json_schema``, each valid
    only at the ``config_revision`` it was learned at, plus each pair's run of consecutive
    non-JSON answers in ``json_schema`` mode (``strike``; #482)."""

    def __init__(self) -> None:
        self._rejected: dict[tuple[str, str], int] = {}
        self._strikes: dict[tuple[str, str], tuple[int, int]] = {}
        self._too_large: dict[tuple[str, str, str], int] = {}

    def schema_supported(self, key: tuple[str, str], schema: str, revision: int) -> bool:
        """Whether ``schema`` (a ``schema_digest``) may go in ``json_schema`` mode to ``key``:
        not once that endpoint refused it as too large at this revision (#1009)."""
        learned = self._too_large.get((*key, schema))
        if learned is not None and learned != revision:
            del self._too_large[(*key, schema)]
            return True
        return learned is None

    def reject_schema(self, key: tuple[str, str], schema: str, revision: int) -> None:
        self._too_large[(*key, schema)] = revision

    def supported(self, key: tuple[str, str], revision: int) -> bool:
        learned = self._rejected.get(key)
        if learned is not None and learned != revision:
            del self._rejected[key]
            return True
        return learned is None

    def reject(self, key: tuple[str, str], revision: int) -> None:
        self._rejected[key] = revision
        self._strikes.pop(key, None)

    def strike(self, key: tuple[str, str], revision: int) -> bool:
        """Count a non-JSON answer in ``json_schema`` mode; ``True`` (and remembered as
        rejected) once ``PROSE_STRIKES`` have come in a row at this revision."""
        learned, count = self._strikes.get(key, (revision, 0))
        count = count + 1 if learned == revision else 1
        if count >= PROSE_STRIKES:
            self.reject(key, revision)
            return True
        self._strikes[key] = (revision, count)
        return False

    def honoured(self, key: tuple[str, str]) -> None:
        """A JSON answer in ``json_schema`` mode: the run of non-JSON answers is broken."""
        self._strikes.pop(key, None)

    def clear(self) -> None:
        self._rejected.clear()
        self._strikes.clear()
        self._too_large.clear()


def schema_digest(schema: type[BaseModel]) -> str:
    """A response schema's identity for ``ResponseFormatMemo.reject_schema``: its JSON Schema's
    hash, so two consumer purposes with the same output shape share one entry (#1009)."""
    text = json.dumps(schema.model_json_schema(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _Failure:
    """A failed attempt: ``problem`` is the correction the model is sent, ``public`` what the
    caller's report keeps (the same text unless a validator gave a content-free form)."""

    reason: Literal["parse_error", "schema_error"]
    problem: str
    public: str = ""
    codes: frozenset[str] = frozenset()
    """#996: the error types of the validation's errors (``correct_once``'s among them)."""

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


CORRECTED = "corrected"
"""#996: the validation context key of the correction codes already sent in this call."""


def correct_once(code: str, problems: str) -> PydanticCustomError:
    """#996: a validator's error that is sent as a correction once per call: after it, an
    attempt is validated with ``code`` in ``corrected(info)``, and the validator repairs the
    answer instead of raising it again. ``problems`` names no story content."""
    return PydanticCustomError(code, "{problems}", {"problems": problems})


def corrected(context: Any, code: str) -> bool:
    """#996: whether ``code``'s correction was already sent in this call (``context`` is the
    validator's ``ValidationInfo.context``; ``None`` outside ``structured_messages``)."""
    return isinstance(context, Mapping) and code in context.get(CORRECTED, ())


def unfence(text: str) -> str:
    """``text`` without a surrounding ```` ```json ```` fence."""
    match = _FENCE.match(text)
    return match.group("body") if match else text


def parse[T: BaseModel](
    text: str, schema: type[T], seen: frozenset[str] = frozenset()
) -> T | _Failure:
    """``text`` as a strict ``schema`` instance, or why it is not one (never its contents).
    ``seen``: the correction codes already sent in this call (#996, ``corrected``)."""
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
        return schema.model_validate_json(body, strict=True, context={CORRECTED: seen})
    except ValidationError as exc:
        problems: list[str] = []
        public: list[str] = []
        codes = frozenset(error["type"] for error in exc.errors())
        for error in exc.errors()[:MAX_FEEDBACK_ERRORS]:
            where = ".".join(str(p) for p in error["loc"]) or "the object"
            problems.append(f"{where}: {error['msg']}")
            context = error.get("ctx") or {}
            public.append(f"{where}: {context.get(PUBLIC, error['msg'])}")
        return _Failure("schema_error", "; ".join(problems), "; ".join(public), codes)


def usable(
    result: ModelResult, schema: type[BaseModel], seen: frozenset[str] = frozenset()
) -> bool:
    """Whether ``result`` is a usable ``schema`` instance (the attempt's recorded outcome)."""
    if result.refused or result.finish_reason == "length":
        return False
    return not isinstance(parse(result.text, schema, seen), _Failure)


def _usable_by(schema: type[BaseModel], seen: frozenset[str]) -> Callable[[ModelResult], bool]:
    """``usable`` for one attempt: its schema and the corrections sent before it (#996)."""
    return lambda answer: usable(answer, schema, seen)


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
    keep_low_confidence: bool = False,
) -> T | Uncertain:
    """See the module docstring. ``variant`` names the purpose's template variant (#150);
    ``failures``, when given, collects each failed attempt's problem (what was wrong, never the
    answer's text; a ``feedback`` error's content-free form) for the caller's report.
    ``keep_low_confidence`` (#227's ``salience_bias = recall``) returns a valid answer whose
    ``confidence`` is below ``uncertain_below`` instead of ``Uncertain("low_confidence")``: the
    caller decides what it counts as."""
    if deterministic_first is None and purpose not in DETERMINISTIC_FIRST_EXEMPT:
        raise MissingDeterministicStep(
            f"a structured {purpose!r} call needs its deterministic_first step (FR-MODEL-5)"
        )
    _, payload, base = mal.prepare(purpose, inputs, variant=variant)
    if deterministic_first is not None:
        conclusive = deterministic_first()
        if conclusive is not None:
            return conclusive
    return await structured_messages(
        mal,
        purpose,
        schema,
        base,
        params=params,
        budget=budget,
        failures=failures,
        keep_low_confidence=keep_low_confidence,
        fields=payload_fields(payload),
    )


async def structured_messages[T: BaseModel](
    mal: ModelAccessLayer,
    purpose: str,
    schema: type[T],
    base: Sequence[Message],
    *,
    params: ModelParams | None = None,
    budget: TurnBudget,
    failures: list[str] | None = None,
    keep_low_confidence: bool = False,
    fields: tuple[str, ...] | None = None,
) -> T | Uncertain:
    """Steps 2 to 4 of the module docstring over messages already rendered (``base``): what
    ``structured_call`` runs after its deterministic step, and what a consumer purpose's call
    runs (FR-MODEL-15, #825: its messages come from its own templates, not the registry's, and
    it has no deterministic step). ``fields`` are the declared input paths the stub records."""
    settings = await mal.structured_settings()
    base_url, model, revision = await mal.endpoint(purpose)
    key = (base_url, model)
    digest = schema_digest(schema)
    json_mode = (
        mal.response_format.supported(key, revision)
        and mal.response_format.schema_supported(key, digest, revision)
        and json_schema_mode(base_url, schema)
    )
    messages = list(base) if json_mode else with_schema(base, schema)
    call_params = params
    retries_left = settings.structured_retry_budget
    attempts = 0
    seen: frozenset[str] = frozenset()  # #996: the correction codes sent so far
    token = REQUEST_FIELDS.set(fields)
    # #737: an eval trace names the schema even when the prompt carries it.
    schema_token = STRUCTURED_SCHEMA.set(schema.__name__)
    model_token = STRUCTURED_MODEL.set(schema)
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
                    with_headroom(call_params),  # #735: on every attempt
                    schema if json_mode else None,
                    budget=budget,
                    attempt=attempts,
                    usable=_usable_by(schema, seen),
                    format_probe=json_mode,
                )
            except ResponseFormatRejected as exc:
                if not json_mode:
                    raise exc.with_attempts(attempts) from None
                if isinstance(exc, SchemaTooLarge):
                    # #1009: this schema only; the endpoint keeps json_schema for the rest.
                    mal.response_format.reject_schema(key, digest, revision)
                else:
                    mal.response_format.reject(key, revision)
                json_mode = False
                messages = with_schema(base, schema)
                # #759: the probe decided nothing. Its slot goes back, and the fallback claims it
                # as the call's first attempt. It stays in ``model_calls``, marked ``probe``
                # (ADR-180, #764).
                budget.refund(purpose)
                attempts -= 1
                continue
            except ModelUnavailable as exc:
                raise exc.with_attempts(attempts) from None
            if result.refused or result.finish_reason == "length":
                return Uncertain("parse_error", attempts)
            outcome = parse(result.text, schema, seen)
            if isinstance(outcome, _Failure) and failures is not None:
                failures.append(outcome.reported())
            prose = isinstance(outcome, _Failure) and outcome.reason == "parse_error"
            if json_mode and not prose:
                mal.response_format.honoured(key)  # JSON, valid or not: json_schema was obeyed
            if not isinstance(outcome, _Failure):
                if not keep_low_confidence and _low_confidence(outcome, settings.uncertain_below):
                    return Uncertain("low_confidence", attempts)
                return outcome
            if json_mode and prose and mal.response_format.strike(key, revision):
                # PROSE_STRIKES non-JSON 200s in a row (across calls): the endpoint ignores
                # json_schema. A single one is a glitch, and the retry stays constrained (#482).
                json_mode = False
                messages = with_schema(base, schema)
            if retries_left == 0:
                return Uncertain(outcome.reason, attempts)
            retries_left -= 1
            seen |= outcome.codes
            messages = [
                *messages,
                Message(role="assistant", content=result.text),
                Message(role="user", content=CORRECTION.format(problem=outcome.problem)),
            ]
            call_params = (
                params  # #685: the call's own temperature, so the retry is not a near-copy
                if outcome.reason == "schema_error"
                else (params or ModelParams()).merged(ModelParams(temperature=0.0))
            )
    finally:
        STRUCTURED_MODEL.reset(model_token)
        STRUCTURED_SCHEMA.reset(schema_token)
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
    "STRUCTURED_HEADROOM_TOKENS",
    "DeterministicFirst",
    "MissingDeterministicStep",
    "ResponseFormatMemo",
    "Uncertain",
    "UncertainReason",
    "correct_once",
    "corrected",
    "feedback",
    "guardrail_deterministic_first",
    "parse",
    "resolve_uncertain",
    "schema_digest",
    "structured_call",
    "structured_messages",
    "uncertain_verdict",
    "unfence",
    "usable",
    "with_headroom",
    "with_schema",
]
