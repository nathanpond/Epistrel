"""Time the first and second strict ``json_schema`` call per schema on an endpoint (#785).

api.anthropic.com compiles a strict ``json_schema`` into a grammar on its first use and caches
it, so the first structured call of a schema can take much longer than the next. This probe
measures that: for every schema a structured call may send strictly (the registry's delivered
purposes' and the director's order and tie-break schemas, each with a strict wire form), it
sends one cold call and then one warm call, sequentially, in each of ``--rounds`` rounds. A
render's beat schema (a character's or the narrator's) has no strict wire form, so a render is
never sent strictly, compiles no grammar, and is left out.

**Cold, by construction.** Each round adds a property named after a per-round nonce
(``probe_<nonce>``, a string, required as strict decoding needs) to every schema. The assumption
is that the provider keys its grammar cache on the schema itself, so a schema with a new
property is a new grammar, and the first call of each round is cold without waiting out the
cache's expiry.

The request is tiny and fixed (a two-line prompt, ``max_tokens`` 64): the timing is of the
compile and the first tokens, not of a real answer, and a reply cut off at ``max_tokens`` is
fine. The client timeout is 600 s, so a slow compile is measured rather than cut off at the
engine's 60 s. The report names each schema's cold and warm latencies, the largest cold one,
and the thresholds that matter: 45 s (Standard's and Full's per-call deadline), 60 s
(``DEFAULT_TIMEOUT_S``) and 210 s (the eval harness's ``CALL_TIMEOUT_S``; 120 s before #785's
measurement).

Measurement tooling, not engine behaviour: ``uv run python -m epistrel.tools.schema_compile_probe
--base-url https://api.anthropic.com/v1 --model claude-haiku-5-5`` with the key in
``EPISTREL_EVALS_API_KEY``. Nothing it sends carries story content.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import openai
from pydantic import BaseModel

from epistrel.core.profiles.schema import DEFAULT_MAX_BEATS
from epistrel.orchestrator.models.adapter import DEFAULT_TIMEOUT_S
from epistrel.orchestrator.models.provider_schema import strict_wire_schema
from epistrel.orchestrator.models.registry import DELIVERED
from epistrel.orchestrator.structured.multi import DirectorOrder
from epistrel.orchestrator.structured.render import render_schema
from epistrel.orchestrator.structured.simultaneous import DirectorTiebreak

API_KEY_ENV = "EPISTREL_EVALS_API_KEY"
CLIENT_TIMEOUT_S = 600.0
MAX_TOKENS = 64
TURN_DEADLINE_S = 45.0
"""Standard's and Full's per-call bound (their ``deadline_ms`` clamps each call)."""
HARNESS_TIMEOUT_S = 210.0
"""``evals.runner.CALL_TIMEOUT_S`` (not imported: the tools never import the eval harness)."""
PROMPT = (
    {"role": "system", "content": "Answer only with a JSON object matching the schema."},
    {"role": "user", "content": "Probe."},
)


def schemas() -> list[tuple[str, dict[str, Any]]]:
    """Every distinct schema a structured call may send strictly, by name, as its wire form."""
    models: list[type[BaseModel]] = [
        p.response_schema for p in DELIVERED.values() if p.response_schema is not None
    ]
    models += [
        render_schema(
            actor_id="a", player_id="p", cast_ids=["a", "p"], max_beats=DEFAULT_MAX_BEATS
        ),
        render_schema(
            actor_id=None, player_id="p", cast_ids=["a", "p"], max_beats=DEFAULT_MAX_BEATS
        ),
        DirectorOrder,
        DirectorTiebreak,
    ]
    found: dict[str, dict[str, Any]] = {}
    for model in models:
        wire = strict_wire_schema(model.model_json_schema())
        if wire is not None:
            found.setdefault(model.__name__, wire)
    return sorted(found.items())


def with_nonce(schema: dict[str, Any], nonce: str) -> dict[str, Any]:
    """``schema`` with a required string property ``probe_<nonce>`` at its root."""
    changed = copy.deepcopy(schema)
    name = f"probe_{nonce}"
    changed.setdefault("properties", {})[name] = {"type": "string"}
    changed["required"] = [*changed.get("required", []), name]
    return changed


def body(model: str, name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [dict(m) for m in PROMPT],
        "max_completion_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        },
    }


@dataclass
class Timing:
    schema: str
    cold_s: list[float] = field(default_factory=list)
    warm_s: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def _call(client: openai.AsyncOpenAI, request: dict[str, Any]) -> tuple[float, str | None]:
    started = time.perf_counter()
    try:
        await client.chat.completions.create(**request, timeout=CLIENT_TIMEOUT_S)
    except openai.APIStatusError as exc:
        return time.perf_counter() - started, f"HTTP {exc.status_code}"
    except openai.APIError as exc:
        return time.perf_counter() - started, type(exc).__name__
    return time.perf_counter() - started, None


async def measure(
    client: openai.AsyncOpenAI,
    model: str,
    rounds: int,
    only: Sequence[str] = (),
) -> list[Timing]:
    """Each schema's cold and warm latency per round (sequential)."""
    chosen = [(n, s) for n, s in schemas() if not only or n in only]
    timings = {name: Timing(name) for name, _ in chosen}
    for _ in range(rounds):
        nonce = uuid.uuid4().hex[:12]
        for name, schema in chosen:
            request = body(model, name, with_nonce(schema, nonce))
            for kind in ("cold", "warm"):
                elapsed, error = await _call(client, request)
                (timings[name].cold_s if kind == "cold" else timings[name].warm_s).append(elapsed)
                if error is not None:
                    timings[name].errors.append(f"{kind}: {error}")
    return list(timings.values())


