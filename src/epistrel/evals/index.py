"""The **Presets** table of ``docs/evals/INDEX.md`` (NFR-QUAL-11, FR-PROF-16; #235).

One row per shipped preset (Lite, Standard, Full, Max) and model it was certified on (the
model of a record's ``generation`` purpose; #730: certifications on several models sit side by
side), or one ``uncertified`` row for a preset with no record, from the committed certification
records (``src/epistrel/core/seeds/certifications/*.json``, written by ``epistrel evals
certify``) and the preset constants (#227, ``evals.presets.PRESETS``):

- **Fingerprint**: the preset's fingerprint now, recomputed from its constants and the models
  the row's newest record names (``current_fingerprint``); with no record, the reference model
  (``REFERENCE_MODEL_ID``) for every story purpose.
- **Certification**: the row's newest record (by ``run_at``) whose fingerprint is that one, or
  ``uncertified``. When the row has records but none matches (its constants, or a prompt
  template, changed since), ``uncertified (preset changed since certification <id>)`` names the
  newest. The published claim never outlives the configuration it was measured on: the
  committed ``INDEX.md`` then differs from what the records regenerate, and ``evals index
  --check`` (and ``evals --check``) fail until it is regenerated.
- **Floors** per NFR-QUAL id (``NFR-QUAL-1.false_support`` counts for ``NFR-QUAL-1``; a suite's
  own metric for its suite): failed when any sub-metric failed, passed when every one that was
  decided passed, insufficient when none was decided.
- **Interventions** covered and insufficient (E-12, NFR-QUAL-14), or ``judge uncalibrated``.
- The record's model (``generation``'s), seeds, date (UTC) and a relative link to its report.

A record that does not parse is left out (the start-up loader logs it; ``evals --check`` lists
it).

The **Image checks** table (NFR-QUAL-12; #271) follows the Presets: one row per seeded
image-output policy (``SEEDED_IMAGE_OUTPUT_POLICIES``) and per policy any committed E-11 report
(``docs/evals/e11-*.md``) names, sorted by ``policy_id``, each from the newest report (by start,
then file name) that has a row for it. A policy with no report reads ``no run``; a report row
with no cases reads ``no_set``. Its certification comes from the committed image-checker records
(``seeds/certifications/image-check/``) for the row's checker fingerprint, never from the
report's own ``certified`` snapshot: the minors policy reads "uncertified backstop" until an
external attestation is recorded (ADR-182). An E-11 report with no embedded summary is listed as
unreadable, as a configuration report is.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from epistrel.core.profiles.certification import (
    SEEDS_DIR,
    CertificationRecord,
    ImageCheckSeed,
    committed_image_checks,
    committed_records,
    parse_record,
)
from epistrel.core.profiles.fingerprint import ModelIdentity, RuntimeFacts, fingerprint
from epistrel.evals.presets import PRESET_NAMES, PRESETS, PresetName

if TYPE_CHECKING:
    from epistrel.evals.suites.e11_image_policies import E11Report, PolicyResult

REFERENCE_MODEL_ID = "cydonia-24b-v4.3"
"""The reference model: the ``local:`` row of ``docs/model-matrix.md`` (#54). A certification
is ``host.tier = reference`` only on it (``epistrel evals certify --reference``)."""
EM_DASH = "—"
PRESETS_HEADING = "## Presets"
RUNS_HEADING = "## Runs"
PRESETS_INTRO = """\
The quality floors each shipped preset is certified for (NFR-QUAL-11), one row per model it
was certified on, from the committed certification records and the presets as they ship. A
preset edited after its certification reads `uncertified` here until it is certified again.
`epistrel evals certify` writes the reports and records; [README.md](README.md) has the
procedure.
"""
PRESETS_HEADER = (
    "Preset",
    "Fingerprint",
    "Certification",
    "Floors passed",
    "Floors failed",
    "Insufficient",
    "Interventions covered",
    "Interventions insufficient",
    "Model",
    "Seeds",
    "Date (UTC)",
    "Report",
)
NOT_CERTIFIED = {"lite": "uncertified (not certified in M7; `--presets lite` certifies it)"}


@dataclass(frozen=True, slots=True)
class Committed:
    """A committed record and where it is."""

    path: Path
    record: CertificationRecord


def records(directory: Path = SEEDS_DIR) -> list[Committed]:
    """The committed records that parse, in file order."""
    found: list[Committed] = []
    for path in committed_records(directory):
        try:
            found.append(Committed(path, parse_record(json.loads(path.read_text("utf-8")))))
        except ValueError, OSError:
            continue
    return found


def _story_purposes() -> tuple[Mapping[str, str], list[str]]:
    from epistrel.orchestrator.models.facts import TEMPLATE_HASHES
    from epistrel.orchestrator.models.registry import STORY_PURPOSES

    return TEMPLATE_HASHES, sorted(STORY_PURPOSES)


def current_fingerprint(preset: PresetName, models: Mapping[str, Mapping[str, object]]) -> str:
    """``preset``'s fingerprint now: its constants (``Preset.effective``), with ``models`` (a
    record's ``models``: purpose → ``{id, reported_version}``) as the deployment's, and with
    ``REFERENCE_MODEL_ID`` for a purpose it does not name."""
    templates, purposes = _story_purposes()
    chosen = PRESETS[preset]
    identities = {
        purpose: ModelIdentity.model_validate(models[purpose])
        if purpose in models
        else ModelIdentity(id=REFERENCE_MODEL_ID)
        for purpose in purposes
    }
    return fingerprint(
        profile_id=chosen.profile_id,
        version=chosen.version,
        settings=chosen.effective(),
        facts=RuntimeFacts(models=identities, templates=templates),
    )


def _head(metric_id: str) -> str:
    return metric_id.split(".", 1)[0]


def floor_cells(record: CertificationRecord) -> tuple[str, str, str]:
    """Floors passed, failed and insufficient, by NFR-QUAL id (see the module docstring)."""
    failed = {_head(f.metric_id) for f in record.floors_failed}
    passed = {_head(f.metric_id) for f in record.floors_passed} - failed
    insufficient = {_head(f.metric_id) for f in record.floors_insufficient} - failed - passed
    return _ids(passed), _ids(failed), _ids(insufficient)


def _key(item: str) -> tuple[int, str]:
    tail = item.rsplit("-", 1)[-1]
    return (int(tail), item) if item.startswith("NFR-QUAL-") and tail.isdigit() else (10**6, item)


def _ids(items: set[str] | Sequence[str]) -> str:
    listed = sorted(set(items), key=_key)
    return ", ".join(listed) if listed else EM_DASH


def _link(committed: Committed, index_dir: Path) -> str:
    path = committed.record.report_path
    if not path:
        return EM_DASH
    target = (committed.path.parent / path).resolve()
    relative = Path(os.path.relpath(target, index_dir.resolve())).as_posix()
    return f"[{target.name}]({relative})"


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def preset_row(preset: PresetName, found: Sequence[Committed], index_dir: Path) -> str:
    """``preset``'s row (see the module docstring)."""
    mine = sorted(
        (c for c in found if c.record.preset == preset),
        key=lambda c: (c.record.run_at, c.record.certification_id),
        reverse=True,
    )
    newest = mine[0] if mine else None
    current = current_fingerprint(preset, newest.record.models if newest else {})
    digest = f"`{current.split(':', 1)[-1][:12]}`"
    matching = next((c for c in mine if c.record.fingerprint == current), None)
    if matching is None:
        status = NOT_CERTIFIED.get(preset, "uncertified")
        if newest is not None:
            status = (
                f"uncertified (preset changed since certification {newest.record.certification_id})"
            )
        return _row([PRESETS[preset].label, digest, status, *([EM_DASH] * 9)])
    record = matching.record
    passed, failed, insufficient = floor_cells(record)
    if record.judge_calibrated:
        covered = _ids(record.intervention_types_covered)
        lacking = _ids(record.intervention_types_insufficient)
    else:
        covered = lacking = "judge uncalibrated"
    named = record.models.get("generation", {}).get("id")
    model = f"`{named}`" if named else EM_DASH
    return _row(
        [
            PRESETS[preset].label,
            digest,
            f"`{record.certification_id}`",
            passed,
            failed,
            insufficient,
            covered,
            lacking,
            model,
            str(record.seed_count),
            (
                record.run_at if record.run_at.tzinfo is None else record.run_at.astimezone(UTC)
            ).strftime("%Y-%m-%d"),
            _link(matching, index_dir),
        ]
    )


def preset_rows(preset: PresetName, found: Sequence[Committed], index_dir: Path) -> list[str]:
    """``preset``'s rows: one per model its records name (``generation``'s), by model id, or
    its single ``uncertified`` row when it has none."""
    by_model: dict[str, list[Committed]] = {}
    for committed in found:
        if committed.record.preset != preset:
            continue
        named = committed.record.models.get("generation", {}).get("id")
        by_model.setdefault(str(named or ""), []).append(committed)
    if not by_model:
        return [preset_row(preset, [], index_dir)]
    return [preset_row(preset, by_model[model], index_dir) for model in sorted(by_model)]


def presets_section(records_dir: Path = SEEDS_DIR, index_dir: Path | None = None) -> str:
    """The Presets section: its heading, introduction and table (one row per preset)."""
    found = records(records_dir)
    where = index_dir if index_dir is not None else records_dir
    lines = [
        PRESETS_HEADING,
        "",
        PRESETS_INTRO,
        _row(PRESETS_HEADER),
        "|" + "---|" * len(PRESETS_HEADER),
        *(row for name in PRESET_NAMES for row in preset_rows(name, found, where)),
    ]
    return "\n".join(lines) + "\n"


# -- Image checks (#271) -----------------------------------------------------------------------

IMAGE_CHECKS_HEADING = "## Image checks"
IMAGE_CHECKS_DIR = "image-check"
"""The image-checker records' subdirectory of the records directory."""
SEEDED_IMAGE_OUTPUT_POLICIES = ("minors_no_sexual_content",)
"""The image-output policies every deployment is seeded with (migrations 0009 and 0133); a
deployment's own policies appear once a committed report measures them."""
IMAGE_CHECKS_INTRO = """\
How each image checker measured on each image-output policy (NFR-QUAL-12, E-11), from the newest
committed E-11 report per policy, and whether a committed record certifies that checker for it.
The minors policy's false negatives are never measured: certified only by external attestation;
otherwise an uncertified backstop (ADR-182). [E11-RUNBOOK.md](E11-RUNBOOK.md) has the procedure.
"""
IMAGE_CHECKS_HEADER = (
    "Policy",
    "Checker",
    "Fingerprint",
    "Host",
    "FN rate (n)",
    "FP rate (n)",
    "Floors (max FN / FP)",
    "Verdict",
    "Certification",
    "Date (UTC)",
    "Report",
)
NO_RUN = "no run"
UNCERTIFIED_BACKSTOP = "uncertified backstop (external attestation only)"


@dataclass(frozen=True, slots=True)
class ImageCheckReport:
    """A committed E-11 report and its embedded summary."""

    name: str
    report: E11Report


def image_check_reports(reports_dir: Path) -> tuple[list[ImageCheckReport], list[str]]:
    """The committed E-11 reports with a summary, newest first, and the names of those without
    one."""
    from epistrel.evals.suites import e11_image_policies as e11

    found: list[ImageCheckReport] = []
    unreadable: list[str] = []
    if not reports_dir.is_dir():
        return found, unreadable
    for path in sorted(reports_dir.glob(f"{e11.REPORT_PREFIX}*.md")):
        try:
            summary = e11.read_summary(path)
        except ValueError:
            summary = None
        if summary is None:
            unreadable.append(path.name)
        else:
            found.append(ImageCheckReport(path.name, summary))
    found.sort(key=lambda r: (r.report.started_at.astimezone(UTC), r.name), reverse=True)
    return found, unreadable


def image_check_records(directory: Path) -> dict[tuple[str, str], str]:
    """(checker fingerprint, policy id) → evidence kind of each committed image-checker record
    that parses (a malformed one is left out: ``test_committed_image_check_certifications``
    fails on it)."""
    found: dict[tuple[str, str], str] = {}
    for path in committed_image_checks(directory):
        try:
            seed = ImageCheckSeed.model_validate(json.loads(path.read_text("utf-8")))
        except ValueError, OSError:
            continue
        found[(seed.checker_fingerprint, seed.policy_id)] = seed.evidence_kind
    return found


def _rate_cell(rate: float | None, n: int) -> str:
    return EM_DASH if rate is None else f"{rate:.2%} ({n})"


def _floor_cell(value: float | None) -> str:
    return EM_DASH if value is None else f"{value:.2%}"


def image_check_row(
    policy_id: str,
    newest: ImageCheckReport | None,
    records: Mapping[tuple[str, str], str],
) -> str:
    """``policy_id``'s row (see the module docstring)."""
    if newest is None:
        cells = [f"`{policy_id}`", *([EM_DASH] * 6), NO_RUN, EM_DASH, EM_DASH, EM_DASH]
        return _row(cells)
    report = newest.report
    row: PolicyResult = next(p for p in report.policies if p.policy_id == policy_id)
    checker = report.checker
    kind = records.get((checker.fingerprint, policy_id))
    if kind is not None:
        certification = f"certified (`{kind}`)"
    elif row.external_evidence_only:
        certification = UNCERTIFIED_BACKSTOP
    elif row.uncertifiable:
        certification = f"uncertified (`{row.uncertifiable}`)"
    else:
        certification = "uncertified"
    fn: str
    fp: str
    if row.status == "no_set":
        fn = fp = "no_set"
    else:
        fn = (
            "not measured"
            if row.fn_status == "not_measured"
            else _rate_cell(row.fn.rate if row.fn else None, row.fn.n if row.fn else 0)
        )
        fp = _rate_cell(row.fp.rate if row.fp else None, row.fp.n if row.fp else 0)
    floors = f"{_floor_cell(row.floors.get('max_fn'))} / {_floor_cell(row.floors.get('max_fp'))}"
    return _row(
        [
            f"`{policy_id}`",
            f"`{checker.model}`",
            f"`{checker.fingerprint}`",
            str(report.host.get("kind") or EM_DASH),
            fn,
            fp,
            floors,
            row.status if row.status == "no_set" else row.verdict,
            certification,
            report.started_at.astimezone(UTC).strftime("%Y-%m-%d"),
            f"[{newest.name}]({newest.name})",
        ]
    )


def image_checks_section(
    reports_dir: Path, records_dir: Path | None = None
) -> tuple[str, list[str]]:
    """The Image checks section, and the E-11 reports with no summary. ``records_dir`` is the
    certification records' directory (default: the committed one); the image-checker records
    are its ``image-check/``."""
    found, unreadable = image_check_reports(reports_dir)
    records = image_check_records((records_dir or SEEDS_DIR) / IMAGE_CHECKS_DIR)
    policies = sorted(
        {*SEEDED_IMAGE_OUTPUT_POLICIES, *(p.policy_id for r in found for p in r.report.policies)}
    )
    rows = [
        image_check_row(
            policy_id,
            next(
                (r for r in found if any(p.policy_id == policy_id for p in r.report.policies)), None
            ),
            records,
        )
        for policy_id in policies
    ]
    lines = [
        IMAGE_CHECKS_HEADING,
        "",
        IMAGE_CHECKS_INTRO,
        _row(IMAGE_CHECKS_HEADER),
        "|" + "---|" * len(IMAGE_CHECKS_HEADER),
        *rows,
    ]
    return "\n".join(lines) + "\n", unreadable


__all__ = [
    "IMAGE_CHECKS_HEADING",
    "NO_RUN",
    "REFERENCE_MODEL_ID",
    "SEEDED_IMAGE_OUTPUT_POLICIES",
    "Committed",
    "ImageCheckReport",
    "current_fingerprint",
    "floor_cells",
    "image_check_records",
    "image_check_reports",
    "image_check_row",
    "image_checks_section",
    "preset_row",
    "preset_rows",
    "presets_section",
    "records",
]
