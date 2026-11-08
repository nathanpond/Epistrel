"""A localhost bridge for E-11's set generator (#271; ``docs/evals/E11-RUNBOOK.md``).

``python -m epistrel.evals author e11`` speaks two image APIs: OpenAI's ``POST
/v1/images/generations`` (``--backend openai``) and A1111's ``POST /sdapi/v1/txt2img``
(``--backend a1111``, which Draw Things serves). This shim serves the first on 127.0.0.1 and
answers it from a server or tool the generator cannot reach directly:

    # an A1111-compatible server (Draw Things' API server, or ComfyUI behind an A1111 bridge)
    uv run python -m tools.e11_image_shim --a1111 http://127.0.0.1:7860
    # a command-line generator (mflux; a ComfyUI CLI workflow): one image a request
    uv run python -m tools.e11_image_shim --command \\
        'mflux-generate --model schnell --steps 4 --prompt {prompt} --seed {seed} \\
         --width {width} --height {height} --output {output}'

then ``author e11 --base-url http://127.0.0.1:7861/v1 --model <the image model> --backend
openai``. A ``--command`` template is split like a shell line (``shlex``) and each word is
formatted with ``{prompt}``, ``{seed}``, ``{width}``, ``{height}`` and ``{output}`` (a temporary
PNG path the command must write). It runs without a shell, so a prompt is one argument whatever
it holds. ``--extra '{"steps": 4}'`` adds fields to every A1111 request.

Only the standard library; it binds the loopback interface only, and forwards only to a loopback
A1111 server. It logs the method, path and status of each request, never a prompt.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PORT = 7861
ROUTE = "/v1/images/generations"
TIMEOUT_S = 600.0
"""One image's limit (a local diffusion model can take minutes)."""
LOOPBACK_NAMES = frozenset({"localhost"})


class ShimError(Exception):
    """A request the shim refuses (400) or an image it could not get (502)."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_GATEWAY) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class ImageRequest:
    """The fields of an OpenAI-style generation request the shim forwards."""

    prompt: str
    seed: int
    width: int
    height: int


type Backend = Callable[[ImageRequest], bytes]


def is_loopback(url_or_host: str) -> bool:
    """Whether ``url_or_host`` names this machine's loopback interface."""
    host = (urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host) or ""
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_request(body: bytes) -> ImageRequest:
    """``body`` as an ``ImageRequest`` (``ShimError`` 400 when it is not one image request)."""
    try:
        document = json.loads(body)
    except ValueError:
        raise ShimError("the body is not JSON", HTTPStatus.BAD_REQUEST) from None
    if not isinstance(document, dict):
        raise ShimError("the body is not a JSON object", HTTPStatus.BAD_REQUEST)
    prompt = document.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ShimError("prompt is required", HTTPStatus.BAD_REQUEST)
    if document.get("n", 1) != 1:
        raise ShimError("n must be 1", HTTPStatus.BAD_REQUEST)
    size = str(document.get("size") or "1024x1024")
    try:
        width, height = (int(v) for v in size.lower().split("x", 1))
        seed = int(document.get("seed", 0))
    except ValueError:
        raise ShimError(
            "size is WIDTHxHEIGHT and seed an integer", HTTPStatus.BAD_REQUEST
        ) from None
    if not (64 <= width <= 4096 and 64 <= height <= 4096):
        raise ShimError("size is out of range", HTTPStatus.BAD_REQUEST)
    return ImageRequest(prompt=prompt, seed=seed, width=width, height=height)


def a1111_backend(
    base_url: str,
    extra: Mapping[str, Any] | None = None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Backend:
    """Forward each request to the A1111-compatible ``POST /sdapi/v1/txt2img`` at
    ``base_url`` (a loopback address only)."""
    if not is_loopback(base_url):
        raise ShimError(f"{base_url} is not a loopback address", HTTPStatus.BAD_REQUEST)
    url = f"{base_url.rstrip('/').removesuffix('/v1')}/sdapi/v1/txt2img"

    def generate(request: ImageRequest) -> bytes:
        body = {
            **(extra or {}),
            "prompt": request.prompt,
            "seed": request.seed,
            "width": request.width,
            "height": request.height,
        }
        call = urllib.request.Request(  # noqa: S310 - a loopback http URL, checked above
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener(call, timeout=TIMEOUT_S) as answer:
                document = json.loads(answer.read())
            encoded = document["images"][0]
            return base64.b64decode(str(encoded).split(",", 1)[-1], validate=False)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ShimError(f"the A1111 server failed ({type(exc).__name__})") from None

    return generate


def command_backend(
    template: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> Backend:
    """Run ``template`` (module docstring) once a request and read the PNG it wrote."""
    words = shlex.split(template)
    if not words:
        raise ShimError("--command is empty", HTTPStatus.BAD_REQUEST)
    if not any("{output}" in word for word in words):
        raise ShimError("--command must name {output}", HTTPStatus.BAD_REQUEST)

    def generate(request: ImageRequest) -> bytes:
        with tempfile.TemporaryDirectory(prefix="e11-shim-") as work:
            output = Path(work) / "image.png"
            fields = {
                "prompt": request.prompt,
                "seed": request.seed,
                "width": request.width,
                "height": request.height,
                "output": str(output),
            }
            argv = [word.format_map(fields) for word in words]
            try:
                done = runner(argv, capture_output=True, timeout=TIMEOUT_S, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ShimError(f"the command failed ({type(exc).__name__})") from None
            if done.returncode != 0:
                raise ShimError(f"the command exited {done.returncode}")
            if not output.is_file() or output.stat().st_size == 0:
                raise ShimError("the command wrote no image at {output}")
            return output.read_bytes()

    return generate


def answer_of(image: bytes) -> dict[str, Any]:
    """OpenAI's image-generation answer for one image."""
    return {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(image).decode()}]}


def make_server(
    backend: Backend, port: int = DEFAULT_PORT, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    """The shim's server on ``host:port`` (a loopback address only; port 0 picks one)."""
    if not is_loopback(host):
        raise ShimError(f"{host} is not a loopback address", HTTPStatus.BAD_REQUEST)

    class Handler(BaseHTTPRequestHandler):
        server_version = "e11-image-shim"

        def _send(self, status: HTTPStatus, document: Mapping[str, Any]) -> None:
            data = json.dumps(document).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if self.path.rstrip("/") != ROUTE:
                self._send(HTTPStatus.NOT_FOUND, {"error": {"message": f"only POST {ROUTE}"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = parse_request(self.rfile.read(length))
                image = backend(request)
            except ShimError as exc:
                self._send(exc.status, {"error": {"message": str(exc)}})
                return
            self._send(HTTPStatus.OK, answer_of(image))

        def log_message(self, format: str, *args: Any) -> None:
            # The request line and status only: never the body, so never a prompt.
            sys.stderr.write(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    return ThreadingHTTPServer((host, port), Handler)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.e11_image_shim", description=__doc__.split("\n\n")[0]
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--a1111", metavar="URL", help="an A1111-compatible server, e.g. Draw Things"
    )
    target.add_argument("--command", help="a generator command template (module docstring)")
    p.add_argument("--extra", default="{}", help="JSON fields added to every A1111 request")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        extra = json.loads(args.extra)
        if not isinstance(extra, dict):
            raise ValueError("--extra is a JSON object")
        backend = a1111_backend(args.a1111, extra) if args.a1111 else command_backend(args.command)
        server = make_server(backend, args.port)
    except (ShimError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    host, port = "127.0.0.1", server.server_address[1]
    print(f"serving POST http://{host}:{port}{ROUTE}; use --base-url http://{host}:{port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
