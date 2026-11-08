"""The owner-run OpenRouter fixture recorder (#255), run here against the stdlib replay server
only: it refuses without ``--i-accept-cost``, plans without spending, records at most six calls,
sanitizes what it writes, judges the dedupe check, and writes nothing when a call fails."""

from __future__ import annotations

import base64
import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from tools import record_openrouter_fixtures as recorder

from tests.image_support import png
from tests.openrouter_support import REFERENCE_PNG, Replay, replay

MODEL = "synthetic-author/synthetic-image"
ENDPOINTS = f"/api/v1/images/models/{MODEL}/endpoints"
SECRET = "sk-or-v1-recorder-secret-0042"
ENV = "EPISTREL_TEST_OPENROUTER_KEY"


def _image(width: int, height: int) -> dict[str, str]:
    return {"b64_json": base64.b64encode(png(width, height)).decode(), "media_type": "image/png"}


def _generation(gen_id: str, cost: float | None = 0.01) -> dict[str, Any]:
    body: dict[str, Any] = {"id": gen_id, "user_id": "user-77", "data": [_image(128, 96)]}
    if cost is not None:
        body["usage"] = {"cost": cost}
    return body


def _serve_listing(server: Replay) -> None:
    server.serve(
        "GET",
        "/api/v1/images/models",
        200,
        {
            "data": [
                {"id": MODEL, "architecture": {"output_modalities": ["image"]}},
                {"id": "text-author/text-model", "architecture": {"output_modalities": ["text"]}},
            ]
        },
    )
    server.serve(
        "GET",
        ENDPOINTS,
        200,
        {
            "data": {
                "id": MODEL,
                "endpoints": [
                    {
                        "tag": "alpha",
                        "supported_parameters": {
                            "seed": {"type": "range"},
                            "size": {"type": "enum", "values": ["1024x1024", "512x512"]},
                        },
                    },
                    {"tag": "gamma", "supported_parameters": ["input_references"]},
                ],
            }
        },
    )


def _args(server: Replay, out: Path, *extra: str) -> list[str]:
    return [
        "--api-key-env",
        ENV,
        "--model",
        MODEL,
        "--out",
        str(out),
        "--base-url",
        server.root,
        *extra,
    ]


def test_recorder_refuses_without_accepting_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--i-accept-cost`` it exits 2 and sends and writes nothing; so it does without
    the key in the environment."""
    monkeypatch.setenv(ENV, SECRET)
    with replay() as server:
        assert recorder.main(_args(server, tmp_path)) == recorder.EXIT_REFUSED
        assert "--i-accept-cost" in capsys.readouterr().err
        monkeypatch.delenv(ENV)
        assert recorder.main(_args(server, tmp_path, "--i-accept-cost")) == 2
        assert server.seen == []
    assert list(tmp_path.iterdir()) == []


def test_recorder_dry_run_spends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` prints the six planned calls and sends none."""
    with replay() as server:
        assert recorder.main(_args(server, tmp_path, "--dry-run")) == 0
        assert server.seen == []
    printed = capsys.readouterr().out
    assert printed.count("\n[") == recorder.CALL_BUDGET
    assert list(tmp_path.iterdir()) == []


