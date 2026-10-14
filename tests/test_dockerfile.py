"""The image installs ffmpeg for video posters, and invariant 2 says why that is allowed (#107).

ffmpeg is a GPL program the image installs and Epistrel runs as a separate process to take a
video's poster from its first frame (FR-MEDIA-3). It is never linked or imported and is not a
Python dependency, so ``check-licenses`` (over the runtime closure of ``uv.lock``) never sees it;
CLAUDE.md's invariant 2 states the exception, as the owner decided (ADR-151). These checks read
the files: the Dockerfile's runtime stage, parsed into its instructions, and CLAUDE.md.
"""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

DOCKERFILE = REPO_ROOT / "Dockerfile"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
EXCEPTION = (
    "ffmpeg is a GPL command-line tool invoked as a separate process for poster extraction; "
    "it is not linked and not a Python dependency"
)
RUNTIME_STAGE = re.compile(r"^FROM\s+\S+\s+AS\s+runtime\s*$", re.IGNORECASE | re.MULTILINE)
INSTRUCTION = re.compile(r"^([A-Z]+)\s+(.*)$")
INSTALLS_FFMPEG = re.compile(r"\bapt-get\s+install\b[^&;]*\sffmpeg(\s|$)")


def instructions(text: str) -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` of each Dockerfile instruction, continuation lines joined
    and comments dropped."""
    found: list[tuple[str, str]] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line[:-1]}" if line.endswith("\\") else f"{pending} {line}"
        if line.endswith("\\"):
            continue
        matched = INSTRUCTION.match(pending.strip())
        if matched:
            found.append((matched.group(1), " ".join(matched.group(2).split())))
        pending = ""
    return found


def runtime_stage() -> list[tuple[str, str]]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = RUNTIME_STAGE.search(text)
    assert start is not None, "the Dockerfile has no `AS runtime` stage"
    return instructions(text[start.start() :])


def test_the_runtime_stage_installs_ffmpeg() -> None:
    """A ``RUN`` instruction of the runtime stage installs Debian's ``ffmpeg`` with ``apt-get
    install``; the stage still switches to the unprivileged user (uid 10001) after it."""
    stage = runtime_stage()
    runs = [arguments for name, arguments in stage if name == "RUN"]
    assert any(INSTALLS_FFMPEG.search(arguments) for arguments in runs), runs
    installing = next(i for i, (name, args) in enumerate(stage) if INSTALLS_FFMPEG.search(args))
    users = [i for i, (name, args) in enumerate(stage) if name == "USER"]
    assert users, "the runtime stage never drops root"
    assert users[-1] > installing
    assert stage[users[-1]][1] == "epistrel"
    assert any("--uid 10001" in args for name, args in stage if name == "RUN")


def test_only_the_runtime_stage_installs_it() -> None:
    """The builder stage installs no ffmpeg: it is a runtime tool, never a build input."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = RUNTIME_STAGE.search(text)
    assert start is not None
    builder = instructions(text[: start.start()])
    assert not any(INSTALLS_FFMPEG.search(arguments) for _, arguments in builder)


def test_claude_md_states_the_exception() -> None:
    """Invariant 2 in CLAUDE.md carries the owner's exception, word for word."""
    text = " ".join(CLAUDE_MD.read_text(encoding="utf-8").split())
    invariant = text[text.index("**Apache-2.0-compatible dependency graph.**") :]
    invariant = invariant[: invariant.index("3. **")]
    assert EXCEPTION in invariant


def test_the_checks_bite() -> None:
    """Complement: the parser sees an install, and a Dockerfile without one fails the check."""
    assert instructions("FROM x AS runtime\nRUN apt-get update \\\n  && apt-get install -y ffmpeg")[
        1
    ] == ("RUN", "apt-get update && apt-get install -y ffmpeg")
    without = instructions("FROM x AS runtime\nRUN apt-get install -y curl\nUSER epistrel")
    assert not any(INSTALLS_FFMPEG.search(arguments) for _, arguments in without)
    assert INSTALLS_FFMPEG.search("apt-get install -y --no-install-recommends ffmpeg")
    assert not INSTALLS_FFMPEG.search("apt-get install -y ffmpegthumbnailer")
