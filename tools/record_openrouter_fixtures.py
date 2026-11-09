"""Record the OpenRouter image adapter's fixtures, once, with the owner's key (#255).

    uv run python -m tools.record_openrouter_fixtures \\
        --api-key-env OPENROUTER_API_KEY --model <id> --i-accept-cost

This spends real money, about $1 or less: up to ``CALL_BUDGET`` (6) small calls at the lowest
resolution, with ``n = 1``. CI never runs it. The calls, in order:

1. the image models list (filtered to image-output models and printed);
2. ``--model``'s endpoints;
3. a plain generation, sent with an ``Idempotency-Key``;
4. a seeded generation;
5. a generation with one reference image (``tests/fixtures/openrouter/ref.png`` as a data URL);
6. call 3 again, with the same ``Idempotency-Key``. If OpenRouter answers it with the same
   generation id, it deduplicates (``dedupes: true``). Otherwise, or if the repeat fails, the
   verdict is ``dedupes: false, inconclusive: true``. OpenRouter's image answers carry no
   ``id`` (the 2026-11-08 recording), so against OpenRouter this check can never answer
   ``true`` (#801): it is kept for an API that returns one, and the adapter's constant stays
   ``False`` until other evidence shows deduplication.

Generations go through the real adapter (``OpenRouterImageGenerator``), with a transport that
records each exchange. The fixtures are sanitized before they are written:

- the key, ``Authorization``, and generation, user and account ids are replaced
  (``REDACTED-<n>``, the same value always getting the same token);
- only ``content-type``-like headers are kept;
- each image is downscaled to at most 64 px, with its true returned size kept in ``meta``
  (``true_width``, ``true_height``).

They go to ``tests/fixtures/openrouter/recorded/<nn>_<name>.json``, plus ``verdict.json``
(``{dedupes, inconclusive, model, recorded_at, total_cost, unknown_cost_calls}``). The write is
atomic: everything is written to a temporary directory and renamed into place, so a run that
fails midway writes nothing (exit 1, no retries). The run refuses to overwrite earlier
recordings without ``--force``. Without ``--i-accept-cost`` it refuses to run (exit 2);
``--dry-run`` prints the planned calls and spends nothing.

Afterwards, copy the verdict into ``OPENROUTER_DEDUPES_IDEMPOTENCY_KEY``
(``src/epistrel/orchestrator/images/adapters/openrouter.py``) in the PR that commits the fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2 as httpx
from pydantic import SecretStr

from epistrel.core.ids import new_ulid
from epistrel.orchestrator.images.adapters.openrouter import (
    DEFAULT_BASE_URL,
    OpenRouterImageGenerator,
    cost_of,
)
from epistrel.orchestrator.images.errors import ImageAdapterError
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.images.port import Cost, ImageRequest
from epistrel.orchestrator.images.registry import ImageAdapterConfig

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "openrouter"
REFERENCE_PNG = FIXTURES / "ref.png"
CALL_BUDGET = 6
DECODED_HEADERS = frozenset({"content-encoding", "content-length"})
"""Response headers that no longer describe the body once ``aread`` has decoded it."""
THUMBNAIL_PX = 64
PROMPT = "A small red paper boat on calm blue water, simple flat illustration"
SEED = 42
REFERENCE_ID = "ref"
EXIT_REFUSED = 2
EXIT_FAILED = 1

CALLS = (
    "01_models",
    "02_endpoints",
    "03_generation_plain",
    "04_generation_seeded",
    "05_generation_reference",
    "06_generation_repeat",
)
KEPT_HEADERS = frozenset({"content-type", "x-title", "idempotency-key"})
REDACTED_KEYS = frozenset(
    {"id", "user_id", "account_id", "generation_id", "organization_id", "request_id", "user"}
)
"""Keys whose values are replaced. A bare ``id`` is replaced only in a generation's answer,
where it is the generation id; elsewhere it names a model."""


class RecordingFailed(Exception):
    """A call failed or the budget ran out: nothing is written."""


@dataclass
class Exchange:
    method: str
    url: str
    request_headers: dict[str, str]
    request_body: Any
    status: int
    response_headers: dict[str, str]
    response_body: Any


@dataclass
class RecordingTransport(httpx.AsyncBaseTransport):
    """Sends through ``inner``, keeps every exchange, and refuses a call past the budget."""

    inner: httpx.AsyncBaseTransport
    budget: int = CALL_BUDGET
    exchanges: list[Exchange] = field(default_factory=list)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if len(self.exchanges) >= self.budget:
            raise RecordingFailed(f"call budget of {self.budget} exhausted")
        content = request.content
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        await response.aclose()
        # ``aread`` has already undone any Content-Encoding, so neither the recording nor the
        # response handed back may still claim one (the client would decode the body twice).
        headers = [
            (name, value)
            for name, value in response.headers.items()
            if name.lower() not in DECODED_HEADERS
        ]
        self.exchanges.append(
            Exchange(
                method=request.method,
                url=str(request.url),
                request_headers=dict(request.headers.items()),
                request_body=_json_or_text(content),
                status=response.status_code,
                response_headers=dict(headers),
                response_body=_json_or_text(body),
            )
        )
        return httpx.Response(response.status_code, headers=headers, content=body, request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def _json_or_text(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


@dataclass
class ReferenceFile:
    """Resolves the one reference image to ``ref.png`` as a data URL."""

    path: Path = REFERENCE_PNG

    async def resolve(self, media_id: str) -> str:
        encoded = base64.b64encode(self.path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"


# -- planning ----------------------------------------------------------------------------------


def planned_calls(model: str) -> list[str]:
    return [
        "GET  /api/v1/images/models (image models list)",
        f"GET  /api/v1/images/models/{model}/endpoints",
        "POST /api/v1/images plain generation, n=1, with Idempotency-Key K",
        f"POST /api/v1/images seeded generation (seed {SEED}), n=1",
        "POST /api/v1/images generation with one reference image (ref.png), n=1",
        "POST /api/v1/images plain generation again, same Idempotency-Key K (dedupe check)",
    ]


def _size_key(size: str) -> int:
    width, _, height = size.partition("x")
    return int(width) * int(height)


def smallest_size(endpoints_body: Any) -> str | None:
    """The smallest ``WxH`` size any endpoint lists among its ``size`` values, if it lists
    any."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "size" and isinstance(value, dict):
                    for listed in value.values():
                        if isinstance(listed, list):
                            found.update(v for v in listed if isinstance(v, str) and _is_size(v))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(endpoints_body)
    return min(found, key=_size_key) if found else None


