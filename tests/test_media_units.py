"""The media building blocks without a database (#105): signed URLs and the hook signature, the
moderation secret command, what a probe reads, and the local blob store."""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr

from epistrel.core.errors import SigningKeyMissing
from epistrel.core.media import probe
from epistrel.core.media.signing import (
    CLOCK_SKEW_S,
    Signer,
    moderation_secret,
    subkey,
)
from epistrel.core.media.store import LocalFilesystemStore, MediaRootUnwritable
from epistrel.core.tables.media import MediaKind
from tests.model_support import CONFIG_KEY

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


# -- signing -------------------------------------------------------------------------------------


def test_signed_urls_verify_until_they_expire() -> None:
    """A signed path verifies for its method and path until ``exp`` plus the 30 s skew; a
    changed path, method, expiry or signature, a later moment, another key, or no key at all
    does not verify."""
    signer = Signer(SecretStr(CONFIG_KEY))
    path = "/media/01J9Z8Q4N5V6W7X8Y9Z0A1B2C6/content"
    signed, exp = signer.sign_path(path, ttl_s=300, now=NOW)
    assert exp == int(NOW.timestamp()) + 300
    query = dict(part.split("=", 1) for part in signed.split("?", 1)[1].split("&"))
    assert int(query["exp"]) == exp
    sig = query["sig"]
    assert "=" not in sig  # unpadded base64url
    assert signer.verify("GET", path, exp, sig, now=NOW)
    assert signer.verify("get", path, exp, sig, now=NOW + timedelta(seconds=300 + CLOCK_SKEW_S))
    assert not signer.verify("GET", path, exp, sig, now=NOW + timedelta(seconds=331))
    assert not signer.verify("GET", path.replace("C6", "C7"), exp, sig, now=NOW)
    assert not signer.verify("HEAD", path, exp, sig, now=NOW)
    assert not signer.verify("GET", path, exp + 1, sig, now=NOW)
    assert not signer.verify("GET", path, exp, sig[:-1] + ("A" if sig[-1] != "A" else "B"), now=NOW)
    other = Signer(SecretStr("b" * 43 + "="))
    assert not other.verify("GET", path, exp, sig, now=NOW)
    unkeyed = Signer(None)
    assert not unkeyed.configured
    assert not unkeyed.verify("GET", path, exp, sig, now=NOW)
    with pytest.raises(SigningKeyMissing) as missing:
        unkeyed.sign_path(path)
    assert (missing.value.code, missing.value.http_status) == ("config_key_missing", 503)


def test_the_hook_secret_is_a_subkey() -> None:
    """The hook signature is the HMAC of ``"<timestamp>." + body`` with the hook subkey, whose
    hex form ``moderation_secret`` prints; it is neither the config key nor the URL subkey."""
    signer = Signer(SecretStr(CONFIG_KEY))
    body = b'{"sha256":"x"}'
    secret = moderation_secret(CONFIG_KEY)
    expected = hmac.new(bytes.fromhex(secret), b"1760000000." + body, hashlib.sha256).hexdigest()
    assert signer.hook_signature(1760000000, body) == f"sha256={expected}"
    assert len(secret) == 64
    raw = CONFIG_KEY.encode()
    assert secret not in (raw.hex(), subkey(CONFIG_KEY, b"media-url").hex())
    with pytest.raises(ValueError, match="base64"):
        subkey("not base64 !", b"x")


