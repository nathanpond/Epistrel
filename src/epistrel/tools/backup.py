"""``epistrel backup dump`` and ``epistrel backup verify <dump>`` (FR-ADMIN-4,7; #125).

**dump** writes ``pg_dump -Fc`` of the database ``EPISTREL_DATABASE_URL`` names to
``./epistrel-<UTC timestamp>.dump`` (or ``--output``). It is the database only: the media volume
(or bucket) and the erasure ledger's directory are backed up separately (``docs/OPERATIONS.md``),
and the ledger is deliberately not bundled, since it must outlive the backups it governs.

**verify** reads a dump without restoring it anywhere (``pg_restore --file=- --data-only`` for
the ``erasure_ledger`` and ``events`` tables, the archive on standard input) and reports:

- the **expected gaps**: every ``erasure_marker`` event (an erased record's place: identifiers
  and scope, never content) and the count of events redacted in place. These are erasures, not
  corruption, and never fail the check;
- the dump's ``erasure_ledger`` rows against the live ledger file (``--ledger``, else
  ``EPISTREL_ERASURE_LEDGER_DIR``, else ``var/erasure-ledger``): a row the file lacks, or one the
  two disagree on, means the file is not this deployment's or was tampered with; the file's
  erasures newer than the dump are listed, since a restore of the dump reapplies them.

Exit codes: 0 (gaps are informational), 2 (the ledger's chain is broken, or the dump holds rows
the file lacks or disagrees with), 3 (the dump cannot be read), 4 (``pg_restore``/``pg_dump`` is
not there), 5 (no ledger file). Output is text, or JSON with ``--json``. A dump taken before the
ledger table existed is noted and passes.

Output carries identifiers and counts only: never a name, never a payload's text.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from epistrel.config import DATABASE_URL_SCHEME, ENV_PREFIX

if TYPE_CHECKING:
    from epistrel.core.erasure.ledger import Entry

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BROKEN = 2
EXIT_UNREADABLE = 3
EXIT_NO_BINARY = 4
EXIT_NO_LEDGER = 5
EX_CONFIG = 78
DEFAULT_LEDGER_DIR = Path("var/erasure-ledger")
TIMEOUT_S = 3600
MARKER = "erasure_marker"
TABLES = ("erasure_ledger", "events")

_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(raw: str) -> str | None:
    """One field of PostgreSQL's ``COPY`` text format (``\\N`` is null)."""
    if raw == "\\N":
        return None
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "\\" or index + 1 >= len(raw):
            out.append(char)
            index += 1
            continue
        nxt = raw[index + 1]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            index += 2
        elif nxt == "x":
            digits = ""
            while len(digits) < 2 and index + 2 + len(digits) < len(raw):
                candidate = raw[index + 2 + len(digits)]
                if candidate not in "0123456789abcdefABCDEF":
                    break
                digits += candidate
            out.append(chr(int(digits, 16)) if digits else "x")
            index += 2 + len(digits)
        elif nxt in "01234567":
            digits = ""
            while len(digits) < 3 and index + 1 + len(digits) < len(raw):
                candidate = raw[index + 1 + len(digits)]
                if candidate not in "01234567":
                    break
                digits += candidate
            out.append(chr(int(digits, 8)))
            index += 1 + len(digits)
        else:
            out.append(nxt)
            index += 2
    return "".join(out)


def copy_blocks(script: str) -> dict[str, list[dict[str, str | None]]]:
    """Every ``COPY <schema>.<table> (<columns>) FROM stdin;`` block of a SQL script, as rows of
    ``{column: value}`` by table name (without the schema)."""
    blocks: dict[str, list[dict[str, str | None]]] = {}
    lines = iter(script.splitlines())
    for line in lines:
        if not line.startswith("COPY ") or not line.endswith("FROM stdin;"):
            continue
        head = line[len("COPY ") : -len(" FROM stdin;")]
        name, _, columns = head.partition(" (")
        table = name.split(".")[-1].strip('"')
        names = [c.strip().strip('"') for c in columns.rstrip(")").split(",")]
        rows = blocks.setdefault(table, [])
        for row in lines:
            if row == "\\.":
                break
            values = [unescape(v) for v in row.split("\t")]
            rows.append(dict(zip(names, values, strict=True)))
    return blocks


