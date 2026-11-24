"""#735 (FR-RESP-3, NFR-QUAL-7; ADR-201): a structured call's ``max_tokens`` is a ceiling with
headroom above its length target, and every chat call to api.anthropic.com sends
``thinking: {"type": "disabled"}``. The live failure this guards: Haiku spent most of a
length-sized render cap on hidden reasoning and finished ``length`` before its JSON closed."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx2 as httpx
import openai
import pytest

from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import call_trace, openai_compatible, structured
from epistrel.orchestrator.models.adapter import MAX_COMPLETION_TOKENS, Message, ModelParams
from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.openai_compatible import (
    THINKING_DISABLED,
    OpenAICompatibleAdapter,
    request_body,
    sdk_args,
    sends_thinking,
)
from epistrel.orchestrator.models.registry import GenerationInputs, GuardrailVerdict, PlayerInput
from epistrel.orchestrator.models.structured import STRUCTURED_HEADROOM_TOKENS, with_headroom
from epistrel.orchestrator.structured.render import (
    RENDER_TOKEN_FACTOR,
    render_beats,
    render_params,
    render_schema,
)
from tests.model_support import config_key
from tests.test_structured_output import models

ANTHROPIC = "https://api.anthropic.com/v1"
LOCAL = "http://localhost:1234/v1"
HELLO = (Message(role="user", content="Say hello"),)
SCHEMA = render_schema(actor_id="c1", player_id=None, cast_ids=["c1"], max_beats=6)
HIDDEN = 200
"""The stub's hidden share in the red/green test: about the share Haiku spent live."""
LENGTH = 120
"""E-06's ``length-120`` group, which failed most often live."""


def _render_text() -> str:
    """A valid three-beat render of about 1,500 characters: past a 120-token render's visible
    budget under the old cap (480 - 200 hidden = 280 tokens, 1,120 characters), inside it with
    the headroom (1,304 tokens)."""
    beats = [{"kind": "say", "text": "Maren smiles. " + "w" * 470} for _ in range(3)]
    text = json.dumps({"beats": beats, "introduces": []})
    assert (
        1_120 < len(text) < 4 * (LENGTH * RENDER_TOKEN_FACTOR + STRUCTURED_HEADROOM_TOKENS - HIDDEN)
    )
    return text


async def _render(monkeypatch: pytest.MonkeyPatch, headroom: int) -> Any:
    monkeypatch.setattr(structured, "STRUCTURED_HEADROOM_TOKENS", headroom)
    mal = models()
    mal.stub.hidden_tokens["generation"] = HIDDEN
    mal.stub.script("generation", [_render_text()] * 3)
    inputs = GenerationInputs(player_input=PlayerInput(name="Wren"))
    params = render_params(ModelParams(temperature=0.8), LENGTH)
    found = await render_beats(
        mal, inputs, SCHEMA, variant="structured", params=params, budget=TurnBudget()
    )
    return found, mal.stub.calls


async def test_hidden_reasoning_no_longer_ends_a_short_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red with the old cap (no headroom): the stub's hidden share leaves too little for the
    JSON, the answer finishes ``length``, and the render is 503 ``invalid_structure`` on its
    first attempt. Green with the headroom: the same answer fits and validates."""
    with pytest.raises(ModelUnavailable):
        await _render(monkeypatch, 0)
    (render, retries), calls = await _render(monkeypatch, STRUCTURED_HEADROOM_TOKENS)
    assert (len(render.beats), retries) == (3, 0)
    assert calls[0].params.max_tokens == LENGTH * RENDER_TOKEN_FACTOR + STRUCTURED_HEADROOM_TOKENS


async def test_the_headroom_rides_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry (here after a non-JSON answer) and its ``temperature=0`` params keep the
    ceiling: the headroom is added once per attempt, never compounded."""
    monkeypatch.setattr(structured, "STRUCTURED_HEADROOM_TOKENS", STRUCTURED_HEADROOM_TOKENS)
    mal = models()
    mal.stub.script("generation", ["not json", _render_text()])
    inputs = GenerationInputs(player_input=PlayerInput(name="Wren"))
    params = render_params(ModelParams(temperature=0.8), LENGTH)
    await render_beats(
        mal, inputs, SCHEMA, variant="structured", params=params, budget=TurnBudget()
    )
    expected = LENGTH * RENDER_TOKEN_FACTOR + STRUCTURED_HEADROOM_TOKENS
    assert [c.params.max_tokens for c in mal.stub.calls] == [expected, expected]
    assert mal.stub.calls[1].params.temperature == 0.0


def test_with_headroom_is_a_clamped_ceiling_and_no_cap_stays_no_cap() -> None:
    assert with_headroom(None) is None
    plain = ModelParams(temperature=0.2)
    assert with_headroom(plain) is plain
    assert with_headroom(ModelParams(max_tokens=100)) == ModelParams(
        max_tokens=100 + STRUCTURED_HEADROOM_TOKENS
    )
    # A fixed built-in cap at the adapter's limit (extraction) stays at the limit.
    assert with_headroom(ModelParams(max_tokens=MAX_COMPLETION_TOKENS)) == ModelParams(
        max_tokens=MAX_COMPLETION_TOKENS
    )


def test_the_reply_length_is_still_the_length_target() -> None:
    """The ceiling is the call's, not the reply's: ``render_params`` still sizes the render from
    the length (the beats past ``max_chars`` are dropped as before, ``test_length_rules``)."""
    assert render_params(ModelParams(), LENGTH).max_tokens == LENGTH * RENDER_TOKEN_FACTOR


# -- the thinking body extra ------------------------------------------------------------------


