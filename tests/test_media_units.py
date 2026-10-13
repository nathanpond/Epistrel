"""The media building blocks without a database (#105, #107): signed URLs and the hook
signature, the moderation secret command, what a probe reads, the numbers generation metadata
may hold, the local blob store, byte ranges, ffmpeg's poster extraction, and a resumable
session's parts on the local store."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr, ValidationError

from epistrel.core.errors import InvalidRequest, SigningKeyMissing
from epistrel.core.media import probe
from epistrel.core.media.poster import ToolStatus
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
    does not verify. Unless told otherwise, a signed path lives 5 minutes (the moderation
    hook's ``content_url``)."""
    signer = Signer(SecretStr(CONFIG_KEY))
    path = "/media/01J9Z8Q4N5V6W7X8Y9Z0A1B2C6/content"
    assert signer.sign_path(path, now=NOW)[1] == int(NOW.timestamp()) + 5 * 60
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
    duration (a stand-in script here); a probe that is missing, cannot start (a file in no
    executable format, or a named executable that is gone), fails, prints junk or reports
    nonsense (a non-finite width or height among it) leaves the values ``None`` and never
    raises: a probe never fails an upload."""
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
        '{"streams": [{"codec_type": "video", "width": 1e400, "height": NaN}], '
        '"format": {"duration": "4"}}': probe.Dimensions(None, None, 4000),
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
    fake.write_bytes(b"\x00\x01 no executable format")  # still mode 755: exec fails (ENOEXEC)
    assert os.access(fake, os.X_OK)
    assert await probe.dimensions(video, MediaKind.VIDEO) == probe.Dimensions()
    fake.unlink()
    assert await probe.dimensions(video, MediaKind.VIDEO) == probe.Dimensions()
    gone = str(bin_dir / "ffprobe")  # named, as the poster seam names it, but no longer there
    assert await probe.video_dimensions(video, executable=gone) == probe.Dimensions()


# -- the metadata ----------------------------------------------------------------------------------


def _with_parameters(parameters: str, **fields: object) -> bytes:
    """Upload metadata whose ``generation_metadata.parameters`` is the JSON text given."""
    document = {
        "kind": "image",
        "content_rating_id": "everyone",
        "depicted_subjects": [],
        **fields,
        "generation_metadata": {"seed": 7, "parameters": "@"},
    }
    return json.dumps(document).replace('"@"', parameters).encode()


def test_generation_parameters_must_be_finite() -> None:
    """Generation metadata round-trips exactly (#317), so a number JSON cannot hold (``1e400``
    parses as Infinity, and Python's and pydantic's parsers also take ``NaN`` and
    ``Infinity``) is refused at ``generation_metadata.parameters`` wherever it sits in the
    object, rather than stored as ``null``: 422 ``validation_error`` for ``POST /media``'s
    metadata part, and the same rule for a resumable session's metadata (``UploadOpen``, as
    its route validates it) and for the library's model (a tuple in a value included).

    Complement asserted: finite numbers, the largest and the smallest a float holds among them,
    are accepted and stored exactly as sent."""
    from epistrel.core.media.assets import MediaUpload, parse_metadata
    from epistrel.core.media.uploads import UploadOpen

    session = {"size": 10, "mime_type": "image/png", "sha256": "0" * 64}
    for parameters in (
        '{"cfg_scale": 1e400}',
        '{"cfg_scale": -1e400}',
        '{"cfg_scale": NaN}',
        '{"cfg_scale": Infinity}',
        '{"cfg_scale": -Infinity}',
        '{"hires": {"steps": [1, 2, {"scale": 1e400}]}}',
    ):
        with pytest.raises(InvalidRequest) as refused:
            parse_metadata(_with_parameters(parameters))
        assert refused.value.details["field"] == "body.metadata.generation_metadata.parameters"
        with pytest.raises(ValidationError) as opened:
            UploadOpen.model_validate_json(_with_parameters(parameters, **session), strict=True)
        assert [e["loc"] for e in opened.value.errors()] == [("generation_metadata", "parameters")]
    with pytest.raises(ValidationError) as library:
        MediaUpload.model_validate(
            {
                "kind": "image",
                "content_rating_id": "everyone",
                "depicted_subjects": [],
                "generation_metadata": {"parameters": {"size": (512, float("inf"))}},
            }
        )
    assert [e["loc"] for e in library.value.errors()] == [("generation_metadata", "parameters")]

    finite = (
        '{"cfg_scale": 1e300, "floor": -1e300, "ceiling": 1.7976931348623157e308, '
        '"denoise": 5e-324, "steps": 30, "hires": {"scale": [1.5, -2.5]}}'
    )
    kept = parse_metadata(_with_parameters(finite)).stored_generation_metadata()
    assert kept == {"seed": 7, "parameters": json.loads(finite)}
    opened_finite = UploadOpen.model_validate_json(_with_parameters(finite, **session), strict=True)
    assert opened_finite.stored_metadata()["generation_metadata"]["parameters"] == json.loads(
        finite
    )


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


def test_the_store_lists_in_order_and_keeps_the_sweep_cursor(tmp_path: Path) -> None:
    """``list_checksums`` lists the blobs in ascending checksum order, all of them or those after
    a given checksum, and nothing else in the root (the staging area, the cursor, a stray file or
    directory, a blob in the wrong shard). The sweep cursor reads back what was set, ``None``
    when cleared, never set or unreadable; only a checksum can be set."""
    store = LocalFilesystemStore(tmp_path / "media")
    store.prepare()
    digests = sorted(
        prefix + digit * 62
        for prefix, digit in (("ab", "1"), ("0f", "e"), ("ab", "0"), ("f0", "1"), ("ab", "f"))
    )
    for digest in reversed(digests):
        path = store.path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    (store.root / "zz").mkdir()
    (store.root / "stray").write_text("x")
    (store.root / "ab" / ("cd" + "3" * 62)).write_bytes(b"x")  # in the wrong shard
    assert store.sweep_cursor() is None
    store.set_sweep_cursor(digests[2])
    assert store.sweep_cursor() == digests[2]
    assert list(store.list_checksums()) == digests
    assert list(store.list_checksums(after=store.sweep_cursor())) == digests[3:]
    assert list(store.list_checksums(after=digests[-1])) == []
    assert list(store.list_checksums(after="0" * 64)) == digests
    assert list(store.staged_files()) == []
    store.set_sweep_cursor(None)
    assert store.sweep_cursor() is None
    store.set_sweep_cursor(None)  # clearing twice is fine
    (store.root / ".sweep-cursor").write_text("not a checksum")
    assert store.sweep_cursor() is None
    with pytest.raises(ValueError, match="SHA-256"):
        store.set_sweep_cursor("../escape")
    assert sorted(p.name for p in store.root.iterdir() if p.name.startswith(".sweep")) == [
        ".sweep-cursor"
    ]


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


# -- delivery (#107): ranges, signatures, posters, probes, session parts ---------------------------


def test_a_range_header_reads_as_rfc_9110_says() -> None:
    """``parse_range`` takes one ``bytes`` range (first-last, first-, -suffix), any case and
    spacing; anything else is ``None``: the whole content. ``ByteRange.resolve`` clamps a range
    to the content and finds none in a range that starts past it or a zero suffix."""
    from epistrel.api.routes.media import entity_tags, parse_range
    from epistrel.core.media.assets import ByteRange

    assert parse_range("bytes=0-99") == ByteRange(first=0, last=99)
    assert parse_range("bytes=100-") == ByteRange(first=100)
    assert parse_range("bytes=-5") == ByteRange(suffix=5)
    assert parse_range(" Bytes = 1 - 2 ") == ByteRange(first=1, last=2)
    for ignored in (None, "", "bytes=", "bytes=-", "bytes=5-2", "bytes=0-1,3-4", "items=0-1"):
        assert parse_range(ignored) is None, ignored
    assert parse_range("bytes=" + "9" * 19 + "-") is None  # longer than any size
    assert ByteRange(first=0, last=99).resolve(50) == (0, 49)
    assert ByteRange(first=10).resolve(50) == (10, 49)
    assert ByteRange(suffix=5).resolve(50) == (45, 49)
    assert ByteRange(suffix=500).resolve(50) == (0, 49)
    assert ByteRange(first=50).resolve(50) is None
    assert ByteRange(suffix=0).resolve(50) is None
    assert ByteRange(first=0).resolve(0) is None
    assert entity_tags(None) == []
    assert entity_tags(' "a" , W/"b",*') == ['"a"', 'W/"b"', "*"]


def test_a_signature_is_checked_as_valid_expired_or_invalid() -> None:
    """``check`` tells a forged or misdirected signature (``invalid``) from a good one past its
    expiry and skew (``expired``); without a key everything is ``invalid``."""
    signer = Signer(SecretStr(CONFIG_KEY))
    path = "/media/01J9Z8Q4N5V6W7X8Y9Z0A1B2C6/content"
    exp = int(NOW.timestamp()) + 60
    sig = signer.url_signature("GET", path, exp)
    assert signer.check("GET", path, exp, sig, now=NOW) == "valid"
    later = NOW + timedelta(seconds=60 + CLOCK_SKEW_S + 1)
    assert signer.check("GET", path, exp, sig, now=later) == "expired"
    assert signer.check("GET", path, exp, "x" + sig[1:], now=NOW) == "invalid"
    assert signer.check("GET", path.replace("C6", "C7"), exp, sig, now=later) == "invalid"
    assert Signer(None).check("GET", path, exp, sig, now=NOW) == "invalid"
    with pytest.raises(SigningKeyMissing, match="no signed media URL"):
        Signer(None).require("no key, so no signed media URL")


def test_ffprobe_is_taken_from_beside_ffmpeg() -> None:
    from epistrel.core.media.poster import ffprobe_beside

    assert ffprobe_beside("ffmpeg") == "ffprobe"
    assert ffprobe_beside("/opt/ff/bin/ffmpeg") == "/opt/ff/bin/ffprobe"
    assert ffprobe_beside("/opt/ff/ffmpeg-7") == "/opt/ff/ffprobe-7"
    assert ffprobe_beside("/usr/local/bin/avconv") == "/usr/local/bin/ffprobe"


def _video_file(tmp_path: Path, width: int, height: int) -> Path:
    from tests.media_support import video

    path = tmp_path / "01J9Z8Q4N5V6W7X8Y9Z0A1B2C6"
    path.write_bytes(video(width, height))
    return path


async def test_the_real_tools_read_and_frame_a_video(tmp_path: Path) -> None:
    """With the installed ffmpeg: ffprobe reads a real video's width, height and duration
    (regression: the probe passed ``-nostdin``, which ffprobe refuses, so it never read a
    video), and the first frame becomes a PNG no larger than 1280 pixels a side, the aspect
    kept."""
    from epistrel.core.media.poster import FfmpegTools

    tools = FfmpegTools()
    assert await tools.status() == ToolStatus("ok", "ok")
    wide = _video_file(tmp_path, 2000, 1000)
    assert await tools.probe_video(wide) == probe.Dimensions(2000, 1000, 1000)
    assert await probe.dimensions(wide, MediaKind.VIDEO) == probe.Dimensions(2000, 1000, 1000)
    out = tmp_path / "poster"
    await tools.extract(wide, out)
    with Image.open(out) as frame:
        assert (frame.format, frame.size) == ("PNG", (1280, 640))


async def test_extraction_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ffmpeg is 422 ``poster_required`` naming ``ffmpeg_path``; ffmpeg failing is
    ``poster_extraction_failed`` (``ffmpeg_failed``), running out of time ``timeout``, and
    writing nothing ``no_frame``; no output file is left behind. ``status`` reads each tool's
    ``-version`` once and keeps the answer."""
    from epistrel.core.errors import PosterExtractionFailed, PosterRequired
    from epistrel.core.media import poster

    video = tmp_path / "v"
    video.write_bytes(b"not a video")
    out = tmp_path / "out"
    with pytest.raises(PosterRequired) as missing:
        await poster.FfmpegTools(str(tmp_path / "absent")).extract(video, out)
    assert missing.value.details == {"setting": "ffmpeg_path"}

    fake = tmp_path / "bin" / "ffmpeg"
    fake.parent.mkdir()
    calls = tmp_path / "calls"

    def script(body: str) -> poster.FfmpegTools:
        fake.write_text(
            f'#!/bin/sh\nif [ "$1" = "-version" ]; then echo v >> {calls}; exit 0; fi\n{body}\n'
        )
        fake.chmod(0o755)
        return poster.FfmpegTools(str(fake))

    failing = script("exit 1")
    with pytest.raises(PosterExtractionFailed) as failed:
        await failing.extract(video, out)
    assert failed.value.details == {"reason": "ffmpeg_failed"}
    assert not out.exists()
    await failing.status()
    assert calls.read_text().count("v") == 1  # ffmpeg asked once; ffprobe is absent here
    assert (await failing.status()).ffprobe == "missing"
    silent = script("exit 0")
    with pytest.raises(PosterExtractionFailed) as nothing:
        await silent.extract(video, out)
    assert nothing.value.details == {"reason": "no_frame"}
    monkeypatch.setattr(poster, "EXTRACT_TIMEOUT_S", 0.2)
    slow = script("exec /bin/sleep 5")
    with pytest.raises(PosterExtractionFailed) as late:
        await slow.extract(video, out)
    assert late.value.details == {"reason": "timeout"}
    assert not out.exists()