@dataclass
class Report:
    """What ``verify`` found."""

    dump: str
    ledger: str
    exit_code: int = EXIT_OK
    ledger_table_in_dump: bool = True
    ledger_rows: int = 0
    file_entries: int = 0
    markers: list[dict[str, Any]] = field(default_factory=list)
    redacted_events: int = 0
    newer_than_dump: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, code: int, problem: str) -> None:
        self.exit_code = max(self.exit_code, code)
        self.problems.append(problem)

    def as_json(self) -> dict[str, Any]:
        return {
            "dump": self.dump,
            "ledger": self.ledger,
            "exit_code": self.exit_code,
            "expected_gaps": {
                "erasure_markers": self.markers,
                "redacted_events": self.redacted_events,
            },
            "ledger_table_in_dump": self.ledger_table_in_dump,
            "ledger_rows_in_dump": self.ledger_rows,
            "ledger_file_entries": self.file_entries,
            "erasures_newer_than_dump": self.newer_than_dump,
            "problems": self.problems,
            "notes": self.notes,
        }

    def as_text(self) -> str:
        lines = [f"dump: {self.dump}", f"ledger: {self.ledger} ({self.file_entries} entries)"]
        lines.append(
            f"expected gaps (erasures, not corruption): {len(self.markers)} erasure marker(s), "
            f"{self.redacted_events} event(s) redacted in place"
        )
        for marker in self.markers:
            lines.append(
                f"  erasure marker {marker['erasure_id']}: {marker['record_table']} "
                f"{marker['record_id']} (story {marker['story_id']}, {marker['scope']})"
            )
        lines.append(f"ledger rows in the dump: {self.ledger_rows}")
        if self.newer_than_dump:
            lines.append(
                f"erasures newer than the dump (a restore reapplies them): "
                f"{', '.join(self.newer_than_dump)}"
            )
        lines += [f"note: {note}" for note in self.notes]
        lines += [f"problem: {problem}" for problem in self.problems]
        lines.append(
            "result: ok" if self.exit_code == EXIT_OK else f"result: exit {self.exit_code}"
        )
        return "\n".join(lines)


class _Unreadable(Exception):
    pass


