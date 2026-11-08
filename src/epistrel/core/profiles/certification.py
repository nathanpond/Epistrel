"""Certification records and the quality status they produce (FR-PROF-15..17, NFR-QUAL-8,11;
#233).

**A record** (``CertificationRecord``, ``schema_version`` 1) is what an eval run against one
configuration fingerprint certifies: the floors it passed, each with its rule, mean, standard
deviation, sample size, seeds and bound (``FloorResult``), the floors it failed or could not
decide (recorded, never hidden), the intervention types NFR-QUAL-14's floor covers and those it
lacks evidence for (E-12), whether its judge was calibrated, the model of each purpose, the run's
host and the SHA-256 of its Markdown report. ``epistrel.evals.certify`` writes one from a run's
report; a certification job records it (``recorded_via = job``), and a hand run's is imported
(``import_record``: ``POST /admin/certifications/import``, and at start-up from
``seeds/certifications/*.json``; ``recorded_via = import``). A record whose fingerprint matches
no profile version is stored all the same: it applies when the configuration matches.

**Validation.** The record's fields are exactly the schema's (extra → 422
``invalid_certification_record``, ``reason: schema``); its ``report_sha256`` is the hash of the
report's bytes normalised to LF line ends and one trailing newline (``report_hash``), checked
against the report given (``report_md``, required by the API import) or read
(``report_path``, the start-up loader) — a mismatch is ``reason: report_hash``. The record's
fingerprint is trusted as given: it cannot be recomputed without the settings.

**Idempotent.** A record is stored by its ``certification_id`` (minted when it was written, so
a re-import is the same row): the same content again stores nothing; other content under that id
is 409 ``certification_record_conflict``.

**Quality status** (FR-PROF-16). A fingerprint is ``certified`` when a ``valid`` record with at
least one floor passed matches it: the newest one's id, its floors passed (metric ids) and its
covered and insufficient intervention types. ``certified`` reads every valid certification in one
indexed query (``ix_profile_certifications_fingerprint``) so a turn's configuration stamp,
an effective-configuration read and an impact report each read it once with the model
configuration (``RuntimeFacts.quality``). Any other fingerprint is ``uncertified``; ``stale``
comes from #234 alone. "Meeting a floor" is never stored apart from a record: ``floors_met``
is computed from the matching certification only.

**Image checkers** (NFR-QUAL-12; #258, #270, ADR-182) are certified per policy, not per
configuration. A record (``image_check_certifications``) names the checker fingerprint, the policy
and its evidence: an ``eval_run`` (one policy's row of a committed E-11 report that passed both
its floors on reviewed labels) for any image-output policy but the ``external_evidence_only``
one, or an ``external_attestation`` (the checker vendor's published evaluation: title, https URL,
publisher, date and the quoted figure) for that one alone, the seeded minors policy, whose false
negatives Epistrel never measures. ``record_image_check`` (``POST
/admin/certifications/image-check``, and ``load_committed_image_checks`` at start-up over
``seeds/certifications/image-check/``) stores one per distinct evidence (the same evidence again
stores nothing, whatever its status); ``invalidate_image_check`` withdraws one. The newest
``valid`` record for the pair decides ``image_check_certified``: an attestation certifies; an
``eval_run`` certifies while the policy's version is the one its report measured.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import audit
from epistrel.core.errors import (
    AttestationNotAllowed,
    CertificationNotFound,
    CertificationNotValid,
    CertificationRecordConflict,
    CertificationUnavailable,
    EvalRunNotAllowed,
    InvalidCertificationRecord,
    InvalidEvidence,
    InvalidSetting,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.profiles.fingerprint import (
    FINGERPRINT_PATTERN,
    QualityStatus,
    QualityStatusValue,
    canonical_json,
)
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.tables import image_check_certifications, policies, profile_certifications
from epistrel.core.tables.audit_log import AuditKind
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1
SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds" / "certifications"
"""The committed certification records the start-up loader imports (#235 writes them)."""
VALID = "valid"
STALE = "stale"
QUALITY_14 = "NFR-QUAL-14"
"""NFR-QUAL-14's metric id: passed when at least one intervention type is covered."""


class FloorResult(BaseModel):
    """One floor's verdict on the run: the metric (``NFR-QUAL-<n>.<sub-metric>``), its floor
    and rule, the mean and standard deviation across seeds, the completed cases (``n``), the
    seeds, the upper bound of its 95 % interval when the rule has one, the verdict, and for
    NFR-QUAL-14's per-type rows the intervention type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str = Field(min_length=1, max_length=200)
    floor: float | None = None
    rule: str = Field(min_length=1, max_length=40)
    mean: float | None = None
    sd: float | None = None
    n: int = Field(ge=0)
    seeds: int = Field(ge=0)
    upper_bound_95: float | None = None
    verdict: str = Field(min_length=1, max_length=40)
    intervention_type: str | None = None


class CertificationRecord(BaseModel):
    """A certification record as ``epistrel.evals`` writes it (see the module docstring)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "schema_version": 1,
                    "certification_id": "01JC0000000000000000000C01",
                    "fingerprint": "sha256:" + "a" * 64,
                    "floors_passed": [
                        {
                            "metric_id": "NFR-QUAL-1.false_support",
                            "rule": "zero",
                            "floor": 0.0,
                            "n": 300,
                            "seeds": 2,
                            "upper_bound_95": 0.01,
                            "verdict": "pass",
                        }
                    ],
                    "seed_count": 2,
                    "model_tier": "reference",
                    "report_sha256": "0" * 64,
                    "run_at": "2026-11-01T12:00:00Z",
                    "report_md": "# The run's report\n",
                }
            ]
        },
    )

    schema_version: Literal[1]
    certification_id: str = Field(pattern=ULID_PATTERN)
    fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    profile_id: str | None = Field(default=None, pattern=ULID_PATTERN)
    profile_version: int | None = Field(default=None, ge=1)
    preset: str | None = Field(default=None, max_length=80)
    models: dict[str, dict[str, Any]] = Field(default_factory=dict)
    quality_settings_hash: str | None = Field(default=None, max_length=100)
    floors_passed: list[FloorResult] = Field(default_factory=list)
    floors_failed: list[FloorResult] = Field(default_factory=list)
    floors_insufficient: list[FloorResult] = Field(default_factory=list)
    intervention_types_covered: list[str] = Field(default_factory=list)
    intervention_types_insufficient: list[str] = Field(default_factory=list)
    judge_calibrated: bool = False
    sample_sizes: dict[str, int] = Field(default_factory=dict)
    seed_count: int = Field(ge=1)
    model_tier: Literal["reference", "other"] = "other"
    host: dict[str, Any] = Field(default_factory=dict)
    report_path: str | None = Field(default=None, max_length=500)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_at: datetime
    report_md: str | None = None
    """The Markdown report the hash covers (the API import embeds it; never stored)."""


