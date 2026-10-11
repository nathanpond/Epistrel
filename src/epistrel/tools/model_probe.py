"""Send one probe through the Model Access Layer and print what happened, as JSON.

    uv run python -m epistrel.tools.model_probe generation "Say hello"
    uv run python -m epistrel.tools.model_probe generation "Say hello" \\
        --base-url http://localhost:1234/v1 --model <loaded model> --api-key lm-studio

The adapter is ``EPISTREL_MODEL_ADAPTER`` (``openai`` by default). With ``--base-url``,
``--model`` and ``--api-key`` all given, or under the stub, no database or other setting is
needed; otherwise the full settings are loaded and the purpose's stored configuration is used,
with any given override applied on top. The output holds the reply, the result's metadata, and
the declared input fields the request carried (never the API key).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import ConfigError, Settings, load_settings
from epistrel.core.db import make_engine
from epistrel.orchestrator.models.access import build_inputs, get_delivered, payload_fields
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox, effective_config
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import render
from epistrel.orchestrator.models.registry import (
    GenerationInputs,
    GuardrailCheckInputs,
    InputSchema,
    LiteSummaryInputs,
    PlayerInput,
    TranscriptLine,
)
from epistrel.orchestrator.models.stub import StubAdapter

EX_USAGE = 64
EX_CONFIG = 78
EX_UNAVAILABLE = 69
PROBE_KEY = SecretStr("ZXBpc3RyZWwtbW9kZWwtcHJvYmUtb25seS1rZXktMDA=")
"""A throwaway Fernet key: the probe encrypts an override key in memory only."""


def probe_inputs(purpose: str, text: str) -> InputSchema:
    """Minimal declared inputs carrying ``text`` for each delivered purpose."""
    if purpose == "generation":
        return GenerationInputs(player_input=PlayerInput(name="Player", say=text))
    if purpose == "guardrail_check":
        return GuardrailCheckInputs(text=text, policy_prompt="Probe policy: flag nothing.")
    return LiteSummaryInputs(messages=[TranscriptLine(name="Player", say=text, role="user")])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m epistrel.tools.model_probe")
    parser.add_argument("purpose", help="a delivered purpose, e.g. generation")
    parser.add_argument("text", help="the probe text")
    parser.add_argument("--base-url", help="override the purpose's base_url")
    parser.add_argument("--model", help="override the purpose's model")
    parser.add_argument("--api-key", help="override the purpose's API key")
    return parser


async def _probe(args: argparse.Namespace) -> dict[str, Any]:
    purpose = get_delivered(args.purpose)
    inputs = build_inputs(purpose, probe_inputs(purpose.name, args.text))
    messages = render(purpose, inputs)
    adapter_name = os.environ.get("EPISTREL_MODEL_ADAPTER", "openai")
    overrides_complete = bool(args.base_url and args.model and args.api_key)
    db: AsyncEngine | None = None
    if adapter_name == "stub":
        adapter: StubAdapter | OpenAICompatibleAdapter = StubAdapter()
    elif overrides_complete:
        keybox = KeyBox(PROBE_KEY)
        config = EffectiveConfig(
            purpose=purpose.name,
            base_url=args.base_url.rstrip("/"),
            model=args.model,
            api_key_ciphertext=keybox.encrypt(args.api_key),
            params=ModelParams(),
            revision="probe",
        )

        async def fixed(_: str) -> EffectiveConfig:
            return config

        adapter = OpenAICompatibleAdapter(fixed, keybox)
    else:
        settings = load_settings()
        db = make_engine(settings)
        adapter = _stored_adapter(db, settings, args)
    try:
        result = await adapter.complete(
            purpose.name, messages, ModelParams(), purpose.response_schema
        )
    finally:
        await adapter.aclose()
        if db is not None:
            await db.dispose()
    return {
        "purpose": purpose.name,
        "adapter": adapter.name,
        "text": result.text,
        "result": result.model_dump(exclude={"text"}),
        "request_fields": list(payload_fields(inputs)),
        "messages": [m.model_dump() for m in messages],
    }


def _stored_adapter(
    db: AsyncEngine, settings: Settings, args: argparse.Namespace
) -> OpenAICompatibleAdapter:
    """The purpose's stored configuration with the given overrides applied on top."""
    override_box = KeyBox(PROBE_KEY)
    keybox = override_box if args.api_key else KeyBox(settings.config_key)

    async def resolve(purpose: str) -> EffectiveConfig:
        stored = await effective_config(db, purpose)
        config = replace(
            stored,
            base_url=args.base_url.rstrip("/") if args.base_url else stored.base_url,
            model=args.model or stored.model,
        )
        if args.api_key:
            config = replace(config, api_key_ciphertext=override_box.encrypt(args.api_key))
        return config

    return OpenAICompatibleAdapter(resolve, keybox)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = asyncio.run(_probe(args))
    except ConfigError as exc:
        print("\n".join(exc.lines), file=sys.stderr)
        return EX_CONFIG
    except Exception as exc:  # report, never a traceback: this is an operator tool
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EX_UNAVAILABLE
    print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