def test_the_local_store_keeps_session_parts_and_reads_ranges(tmp_path: Path) -> None:
    """The local store keeps a session's parts as numbered files under
    ``<root>/uploads/<upload id>`` (a part sent again replaces the first), lists them, writes
    them in order into a target, and removes them on finishing or aborting; directories older
    than an age are the stray ones. ``open_range`` reads a window of a blob."""
    store = LocalFilesystemStore(tmp_path / "media")
    store.prepare()
    upload_id = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C6"
    assert store.begin_parts(upload_id, "image/png") is None
    for number, data in ((2, b"world"), (1, b"x" * 6), (1, b"hello ")):
        staged = store.part_staging(upload_id, number)
        staged.write_bytes(data)
        store.store_part(upload_id, number, staged, None)
        assert not staged.exists()
    assert store.stored_parts(upload_id, None) == {1: 6, 2: 5}
    target = io.BytesIO()
    store.assemble(upload_id, 2, None, target)
    assert target.getvalue() == b"hello world"
    assert store.reopen_parts(upload_id, None, 6, 11) is None
    assert store.stale_part_uploads(3600) == []
    assert store.stale_part_uploads(0) == [(upload_id, None)]
    store.finish_parts(upload_id, None)
    assert not store.session_dir(upload_id).exists()
    assert store.stored_parts(upload_id, None) == {}
    with pytest.raises(ValueError, match="upload session"):
        store.session_dir("../escape")
    data = bytes(range(200))
    digest = hashlib.sha256(data).hexdigest()
    with store.open_staging("01J9Z8Q4N5V6W7X8Y9Z0A1B2C7") as handle:
        handle.write(data)
    store.put(store.staging_path("01J9Z8Q4N5V6W7X8Y9Z0A1B2C7"), digest)
    window = store.open_range(digest, 100, 109)
    assert window.read(4) + window.read() == data[100:110]
    assert window.read() == b""
    window.close()
    assert store.presign(digest, 60) is None
