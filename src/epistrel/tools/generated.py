"""Shared ``--check`` / ``--write`` handling for files generated from code.

A generated document (the model guide now, the OpenAPI schema with #73) is committed. The
generator's ``--check`` mode exits 1 with a unified diff when the committed copy differs from
what the code generates, byte for byte; ``--write`` replaces it. CI runs ``--check``.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAX_DIFF_LINES = 200


def diff(committed: str, generated: str, name: str) -> list[str]:
    """A unified diff from the committed text to the generated text (empty when equal)."""
    return list(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            generated.splitlines(keepends=True),
            fromfile=f"{name} (committed)",
            tofile=f"{name} (generated)",
        )
    )


def check(path: Path, generated: str, *, regenerate: str) -> int:
    """0 when ``path`` holds exactly ``generated``; otherwise print the diff and return 1."""
    committed = path.read_text(encoding="utf-8") if path.exists() else ""
    if committed == generated:
        print(f"{path.name} is up to date")
        return 0
    lines = diff(committed, generated, path.name)
    sys.stderr.writelines(lines[:MAX_DIFF_LINES])
    if len(lines) > MAX_DIFF_LINES:
        print(f"\n... {len(lines) - MAX_DIFF_LINES} more diff lines", file=sys.stderr)
    state = "is missing" if not path.exists() else "differs from what the code generates"
    print(f"\n{path} {state}; run `{regenerate}` and commit it.", file=sys.stderr)
    return 1


def write(path: Path, generated: str) -> int:
    if path.exists() and path.read_text(encoding="utf-8") == generated:
        print(f"{path.name} is up to date")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated, encoding="utf-8")
    print(f"wrote {path}")
    return 0


def main(
    argv: Sequence[str] | None,
    *,
    prog: str,
    description: str,
    default_path: Path,
    generate: Callable[[], str],
) -> int:
    """Parse ``--check`` / ``--write`` (and ``--path``) and run the chosen mode."""
    parser = argparse.ArgumentParser(prog=prog, description=description)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 when the file is stale")
    mode.add_argument("--write", action="store_true", help="regenerate the file")
    parser.add_argument("--path", type=Path, default=default_path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    generated = generate()
    if args.check:
        return check(args.path, generated, regenerate=f"uv run python -m {prog} --write")
    return write(args.path, generated)


__all__ = ["REPO_ROOT", "check", "diff", "main", "write"]