class Certification(BaseModel):
    """A stored certification: its record's fields (``report_md`` aside), how and when it was
    recorded, and its status (``valid``; ``stale`` and why, #234)."""

    id: str
    fingerprint: str
    profile_id: str | None
    profile_version: int | None
    preset: str | None
    models: dict[str, Any]
    quality_settings_hash: str | None
    floors_passed: list[dict[str, Any]]
    floors_failed: list[dict[str, Any]]
    floors_insufficient: list[dict[str, Any]]
    intervention_types_covered: list[str]
    intervention_types_insufficient: list[str]
    judge_calibrated: bool
    sample_sizes: dict[str, Any]
    seed_count: int
    model_tier: str
    host: dict[str, Any]
    report_path: str | None
    report_sha256: str
    run_at: datetime
    recorded_at: datetime
    recorded_via: str
    status: str
    invalidated_at: datetime | None
    invalidated_reason: str | None
    invalidated_by_run_id: str | None


@dataclass(frozen=True, slots=True)
class CertificationImported:
    """An imported record: its certification id, whether a row was written (``False``: the
    same record was stored already), and the deployment's ``config_revision`` (unchanged)."""

    certification_id: str
    stored: bool
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


class CertificationPage(BaseModel):
    """A page of certifications, newest first; ``next_cursor`` continues it."""

    items: list[Certification]
    next_cursor: str | None = None


def report_hash(report_md: str) -> str:
    """The SHA-256 a record's ``report_sha256`` holds: the report's bytes with LF line ends and
    exactly one trailing newline."""
    text = report_md.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_record(document: Mapping[str, Any]) -> CertificationRecord:
    """``document`` as a record, or 422 ``invalid_certification_record`` (``reason: schema``)."""
    try:
        return CertificationRecord.model_validate(document)
    except ValidationError as exc:
        errors = [
            {"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"]} for e in exc.errors()
        ]
        raise InvalidCertificationRecord(
            "the certification record does not validate", {"reason": "schema", "errors": errors}
        ) from None


def check_report(record: CertificationRecord, report_md: str | None) -> None:
    """422 ``invalid_certification_record`` unless ``report_md`` hashes to the record's
    ``report_sha256`` (``reason: report_missing`` without one, ``report_hash`` on a
    mismatch)."""
    if report_md is None:
        raise InvalidCertificationRecord(
            "the certification record needs its report (report_md)",
            {"reason": "report_missing"},
        )
    if report_hash(report_md) != record.report_sha256:
        raise InvalidCertificationRecord(
            "the report does not match the record's report_sha256", {"reason": "report_hash"}
        )


def _values(record: CertificationRecord, via: str) -> dict[str, Any]:
    dumped = record.model_dump(mode="json", exclude={"schema_version", "report_md"})
    dumped["id"] = dumped.pop("certification_id")
    dumped["run_at"] = record.run_at
    dumped["recorded_via"] = via
    return dumped


_COMPARED = (
    "fingerprint",
    "profile_id",
    "profile_version",
    "preset",
    "models",
    "quality_settings_hash",
    "floors_passed",
    "floors_failed",
    "floors_insufficient",
    "intervention_types_covered",
    "intervention_types_insufficient",
    "judge_calibrated",
    "sample_sizes",
    "seed_count",
    "model_tier",
    "host",
    "report_sha256",
)


async def store_record(
    conn: AsyncConnection, record: CertificationRecord, *, via: Literal["job", "import"]
) -> tuple[str, bool]:
    """Store ``record`` (see the module docstring): its id and whether a row was written."""
    t = profile_certifications
    values = _values(record, via)
    found = (await conn.execute(select(t).where(t.c.id == values["id"]))).one_or_none()
    if found is not None:
        same = (
            all(_normal(getattr(found, key)) == _normal(values.get(key)) for key in _COMPARED)
            and found.run_at == record.run_at
        )
        if not same:
            raise CertificationRecordConflict(
                "a different certification record is stored under this id",
                {"certification_id": values["id"]},
            )
        return values["id"], False
    await conn.execute(t.insert().values(**values))
    log.info(
        "certification recorded",
        certification_id=values["id"],
        via=via,
        floors_passed=len(record.floors_passed),
    )
    return values["id"], True


def _normal(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_normal(v) for v in value]
    if isinstance(value, list):
        return [_normal(v) for v in value]
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in value.items()}
    return value


async def import_record(
    conn: AsyncConnection, document: Mapping[str, Any], *, report_md: str | None = None
) -> tuple[str, bool]:
    """Validate and store one hand-run record (``recorded_via = import``); ``report_md`` is the
    report the hash covers (default: the record's embedded ``report_md``)."""
    record = parse_record(document)
    check_report(record, report_md if report_md is not None else record.report_md)
    return await store_record(conn, record, via="import")


def _status(row: Any) -> QualityStatus:
    return QualityStatus(
        status=QualityStatusValue.certified,
        certification_id=str(row.id),
        floors_passed=tuple(str(f["metric_id"]) for f in row.floors_passed),
        intervention_types_covered=tuple(row.intervention_types_covered),
        intervention_types_insufficient=tuple(row.intervention_types_insufficient),
    )


async def _stale_failures(conn: AsyncConnection, ids: list[str]) -> dict[str, list[str]]:
    """The floors the canary that invalidated each certification missed."""
    from epistrel.core.tables import certification_runs

    if not ids:
        return {}
    r = certification_runs.c
    rows = await conn.execute(
        select(r.checks_certification_id, r.report).where(
            r.checks_certification_id.in_(ids), r.outcome == "missed"
        )
    )
    found: dict[str, list[str]] = {}
    for row in rows:
        report = dict(row.report or {})
        found[str(row.checks_certification_id)] = list(report.get("missed_floors") or [])
    return found


