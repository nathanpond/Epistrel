"""The seed-normalization check (FR-SCN-5, FR-PROP-12; #111 registers it, M4 implements it): every
seed normalizes to registered predicates with resolved entities.

It needs the ``structured`` pipeline, which this deployment has not delivered in M3, so the
runner lists it ``unavailable`` and never runs it. Until M4 implements it, a run lists it
``unavailable`` too: a deployment that delivers the capability without the check never passes
a seed silently.
"""

from __future__ import annotations

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext


class SeedNormalizationCheck(Check):
    """See the module docstring."""

    name = "seed_normalization"
    capability = "structured"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        return Findings(unavailable=[self.name])


CHECK = SeedNormalizationCheck()

__all__ = ["CHECK", "SeedNormalizationCheck"]
