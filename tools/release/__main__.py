"""CLI: `python -m tools.release validate --tag vX.Y.Z [--pyproject P] [--released-file F]`.

Prints GITHUB_OUTPUT-style `key=value` lines; exit 1 with `error: …` when the tag must not ship.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from tools.release.validate import ReleaseError, read_project_version, validate

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.release")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check a release tag against pyproject and past releases")
    v.add_argument("--tag", required=True)
    v.add_argument("--pyproject", type=Path, default=REPO_ROOT / "pyproject.toml")
    v.add_argument(
        "--released-file",
        type=Path,
        default=None,
        help="file with one released tag per line (from `gh release list`)",
    )
    args = parser.parse_args(argv)
    try:
        released = (
            [line.strip() for line in args.released_file.read_text().splitlines() if line.strip()]
            if args.released_file
            else []
        )
        outputs = validate(args.tag, read_project_version(args.pyproject), released)
    except (OSError, ReleaseError) as exc:
        print(f"error: {exc}")
        return 1
    print("\n".join(outputs.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