def test_recorder_writes_sanitized_fixtures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six calls through the real adapter: the list, the endpoints, a plain generation with an
    idempotency key, a seeded one, one with ``ref.png``, and the plain one again with the same
    key. The fixtures hold no key and no generation or user id (the repeated id gets the same
    token), images are at most 64 px with their true size in ``meta``, and ``verdict.json``
    reads ``dedupes: true`` when the repeat answered the same id. A second run refuses to
    overwrite without ``--force``."""
    monkeypatch.setenv(ENV, SECRET)
    with replay() as server:
        _serve_listing(server)
        for answer in ("gen-A", "gen-B", "gen-C", "gen-A"):
            server.serve("POST", "/api/v1/images", 200, _generation(answer))
        assert recorder.main(_args(server, tmp_path, "--i-accept-cost")) == 0
        seen = list(server.seen)
        assert recorder.main(_args(server, tmp_path, "--i-accept-cost")) == 2
        assert len(server.seen) == len(seen)
    printed = capsys.readouterr()
    assert "dedupes: true" in printed.out
    assert "total spent: $0.04" in printed.out
    assert "already holds recordings" in printed.err
    assert len(seen) == recorder.CALL_BUDGET
    posts = [s for s in seen if s.method == "POST"]
    keys = [{k.lower(): v for k, v in s.headers.items()}.get("idempotency-key") for s in posts]
    assert keys[0] is not None
    assert keys == [keys[0], None, None, keys[0]]
    assert posts[0].body["size"] == "512x512"  # the smallest listed size
    assert posts[1].body["seed"] == recorder.SEED
    assert posts[2].body["input_references"][0]["image_url"]["url"].startswith("data:image/png")

    recorded = tmp_path / "recorded"
    names = sorted(p.name for p in recorded.iterdir())
    assert names == [f"{name}.json" for name in recorder.CALLS]
    texts = [p.read_text() for p in sorted(recorded.iterdir())]
    for text in texts:
        assert SECRET not in text
        assert "gen-A" not in text
        assert "gen-B" not in text
        assert "user-77" not in text
    calls = [json.loads(text) for text in texts]
    assert calls[0]["response"]["body"]["data"][0]["id"] == MODEL  # model ids are kept
    plain, repeat = calls[2], calls[5]
    assert plain["request"]["headers"]["authorization"] == "REDACTED"
    assert plain["response"]["body"]["id"] == repeat["response"]["body"]["id"]
    assert plain["response"]["body"]["id"].startswith("REDACTED-")
    assert plain["response"]["body"]["id"] != calls[3]["response"]["body"]["id"]
    assert plain["meta"] == {"true_width": [128], "true_height": [96]}
    thumb = base64.b64decode(plain["response"]["body"]["data"][0]["b64_json"])
    with Image.open(io.BytesIO(thumb)) as image:
        assert max(image.size) <= recorder.THUMBNAIL_PX
    verdict = json.loads((tmp_path / "verdict.json").read_text())
    assert verdict["dedupes"] is True
    assert verdict["inconclusive"] is False
    assert verdict["model"] == MODEL
    assert verdict["total_cost"] == "0.04"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_recorder_writes_nothing_when_a_call_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed generation ends the run with exit 1, no retry, and nothing written."""
    monkeypatch.setenv(ENV, SECRET)
    with replay() as server:
        _serve_listing(server)
        server.serve("POST", "/api/v1/images", 200, _generation("gen-A"))
        server.serve("POST", "/api/v1/images", 500, {"error": {"code": 500}})
        assert recorder.main(_args(server, tmp_path, "--i-accept-cost")) == recorder.EXIT_FAILED
        assert len(server.requests("POST")) == 2
    assert "nothing written" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_recorder_inconclusive_dedupe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat that fails, or answers another id, is ``dedupes: false, inconclusive: true``;
    a missing cost is counted as unknown."""
    monkeypatch.setenv(ENV, SECRET)
    for last, out in ((500, tmp_path / "failed"), (200, tmp_path / "differs")):
        with replay() as server:
            _serve_listing(server)
            for answer in ("gen-A", "gen-B", "gen-C"):
                server.serve("POST", "/api/v1/images", 200, _generation(answer, cost=None))
            server.serve("POST", "/api/v1/images", last, _generation("gen-D"))
            assert recorder.main(_args(server, out, "--i-accept-cost")) == 0
        verdict = json.loads((out / "verdict.json").read_text())
        assert (verdict["dedupes"], verdict["inconclusive"]) == (False, True)
        printed = capsys.readouterr().out
        assert "dedupes: false, inconclusive: true" in printed
        assert "unknown (" in printed


def test_recorder_records_gzip_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenRouter compresses its answers. Every answer here comes back gzip-encoded, and the
    recorder still records all six calls, decoded, and keeps no ``Content-Encoding`` header in
    them (the real run once failed with "incorrect header check": the body was decoded twice)."""
    monkeypatch.setenv(ENV, SECRET)
    with replay() as server:
        _serve_listing(server)
        for answer in ("gen-A", "gen-B", "gen-C", "gen-A"):
            server.serve("POST", "/api/v1/images", 200, _generation(answer))
        for queue in server.routes.values():
            for queued in queue:
                queued.raw = gzip.compress(json.dumps(queued.body).encode())
                queued.headers = {**queued.headers, "Content-Encoding": "gzip"}
        assert recorder.main(_args(server, tmp_path, "--i-accept-cost")) == 0
    assert "dedupes: true" in capsys.readouterr().out
    calls = [json.loads(p.read_text()) for p in sorted((tmp_path / "recorded").iterdir())]
    assert len(calls) == recorder.CALL_BUDGET
    assert calls[0]["response"]["body"]["data"][0]["id"] == MODEL
    for call in calls:
        assert "content-encoding" not in {k.lower() for k in call["response"]["headers"]}


def test_reference_png_is_bundled() -> None:
    """The recorder's reference image is a 256 px PNG."""
    with Image.open(REFERENCE_PNG) as image:
        assert (image.format, image.size) == ("PNG", (256, 256))
    assert REFERENCE_PNG.resolve() == recorder.REFERENCE_PNG