def test_thinking_is_sent_to_anthropic_alone() -> None:
    assert sends_thinking(ANTHROPIC)
    for other in (LOCAL, "https://api.openai.com/v1", "https://api.anthropic.com.example.net/v1"):
        assert not sends_thinking(other)
    body = request_body("m", HELLO, ModelParams(max_tokens=50), None, thinking=True)
    assert body["thinking"] == THINKING_DISABLED == {"type": "disabled"}
    assert "thinking" not in request_body("m", HELLO, ModelParams(max_tokens=50), None)
    args = sdk_args(body)
    assert "thinking" not in args
    assert args["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "extra_body" not in sdk_args(request_body("m", HELLO, ModelParams(), None))


class _Server:
    """An httpx transport standing in for an endpoint: records each request body, and answers
    a body carrying ``thinking`` with a 400 naming it while ``reject_thinking`` is set."""

    def __init__(self) -> None:
        self.reject_thinking = False
        self.reject_all: str | None = None
        """A 400 message every request gets while set."""
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        message = self.reject_all
        if message is None and self.reject_thinking and "thinking" in body:
            message = "thinking: not supported"
        if message is not None:
            error = {"type": "invalid_request_error", "message": message}
            return httpx.Response(400, json={"error": error})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "hello"},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    found = _Server()
    real = openai.AsyncOpenAI

    def client(**kwargs: Any) -> openai.AsyncOpenAI:
        transport = httpx.MockTransport(found)
        return real(**kwargs, http_client=httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(openai, "AsyncOpenAI", client)
    return found


def _adapter(base_url: str, holder: list[EffectiveConfig]) -> OpenAICompatibleAdapter:
    keybox = KeyBox(config_key())
    holder.append(
        EffectiveConfig(
            purpose="generation",
            base_url=base_url,
            model="claude-haiku-5-5",
            api_key_ciphertext=keybox.encrypt("sk-test"),
            params=ModelParams(timeout=5),
            revision=1,
        )
    )

    async def resolver(_: str) -> EffectiveConfig:
        return holder[-1]

    return OpenAICompatibleAdapter(resolver, keybox)


async def test_anthropic_calls_carry_thinking_and_local_ones_do_not(server: _Server) -> None:
    for base in (ANTHROPIC, LOCAL):
        adapter = _adapter(base, [])
        try:
            await adapter.complete("generation", HELLO, ModelParams(max_tokens=20))
        finally:
            await adapter.aclose()
    assert [b.get("thinking") for b in server.bodies] == [{"type": "disabled"}, None]


async def test_a_400_about_thinking_is_retried_without_it_and_remembered(
    server: _Server, caplog: pytest.LogCaptureFixture
) -> None:
    """One 400 naming ``thinking``: the call is sent again without it, and later calls skip it
    until the configuration revision moves. The fallback is logged with the purpose and model
    only."""
    server.reject_thinking = True
    holder: list[EffectiveConfig] = []
    adapter = _adapter(ANTHROPIC, holder)
    try:
        with caplog.at_level("INFO", logger=openai_compatible.__name__):
            first = await adapter.complete("generation", HELLO, ModelParams(max_tokens=20))
        again = await adapter.complete("generation", HELLO, ModelParams(max_tokens=20))
        holder.append(replace(holder[-1], revision=2))
        server.reject_thinking = False
        moved = await adapter.complete("generation", HELLO, ModelParams(max_tokens=20))
    finally:
        await adapter.aclose()
    assert first.text == again.text == moved.text == "hello"
    assert ["thinking" in b for b in server.bodies] == [True, False, False, True]
    logged = [r.getMessage() for r in caplog.records if "thinking_fallback" in r.getMessage()]
    assert logged == ["model_call_thinking_fallback purpose=generation model=claude-haiku-5-5"]


async def test_the_trace_keeps_both_requests_of_a_thinking_fallback(server: _Server) -> None:
    """#1004 (ADR-201: "the trace keeps both requests"): under an eval run's call trace (the
    exchange list ``ModelAccessLayer.send`` opens and hands its sink), a call whose first
    request is refused for ``thinking`` records two exchanges: the first with ``thinking`` and
    the 400, the second without it and the reply.

    Complement asserted: the endpoint saw the same two bodies."""
    server.reject_thinking = True
    adapter = _adapter(ANTHROPIC, [])
    opened: list[call_trace.Exchange] = []
    token = call_trace.EXCHANGES.set(opened)
    try:
        answered = await adapter.complete("generation", HELLO, ModelParams(max_tokens=20))
    finally:
        call_trace.EXCHANGES.reset(token)
        await adapter.aclose()
    assert answered.text == "hello"
    assert len(opened) == 2, opened
    refused, retried = opened
    assert refused.body["thinking"] == {"type": "disabled"}
    assert refused.error is not None
    assert refused.error["status"] == 400
    assert "thinking" in json.dumps(refused.error["body"])
    assert refused.response is None
    assert "thinking" not in retried.body
    assert retried.error is None
    assert retried.response is not None
    assert retried.response["text"] == "hello"
    assert [e.body for e in opened] == server.bodies


async def test_a_400_about_something_else_is_not_swallowed(server: _Server) -> None:
    """Complement: a 400 that does not name ``thinking`` is not retried by the thinking path
    (here ``response_format``'s, which the structured caller handles)."""
    server.reject_all = "response_format: unsupported"
    adapter = _adapter(ANTHROPIC, [])
    try:
        with pytest.raises(openai.BadRequestError, match="response_format"):
            await adapter.complete("generation", HELLO, ModelParams(), GuardrailVerdict)
    finally:
        await adapter.aclose()
    assert len(server.bodies) == 1
    assert server.bodies[0]["thinking"] == {"type": "disabled"}
