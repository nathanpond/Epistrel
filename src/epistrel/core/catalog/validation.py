"""Catalog validation (FR-SCN-5; #111): the report shape, the check runner and the dry run.

A scenario is validated when it is published and on demand (``POST
/catalog/scenarios/{id}/validate``, a dry run that writes nothing); story creation (#119) runs
it again for what the new story uses. Catalog characters run through the same runner with their
own checks (#103's interim rules; #115 adds its own). Every surface answers one
``ValidationReport`` (``core.catalog.report``, re-exported here with ``Entry``).

**The checks** are ``CHECKS``, an explicit ordered list of modules under
``core.catalog.checks``, one per check. Each names the entry types it applies to and the
capability it needs. The runner lists a check whose capability the deployment has not delivered
as ``unavailable`` (never passed silently; in M3 ``seed_normalization`` and ``fact_consistency``,
which need the ``structured`` pipeline, and ``state_schema``, which needs States), and runs the
others in order. A check that gates (the document check) stops the checks after it when it finds
an error.

**Two phases.** ``prepare`` runs each check's work that may call a model (the input policies)
outside any transaction; ``run`` runs every check inside the caller's transaction, with what was
prepared. A publish (``CatalogService.publish``) prepares from the draft it read, then validates
under the entry's row lock, after checking that the draft's revision is still the one it
prepared from; so no lock is held across a model call (FR-CONC-2).

**The dry run** (``validate_entry``) validates the current draft (``draft_revision``, when
given, must still be the draft's: 409 ``draft_stale`` otherwise) or a published version
(``version``), as the deployment and the catalog stand now, archived entries included. It
records nothing: no operation, version, key or audit row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.catalog.checks import (
    cast_limit,
    document,
    eligibility,
    fact_consistency,
    input_policy,
    media,
    personality,
    providers,
    rating_mismatch,
    required,
    role_characters,
    seed_normalization,
    seeds,
    state_schema,
)
from epistrel.core.catalog.checks.base import (
    DEPLOYMENT_CAPABILITIES,
    Check,
    Findings,
    Preparation,
    Subject,
    ValidationContext,
)
from epistrel.core.catalog.report import (
    DRAFT,
    Entry,
    EntryLocation,
    Outcome,
    ValidationReport,
    ValidationResult,
)
from epistrel.core.catalog.service import CatalogService
from epistrel.core.config_changes import read_settings
from epistrel.core.errors import InvalidRequest
from epistrel.core.ports import Ports
from epistrel.core.tables.ownership import EntityType

CHECKS: tuple[Check, ...] = (
    document.CHECK,
    required.CHECK,
    cast_limit.CHECK,
    role_characters.CHECK,
    eligibility.CHECK,
    seeds.CHECK,
    media.CHECK,
    providers.CHECK,
    rating_mismatch.CHECK,
    input_policy.CHECK,
    personality.CHECK,
    seed_normalization.CHECK,
    fact_consistency.CHECK,
    state_schema.CHECK,
)
"""Every check, in the order they run (see the module docstring)."""


def applicable(entity_type: EntityType, checks: Sequence[Check] = CHECKS) -> tuple[Check, ...]:
    """The checks of ``checks`` that apply to ``entity_type``, in order."""
    return tuple(check for check in checks if entity_type in check.kinds)


def _available(check: Check, capabilities: frozenset[str]) -> bool:
    return check.capability is None or check.capability in capabilities


async def prepare(
    subject: Subject,
    env: Preparation,
    *,
    checks: Sequence[Check] = CHECKS,
    capabilities: frozenset[str] = DEPLOYMENT_CAPABILITIES,
) -> dict[str, Any]:
    """What each applicable, available check prepares for ``subject``, by check name (outside
    any transaction; it may call a model)."""
    prepared: dict[str, Any] = {}
    for check in applicable(subject.entity_type, checks):
        if _available(check, capabilities):
            prepared[check.name] = await check.prepare(subject, env)
    return prepared


async def run(
    subject: Subject, ctx: ValidationContext, *, checks: Sequence[Check] = CHECKS
) -> ValidationReport:
    """Run every check of ``checks`` that applies to ``subject`` (see the module docstring)."""
    entries: list[Entry] = []
    unavailable: list[str] = []
    stopped = False
    for check in applicable(subject.entity_type, checks):
        if not _available(check, ctx.capabilities):
            unavailable.append(check.name)
            continue
        if stopped:
            continue
        found: Findings = await check.run(subject, ctx)
        entries.extend(found.entries)
        unavailable.extend(found.unavailable)
        if check.gate and any(entry.error for entry in found.entries):
            stopped = True
    return ValidationReport.of(entries, unavailable)


class Runner:
    """The catalog services' ``Validator``: ``prepare`` and ``run`` over ``CHECKS``."""

    def __init__(self, checks: Sequence[Check] = CHECKS) -> None:
        self.checks = tuple(checks)

    async def prepare(self, subject: Subject, env: Preparation) -> Mapping[str, Any]:
        return await prepare(subject, env, checks=self.checks)

    async def validate(self, subject: Subject, ctx: ValidationContext) -> ValidationReport:
        return await run(subject, ctx, checks=self.checks)


RUNNER = Runner()
"""The validator both catalog kinds publish with."""


async def validate_entry(
    db: AsyncEngine,
    service: CatalogService,
    entry_id: str,
    *,
    draft_revision: int | None = None,
    version: int | None = None,
    ports: Ports | None = None,
) -> ValidationResult:
    """The dry run (see the module docstring): 404 for an unknown entry or version, 409
    ``draft_stale`` for a draft revision that is no longer the draft's, 422 when both a version
    and a draft revision are named. Writes nothing."""
    if version is not None and draft_revision is not None:
        raise InvalidRequest(
            "validate one version (`version`) or the draft (`draft_revision`), not both",
            {"field": "query.version"},
        )
    async with db.connect() as conn:
        entry = await service.entry(conn, entry_id)
        if version is None:
            if draft_revision is not None and int(entry.draft_revision) != draft_revision:
                raise service.stale(entry, entry_id, draft_revision)
            subject = service.subject(entry_id, DRAFT, entry.draft)
        else:
            content = await conn.scalar(
                select(service.versions.c.content).where(
                    service.versions.c.catalog_id == entry_id,
                    service.versions.c.version == version,
                )
            )
            if content is None:
                raise service.not_found(entry_id, version)
            subject = service.subject(entry_id, version, content)
    ports = ports or Ports()
    settings = (await read_settings(db)).as_settings()
    prepared = await service.validator.prepare(subject, Preparation(db, settings, ports))
    async with db.connect() as conn:
        context = ValidationContext(
            conn=conn,
            settings=(await read_settings(conn)).as_settings(),
            ports=ports,
            prepared=prepared,
        )
        report = await service.validator.validate(subject, context)
    return ValidationResult(valid=report.ok, report=report)


__all__ = [
    "CHECKS",
    "DEPLOYMENT_CAPABILITIES",
    "DRAFT",
    "RUNNER",
    "Check",
    "Entry",
    "EntryLocation",
    "Findings",
    "Outcome",
    "Preparation",
    "Runner",
    "Subject",
    "ValidationContext",
    "ValidationReport",
    "ValidationResult",
    "applicable",
    "prepare",
    "run",
    "validate_entry",
]
