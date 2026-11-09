"""The owner's label review of E-11's generated set (#271; ``docs/evals/E11-RUNBOOK.md``).

    uv run python -m tools.review_e11_set [--sets-dir evals/sets] [--no-open]

``author e11`` writes every case ``reviewed: false``, and a run over a set with an unreviewed
case certifies nothing (``uncertifiable: unreviewed_labels``, #270). This walks the unreviewed
cases in manifest order. It opens each image with ``open`` (macOS) or ``xdg-open``, prints the
case's policy, label, tags, subjects (true and apparent ages), ``sexual_content`` and prompt,
and asks:

- ``k`` keep: the labels are right. The case becomes ``reviewed: true``, and its
  ``image_sha256`` is set to the bytes just shown.
- ``r`` relabel: correct the label, the apparent age (a one-subject case's subject too) and
  ``sexual_content``, then keep. A correction the eval-set safety rule refuses
  (``e11_safety.safety_problems``: a minors-policy violation, a sexual case with a subject who
  is or looks young) is not written. Drop that case instead.
- ``d`` drop: the manifest line and its image are deleted.
- ``s`` skip (it stays unreviewed), ``q`` quit.

The manifest is rewritten after every answer, so quitting loses nothing, and a rerun resumes at
the first unreviewed case. Hand-editing ``cases.jsonl`` is the fallback (set ``"reviewed":
true`` per line). ``uv run python -m epistrel.evals --check-sets`` then validates the result.
The approval line in ``PROVENANCE.md`` is the owner's own step, after the review. This tool
never writes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epistrel.evals import e11_safety as safety

DEFAULT_SETS_DIR = Path("evals/sets")
MANIFEST = "e11/cases.jsonl"
APPARENT = tuple(a.value for a in safety.AGE_ORDER)

type Ask = Callable[[str], str]
type Show = Callable[[Path], None]


def _no_close(_: Path) -> None:
    """The default ``close``: leave the viewer alone."""


def show_image(path: Path) -> None:
    """Open ``path`` in the system's viewer (``open`` on macOS, ``xdg-open`` elsewhere). On macOS
    it opens in the background (``open -g``), so the terminal keeps the keyboard."""
    viewer = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(viewer) is None:
        print(f"  (no {viewer}: open {path} yourself)")
        return
    command = [viewer, "-g", str(path)] if viewer == "open" else [viewer, str(path)]
    subprocess.run(command, check=False)  # noqa: S603 - a fixed viewer, a local path


def close_image(path: Path) -> None:
    """Close ``path``'s Preview window once it is answered (macOS only; elsewhere, and when
    Preview is not running or the window is already closed, nothing happens). macOS asks once
    for permission to let the terminal control Preview."""
    osascript = shutil.which("osascript")
    if sys.platform != "darwin" or osascript is None:
        return
    name = path.name.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'if application "Preview" is running then tell application "Preview" to '
        f'close (every document whose name is "{name}")'
    )
    subprocess.run(  # noqa: S603 - a fixed program; the name is quoted for AppleScript
        [osascript, "-e", script], check=False, capture_output=True
    )


@dataclass
class Review:
    """What a review did."""

    kept: list[str] = field(default_factory=list)
    relabelled: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    remaining: int = 0


def read_manifest(path: Path) -> list[dict[str, Any]]:
    """The manifest's lines as JSON objects, in order."""
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def write_manifest(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Rewrite the manifest atomically (a temporary file renamed over it)."""
    work = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    work.write_text(text, encoding="utf-8")
    work.replace(path)


def describe(row: dict[str, Any]) -> str:
    subjects = ", ".join(
        f"{s['label']} (true age {s['true_age']}, looks {s['apparent_age']})"
        for s in row.get("subjects", [])
    )
    return "\n".join(
        [
            f"case {row['case_id']}  policy {row['policy_slug']}",
            f"  label {row['label']}  apparent_age {row['apparent_age']}  "
            f"sexual_content {str(row['sexual_content']).lower()}",
            f"  tags {', '.join(row.get('tags', []))}",
            f"  subjects {subjects}",
            f"  prompt {row.get('prompt', '')}",
        ]
    )


def problems_of(row: dict[str, Any]) -> list[str]:
    """Why ``row`` fails the manifest schema or the eval-set safety rule (empty when it holds)."""
    try:
        case = safety.ManifestCase.model_validate(row)
    except ValueError as exc:
        return [str(exc).splitlines()[0]]
    return safety.safety_problems(case)


def _choice(ask: Ask, question: str, options: Sequence[str], current: str) -> str:
    while True:
        answer = ask(f"  {question} [{'/'.join(options)}] (enter keeps {current}): ").strip()
        if not answer:
            return current
        if answer in options:
            return answer
        print(f"  one of {', '.join(options)}")


def relabel(row: dict[str, Any], ask: Ask) -> dict[str, Any] | None:
    """``row`` with the owner's corrections, or ``None`` when the safety rule refuses them."""
    changed = dict(row)
    changed["label"] = _choice(ask, "label", ("clean", "violation"), str(row["label"]))
    subjects = [dict(s) for s in row["subjects"]]
    if len(subjects) == 1:
        apparent = _choice(ask, "apparent age", APPARENT, str(row["apparent_age"]))
        subjects[0]["apparent_age"] = apparent
        changed["subjects"] = subjects
        changed["apparent_age"] = apparent
    else:
        print("  several subjects: correct their apparent ages by hand-editing cases.jsonl")
    sexual = _choice(ask, "sexual_content", ("true", "false"), str(row["sexual_content"]).lower())
    changed["sexual_content"] = sexual == "true"
    found = problems_of({**changed, "reviewed": True})
    if found:
        print("  refused by the eval-set safety rule, so nothing was written:")
        for problem in found:
            print(f"    {problem}")
        print("  drop the case instead (d)")
        return None
    return changed


def review(
    sets_dir: Path,
    *,
    ask: Ask = input,
    show: Show = show_image,
    close: Show = _no_close,
) -> Review:
    """Walk the unreviewed cases (module docstring); what was done."""
    manifest = sets_dir / MANIFEST
    directory = manifest.parent
    rows = read_manifest(manifest)
    done = Review()
    index = 0
    while index < len(rows):
        row = rows[index]
        if row.get("reviewed"):
            index += 1
            continue
        image = directory / str(row["image_path"])
        if not image.is_file():
            print(f"{row['case_id']}: no image at {row['image_path']} (drop it, or regenerate)")
        else:
            show(image)
        print(describe(row))
        answer = ask("  k(eep) / r(elabel) / d(rop) / s(kip) / q(uit): ").strip().lower()[:1]
        if answer != "r":
            close(image)  # a relabel keeps the image up while its questions are asked
        if answer == "q":
            break
        if answer == "s" or answer not in {"k", "r", "d"}:
            if answer not in {"s", ""}:
                print("  not an answer: skipped")
            done.skipped.append(str(row["case_id"]))
            index += 1
            continue
        if answer == "d":
            image.unlink(missing_ok=True)
            del rows[index]
            write_manifest(manifest, rows)
            done.dropped.append(str(row["case_id"]))
            continue
        if not image.is_file():
            print("  no image to keep: skipped")
            done.skipped.append(str(row["case_id"]))
            index += 1
            continue
        updated = row if answer == "k" else relabel(row, ask)
        if answer == "r" and updated is not None:
            close(image)
        if updated is None:
            continue  # the same case again
        updated = {
            **updated,
            "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "reviewed": True,
        }
        rows[index] = updated
        write_manifest(manifest, rows)
        (done.kept if answer == "k" else done.relabelled).append(str(row["case_id"]))
        index += 1
    done.remaining = sum(1 for row in rows if not row.get("reviewed"))
    return done


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tools.review_e11_set", description=__doc__.split("\n\n")[0]
    )
    p.add_argument("--sets-dir", type=Path, default=DEFAULT_SETS_DIR)
    p.add_argument("--no-open", action="store_true", help="print each image's path, open nothing")
    args = p.parse_args(argv)
    manifest = args.sets_dir / MANIFEST
    if not manifest.is_file():
        print(f"error: no E-11 set at {manifest}: run `author e11` first", file=sys.stderr)
        return 2

    def printed(path: Path) -> None:
        print(f"image {path}")

    done = review(
        args.sets_dir,
        show=printed if args.no_open else show_image,
        close=_no_close if args.no_open else close_image,
    )
    print(
        f"kept {len(done.kept)}, relabelled {len(done.relabelled)}, dropped {len(done.dropped)}, "
        f"skipped {len(done.skipped)}; {done.remaining} unreviewed left"
    )
    if done.remaining == 0:
        print(
            "every case is reviewed: run `uv run python -m epistrel.evals --check-sets`, then "
            "record `approved by owner YYYY-MM-DD` in evals/sets/e11/PROVENANCE.md"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
