"""Rebuild the subject-reference index from the records (FR-ADMIN-6; #123).

``python -m epistrel.tools.reindex_subjects [--story ID | --catalog] [--write]`` recomputes the
index of one story, of the catalog scope, or (bare) of every story and the catalog, through the
writer registry's readers (``core.erasure.index.SUBJECT_WRITERS``), and reports the
differences as JSON on stdout: per scope, the rows the records give that the index lacks
(``missing``) and the rows the index holds that they no longer give (``extra``). Surrogate ids
and order are ignored. Rows carry identifiers and field paths, never text.

Report-only unless ``--write``, which replaces each differing scope's rows: a story's in one
``maintenance`` operation under the story lock (one transaction per story), the catalog's under
the deployment lock. Exit 0 when every scope matched, 1 when any differed (also after
``--write``, which reports what it fixed), 78 for a configuration error and 69 when the database
cannot be used. The DSN comes from the service's settings (``EPISTREL_DATABASE_URL``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from epistrel.config import ConfigError, load_settings
from epistrel.core.db import make_engine
from epistrel.core.erasure.index import Difference, RowKey, rebuild_scope, story_ids

EX_DIFFERENT = 1
EX_UNAVAILABLE = 69
EX_CONFIG = 78
ROW_KEYS = (
    "subject_kind",
    "subject_id",
    "scope",
    "story_id",
    "record_table",
    "record_id",
    "field",
    "ref_kind",
    "span_start",
    "span_end",
    "rule",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m epistrel.tools.reindex_subjects",
        description="Recompute the subject-reference index and report the differences.",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--story", metavar="ID", help="only this story")
    which.add_argument("--catalog", action="store_true", help="only the catalog scope")
    parser.add_argument(
        "--write", action="store_true", help="replace the rows of each scope that differs"
    )
    return parser


def _row(row: RowKey) -> dict[str, Any]:
    return dict(zip(ROW_KEYS, row, strict=True))


def report_of(difference: Difference) -> dict[str, Any]:
    """One scope's report: its scope, whether it matched, and the differing rows."""
    return {
        "scope": "catalog" if difference.story_id is None else "story",
        "story_id": difference.story_id,
        "clean": difference.clean,
        "missing": [_row(r) for r in difference.missing],
        "extra": [_row(r) for r in difference.extra],
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Rebuild the scopes ``args`` names; the JSON report."""
    db = make_engine(load_settings())
    try:
        scopes: list[str | None]
        if args.story is not None:
            scopes = [args.story]
        elif args.catalog:
            scopes = [None]
        else:
            scopes = [*await story_ids(db), None]
        reports = [report_of(await rebuild_scope(db, s, write=args.write)) for s in scopes]
    finally:
        await db.dispose()
    return {
        "written": bool(args.write),
        "clean": all(r["clean"] for r in reports),
        "scopes": reports,
    }


async def execute(argv: Sequence[str] | None = None) -> int:
    """The tool on the running event loop: prints the report, answers the exit code."""
    args = _parser().parse_args(argv)
    try:
        output = await run(args)
    except ConfigError as exc:
        print("\n".join(exc.lines), file=sys.stderr)
        return EX_CONFIG
    except Exception as exc:  # report, never a traceback: this is an operator tool
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return EX_UNAVAILABLE
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["clean"] else EX_DIFFERENT


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(execute(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