def _is_size(value: str) -> bool:
    width, sep, height = value.partition("x")
    return bool(sep) and width.isdigit() and height.isdigit()


def image_models(listing: Any) -> list[str]:
    """The ids in an image models list (``{"data": [{"id": ...}, ...]}``)."""
    rows = listing.get("data") if isinstance(listing, dict) else listing
    if not isinstance(rows, list):
        return []
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        modalities = (row.get("architecture") or {}).get("output_modalities")
        if isinstance(modalities, list) and "image" not in modalities:
            continue
        ids.append(row["id"])
    return ids


# -- sanitizing --------------------------------------------------------------------------------


@dataclass
class Redactor:
    secret: str
    tokens: dict[str, str] = field(default_factory=dict)

    def token(self, value: Any) -> str:
        key = json.dumps(value, sort_keys=True)
        if key not in self.tokens:
            self.tokens[key] = f"REDACTED-{len(self.tokens) + 1}"
        return self.tokens[key]

    def body(self, node: Any, *, generation: bool, top: bool = True) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                bare_id = key == "id" and not (generation and top)
                if key in REDACTED_KEYS and not bare_id and value is not None:
                    out[key] = self.token(value)
                else:
                    out[key] = self.body(value, generation=generation, top=False)
            return out
        if isinstance(node, list):
            return [self.body(item, generation=generation, top=False) for item in node]
        return node

    def text(self, encoded: str) -> str:
        return encoded.replace(self.secret, "REDACTED") if self.secret else encoded