def report(timings: Sequence[Timing], model: str, rounds: int) -> str:
    """The Markdown table and the thresholds' verdicts."""
    lines = [
        f"Model `{model}`, {rounds} rounds, cold then warm per schema, sequential "
        f"(seconds; cold = a schema new to the endpoint, by a per-round nonce property).",
        "",
        "| Schema | Cold (each round) | Warm (each round) | Max cold |",
        "|---|---|---|---|",
    ]
    for t in timings:
        cold = ", ".join(f"{s:.1f}" for s in t.cold_s)
        warm = ", ".join(f"{s:.1f}" for s in t.warm_s)
        note = f" ({'; '.join(t.errors)})" if t.errors else ""
        lines.append(f"| `{t.schema}` | {cold} | {warm} | {max(t.cold_s, default=0):.1f}{note} |")
    worst = max((s for t in timings for s in t.cold_s), default=0.0)
    lines += ["", f"Largest cold call: {worst:.1f} s."]
    for limit, what in (
        (TURN_DEADLINE_S, "Standard's and Full's per-call deadline"),
        (DEFAULT_TIMEOUT_S, "the default per-call timeout (`DEFAULT_TIMEOUT_S`)"),
        (HARNESS_TIMEOUT_S, "the eval harness's per-call timeout (`CALL_TIMEOUT_S`)"),
    ):
        verdict = "exceeds" if worst > limit else "is within"
        lines.append(f"- It {verdict} {limit:.0f} s, {what}.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m epistrel.tools.schema_compile_probe")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--schema", action="append", default=[], help="only these (repeatable)")
    args = parser.parse_args(argv)
    key = os.environ.get(API_KEY_ENV)
    if not key:
        print(f"error: set {API_KEY_ENV}", file=sys.stderr)
        return 2

    async def run() -> list[Timing]:
        client = openai.AsyncOpenAI(base_url=args.base_url, api_key=key, max_retries=0)
        try:
            return await measure(client, args.model, args.rounds, args.schema)
        finally:
            await client.close()

    print(report(asyncio.run(run()), args.model, args.rounds), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
