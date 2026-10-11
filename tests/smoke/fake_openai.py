"""A tiny OpenAI-compatible endpoint for the deployment smoke (C-10, #77).

NFR-OPS-1 says a compose stack plus *one model endpoint* is a working deployment. CI has no real
model, so this fake is that endpoint: it speaks the two calls the ``openai`` adapter makes
(``GET /v1/models`` and non-streaming ``POST /v1/chat/completions``) and answers every chat
with a fixed, deterministic reply. A request carrying ``response_format`` (a structured
purpose such as ``guardrail_check``) gets ``{"violation": false, "confidence": 0.99}``.
``GET /_calls`` reports what was asked — model, message count and whether it was structured,
never the text — so the smoke can prove the engine really called it; ``DELETE /_calls``
clears the log.

Run it on the host (the engine container reaches it as ``host.docker.internal``)::

    uv run python tests/smoke/fake_openai.py --host 0.0.0.0 --port 1234
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL_ID = "fake-1"
REPLY = "The fake model says hello."
VERDICT = {"violation": False, "confidence": 0.99}

app = FastAPI(title="fake-openai", docs_url=None, redoc_url=None, openapi_url=None)
_calls: list[dict[str, Any]] = []
_ids = itertools.count(1)


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "fake"}]}


@app.post("/v1/chat/completions", response_model=None)
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    if body.get("stream"):
        return JSONResponse(
            {"error": {"message": "streaming is not supported", "type": "invalid_request"}},
            status_code=400,
        )
    structured = "response_format" in body
    _calls.append(
        {
            "model": body.get("model"),
            "messages": len(body.get("messages", [])),
            "structured": structured,
            "authorized": request.headers.get("authorization", "").startswith("Bearer "),
        }
    )
    content = json.dumps(VERDICT) if structured else REPLY
    return JSONResponse(
        {
            "id": f"chatcmpl-fake-{next(_ids)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
        }
    )


@app.get("/_calls")
async def calls() -> list[dict[str, Any]]:
    return _calls


@app.delete("/_calls", status_code=204)
async def clear() -> None:
    _calls.clear()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - the engine container calls in
    parser.add_argument("--port", type=int, default=1234)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