def _restore_script(dump: Path, pg_restore: str) -> str:
    args = [pg_restore, "--file=-", "--data-only", *(f"--table={t}" for t in TABLES)]
    try:
        with dump.open("rb") as archive:
            done = subprocess.run(  # noqa: S603 - the operator's own pg_restore and dump
                args, stdin=archive, capture_output=True, timeout=TIMEOUT_S, check=False
            )
    except OSError as exc:
        raise _Unreadable(f"{type(exc).__name__}: {exc.strerror or exc}") from None
    if done.returncode != 0:
        first = (done.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"])[0]
        raise _Unreadable(first)
    return done.stdout.decode("utf-8")


def _marker(row: dict[str, str | None]) -> dict[str, Any]:
    payload = json.loads(row.get("payload") or "{}")
    return {
        "erasure_id": payload.get("erasure_id"),
        "story_id": row.get("story_id"),
        "scope": payload.get("scope"),
        "record_table": payload.get("record_table"),
        "record_id": payload.get("record_id"),
    }


def _entry(row: dict[str, str | None]) -> Entry:
    from epistrel.core.erasure.ledger import Entry

    hashes = row.get("scan_term_hashes") or "{}"
    return Entry(
        erasure_id=str(row["id"]),
        at=datetime.fromisoformat(_timestamp(str(row["at"]))),
        subject_kind=str(row["subject_kind"]),
        subject_id=str(row["subject_id"]),
        scope=str(row["scope"]),
        story_id=row.get("story_ref"),
        principal=str(row["principal"]),
        operation_id=row.get("operation_id"),
        counts=json.loads(row.get("counts") or "{}"),
        scan_term_hashes=tuple(h for h in hashes.strip("{}").split(",") if h),
        ledger_hash_prev=row.get("ledger_hash_prev"),
        ledger_hash=row.get("ledger_hash"),
    )


def _timestamp(value: str) -> str:
    """A ``timestamptz`` as ``COPY`` writes it (``2026-10-14 10:00:00.123456+00``), in ISO form."""
    value = value.replace(" ", "T", 1)
    if len(value) > 3 and value[-3] in "+-" and value[-2:].isdigit():
        value += ":00"
    return value


def verify(dump: Path, *, ledger_dir: Path, pg_restore: str = "pg_restore") -> Report:
    """Check ``dump`` against the ledger file in ``ledger_dir`` (see the module docstring)."""
    from epistrel.core.erasure.ledger import LedgerCorrupt, LedgerFile

    ledger = LedgerFile(ledger_dir)
    report = Report(dump=str(dump), ledger=str(ledger.path))
    executable = shutil.which(pg_restore)
    if executable is None:
        report.fail(EXIT_NO_BINARY, f"{pg_restore} was not found (install PostgreSQL's client)")
        return report
    if not ledger.exists():
        report.fail(EXIT_NO_LEDGER, "there is no erasure ledger file to check the dump against")
        return report
    try:
        entries = ledger.read()
    except LedgerCorrupt as exc:
        report.fail(EXIT_BROKEN, str(exc))
        return report
    report.file_entries = len(entries)
    try:
        blocks = copy_blocks(_restore_script(dump, executable))
    except (_Unreadable, UnicodeDecodeError) as exc:
        report.fail(EXIT_UNREADABLE, f"the dump cannot be read: {exc}")
        return report
    for row in blocks.get("events", []):
        if row.get("type") == MARKER:
            report.markers.append(_marker(row))
        elif row.get("redacted_at") is not None:
            report.redacted_events += 1
    if "erasure_ledger" not in blocks:
        report.ledger_table_in_dump = False
        report.notes.append(
            "the dump predates the erasure ledger table: a restore reapplies every erasure the "
            "file holds"
        )
        report.newer_than_dump = [e.erasure_id for e in entries]
        return report
    rows = [r for r in blocks["erasure_ledger"] if r.get("copied_from") is None]
    report.ledger_rows = len(rows)
    by_id = {e.erasure_id: e for e in entries}
    seen: set[str] = set()
    for row in rows:
        try:
            stored = _entry(row)
        except (KeyError, ValueError, TypeError) as exc:
            report.fail(EXIT_UNREADABLE, f"a ledger row of the dump cannot be read: {exc}")
            continue
        seen.add(stored.erasure_id)
        entry = by_id.get(stored.erasure_id)
        if entry is None:
            report.fail(
                EXIT_BROKEN,
                f"the dump holds ledger row {stored.erasure_id}, which the file lacks: this is "
                "not this deployment's ledger file, or it lost entries",
            )
        elif not entry.same(stored):
            report.fail(
                EXIT_BROKEN, f"ledger row {stored.erasure_id} differs between the dump and the file"
            )
    report.newer_than_dump = [e.erasure_id for e in entries if e.erasure_id not in seen]
    return report


def _database_args(url: str) -> tuple[list[str], dict[str, str]]:
    """``pg_dump`` connection arguments and environment for a ``postgresql+psycopg://`` URL;
    the password goes in ``PGPASSWORD``, never on the command line."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    args: list[str] = []
    if parsed.host:
        args.append(f"--host={parsed.host}")
    if parsed.port:
        args.append(f"--port={parsed.port}")
    if parsed.username:
        args.append(f"--username={parsed.username}")
    if parsed.database:
        args.append(f"--dbname={parsed.database}")
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = str(parsed.password)
    return args, env


def dump(output: Path | None, *, pg_dump: str = "pg_dump") -> int:
    """``epistrel backup dump``: ``pg_dump -Fc`` to ``output``; the exit code."""
    url = os.environ.get(f"{ENV_PREFIX}DATABASE_URL", "").strip()
    if not url.startswith(DATABASE_URL_SCHEME):
        print(
            f"error: {ENV_PREFIX}DATABASE_URL: must be a {DATABASE_URL_SCHEME} URL",
            file=sys.stderr,
        )
        return EX_CONFIG
    executable = shutil.which(pg_dump)
    if executable is None:
        print(f"error: {pg_dump} was not found (install PostgreSQL's client)", file=sys.stderr)
        return EXIT_NO_BINARY
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = output or Path(f"epistrel-{stamp}.dump")
    args, env = _database_args(url)
    done = subprocess.run(  # noqa: S603 - the operator's own pg_dump
        [executable, "--format=custom", f"--file={target}", *args],
        env=env,
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    if done.returncode != 0:
        first = (done.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"])[0]
        print(f"error: pg_dump failed: {first}", file=sys.stderr)
        return EXIT_FAILED
    print(
        f"{target}: the database only. Back up the media volume and the erasure ledger "
        "directory (EPISTREL_ERASURE_LEDGER_DIR) separately; see docs/OPERATIONS.md."
    )
    return EXIT_OK


def ledger_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get(f"{ENV_PREFIX}ERASURE_LEDGER_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_LEDGER_DIR


def add_parser(sub: Any) -> None:
    """``epistrel backup {dump,verify}`` on the CLI's subparsers."""
    backup = sub.add_parser("backup", help="back up the database, or check a backup")
    actions = backup.add_subparsers(dest="backup_command")
    dumping = actions.add_parser(
        "dump", help="pg_dump -Fc the database (the media and the erasure ledger go separately)"
    )
    dumping.add_argument("--output", type=Path, help="where to write the dump")
    dumping.add_argument("--pg-dump", default="pg_dump", help="the pg_dump executable")
    checking = actions.add_parser(
        "verify",
        help="check a dump: erasure markers as expected gaps, its ledger rows against the file",
    )
    checking.add_argument("dump", type=Path)
    checking.add_argument("--ledger", type=Path, help="the erasure ledger directory")
    checking.add_argument("--json", action="store_true", help="print JSON")
    checking.add_argument("--pg-restore", default="pg_restore", help="the pg_restore executable")


def run(args: argparse.Namespace) -> int:
    if args.backup_command == "dump":
        return dump(args.output, pg_dump=args.pg_dump)
    if args.backup_command == "verify":
        report = verify(args.dump, ledger_dir=ledger_dir(args.ledger), pg_restore=args.pg_restore)
        print(json.dumps(report.as_json(), indent=2) if args.json else report.as_text())
        return report.exit_code
    print("usage: epistrel backup {dump,verify} ...", file=sys.stderr)
    return 2


__all__ = [
    "EXIT_BROKEN",
    "EXIT_NO_BINARY",
    "EXIT_NO_LEDGER",
    "EXIT_OK",
    "EXIT_UNREADABLE",
    "Report",
    "add_parser",
    "copy_blocks",
    "dump",
    "run",
    "unescape",
    "verify",
]
