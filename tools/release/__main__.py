"""CLI for .github/workflows/release.yml and rollback.yml.

- `python -m tools.release validate --tag vX.Y.Z [--pyproject P] [--released-file F]`
- `python -m tools.release retag --released-file F --good X.Y.Z` (#96): prints `tags=latest 0.1 0`,
  the floating tags a bare re-promotion of X.Y.Z re-points.

`F` is the JSON from `gh release list --json tagName,isDraft,isPrerelease`. Prints
GITHUB_OUTPUT-style `key=value` lines; exit 1 with `error: …` when the tag must not ship or the
input is unusable.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from tools.release.validate import (
    ReleaseError,
    read_project_version,
    read_released,
    retag,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASED_HELP = "JSON from `gh release list --json tagName,isDraft,isPrerelease`"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.release")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check a release tag against pyproject and past releases")
    v.add_argument("--tag", required=True)
    v.add_argument("--pyproject", type=Path, default=REPO_ROOT / "pyproject.toml")
    v.add_argument("--released-file", type=Path, default=None, help=RELEASED_HELP)
    r = sub.add_parser(
        "retag", help="floating tags a bare re-promotion of a good version re-points"
    )
    r.add_argument("--released-file", type=Path, required=True, help=RELEASED_HELP)
    r.add_argument("--good", required=True, help="bare X.Y.Z")
    args = parser.parse_args(argv)
    try:
        released, demoted = (
            read_released(args.released_file.read_text(encoding="utf-8"))
            if args.released_file
            else ([], set())
        )
        if args.command == "retag":
            lines = ["tags=" + " ".join(retag(args.good, released, demoted))]
        else:
            project = read_project_version(args.pyproject)
            lines = validate(args.tag, project, released, demoted).lines()
    except (OSError, ReleaseError) as exc:
        print(f"error: {exc}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