async def certified(conn: AsyncConnection) -> dict[str, QualityStatus]:
    """Every certified or stale fingerprint and its status (#233, #234): the newest ``valid``
    certification with a floor passed makes it ``certified``; a fingerprint with none valid but
    one invalidated since is ``stale`` (#234: a canary miss or declared drift). One indexed
    query, plus the stale ones' missed floors."""
    t = profile_certifications
    rows = list(
        await conn.execute(
            select(
                t.c.id,
                t.c.fingerprint,
                t.c.status,
                t.c.floors_passed,
                t.c.invalidated_reason,
                t.c.intervention_types_covered,
                t.c.intervention_types_insufficient,
            ).order_by(t.c.fingerprint, t.c.run_at.desc(), t.c.id.desc())
        )
    )
    found: dict[str, QualityStatus] = {}
    stale: dict[str, Any] = {}
    for row in rows:
        key = str(row.fingerprint)
        if row.status == VALID and row.floors_passed and key not in found:
            found[key] = _status(row)
        elif row.status == STALE and key not in stale:
            stale[key] = row
    misses = await _stale_failures(conn, [str(r.id) for k, r in stale.items() if k not in found])
    for key, row in stale.items():
        if key not in found:
            found[key] = QualityStatus(
                status=QualityStatusValue.stale,
                certification_id=str(row.id),
                invalidated_reason=row.invalidated_reason,
                failed_floors=tuple(misses.get(str(row.id), ())),
            )
    return found


async def mark_stale(
    conn: AsyncConnection,
    certification_id: str,
    *,
    reason: Literal["canary", "drift", "manual"],
    run_id: str | None = None,
) -> bool:
    """Invalidate a valid certification (#234): ``stale``, with when, why and the run that
    found it; whether it changed (a stale one stays as it was). Its record is never deleted."""
    t = profile_certifications
    result = await conn.execute(
        t.update()
        .where(t.c.id == certification_id, t.c.status == VALID)
        .values(
            status=STALE,
            invalidated_at=func.transaction_timestamp(),
            invalidated_reason=reason,
            invalidated_by_run_id=run_id,
        )
    )
    changed = bool(result.rowcount)
    if changed:
        log.info("certification invalidated", certification_id=certification_id, reason=reason)
    return changed


