"""#271's label review of E-11's generated set (``tools/review_e11_set.py``), driven by scripted
answers over a synthetic set of solid-colour PNGs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tools import review_e11_set as review

from epistrel.evals import e11_safety as safety
from epistrel.evals.suites.e11_image_policies import solid_png

MINORS = safety.MINORS_POLICY_ID


def row(case_id: str, *, age: int = 30, apparent: str = "clearly_adult") -> dict[str, Any]:
    return {
        "case_id": case_id,
        "policy_slug": case_id.split("-", 1)[0],
        "image_path": f"images/{case_id.replace(':', '_')}.png",
        "image_sha256": "0" * 64,
        "label": "clean",
        "tags": [case_id.split("-")[1]],
        "subjects": [{"label": "subject_1", "true_age": age, "apparent_age": apparent}],
        "apparent_age": apparent,
        "sexual_content": False,
        "prompt": f"synthetic {case_id}",
        "caption": "",
        "reviewed": False,
    }


def write(sets_dir: Path, rows: list[dict[str, Any]]) -> Path:
    directory = sets_dir / "e11"
    (directory / "images").mkdir(parents=True)
    for n, r in enumerate(rows):
        (directory / r["image_path"]).write_bytes(solid_png(8, 8, (n, 1, 2)))
    manifest = directory / "cases.jsonl"
    manifest.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return manifest


def answers(*given: str) -> Any:
    found: Iterator[str] = iter(given)
    return lambda _prompt: next(found)


def test_review_keeps_relabels_drops(tmp_path: Path) -> None:
    """``k`` marks a case reviewed with the shown image's SHA-256, ``r`` writes the corrections
    and marks it reviewed, ``d`` deletes the line and the image, ``s`` leaves it unreviewed; the
    manifest still passes the safety rule. A rerun resumes at the first unreviewed case."""
    manifest = write(
        tmp_path,
        [
            row(f"{MINORS}-age_sensitive:youthful_adult-0001", age=22, apparent="youthful"),
            row(f"{MINORS}-plain-0002"),
            row(f"{MINORS}-plain-0003"),
            row(f"{MINORS}-plain-0004"),
        ],
    )
    shown: list[Path] = []
    done = review.review(
        tmp_path,
        ask=answers("k", "r", "", "youthful", "", "d", "s"),
        show=shown.append,
    )
    assert (len(done.kept), len(done.relabelled), len(done.dropped), len(done.skipped)) == (
        1,
        1,
        1,
        1,
    )
    assert done.remaining == 1
    assert len(shown) == 4
    rows = review.read_manifest(manifest)
    assert [r["case_id"].rsplit("-", 1)[1] for r in rows] == ["0001", "0002", "0004"]
    first, second, last = rows
    image = manifest.parent / first["image_path"]
    assert first["reviewed"] is True
    assert first["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert second["reviewed"] is True
    assert (second["apparent_age"], second["subjects"][0]["apparent_age"]) == ("youthful",) * 2
    assert last["reviewed"] is False
    assert not (manifest.parent / f"images/{MINORS}-plain-0003.png").exists()
    safety.parse_lines(manifest)  # every line still validates

    again = review.review(tmp_path, ask=answers("k"), show=lambda _: None)
    assert again.kept == [last["case_id"]]
    assert again.remaining == 0


def test_review_refuses_an_unsafe_relabel(tmp_path: Path) -> None:
    """A relabel the eval-set safety rule refuses (a violation for the minors policy) writes
    nothing; the same case is asked again, and ``q`` stops with it unreviewed."""
    manifest = write(tmp_path, [row(f"{MINORS}-plain-0001")])
    before = manifest.read_text("utf-8")
    done = review.review(tmp_path, ask=answers("r", "violation", "", "", "q"), show=lambda _: None)
    assert done.remaining == 1
    assert manifest.read_text("utf-8") == before


def test_review_cli_without_a_set(tmp_path: Path) -> None:
    """With no generated set the tool exits 2 having written nothing."""
    assert review.main(["--sets-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []
