"""The **Presets** table of ``docs/evals/INDEX.md`` (NFR-QUAL-11, FR-PROF-16; #235).

One row per shipped preset (Lite, Standard, Full, Max), from the committed certification
records (``src/epistrel/core/seeds/certifications/*.json``, written by ``epistrel evals
certify``) and the preset constants (#227, ``evals.presets.PRESETS``):

- **Fingerprint**: the preset's fingerprint now, recomputed from its constants and the models
  its newest record names (``current_fingerprint``); with no record, the reference model
  (``REFERENCE_MODEL_ID``) for every story purpose.
- **Certification**: the newest record (by ``run_at``) whose fingerprint is that one, or
  ``uncertified``. When the preset has records but none matches (its constants, or a prompt
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
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from epistrel.core.profiles.certification import (
    SEEDS_DIR,
    CertificationRecord,
    committed_records,
    parse_record,
)
from epistrel.core.profiles.fingerprint import ModelIdentity, RuntimeFacts, fingerprint
from epistrel.evals.presets import PRESET_NAMES, PRESETS, PresetName

REFERENCE_MODEL_ID = "cydonia-24b-v4.3"
"""The reference model: the ``local:`` row of ``docs/model-matrix.md`` (#54). A certification
is ``host.tier = reference`` only on it (``epistrel evals certify --reference``)."""
EM_DASH = "—"
PRESETS_HEADING = "## Presets"
RUNS_HEADING = "## Runs"
PRESETS_INTRO = """\
The quality floors each shipped preset is certified for on the reference model (NFR-QUAL-11),
from the committed certification records and the presets as they ship. A preset edited after
its certification reads `uncertified` here until it is certified again. `epistrel evals
certify` writes the reports and records; [README.md](README.md) has the procedure.
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
        *(preset_row(name, found, where) for name in PRESET_NAMES),
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "REFERENCE_MODEL_ID",
    "Committed",
    "current_fingerprint",
    "floor_cells",
    "preset_row",
    "presets_section",
    "records",
]
