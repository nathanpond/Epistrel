"""The state-schema check (FR-SCN-5; #111 registers it, M6 implements it): the schemas of the
starting values a scenario tracks, and their initial values, are valid.

It needs the ``states`` module, which this deployment has not delivered in M3, so the runner
lists it ``unavailable`` and never runs it. Until M6 implements it, a run lists it
``unavailable`` too: a deployment that delivers the capability without the check never passes
a schema silently.
"""

from __future__ import annotations

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext


class StateSchemaCheck(Check):
    """See the module docstring."""

    name = "state_schema"
    capability = "states"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        return Findings(unavailable=[self.name])


CHECK = StateSchemaCheck()

__all__ = ["CHECK", "StateSchemaCheck"]
