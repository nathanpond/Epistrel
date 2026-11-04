"""The results journal (#730): every played case's outcome, kept as it lands, so an eval run
builds up over several sessions instead of losing everything when it stops.

**What a line is.** One JSON object per played case and seed: the suite, the suite's
fingerprint when it plays a configuration of its own (``runner.suite_fingerprint``), the case id
and a digest of the labelled case, the seed, the outcome (``Outcome``: observations, output,
detail), the case's latency, the engine commit and the model version the endpoint reported, and
when it was played. A harness error (an exception that is not a ``ModelError``) is never
written: the case plays again next time.

**Where.** ``<dir>/<model>/<preset>-<fingerprint[:12]>.jsonl`` (``journal_path``), one file per
configuration fingerprint. The fingerprint names the preset, the profile version, every
purpose's model and every quality-relevant setting (FR-PROF-16), so results of different models
or configurations never mix, and every model's results are kept side by side for comparison.
The default directory, ``.evals-results/`` at the repository root, is ignored by git: a line
keeps the model's output, which is never committed (like ``failures``).

**Reuse.** A run reads its journal first and plays only the cases it has no line for. A line is
reused when its suite, suite fingerprint, case id, case digest and seed all match: an edited
labelled case plays again. The engine commit is recorded, never compared: a resumed run may mix
commits, and its report lists them (``Report.resumed``). A case a transient model error ended
(``TRANSIENT``: a timeout, a dropped connection, a rate limit, a rejected key, a provider 5xx)
plays again unless ``retry_errors`` is off; ``retry_errors="all"`` replays every model error.

**Durability.** Each line is written, flushed and fsynced as its case ends, so a killed process
loses at most the cases in flight. A torn last line (a kill mid-write) is skipped on load.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from epistrel.evals.suites import CaseModel, Outcome

JOURNAL_DIR_NAME = ".evals-results"
JOURNAL_VERSION = 1
TRANSIENT_REASONS = frozenset({"timeout", "connection", "rate_limited", "auth"})
TRANSIENT_STATUSES = frozenset({408, 409, 429, 529})
"""HTTP statuses a provider sends for a passing condition (529: Anthropic's ``overloaded``);
every 5xx is transient too."""

type RetryErrors = Literal["transient", "all", "none"]


def case_digest(case: CaseModel) -> str:
    """A digest of the labelled case as played: an edited case is a new case."""
    canonical = json.dumps(case.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "model"


def journal_path(root: Path, model: str, preset: str | None, fingerprint: str) -> Path:
    """``<root>/<model>/<preset>-<fingerprint[:12]>.jsonl`` (module docstring)."""
    digest = fingerprint.split(":", 1)[-1][:12]
    name = f"{preset}-{digest}" if preset else digest
    return root / _slug(model) / f"{name}.jsonl"


def transient(detail: Mapping[str, Any]) -> bool:
    """Whether a model error's ``detail`` names a passing condition (module docstring)."""
    if "model_error" not in detail:
        return False
    if detail.get("reason") in TRANSIENT_REASONS:
        return True
    status = detail.get("provider_status")
    return isinstance(status, int) and (status >= 500 or status in TRANSIENT_STATUSES)


@dataclass(frozen=True, slots=True)
class Recorded:
    """One journalled case: its outcome and latency, and what it was played on."""

    outcome: Outcome
    latency_ms: int
    engine_sha: str
    model_version: str | None
    at: str


type Key = tuple[str, str, str, str, int]
"""``(suite, suite fingerprint or "", case id, case digest, seed)``."""


@dataclass
class Journal:
    """A run's journal (module docstring). ``reused`` counts the lines a run took instead of
    playing the case; ``played`` the lines it wrote."""

    path: Path
    model: str
    fingerprint: str
    retry_errors: RetryErrors = "transient"
    entries: dict[Key, Recorded] = field(default_factory=dict)
    reused: int = 0
    played: int = 0
    reused_shas: set[str] = field(default_factory=set)

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        model: str,
        fingerprint: str,
        retry_errors: RetryErrors = "transient",
        fresh: bool = False,
    ) -> Journal:
        """The journal at ``path`` with its lines loaded; with ``fresh`` the file is set aside
        (renamed ``.<n>.jsonl``, never deleted) and the run starts empty."""
        journal = cls(path, model, fingerprint, retry_errors)
        if fresh and path.is_file():
            number = 1
            while (aside := path.with_suffix(f".{number}.jsonl")).exists():
                number += 1
            path.rename(aside)
        for line in _lines(path):
            if line.get("fingerprint") != fingerprint:
                continue
            try:
                key: Key = (
                    str(line["suite"]),
                    str(line.get("suite_fingerprint") or ""),
                    str(line["case"]),
                    str(line["digest"]),
                    int(line["seed"]),
                )
                outcome = Outcome(
                    observations=dict(line["observations"]),
                    output=str(line.get("output", "")),
                    detail=dict(line.get("detail") or {}),
                )
            except KeyError, TypeError, ValueError:
                continue
            journal.entries[key] = Recorded(
                outcome,
                int(line.get("latency_ms") or 0),
                str(line.get("engine_sha") or ""),
                line.get("model_version"),
                str(line.get("at") or ""),
            )
        return journal

    def lookup(
        self, suite: str, suite_fingerprint: str | None, case: CaseModel, seed: int
    ) -> Recorded | None:
        """The journalled outcome of ``case`` on ``seed``, unless it must play again."""
        found = self.entries.get((suite, suite_fingerprint or "", case.id, case_digest(case), seed))
        if found is None:
            return None
        if "model_error" in found.outcome.detail and (
            self.retry_errors == "all"
            or (self.retry_errors == "transient" and transient(found.outcome.detail))
        ):
            return None
        self.reused += 1
        if found.engine_sha:
            self.reused_shas.add(found.engine_sha)
        return found

    def record(
        self,
        suite: str,
        suite_fingerprint: str | None,
        case: CaseModel,
        seed: int,
        outcome: Outcome,
        latency_ms: int,
        *,
        engine_sha: str,
        model_version: str | None,
        preset: str | None,
    ) -> None:
        """Append ``outcome`` and make it durable before returning."""
        at = datetime.now(UTC).isoformat(timespec="seconds")
        digest = case_digest(case)
        line = {
            "v": JOURNAL_VERSION,
            "fingerprint": self.fingerprint,
            "model": self.model,
            "model_version": model_version,
            "preset": preset,
            "suite": suite,
            "suite_fingerprint": suite_fingerprint,
            "case": case.id,
            "digest": digest,
            "seed": seed,
            "observations": dict(outcome.observations),
            "output": outcome.output,
            "detail": dict(outcome.detail),
            "latency_ms": latency_ms,
            "engine_sha": engine_sha,
            "at": at,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.entries[(suite, suite_fingerprint or "", case.id, digest, seed)] = Recorded(
            outcome, latency_ms, engine_sha, model_version, at
        )
        self.played += 1


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            try:
                line = json.loads(raw)
            except ValueError:
                continue  # a torn line from a kill mid-write
            if isinstance(line, dict):
                yield line


def default_root(repo: Path) -> Path:
    return repo / JOURNAL_DIR_NAME


__all__ = [
    "JOURNAL_DIR_NAME",
    "Journal",
    "Recorded",
    "RetryErrors",
    "case_digest",
    "default_root",
    "journal_path",
    "transient",
]