def thumbnail(encoded: str) -> tuple[str, int, int]:
    """A ``b64_json`` image downscaled to ``THUMBNAIL_PX``, in its own format, and the true
    width and height it was returned at."""
    from PIL import Image

    text = encoded.partition(",")[2] if encoded.startswith("data:") else encoded
    raw = base64.b64decode(text, validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        kind = image.format or "PNG"
        width, height = image.size
        image.load()
        small = image.copy()
    small.thumbnail((THUMBNAIL_PX, THUMBNAIL_PX))
    buffer = io.BytesIO()
    small.save(buffer, format=kind)
    return base64.b64encode(buffer.getvalue()).decode("ascii"), width, height


def sanitize(exchange: Exchange, redactor: Redactor) -> dict[str, Any]:
    """One exchange as a fixture (module docs)."""
    generation = exchange.method == "POST"
    headers = {
        k.lower(): v for k, v in exchange.request_headers.items() if k.lower() in KEPT_HEADERS
    }
    headers["authorization"] = "REDACTED"
    body = redactor.body(exchange.response_body, generation=generation)
    meta: dict[str, Any] = {}
    if generation and isinstance(body, dict) and isinstance(body.get("data"), list):
        widths: list[int] = []
        heights: list[int] = []
        for item in body["data"]:
            if isinstance(item, dict) and isinstance(item.get("b64_json"), str):
                item["b64_json"], width, height = thumbnail(item["b64_json"])
                widths.append(width)
                heights.append(height)
        meta = {"true_width": widths, "true_height": heights}
    path = httpx.URL(exchange.url)
    fixture = {
        "request": {
            "method": exchange.method,
            "url": path.raw_path.decode("ascii"),
            "headers": headers,
            "body": exchange.request_body,
        },
        "response": {
            "status": exchange.status,
            "headers": {
                k.lower(): v
                for k, v in exchange.response_headers.items()
                if k.lower() == "content-type"
            },
            "body": body,
        },
        "meta": meta,
    }
    text = redactor.text(json.dumps(fixture, indent=2, sort_keys=True))
    loaded: dict[str, Any] = json.loads(text)
    return loaded


# -- the run -----------------------------------------------------------------------------------


@dataclass
class Recording:
    exchanges: list[Exchange]
    dedupes: bool
    inconclusive: bool
    total_cost: Decimal
    unknown_cost_calls: int


async def record(model: str, api_key: str, base_url: str) -> Recording:
    """Make the calls (module docs) and judge the dedupe check; nothing is written here."""
    transport = RecordingTransport(httpx.AsyncHTTPTransport())
    adapter = OpenRouterImageGenerator(
        ImageAdapterConfig(
            adapter="openrouter",
            model=model,
            base_url=base_url,
            api_key=SecretStr(api_key),
            params=ImageGenerationParams(adapter="openrouter"),
            revision=0,
        ),
        references=ReferenceFile(),
        transport=transport,
        send_idempotency_key=True,
    )
    try:
        listing = await adapter.models()
        listed = image_models(listing)
        print(f"[1] models list: {len(listed)} image models")
        for listed_id in listed:
            print(f"      {listed_id}")
        endpoints = await adapter.endpoints()
        print(f"[2] endpoints of {model}: {', '.join(e.slug for e in endpoints)}")
        size = smallest_size(transport.exchanges[-1].response_body)
        dims: dict[str, Any] = (
            {"size": size} if size is not None else {"aspect_ratio": "1:1", "quality": "low"}
        )
        key = f"epistrel-record-{new_ulid()}"
        plain = ImageRequest(prompt=PROMPT, n=1, **dims)
        first = await adapter.submit(plain, key)
        print(f"[3] plain generation: {_described(first.cost)}")
        seeded = await adapter.submit(plain.model_copy(update={"seed": SEED}), None)
        print(f"[4] seeded generation: {_described(seeded.cost)}")
        referenced = plain.model_copy(update={"reference_images": (REFERENCE_ID,)})
        with_ref = await adapter.submit(referenced, None)
        print(f"[5] reference generation: {_described(with_ref.cost)}")
        try:
            repeat = await adapter.submit(plain, key)
        except ImageAdapterError as failure:
            print(f"[6] repeat failed: {failure}")
            repeat_id = None
        else:
            print(f"[6] repeat: {_described(repeat.cost)}")
            repeat_id = repeat.attempt_ref
    except (ImageAdapterError, httpx.HTTPError) as failure:
        raise RecordingFailed(f"call {len(transport.exchanges)} failed: {failure}") from None
    finally:
        await adapter.aclose()
    generations = [e for e in transport.exchanges if e.method == "POST"]
    first_id = _generation_id(generations[0])
    dedupes = first_id is not None and repeat_id == first_id
    total = Decimal(0)
    unknown = 0
    for exchange in generations:
        if exchange.status != 200:
            continue
        cost = cost_of(
            exchange.response_body.get("usage")
            if isinstance(exchange.response_body, dict)
            else None
        )
        if isinstance(cost, Cost):
            total += cost.amount
        else:
            unknown += 1
    return Recording(transport.exchanges, dedupes, not dedupes, total, unknown)


def _generation_id(exchange: Exchange) -> str | None:
    body = exchange.response_body
    value = body.get("id") if isinstance(body, dict) else None
    return value if isinstance(value, str) else None


def _described(cost: object) -> str:
    return f"${cost.amount}" if isinstance(cost, Cost) else "cost unknown"


def spent(recording: Recording) -> str:
    line = f"${recording.total_cost}"
    if recording.unknown_cost_calls:
        line += f" + unknown ({recording.unknown_cost_calls} calls)"
    return line


def write(recording: Recording, out: Path, *, model: str, api_key: str, force: bool) -> None:
    """Write the fixtures and the verdict atomically (module docs)."""
    redactor = Redactor(api_key)
    out.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".recording-", dir=out))
    try:
        recorded = staging / "recorded"
        recorded.mkdir()
        for name, exchange in zip(CALLS, recording.exchanges, strict=False):
            fixture = sanitize(exchange, redactor)
            (recorded / f"{name}.json").write_text(json.dumps(fixture, indent=2) + "\n")
        verdict = {
            "dedupes": recording.dedupes,
            "inconclusive": recording.inconclusive,
            "model": model,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "total_cost": str(recording.total_cost),
            "unknown_cost_calls": recording.unknown_cost_calls,
        }
        (staging / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
        for path in staging.rglob("*.json"):
            if api_key and api_key in path.read_text():
                raise RecordingFailed("the key survived sanitizing; nothing written")
        if force:
            shutil.rmtree(out / "recorded", ignore_errors=True)
            (out / "verdict.json").unlink(missing_ok=True)
        os.replace(recorded, out / "recorded")
        os.replace(staging / "verdict.json", out / "verdict.json")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.record_openrouter_fixtures",
        description="Record the OpenRouter image adapter's fixtures (real calls, real cost).",
    )
    parser.add_argument("--api-key-env", required=True, help="the variable holding the key")
    parser.add_argument("--model", required=True, help="the image model id (author/slug)")
    parser.add_argument(
        "--i-accept-cost", action="store_true", help="confirm the run may spend (about $1)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the calls; spend nothing")
    parser.add_argument("--force", action="store_true", help="replace earlier recordings")
    parser.add_argument("--out", type=Path, default=FIXTURES, help=argparse.SUPPRESS)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    if args.dry_run:
        print(f"dry run: {CALL_BUDGET} calls planned against {args.base_url}")
        for number, line in enumerate(planned_calls(args.model), start=1):
            print(f"[{number}] {line}")
        return 0
    if not args.i_accept_cost:
        print(
            "refused: this run makes real, billed calls to OpenRouter (about $1 or less); "
            "pass --i-accept-cost to run it, or --dry-run to see the calls",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        print(f"refused: ${args.api_key_env} is not set", file=sys.stderr)
        return EXIT_REFUSED
    out: Path = args.out
    if not args.force and ((out / "verdict.json").exists() or (out / "recorded").exists()):
        print(
            f"refused: {out} already holds recordings; pass --force to replace them",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        recording = asyncio.run(record(args.model, api_key, args.base_url))
        write(recording, out, model=args.model, api_key=api_key, force=args.force)
    except RecordingFailed as failure:
        print(f"failed: {failure}; nothing written", file=sys.stderr)
        return EXIT_FAILED
    verdict = "true" if recording.dedupes else "false"
    suffix = "" if recording.dedupes else ", inconclusive: true"
    print(f"dedupes: {verdict}{suffix}")
    print(f"total spent: {spent(recording)}")
    print(f"wrote {out / 'recorded'} and {out / 'verdict.json'}")
    print(f"next: set OPENROUTER_DEDUPES_IDEMPOTENCY_KEY = {recording.dedupes} and commit both")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