class InvalidateRequest(BaseModel):
    """``POST /admin/certifications/{id}/invalidate`` (#234): why (``drift``: the model changed
    under an unchanged identifier and reported version; ``manual``) and a note (required; it is
    for the operator's audit trail and never logged)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"reason": "drift", "note": "the provider confirmed a silent update"}]
        },
    )

    reason: Literal["drift", "manual"]
    note: StrictStr = Field(min_length=1, max_length=1_000)


@dataclass(frozen=True, slots=True)
class CertificationInvalidated:
    """An invalidated certification: its id, its status (``stale``), why, and the deployment's
    ``config_revision`` (unchanged)."""

    certification_id: str
    status: str
    invalidated_reason: str
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


async def invalidate(
    conn: AsyncConnection, certification_id: str, request: InvalidateRequest
) -> tuple[str, str]:
    """Declare a ``valid`` certification stale (#234); its status and reason after. 404
    ``certification_not_found``; 409 ``certification_not_valid`` for one already stale or
    revoked."""
    found = await get_certification(conn, certification_id)
    if found.status != VALID or not await mark_stale(conn, certification_id, reason=request.reason):
        raise CertificationNotValid(
            "only a valid certification can be invalidated",
            {"certification_id": certification_id, "status": found.status},
        )
    return STALE, request.reason


CANARY_HELD = "held"
CANARY_MISSED = "missed"
CANARY_INCONCLUSIVE = "inconclusive"


def canary_verdicts(summary: Mapping[str, Any]) -> dict[str, str]:
    """Each floor a run measured and its verdict for a canary (#234): ``pass``; ``fail`` only
    for a measured miss; ``inconclusive`` for insufficient evidence, and for a zero-tolerance
    floor that failed on too few cases with no violation (only a measured miss invalidates)."""
    floors = dict(summary.get("floors") or {})
    found: dict[str, str] = {}
    for item in floors.get("insufficient") or ():
        found[str(item["metric_id"])] = CANARY_INCONCLUSIVE
    for item in floors.get("passed") or ():
        found.setdefault(str(item["metric_id"]), "pass")
    for item in floors.get("failed") or ():
        clean = item.get("rule") == "zero" and not item.get("mean")
        found[str(item["metric_id"])] = CANARY_INCONCLUSIVE if clean else "fail"
    return found


def canary_outcome(
    passed_before: Sequence[str], summary: Mapping[str, Any]
) -> tuple[str, list[str]]:
    """A canary's outcome over the floors the certification passed (#234): ``missed`` (with the
    floors that missed) when any measured below its floor; else ``inconclusive`` when any lacks
    evidence (or was not measured); else ``held``. Floors it never passed are not re-checked."""
    verdicts = canary_verdicts(summary)
    missed = [m for m in passed_before if verdicts.get(m) == "fail"]
    if missed:
        return CANARY_MISSED, missed
    if any(verdicts.get(m) != "pass" for m in passed_before):
        return CANARY_INCONCLUSIVE, []
    return CANARY_HELD, []


async def quality_status(conn: AsyncConnection, fingerprint_value: str) -> QualityStatus:
    """``fingerprint_value``'s status: ``certified`` by its newest valid certification, else
    ``uncertified``."""
    t = profile_certifications
    row = (
        await conn.execute(
            select(
                t.c.id,
                t.c.floors_passed,
                t.c.intervention_types_covered,
                t.c.intervention_types_insufficient,
            )
            .where(t.c.fingerprint == fingerprint_value, t.c.status == VALID)
            .order_by(t.c.run_at.desc(), t.c.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None or not row.floors_passed:
        return QualityStatus(status=QualityStatusValue.uncertified)
    return _status(row)


def _certification(row: Any) -> Certification:
    return Certification.model_validate(
        {key: getattr(row, key) for key in Certification.model_fields}
    )


async def get_certification(conn: AsyncConnection, certification_id: str) -> Certification:
    """The certification ``certification_id``; 404 ``certification_not_found``."""
    t = profile_certifications
    row = (await conn.execute(select(t).where(t.c.id == certification_id))).one_or_none()
    if row is None:
        raise CertificationNotFound("no such certification", {"certification_id": certification_id})
    return _certification(row)


async def list_certifications(
    conn: AsyncConnection,
    *,
    fingerprint_value: str | None = None,
    profile_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> CertificationPage:
    """Certifications, newest recorded first (stale ones included), narrowed by the filters
    given (ANDed); ``cursor`` is the last id of the previous page."""
    t = profile_certifications
    query = select(t).order_by(t.c.id.desc()).limit(limit + 1)
    if fingerprint_value is not None:
        query = query.where(t.c.fingerprint == fingerprint_value)
    if profile_id is not None:
        query = query.where(t.c.profile_id == profile_id)
    if cursor is not None:
        query = query.where(t.c.id < cursor)
    rows = list(await conn.execute(query))
    items = [_certification(r) for r in rows[:limit]]
    return CertificationPage(
        items=items, next_cursor=items[-1].id if len(rows) > limit and items else None
    )


class FloorsMet(BaseModel):
    """A profile's quality under the deployment's models and no overrides (FR-PROF-16): its
    fingerprint, status and the floors its newest valid certification passed (empty when none
    matches: a floor is never labelled met without a certification)."""

    fingerprint: str
    status: QualityStatusValue
    floors_met: list[str]


def floors_met(fingerprint_value: str, status: QualityStatus) -> FloorsMet:
    """``fingerprint_value``'s quality read: the floors are the certification's, only."""
    return FloorsMet(
        fingerprint=fingerprint_value,
        status=status.status,
        floors_met=list(status.floors_passed or ()),
    )


def committed_records(directory: Path = SEEDS_DIR) -> list[Path]:
    """The committed record files, in name order (none when the directory is absent)."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


async def load_committed(
    conn: AsyncConnection, directory: Path = SEEDS_DIR
) -> tuple[int, list[str]]:
    """Import every committed record (idempotent): how many rows it wrote, and the files it
    skipped (malformed, or a report that does not match its hash), each logged as an error."""
    import json

    written = 0
    skipped: list[str] = []
    for path in committed_records(directory):
        try:
            document = json.loads(path.read_text("utf-8"))
            record = parse_record(document)
            report = record.report_md
            if report is None and record.report_path is not None:
                found = (directory / record.report_path).resolve()
                report = found.read_text("utf-8") if found.is_file() else None
            check_report(record, report)
            _, made = await store_record(conn, record, via="import")
            written += int(made)
        except InvalidCertificationRecord, CertificationRecordConflict, ValueError, OSError:
            log.error("certification record skipped", file=path.name)
            skipped.append(path.name)
    return written, skipped


# -- eval runs (#532) --------------------------------------------------------------------------


class EvalMetric(BaseModel):
    """One metric of a run: its id, the NFR-QUAL requirement it measures, its value (the mean
    across seeds), floor, sample size and verdict."""

    id: str
    requirement: str | None = None
    value: float | None = None
    floor: float | None = None
    sample_size: int | None = None
    verdict: str


class EvalRun(BaseModel):
    """One eval run as ``GET /admin/evals/runs`` serves it: when and against what it ran (model,
    version, fingerprint, preset, host kind), its verdicts per suite and per requirement, its
    metrics, seeds and sample size (``null`` in a report written before #532 recorded them), and
    where it comes from (``committed``: a report under ``docs/evals``; ``certification``: a
    certification run's report, #233)."""

    run_id: str
    date: str | None = None
    model: str | None = None
    model_version: str | None = None
    fingerprint: str | None = None
    preset: str | None = None
    host: str | None = None
    verdict: str | None = None
    suites: dict[str, str] = Field(default_factory=dict)
    requirements: dict[str, str] = Field(default_factory=dict)
    metrics: list[EvalMetric] | None = None
    seeds: list[int] | None = None
    sample_size: int | None = None
    source: Literal["committed", "certification"] = "committed"


class EvalRunPage(BaseModel):
    """A page of runs, newest first; ``next_cursor`` continues it."""

    items: list[EvalRun]
    next_cursor: str | None = None


def eval_run(summary: Mapping[str, Any], *, source: str) -> EvalRun:
    """A run from a report's embedded summary (``epistrel.evals.report.summary``)."""
    metrics = summary.get("metrics")
    return EvalRun(
        run_id=str(summary.get("run_id") or ""),
        date=summary.get("date"),
        model=summary.get("model"),
        model_version=summary.get("model_version"),
        fingerprint=summary.get("fingerprint"),
        preset=summary.get("preset"),
        host=summary.get("host"),
        verdict=summary.get("verdict"),
        suites=dict(summary.get("suites") or {}),
        requirements=dict(summary.get("requirements") or {}),
        metrics=None if metrics is None else [EvalMetric.model_validate(m) for m in metrics],
        seeds=summary.get("seeds"),
        sample_size=summary.get("sample_size"),
        source="certification" if source == "certification" else "committed",
    )


async def eval_runs(conn: AsyncConnection) -> list[EvalRun]:
    """Every run: the committed reports (the installed harness reads them; none without it) and
    every completed certification run's report (#233), newest first."""
    from epistrel.core.tables import certification_runs

    found: list[EvalRun] = []
    if _HARNESS:
        found += [eval_run(s, source="committed") for s in _HARNESS[0].committed_runs()]
    rows = await conn.execute(
        select(certification_runs.c.report).where(
            certification_runs.c.status == "completed",
            certification_runs.c.report.is_not(None),
        )
    )
    found += [eval_run(dict(r.report), source="certification") for r in rows]
    return sorted(found, key=lambda r: (r.date or "", r.run_id), reverse=True)


async def page_eval_runs(
    conn: AsyncConnection, *, limit: int = 50, cursor: str | None = None
) -> EvalRunPage:
    """A page of ``eval_runs``; ``cursor`` is the previous page's last run id."""
    every = await eval_runs(conn)
    start = 0
    if cursor is not None:
        ids = [r.run_id for r in every]
        start = ids.index(cursor) + 1 if cursor in ids else len(every)
    items = every[start : start + limit]
    more = start + limit < len(every)
    return EvalRunPage(items=items, next_cursor=items[-1].run_id if more and items else None)


async def get_eval_run(conn: AsyncConnection, run_id: str) -> EvalRun:
    """One run; 404 ``eval_run_not_found``."""
    from epistrel.core.errors import EvalRunNotFound

    for run in await eval_runs(conn):
        if run.run_id == run_id:
            return run
    raise EvalRunNotFound("no such eval run", {"run_id": run_id})


# -- the harness port ---------------------------------------------------------------------------


class RunEstimate(BaseModel):
    """What a certification run would take (FR-PROF-17): model calls (cases times seeds times each
    suite's calls per case), tokens, duration with the calls one after another, and its cost in
    USD only when the model matrix prices tokens."""

    model_calls: int
    tokens: int
    duration_s: float
    cost: float | None = Field(default=None, exclude_if=lambda value: value is None)


class HarnessRequest(BaseModel):
    """One certification run as the harness plays it: the run, the configuration it certifies
    (its fingerprint captured at the request, the profile version's settings document and, for a
    story's run, the story's overrides), the deployment's model of each purpose, and what it
    runs (suites, a seed count, a per-suite case cap)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    fingerprint: str
    profile_id: str
    profile_name: str
    profile_version: int
    document: dict[str, Any]
    overrides: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)
    suites: list[str]
    seeds: int
    sample: int | None = None
    reference: bool = False


class HarnessOutcome(BaseModel):
    """What a run produced: its report summary (per suite and per floor), the Markdown report,
    and the certification record when at least one floor passed (``None`` otherwise: the run
    is recorded, and no certification is made)."""

    summary: dict[str, Any]
    report_md: str
    record: CertificationRecord | None = None


class Harness(Protocol):
    """The eval harness as the Engine reaches it (``epistrel.evals.certify`` implements it).
    Nothing in the engine imports the harness (``nothing-in-the-engine-imports-the-eval-harness``):
    the process's composition root installs it (``install_harness``), and a deployment without
    one answers 503 ``certification_unavailable``."""

    def suites(self) -> list[str]:
        """Every suite a run may name."""
        ...

    def applicable(self, settings: ProfileSettings) -> list[str]:
        """The suites whose capability ``settings`` enables (#227's ``Suite.requires``)."""
        ...

    def estimate(self, suites: Sequence[str], seeds: int, sample: int | None) -> RunEstimate:
        """What running ``suites`` would take."""
        ...

    async def run(self, request: HarnessRequest) -> HarnessOutcome:
        """Play the run on scratch stories and build its record."""
        ...

    def committed_runs(self) -> list[dict[str, Any]]:
        """The committed reports' embedded summaries (#532)."""
        ...

    def suites_of(self, metric_ids: Sequence[str]) -> list[str]:
        """The suites that measure ``metric_ids`` (a canary re-runs those behind a
        certification's floors, #234)."""
        ...


_HARNESS: list[Harness] = []


def install_harness(harness: Harness | None) -> None:
    """Install (or, with ``None``, remove) the process's harness."""
    _HARNESS.clear()
    if harness is not None:
        _HARNESS.append(harness)


def harness() -> Harness:
    """The installed harness; 503 ``certification_unavailable`` without one."""
    if not _HARNESS:
        raise CertificationUnavailable(
            "certification runs need the eval harness, which this process has not installed",
            {},
        )
    return _HARNESS[0]


def harness_or_none() -> Harness | None:
    """The installed harness, if any (a recurring re-check without one does nothing)."""
    return _HARNESS[0] if _HARNESS else None


# -- image checkers (NFR-QUAL-12; #258, #270, ADR-182) ------------------------------------------

EVAL_RUN = "eval_run"
EXTERNAL_ATTESTATION = "external_attestation"
IMAGE_CHECK_SCHEMA_VERSION = 1
IMAGE_CHECK_SEEDS_DIR = SEEDS_DIR / "image-check"
"""The committed image-checker records (#271 writes them): a subdirectory, which the preset
loader's non-recursive glob never reads."""
REPO_ROOT = Path(__file__).resolve().parents[4]
"""The checkout an ``eval_run``'s repo-relative ``report_path`` resolves against (in an installed
package it names no report, and the hash is trusted)."""
CHECKER_FINGERPRINT_PATTERN = r"^[0-9a-f]{12}$"
REPORT_PATH_PATTERN = r"^[A-Za-z0-9_-][A-Za-z0-9._/-]{0,496}\.md$"
"""A repo-relative Markdown path (no ``..`` segment: ``EvalRunEvidence``)."""
SEED_PRINCIPAL = "system:certification-seeds"
IMAGE_OUTPUT = "image_output"
MINORS_POLICY = "minors_no_sexual_content"


class RateEvidence(BaseModel):
    """A measured rate and the cases behind it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rate: float | None = Field(ge=0.0, le=1.0)
    n: int = Field(ge=0)


class EvalRunEvidence(BaseModel):
    """An ``eval_run``'s ``evidence_ref``: one policy's row of a committed E-11 report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_path: str = Field(pattern=REPORT_PATH_PATTERN)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checker_fingerprint: str = Field(pattern=CHECKER_FINGERPRINT_PATTERN)
    policy_id: str = Field(min_length=1, max_length=200)
    policy_version: int = Field(ge=1)
    verdict: str = Field(min_length=1, max_length=40)
    fn: RateEvidence | None
    fp: RateEvidence
    floors: dict[str, float | None]
    uncertifiable: str | None = Field(default=None, max_length=80)

    @field_validator("report_path")
    @classmethod
    def _inside(cls, value: str) -> str:
        if ".." in value.split("/"):
            raise ValueError("report_path is repo-relative and never leaves the checkout")
        return value


class QuotedFigure(BaseModel):
    """The figure an attestation quotes from the vendor's evaluation, as published."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str = Field(min_length=1, max_length=100)
    value: float = Field(ge=0.0, le=1.0)
    n: int | None = Field(default=None, gt=0)


class AttestationEvidence(BaseModel):
    """An ``external_attestation``'s ``evidence_ref``: the vendor's published evaluation (its URL
    is recorded, never fetched)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=300)
    url: str = Field(pattern=r"^https://[^\s]+$", max_length=2048)
    publisher: str = Field(min_length=1, max_length=200)
    publication_date: date
    figure: QuotedFigure

    @field_validator("publication_date")
    @classmethod
    def _not_future(cls, value: date) -> date:
        if value > datetime.now(UTC).date():
            raise ValueError("the publication date is in the future")
        return value


class ImageCheckCertificationRequest(BaseModel):
    """``POST /admin/certifications/image-check`` (#270): the checker, the policy and the
    evidence (``evidence_ref``: ``EvalRunEvidence`` or ``AttestationEvidence`` by kind)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "checker_fingerprint": "0123456789ab",
                    "policy_id": MINORS_POLICY,
                    "evidence_kind": EXTERNAL_ATTESTATION,
                    "evidence_ref": {
                        "title": "Vision safety model card",
                        "url": "https://example.com/model-card",
                        "publisher": "Example Labs",
                        "publication_date": "2026-09-01",
                        "figure": {"metric": "recall", "value": 0.99, "n": 5000},
                    },
                }
            ]
        },
    )

    checker_fingerprint: str = Field(pattern=CHECKER_FINGERPRINT_PATTERN)
    policy_id: str = Field(min_length=1, max_length=200)
    evidence_kind: Literal["eval_run", "external_attestation"]
    evidence_ref: dict[str, Any]


class ImageCheckCertification(BaseModel):
    """One image-checker certification record (``image_check_certifications``)."""

    id: str
    checker_fingerprint: str
    policy_id: str
    policy_version: int | None
    evidence_kind: str
    evidence_ref: dict[str, Any]
    recorded_by: str
    recorded_at: datetime
    status: str
    invalidated_at: datetime | None = None
    invalidated_by: str | None = None
    invalidated_reason: str | None = None


class ImageCheckCertificationPage(BaseModel):
    """A page of image-checker certifications, newest first."""

    items: list[ImageCheckCertification]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ImageCheckRecorded:
    """A recorded (or already recorded) image-checker certification: ``stored`` is false when
    the same evidence was recorded before (whatever its status now)."""

    certification: ImageCheckCertification
    stored: bool
    config_revision: int = 0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ImageCheckInvalidated:
    """An invalidated image-checker certification (``changed`` false: it was stale already)."""

    certification: ImageCheckCertification
    changed: bool
    config_revision: int = 0
    replayed: bool = False


def _schema_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [{"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"]} for e in exc.errors()]


def parse_evidence(kind: str, ref: Mapping[str, Any]) -> EvalRunEvidence | AttestationEvidence:
    """``ref`` as ``kind``'s evidence; 422 ``invalid_evidence`` (``reason: schema``)."""
    model: type[EvalRunEvidence] | type[AttestationEvidence] = (
        EvalRunEvidence if kind == EVAL_RUN else AttestationEvidence
    )
    try:
        return model.model_validate(ref)
    except ValidationError as exc:
        raise InvalidEvidence(
            "the evidence does not validate", {"reason": "schema", "errors": _schema_errors(exc)}
        ) from None


def evidence_hash(
    checker_fingerprint: str,
    policy_id: str,
    kind: str,
    evidence: EvalRunEvidence | AttestationEvidence,
) -> str:
    """The record's content hash: the canonical JSON of what it certifies and why (never who
    recorded it or when)."""
    document = {
        "checker_fingerprint": checker_fingerprint,
        "policy_id": policy_id,
        "evidence_kind": kind,
        "evidence_ref": evidence.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


async def _image_policy(conn: AsyncConnection, policy_id: str) -> Any:
    t = policies.c
    row = (
        await conn.execute(
            select(t.id, t.version, t.applies_to, t.external_evidence_only).where(t.id == policy_id)
        )
    ).one_or_none()
    if row is None:
        raise InvalidEvidence(
            "no such policy", {"reason": "unknown_policy", "policy_id": policy_id}
        )
    if IMAGE_OUTPUT not in (row.applies_to or ()) and policy_id != MINORS_POLICY:
        raise InvalidEvidence(
            "the policy does not apply to image outputs",
            {"reason": "not_image_output", "policy_id": policy_id},
        )
    return row


def _check_eval_run(
    request: ImageCheckCertificationRequest, evidence: EvalRunEvidence, repo_root: Path
) -> None:
    if (evidence.checker_fingerprint, evidence.policy_id) != (
        request.checker_fingerprint,
        request.policy_id,
    ):
        raise InvalidEvidence(
            "the report's checker or policy is not the record's", {"reason": "mismatch"}
        )
    if evidence.uncertifiable is not None:
        raise InvalidEvidence(
            "the report marks this policy uncertifiable",
            {"reason": "uncertifiable", "uncertifiable": evidence.uncertifiable},
        )
    if evidence.verdict != "pass":
        raise InvalidEvidence(
            "an eval run certifies a policy only when it passed both floors",
            {"reason": "verdict", "verdict": evidence.verdict},
        )
    if evidence.fn is None or evidence.floors.get("max_fn") is None:
        raise InvalidEvidence("the run set no false-negative floor", {"reason": "verdict"})
    if evidence.floors.get("max_fp") is None:
        raise InvalidEvidence("the run set no false-positive floor", {"reason": "verdict"})
    found = repo_root / evidence.report_path
    if found.is_file() and report_hash(found.read_text("utf-8")) != evidence.report_sha256:
        raise InvalidEvidence(
            "the report does not match its report_sha256", {"reason": "report_hash"}
        )


def _image_certification(row: Any) -> ImageCheckCertification:
    return ImageCheckCertification.model_validate(
        {key: getattr(row, key) for key in ImageCheckCertification.model_fields}
    )


async def _image_row(conn: AsyncConnection, certification_id: str) -> Any:
    t = image_check_certifications
    row = (await conn.execute(select(t).where(t.c.id == certification_id))).one_or_none()
    if row is None:
        raise CertificationNotFound(
            "no such image-check certification", {"certification_id": certification_id}
        )
    return row


async def record_image_check(
    conn: AsyncConnection,
    request: ImageCheckCertificationRequest,
    *,
    principal: str,
    repo_root: Path = REPO_ROOT,
) -> tuple[ImageCheckCertification, bool]:
    """Record an image checker's certification for one policy (NFR-QUAL-12, ADR-182): the
    record, and whether a row was written (``False``: this evidence was recorded before, in any
    status). An ``external_attestation`` only for an ``external_evidence_only`` policy (else 422
    ``attestation_not_allowed``), an ``eval_run`` only for any other image-output policy (else
    422 ``eval_run_not_allowed``), and only one whose report passed both floors on reviewed
    labels; 422 ``invalid_evidence`` for anything else that does not hold. An ``eval_run``'s
    report is re-hashed when its path names a readable file of the checkout; otherwise its hash
    is trusted."""
    policy = await _image_policy(conn, request.policy_id)
    if request.evidence_kind == EXTERNAL_ATTESTATION and not policy.external_evidence_only:
        raise AttestationNotAllowed(
            "an external attestation certifies only an external-evidence-only policy",
            {"policy_id": request.policy_id},
        )
    if request.evidence_kind == EVAL_RUN and policy.external_evidence_only:
        raise EvalRunNotAllowed(
            "this policy is certified only by an external attestation (ADR-182)",
            {"policy_id": request.policy_id},
        )
    evidence = parse_evidence(request.evidence_kind, request.evidence_ref)
    if isinstance(evidence, EvalRunEvidence):
        _check_eval_run(request, evidence, repo_root)
    content = evidence_hash(
        request.checker_fingerprint, request.policy_id, request.evidence_kind, evidence
    )
    t = image_check_certifications
    found = (await conn.execute(select(t).where(t.c.content_hash == content))).one_or_none()
    if found is not None:
        return _image_certification(found), False
    values: dict[str, Any] = {
        "id": new_ulid(),
        "checker_fingerprint": request.checker_fingerprint,
        "policy_id": request.policy_id,
        "policy_version": policy.version,
        "metrics": {},
        "report_sha256": None,
        "recorded_via": "import",
        "evidence_kind": request.evidence_kind,
        "evidence_ref": evidence.model_dump(mode="json"),
        "content_hash": content,
        "recorded_by": principal,
    }
    if isinstance(evidence, EvalRunEvidence):
        values["policy_version"] = evidence.policy_version
        values["report_sha256"] = evidence.report_sha256
        values["metrics"] = {
            "fn": None if evidence.fn is None else evidence.fn.model_dump(mode="json"),
            "fp": evidence.fp.model_dump(mode="json"),
        }
    inserted = await conn.execute(
        pg_insert(t).values(**values).on_conflict_do_nothing(index_elements=["content_hash"])
    )
    row = (await conn.execute(select(t).where(t.c.content_hash == content))).one()
    if not inserted.rowcount:
        return _image_certification(row), False
    await audit.record(
        conn,
        AuditKind.IMAGE_CHECK_CERTIFICATION_RECORDED,
        principal=principal,
        policy_ids=[request.policy_id],
        details={
            "certification_id": values["id"],
            "checker_fingerprint": request.checker_fingerprint,
            "evidence_kind": request.evidence_kind,
        },
    )
    log.info(
        "image check certification recorded",
        certification_id=values["id"],
        policy_id=request.policy_id,
        evidence_kind=request.evidence_kind,
    )
    return _image_certification(row), True


async def invalidate_image_check(
    conn: AsyncConnection, certification_id: str, request: InvalidateRequest, *, principal: str
) -> tuple[ImageCheckCertification, bool]:
    """Withdraw an image-checker certification (``stale``, with when, who and why; the note is
    never stored or logged): the record and whether it changed. A stale one answers unchanged;
    404 ``certification_not_found``. Images checked before keep the ``certified`` they were
    checked with: the flag is a snapshot of check time."""
    row = await _image_row(conn, certification_id)
    if row.status != VALID:
        return _image_certification(row), False
    t = image_check_certifications
    await conn.execute(
        t.update()
        .where(t.c.id == certification_id, t.c.status == VALID)
        .values(
            status=STALE,
            invalidated_at=func.transaction_timestamp(),
            invalidated_by=principal,
            invalidated_reason=request.reason,
        )
    )
    await audit.record(
        conn,
        AuditKind.IMAGE_CHECK_CERTIFICATION_INVALIDATED,
        principal=principal,
        policy_ids=[str(row.policy_id)],
        details={"certification_id": certification_id, "reason": request.reason},
    )
    log.info(
        "image check certification invalidated",
        certification_id=certification_id,
        reason=request.reason,
    )
    return _image_certification(await _image_row(conn, certification_id)), True


async def list_image_check_certifications(
    conn: AsyncConnection,
    *,
    checker_fingerprint: str | None = None,
    policy_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> ImageCheckCertificationPage:
    """Image-checker certifications, newest recorded first (stale ones included), narrowed by
    the filters given (ANDed)."""
    t = image_check_certifications
    query = select(t).order_by(t.c.id.desc()).limit(limit + 1)
    if checker_fingerprint is not None:
        query = query.where(t.c.checker_fingerprint == checker_fingerprint)
    if policy_id is not None:
        query = query.where(t.c.policy_id == policy_id)
    if cursor is not None:
        query = query.where(t.c.id < cursor)
    rows = list(await conn.execute(query))
    items = [_image_certification(r) for r in rows[:limit]]
    return ImageCheckCertificationPage(
        items=items, next_cursor=items[-1].id if len(rows) > limit and items else None
    )


async def image_check_certified(
    conn: AsyncConnection,
    checker_fingerprint: str,
    policy_ids: Sequence[str],
    versions: Mapping[str, int] | None = None,
) -> frozenset[str]:
    """The ids among ``policy_ids`` that the image checker ``checker_fingerprint`` is certified
    for (NFR-QUAL-12; #258, #270): the newest ``valid`` record for the pair decides. An
    attestation certifies (it exists only for an ``external_evidence_only`` policy); an
    ``eval_run`` certifies while the policy's current version (``versions``, by id) is the one
    its report measured, so a policy edited since reads uncertified until a new run. One indexed
    query."""
    wanted = sorted(set(policy_ids))
    if not wanted:
        return frozenset()
    t = image_check_certifications.c
    rows = await conn.execute(
        select(t.policy_id, t.evidence_kind, t.policy_version)
        .where(
            t.checker_fingerprint == checker_fingerprint,
            t.policy_id.in_(wanted),
            t.status == VALID,
        )
        .order_by(t.policy_id, t.recorded_at.desc(), t.id.desc())
    )
    found: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        key = str(row.policy_id)
        if key in seen:
            continue
        seen.add(key)
        current = None if versions is None else versions.get(key)
        if (
            row.evidence_kind == EXTERNAL_ATTESTATION
            or current is None
            or row.policy_version == current
        ):
            found.add(key)
    return frozenset(found)


async def require_image_check_floors(
    conn: AsyncConnection, floors: Mapping[str, Mapping[str, float | None]]
) -> None:
    """422 ``invalid_setting`` unless every policy ``floors`` names exists and applies to image
    outputs, and no ``external_evidence_only`` policy has a ``max_fn`` (its false negatives are
    never measured; ADR-182): the deployment setting's rule (#270), and ``evals run --floors``'s.
    """
    if not floors:
        return
    t = policies.c
    rows = {
        str(r.id): r
        for r in await conn.execute(
            select(t.id, t.applies_to, t.external_evidence_only, t.retired_at).where(
                t.id.in_(sorted(floors))
            )
        )
    }
    for policy_id in sorted(floors):
        field = f"body.image_check_floors.{policy_id}"
        row = rows.get(policy_id)
        if row is None or row.retired_at is not None:
            raise InvalidSetting(
                "no such policy",
                {"field": field, "policy_id": policy_id, "reason": "unknown_policy"},
            )
        if IMAGE_OUTPUT not in (row.applies_to or ()) and policy_id != MINORS_POLICY:
            raise InvalidSetting(
                "the policy does not apply to image outputs",
                {"field": field, "policy_id": policy_id, "reason": "not_image_output"},
            )
        if row.external_evidence_only and floors[policy_id].get("max_fn") is not None:
            raise InvalidSetting(
                "this policy's false negatives are never measured: it takes max_fp only",
                {"field": f"{field}.max_fn", "policy_id": policy_id, "reason": "fn_not_measurable"},
            )


class ImageCheckSeed(BaseModel):
    """A committed image-checker record (``seeds/certifications/image-check/*.json``; #271)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    checker_fingerprint: str = Field(pattern=CHECKER_FINGERPRINT_PATTERN)
    policy_id: str = Field(min_length=1, max_length=200)
    evidence_kind: Literal["eval_run", "external_attestation"]
    evidence_ref: dict[str, Any]

    def request(self) -> ImageCheckCertificationRequest:
        return ImageCheckCertificationRequest(
            checker_fingerprint=self.checker_fingerprint,
            policy_id=self.policy_id,
            evidence_kind=self.evidence_kind,
            evidence_ref=dict(self.evidence_ref),
        )


def committed_image_checks(directory: Path = IMAGE_CHECK_SEEDS_DIR) -> list[Path]:
    """The committed image-checker record files, in name order (none without the directory)."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


async def load_committed_image_checks(
    conn: AsyncConnection,
    directory: Path = IMAGE_CHECK_SEEDS_DIR,
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[int, list[str]]:
    """Record every committed image-checker record (idempotent by content: one recorded before,
    even since invalidated, is left as it is): how many rows it wrote, and the files it skipped
    (malformed, or evidence that does not hold), each logged as an error. Records reach the
    database only through this loader or the admin route, never by themselves."""
    import json

    written = 0
    skipped: list[str] = []
    for path in committed_image_checks(directory):
        try:
            seed = ImageCheckSeed.model_validate(json.loads(path.read_text("utf-8")))
            async with conn.begin_nested():
                _, made = await record_image_check(
                    conn, seed.request(), principal=SEED_PRINCIPAL, repo_root=repo_root
                )
            written += int(made)
        except (
            InvalidEvidence,
            AttestationNotAllowed,
            EvalRunNotAllowed,
            ValidationError,
            ValueError,
            OSError,
        ):
            log.error("image check certification record skipped", file=path.name)
            skipped.append(path.name)
    return written, skipped


__all__ = [
    "CANARY_HELD",
    "CANARY_INCONCLUSIVE",
    "CANARY_MISSED",
    "EVAL_RUN",
    "EXTERNAL_ATTESTATION",
    "IMAGE_CHECK_SEEDS_DIR",
    "QUALITY_14",
    "SCHEMA_VERSION",
    "SEEDS_DIR",
    "AttestationEvidence",
    "Certification",
    "CertificationImported",
    "CertificationInvalidated",
    "CertificationPage",
    "CertificationRecord",
    "EvalMetric",
    "EvalRun",
    "EvalRunEvidence",
    "EvalRunPage",
    "FloorResult",
    "FloorsMet",
    "Harness",
    "HarnessOutcome",
    "HarnessRequest",
    "ImageCheckCertification",
    "ImageCheckCertificationPage",
    "ImageCheckCertificationRequest",
    "ImageCheckInvalidated",
    "ImageCheckRecorded",
    "ImageCheckSeed",
    "InvalidateRequest",
    "QuotedFigure",
    "RateEvidence",
    "RunEstimate",
    "canary_outcome",
    "canary_verdicts",
    "certified",
    "check_report",
    "committed_image_checks",
    "committed_records",
    "eval_run",
    "eval_runs",
    "evidence_hash",
    "floors_met",
    "get_certification",
    "get_eval_run",
    "harness",
    "harness_or_none",
    "image_check_certified",
    "import_record",
    "install_harness",
    "invalidate",
    "invalidate_image_check",
    "list_certifications",
    "list_image_check_certifications",
    "load_committed",
    "load_committed_image_checks",
    "mark_stale",
    "page_eval_runs",
    "parse_evidence",
    "parse_record",
    "quality_status",
    "record_image_check",
    "report_hash",
    "require_image_check_floors",
    "store_record",
]
