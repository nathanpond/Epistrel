"""#271's E-11 generator bridge (``tools/e11_image_shim.py``), run against a stdlib fake A1111
server and a Python one-liner standing in for a CLI generator. No image model is involved."""

from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from tools import e11_image_shim as shim

from epistrel.evals.suites.e11_image_policies import solid_png

PNG = solid_png(8, 8)


@contextmanager
def serving(server: ThreadingHTTPServer) -> Iterator[str]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def post(url: str, body: Any) -> tuple[int, dict[str, Any]]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST")  # noqa: S310 - loopback
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310 - loopback
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def fake_a1111() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, **body})
            data = json.dumps({"images": [base64.b64encode(PNG).decode()]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with serving(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as url:
        yield url, seen


def test_shim_forwards_to_a1111(fake_a1111: tuple[str, list[dict[str, Any]]]) -> None:
    """The shim answers ``POST /v1/images/generations`` with the A1111 server's image as
    ``b64_json``, sending the prompt, seed, size and ``--extra`` fields to
    ``/sdapi/v1/txt2img``."""
    upstream, seen = fake_a1111
    backend = shim.a1111_backend(f"{upstream}/v1", {"steps": 4})
    with serving(shim.make_server(backend, port=0)) as url:
        status, answer = post(
            f"{url}{shim.ROUTE}",
            {"model": "sdxl", "prompt": "a lighthouse", "n": 1, "size": "512x768", "seed": 7},
        )
    assert status == 200
    assert base64.b64decode(answer["data"][0]["b64_json"]) == PNG
    assert seen == [
        {
            "path": "/sdapi/v1/txt2img",
            "steps": 4,
            "prompt": "a lighthouse",
            "seed": 7,
            "width": 512,
            "height": 768,
        }
    ]


def test_shim_runs_a_command(tmp_path: Any) -> None:
    """A ``--command`` template runs without a shell, one word a field, and the PNG it wrote at
    ``{output}`` is the answer; a prompt full of shell syntax stays one argument."""
    script = tmp_path / "gen.py"
    script.write_text(
        "import sys, base64, json\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        f"open(out, 'wb').write(base64.b64decode({base64.b64encode(PNG).decode()!r}))\n"
        "open(out + '.args.json', 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    argv_seen: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        import subprocess

        done = subprocess.run(argv, **kwargs)
        argv_seen.append(argv)
        return done

    template = (
        f"{sys.executable} {script} --prompt {{prompt}} --seed {{seed}} --width {{width}} "
        "--height {height} --output {output}"
    )
    backend = shim.command_backend(template, runner=runner)
    image = backend(shim.ImageRequest(prompt="a cat; rm -rf / $(x)", seed=3, width=64, height=96))
    assert image == PNG
    (argv,) = argv_seen
    assert argv[argv.index("--prompt") + 1] == "a cat; rm -rf / $(x)"
    assert argv[argv.index("--seed") + 1] == "3"
    assert (argv[argv.index("--width") + 1], argv[argv.index("--height") + 1]) == ("64", "96")


def test_shim_refusals(tmp_path: Any) -> None:
    """The shim binds and forwards to loopback addresses only, refuses a malformed request
    (400) and a failed generator (502), and serves only its one route (404)."""
    with pytest.raises(shim.ShimError):
        shim.a1111_backend("http://192.0.2.10:7860")
    with pytest.raises(shim.ShimError):
        shim.make_server(lambda _: PNG, port=0, host="0.0.0.0")  # noqa: S104 - refused
    with pytest.raises(shim.ShimError):
        shim.command_backend("mflux-generate --prompt {prompt}")  # no {output}
    failing = shim.command_backend(f"{sys.executable} -c 'raise SystemExit(3)' {{output}}")
    with serving(shim.make_server(failing, port=0)) as url:
        assert post(f"{url}{shim.ROUTE}", {"prompt": "x"})[0] == 502
        assert post(f"{url}{shim.ROUTE}", {"prompt": ""})[0] == 400
        assert post(f"{url}{shim.ROUTE}", {"prompt": "x", "n": 2})[0] == 400
        assert post(f"{url}{shim.ROUTE}", b"not json")[0] == 400
        assert post(f"{url}{shim.ROUTE}", {"prompt": "x", "size": "big"})[0] == 400
        assert post(f"{url}/other", {"prompt": "x"})[0] == 404
    assert shim.is_loopback("http://localhost:7860")
    assert shim.is_loopback("::1")
    assert not shim.is_loopback("http://example.com")
