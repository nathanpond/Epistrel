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
from tools.gates.deps import DepsError, check_deps, load_deny, load_lock
from tools.gates.lexicon import LexiconError, load_parts, parse_lexicon
from tools.gates.lexicon import check as check_lexicon
from tools.gates.parts import PartsError, check_parts, collect_parts, write_json
from tools.gates.spec import SpecError, load_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = REPO_ROOT / "docs" / "03-requirements-spec.md"
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"
DEFAULT_TESTS = REPO_ROOT / "tests"
DEFAULT_LOCK = REPO_ROOT / "uv.lock"
DEFAULT_DOCS04 = REPO_ROOT / "docs" / "04-testing-plan.md"
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
    deps = sub.add_parser("check-deps", help="runtime closure in uv.lock avoids the deny-lists")
    deps.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="uv.lock path")
    lexicon = sub.add_parser(
        "check-lexicon", help="no part uses a later milestone's capability term"
    )
    lexicon.add_argument("--docs", type=Path, default=DEFAULT_DOCS04, help="docs/04 path")
    lexicon.add_argument("--parts", type=Path, default=DEFAULT_PARTS_JSON, help="parts.json path")
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
    if args.check == "check-deps":
        return run_check_deps(args.lock, args.pyproject)
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
    if args.check == "check-lexicon":
        try:
            lexicon = parse_lexicon(args.docs.read_text(encoding="utf-8"), set(spec.assignment))
            findings = check_lexicon(load_parts(args.parts), lexicon)
        except (OSError, LexiconError, SpecError) as exc:
            print(f"error: {exc}")
            return EXIT_ERROR
        for finding in findings:
            print(finding.line())
        print(f"{len(lexicon.terms)} lexicon terms, {len(findings)} violations")
        return EXIT_OK if not findings else EXIT_FINDINGS
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


def run_check_deps(lock_path: Path, pyproject: Path) -> int:
    try:
        report = check_deps(load_lock(lock_path), load_deny(pyproject))
    except DepsError as exc:
        print(f"error: {exc}")
        return EXIT_ERROR
    for line in report.findings:
        print(line)
    print(report.summary())
    return EXIT_OK if report.ok else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