def test_moderation_secret_command() -> None:
    """``epistrel moderation-secret`` prints the hook secret from ``EPISTREL_CONFIG_KEY`` and
    exits 78 without a valid key."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("EPISTREL_")}
    printed = subprocess.run(
        [sys.executable, "-m", "epistrel.cli", "moderation-secret"],
        capture_output=True,
        text=True,
        timeout=20,
        env={**env, "EPISTREL_CONFIG_KEY": CONFIG_KEY},
        check=False,
    )
    assert (printed.returncode, printed.stdout.strip()) == (0, moderation_secret(CONFIG_KEY))
    for key in ("", "short"):
        refused = subprocess.run(
            [sys.executable, "-m", "epistrel.cli", "moderation-secret"],
            capture_output=True,
            text=True,
            timeout=20,
            env={**env, "EPISTREL_CONFIG_KEY": key},
            check=False,
        )
        assert refused.returncode == 78
        assert "EPISTREL_CONFIG_KEY" in refused.stderr
        assert refused.stdout == ""


# -- probes ---------------------------------------------------------------------------------------


def _image(kind: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (6, 5), (1, 2, 3)).save(out, format=kind)
    return out.getvalue()


def test_sniffing() -> None:
    """The type comes from the bytes: PNG, JPEG, WebP, GIF, MP4, QuickTime, 3GP, AVIF, HEIF and
    WebM are recognised; anything else is ``None``. ``kind_of`` splits image from video."""
    found = {
        "PNG": _image("PNG"),
        "JPEG": _image("JPEG"),
        "WEBP": _image("WEBP"),
        "GIF": _image("GIF"),
    }
    assert [probe.sniff(data[: probe.SNIFF_BYTES]) for data in found.values()] == [
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    ]
    iso = b"\x00\x00\x00\x18ftyp"
    assert probe.sniff(iso + b"isom") == "video/mp4"
    assert probe.sniff(iso + b"qt  ") == "video/quicktime"
    assert probe.sniff(iso + b"3gp5") == "video/3gpp"
    assert probe.sniff(iso + b"avif") == "image/avif"
    assert probe.sniff(iso + b"mif1") == "image/heif"
    assert probe.sniff(iso + b"M4A ") == "audio/mp4"
    assert probe.sniff(b"\x1aE\xdf\xa3\x01") == "video/webm"
    assert probe.sniff(b"") is None
    assert probe.sniff(b"%PDF-1.7") is None
    assert probe.sniff(iso[:6]) is None
    assert probe.kind_of("image/png") is MediaKind.IMAGE
    assert probe.kind_of("video/mp4") is MediaKind.VIDEO
    assert probe.kind_of("audio/mp4") is None


async def test_dimensions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Images: Pillow reads the header. Videos: ``ffprobe`` from ``PATH`` gives width, height and
    duration (a stand-in script here); a probe that is missing, fails, prints junk or reports
    nonsense leaves the values ``None``."""
    image = tmp_path / "a.png"
    image.write_bytes(_image("PNG"))
    assert await probe.dimensions(image, MediaKind.IMAGE) == probe.Dimensions(6, 5, None)
    broken = tmp_path / "b.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    assert await probe.dimensions(broken, MediaKind.IMAGE) == probe.Dimensions()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffprobe"
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom")
    monkeypatch.setenv("PATH", str(bin_dir))
    answers = {
        '{"streams": [{"codec_type": "audio"}, {"codec_type": "video", "width": 640, '
        '"height": 360}], "format": {"duration": "2.5"}}': probe.Dimensions(640, 360, 2500),
        '{"streams": [{"codec_type": "video", "width": -1, "height": 99999999999}], '
        '"format": {"duration": "nan"}}': probe.Dimensions(),
        "not json": probe.Dimensions(),
        "[]": probe.Dimensions(),
    }
    for printed, expected in answers.items():
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' '{printed}'\n")
        fake.chmod(0o755)
        assert await probe.dimensions(video, MediaKind.VIDEO) == expected, printed
    fake.write_text("#!/bin/sh\nexit 1\n")
    assert await probe.dimensions(video, MediaKind.VIDEO) == probe.Dimensions()
    monkeypatch.setattr(probe, "FFPROBE_TIMEOUT_S", 0.2)
    fake.write_text("#!/bin/sh\nexec /bin/sleep 5\n")
    assert await probe.dimensions(video, MediaKind.VIDEO) == probe.Dimensions()
    fake.unlink()
    assert await probe.dimensions(video, MediaKind.VIDEO) == probe.Dimensions()


# -- the store -------------------------------------------------------------------------------------


def test_the_local_store(tmp_path: Path) -> None:
    """``put`` moves a staged file to ``<root>/<sha256[:2]>/<sha256>`` read-only, or drops the
    staged copy when the blob exists; ``open``, ``size``, ``age`` and ``list_checksums`` read
    it; ``delete`` removes it once. Checksums and staged names are validated before they become
    paths."""
    store = LocalFilesystemStore(tmp_path / "media")
    store.prepare()
    data = b"bytes of an asset"
    digest = hashlib.sha256(data).hexdigest()
    for name in ("01J9Z8Q4N5V6W7X8Y9Z0A1B2C6", "01J9Z8Q4N5V6W7X8Y9Z0A1B2C7"):
        with store.open_staging(name) as handle:
            handle.write(data)
    first = store.put(store.staging_path("01J9Z8Q4N5V6W7X8Y9Z0A1B2C6"), digest)
    second = store.put(store.staging_path("01J9Z8Q4N5V6W7X8Y9Z0A1B2C7"), digest)
    assert (first, second) == (True, False)
    assert list(store.staged_files()) == []
    blob = tmp_path / "media" / digest[:2] / digest
    assert blob.read_bytes() == data
    assert not blob.stat().st_mode & stat.S_IWUSR
    assert store.exists(digest)
    assert store.size(digest) == len(data)
    with store.open(digest) as handle:
        assert handle.read() == data
    age = store.age(digest)
    assert age is not None
    assert 0 <= age < 60
    assert list(store.list_checksums()) == [digest]
    assert store.delete(digest) is True
    assert store.delete(digest) is False
    assert store.age(digest) is None
    assert not store.exists(digest)
    for bad in ("../etc/passwd", "A" * 64, digest[:63]):
        with pytest.raises(ValueError, match="SHA-256"):
            store.path(bad)
    with pytest.raises(ValueError, match="asset id"):
        store.staging_path("../escape")


def test_an_unwritable_root(tmp_path: Path) -> None:
    """``prepare`` refuses a root it cannot create, naming the path."""
    blocked = tmp_path / "file"
    blocked.write_text("x")
    with pytest.raises(MediaRootUnwritable, match=str(blocked / "media")):
        LocalFilesystemStore(blocked / "media").prepare()


def test_processes_prepare_one_root_at_once(tmp_path: Path) -> None:
    """Several processes (an API and its workers) start on one media root together: each
    start-up check writes and removes a probe of its own, so none trips over another's.
    Regression: one shared probe name let a worker unlink the API's probe mid-check, and that
    start was refused."""
    import threading

    root = tmp_path / "shared"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def starting() -> None:
        start.wait()
        for _ in range(50):
            try:
                LocalFilesystemStore(root).prepare()
            except BaseException as exc:  # recorded and asserted below
                errors.append(exc)

    threads = [threading.Thread(target=starting) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert list(LocalFilesystemStore(root).staged_files()) == []
