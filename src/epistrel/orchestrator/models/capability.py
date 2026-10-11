"""Protocol compatibility and per-purpose capability, measured separately (NFR-PORT-1,3;
FR-MODEL-10; docs/04 C-07 and E-06).

A model "works" for a purpose only when it meets that purpose's capability floor
(``registry.CapabilityFloor``); speaking the protocol is not a quality claim. So the checks
return two separate results:

- **Protocol** (``check_protocol``): ``GET /models`` answers and one minimal completion succeeds
  (``ModelAccessLayer.probe``). ``json_schema`` support is not part of it.
- **Capability per purpose** (``assess_purpose``), each floor check reported on its own:
  ``context``: a prompt filled to about ``min_context_tokens - 192`` estimated tokens
  (characters / 4) completes with a non-empty answer within 120 s;
  ``structured_output``: for a purpose whose floor needs it, at least 95 % of 20 calls return a
  valid instance of a synthetic schema (``ProbeAnswer``) on the first attempt. The call asks for
  ``json_schema`` first and falls back to the schema in the prompt when the endpoint refuses it
  or answers prose, and the mode it ended in is recorded. ``generation`` and ``lite_summary``
  skip it (their floor is ``none``).

``check_prompt_params`` is E-06's M2 part (FR-RESP-3): for each ``max_tokens`` length the
answer's ``tokens_out`` (or characters / 4 when the endpoint reports no usage) stays within it,
and at temperature 0 at least 80 % of 10 samples are identical.

The probes carry synthetic text only, never story content. They go through
``ModelAccessLayer.send``, so they are counted like any other call.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.orchestrator.models.adapter import Message, ModelParams, ModelResult
from epistrel.orchestrator.models.errors import (
    EmptyCompletion,
    ModelError,
    ResponseFormatRejected,
)
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    GenerationInputs,
    PlayerInput,
    Purpose,
    StructuredOutput,
)
from epistrel.orchestrator.models.structured import parse, usable, with_schema

if TYPE_CHECKING:
    from epistrel.orchestrator.models.access import ModelAccessLayer

CONTEXT_MARGIN_TOKENS = 192
CONTEXT_TIMEOUT_S = 120.0
CHARS_PER_TOKEN = 4
STRUCTURED_CALLS = 20
STRUCTURED_THRESHOLD = 0.95
E06_LENGTHS = (32, 128)
E06_SAMPLES = 10
E06_THRESHOLD = 0.8
CODE_WORD = "LANTERN"
FILLER = "The quick brown fox jumps over the lazy dog while the mill wheel turns. "

type Mode = Literal["json_schema", "parse_validate_retry"]


class ProbeAnswer(BaseModel):
    """The structured-output probe's synthetic schema (no story content)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    colour: Literal["red", "green", "blue"]
    count: int = Field(ge=0, le=100)


@dataclass(frozen=True)
class ProtocolResult:
    ok: bool
    reason: str | None = None
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class FloorCheck:
    """One floor check for one purpose. ``passed`` is ``None`` when the floor does not ask
    for it."""

    name: Literal["context", "structured_output"]
    passed: bool | None
    detail: str
    mode: Mode | None = None
    valid: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class PurposeCapability:
    purpose: str
    checks: tuple[FloorCheck, ...]

    @property
    def capable(self) -> bool:
        return all(check.passed is not False for check in self.checks)


@dataclass(frozen=True)
class CapabilityReport:
    """Protocol and capability, kept apart: a protocol pass says nothing about capability,
    and an incapable purpose is not a protocol failure."""

    protocol: ProtocolResult
    purposes: dict[str, PurposeCapability] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptParamsResult:
    """E-06's M2 part: ``lengths`` maps each ``max_tokens`` to the ``tokens_out`` observed."""

    lengths: dict[int, int | None]
    identical: int
    samples: int

    @property
    def lengths_ok(self) -> bool:
        return all(out is not None and out <= cap for cap, out in self.lengths.items())

    @property
    def repeatable(self) -> bool:
        return self.samples > 0 and self.identical / self.samples >= E06_THRESHOLD

    @property
    def passed(self) -> bool:
        return self.lengths_ok and self.repeatable


async def check_protocol(mal: ModelAccessLayer, purpose: str = "generation") -> ProtocolResult:
    try:
        probe = await mal.probe(purpose)
    except ModelError as exc:
        return ProtocolResult(ok=False, reason=str(exc.details.get("reason", exc.code)))
    return ProtocolResult(ok=True, models=probe.models)


