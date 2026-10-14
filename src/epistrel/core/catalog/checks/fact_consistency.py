"""The fact-consistency check (FR-SCN-5,7, FR-PROP-10; #111 registers it, M4 implements it): the
scenario's seeds are mutually consistent under the contradiction rules, or the conflicts are
reported.

It needs the ``structured`` pipeline, which this deployment has not delivered in M3, so the
runner lists it ``unavailable`` and never runs it. Until M4 implements it, a run lists it
``unavailable`` too: a deployment that delivers the capability without the check never passes
a seed silently.
"""

from __future__ import annotations

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext


class FactConsistencyCheck(Check):
    """See the module docstring."""

    name = "fact_consistency"
    capability = "structured"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        return Findings(unavailable=[self.name])


CHECK = FactConsistencyCheck()

__all__ = ["CHECK", "FactConsistencyCheck"]
