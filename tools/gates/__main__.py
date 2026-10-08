"""CLI: `python -m tools.gates <check> [--spec PATH] [--pyproject PATH]`.

Exit codes: 0 clean, 1 findings, 2 structural or usage error. All output on stdout.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from tools.gates.assignment import check_assignment
from tools.gates.config import ConfigError, load_config
from tools.gates.parts import PartsError, check_parts, collect_parts, write_json
from tools.gates.spec import SpecError, load_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = REPO_ROOT / "docs" / "03-requirements-spec.md"
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"
DEFAULT_TESTS = REPO_ROOT / "tests"
DEFAULT_PARTS_JSON = REPO_ROOT / "build" / "parts.json"

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.gates", description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC, help="docs/03 path")
    parser.add_argument(
        "--pyproject", type=Path, default=DEFAULT_PYPROJECT, help="pyproject.toml path"
    )
    sub = parser.add_subparsers(dest="check", required=True)
    sub.add_parser("check-assignment", help="every requirement is assigned exactly once in §22")
    parts = sub.add_parser(
        "check-parts", help="part markers are valid; M requirements ≤ current are covered"
    )
    parts.add_argument("--tests", type=Path, default=DEFAULT_TESTS, help="tests directory to walk")
    parts.add_argument(
        "--json",
        nargs="?",
        const=DEFAULT_PARTS_JSON,
        default=None,
        type=Path,
        metavar="PATH",
        help="write the collected parts as JSON (default build/parts.json)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.pyproject)
        spec = load_spec(args.spec)
        if config.current_milestone not in spec.assignment:
            raise ConfigError(
                f"current_milestone = {config.current_milestone} names no §22 assignment row "
                f"(rows: {', '.join(f'M{m}' for m in sorted(spec.assignment))})"
            )
    except (ConfigError, SpecError) as exc:
        print(f"error: {exc}")
        return EXIT_ERROR
    current = config.current_milestone

    if args.check == "check-assignment":
        report = check_assignment(spec)
        for line in report.findings:
            print(line)
        print(report.summary())
        print(f"current_milestone M{current}")
        return EXIT_OK if report.ok else EXIT_FINDINGS
    if args.check == "check-parts":
        try:
            parts, part_findings = collect_parts(args.tests, REPO_ROOT, spec)
        except PartsError as exc:
            print(f"error: {exc}")
            return EXIT_ERROR
        parts_report = check_parts(parts, part_findings, spec, current)
        for line in parts_report.findings:
            print(line)
        print(parts_report.summary())
        if parts_report.ok and args.json is not None:
            write_json(parts, args.json)
            print(f"wrote {args.json}")
        return EXIT_OK if parts_report.ok else EXIT_FINDINGS
    print(f"error: unknown check {args.check!r}")
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