def context_prompt(target_tokens: int) -> list[Message]:
    """A prompt of about ``target_tokens`` estimated tokens, the code word at its start."""
    body_chars = max(0, target_tokens * CHARS_PER_TOKEN)
    filler = (FILLER * (body_chars // len(FILLER) + 1))[:body_chars]
    return [
        Message(role="system", content="You answer questions about the text you are given."),
        Message(
            role="user",
            content=f"The code word is {CODE_WORD}.\n{filler}\n"
            "What was the code word at the start? Answer with the word only.",
        ),
    ]


async def check_context(mal: ModelAccessLayer, purpose: Purpose) -> FloorCheck:
    target = purpose.capability_floor.min_context_tokens - CONTEXT_MARGIN_TOKENS
    params = ModelParams(max_tokens=16, temperature=0.0, timeout=CONTEXT_TIMEOUT_S)
    try:
        result = await mal.complete(purpose.name, context_prompt(target), params, None)
    except EmptyCompletion:
        return FloorCheck("context", False, f"~{target} tokens: empty answer")
    except ModelError as exc:
        return FloorCheck("context", False, f"~{target} tokens: {exc.details.get('reason')}")
    return FloorCheck("context", True, f"~{target} tokens: answered ({_snippet(result)})")


def _snippet(result: ModelResult) -> str:
    return f"{result.tokens_out or len(result.text) // CHARS_PER_TOKEN} tokens out"


def structured_prompt(index: int) -> list[Message]:
    return [
        Message(role="system", content="You produce small JSON objects on request."),
        Message(
            role="user",
            content=f"Item {index}: choose a colour (red, green or blue) and a count between "
            "0 and 100. Answer with a JSON object with the keys colour and count.",
        ),
    ]


async def check_structured(
    mal: ModelAccessLayer, purpose: Purpose, calls: int = STRUCTURED_CALLS
) -> FloorCheck:
    if purpose.capability_floor.structured_output is StructuredOutput.NONE:
        return FloorCheck("structured_output", None, "not required by the floor")
    json_mode = True
    valid = attempts = 0
    params = ModelParams(temperature=0.0, max_tokens=64)
    while attempts < calls:
        messages = structured_prompt(attempts)
        if not json_mode:
            messages = with_schema(messages, ProbeAnswer)
        try:
            result = await mal.send(
                purpose.name,
                messages,
                params,
                ProbeAnswer if json_mode else None,
                usable=lambda answer: usable(answer, ProbeAnswer),
            )
        except ResponseFormatRejected:
            # The endpoint refuses json_schema: ask in the prompt; the 400 is not an attempt.
            json_mode = False
            continue
        except ModelError:
            attempts += 1
            continue
        attempts += 1
        if usable(result, ProbeAnswer):
            valid += 1
        elif json_mode and getattr(parse(result.text, ProbeAnswer), "reason", None) == (
            "parse_error"
        ):
            # Prose in json_schema mode: the endpoint ignores it, so ask in the prompt.
            json_mode = False
    mode: Mode = "json_schema" if json_mode else "parse_validate_retry"
    passed = valid >= STRUCTURED_THRESHOLD * calls
    return FloorCheck(
        "structured_output",
        passed,
        f"{valid}/{calls} valid ({mode})",
        mode=mode,
        valid=valid,
        total=calls,
    )


async def assess_purpose(mal: ModelAccessLayer, purpose: Purpose) -> PurposeCapability:
    context = await check_context(mal, purpose)
    structured = await check_structured(mal, purpose)
    return PurposeCapability(purpose.name, (context, structured))


async def assess(
    mal: ModelAccessLayer, purposes: Iterable[Purpose] | None = None
) -> CapabilityReport:
    """The protocol probe, then each purpose's floor checks (registry order)."""
    protocol = await check_protocol(mal)
    found: dict[str, PurposeCapability] = {}
    if protocol.ok:
        for purpose in purposes if purposes is not None else DELIVERED.values():
            found[purpose.name] = await assess_purpose(mal, purpose)
    return CapabilityReport(protocol, found)


def _probe_inputs(text: str) -> GenerationInputs:
    return GenerationInputs(player_input=PlayerInput(name="Probe", say=text))


async def check_prompt_params(
    mal: ModelAccessLayer,
    *,
    lengths: Iterable[int] = E06_LENGTHS,
    samples: int = E06_SAMPLES,
) -> PromptParamsResult:
    """E-06 (FR-RESP-3): ``max_tokens`` and temperature are honoured for ``generation``."""
    observed: dict[int, int | None] = {}
    for cap in lengths:
        prompt = _probe_inputs("Describe a long walk through a quiet town, in detail.")
        try:
            result = await mal.call("generation", prompt, ModelParams(max_tokens=cap))
        except ModelError:
            observed[cap] = None
            continue
        observed[cap] = (
            result.tokens_out
            if result.tokens_out is not None
            else len(result.text) // CHARS_PER_TOKEN
        )
    texts: list[str] = []
    for _ in range(samples):
        prompt = _probe_inputs("Say one short sentence about the weather.")
        try:
            result = await mal.call(
                "generation", prompt, ModelParams(temperature=0.0, max_tokens=48)
            )
        except ModelError:
            continue
        texts.append(result.text)
    identical = Counter(texts).most_common(1)[0][1] if texts else 0
    return PromptParamsResult(lengths=observed, identical=identical, samples=samples)


__all__ = [
    "CapabilityReport",
    "FloorCheck",
    "ProbeAnswer",
    "PromptParamsResult",
    "ProtocolResult",
    "PurposeCapability",
    "assess",
    "assess_purpose",
    "check_context",
    "check_prompt_params",
    "check_protocol",
    "check_structured",
    "context_prompt",
]
