"""The starting-cast limit check (FR-SCN-4,5; #111): every role fits the effective limit.

The effective limit is the scenario's ``max_characters_at_start``, else the deployment's
``max_characters_at_start_default``, capped by its ``max_characters_at_start_ceiling``
(``effective_cast_limit``). Every role counts, the player's and the optional ones included: a
story may fill them all at creation. More roles than the limit is ``cast_limit_exceeded`` at
``cast.roles``; a scenario value above a ceiling lowered since it was saved is
``cast_limit_exceeded`` at ``cast.max_characters_at_start`` (a save refuses such a value, and
validation catches the one stored before the ceiling came down). Both are errors.
"""

from __future__ import annotations

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.report import Outcome
from epistrel.core.catalog.scenario_models import CastLimits, effective_cast_limit


class CastLimitCheck(Check):
    """See the module docstring."""

    name = "cast_limit"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        draft = draft_of(subject)
        limits = CastLimits.of(ctx.settings)
        own = draft.cast.max_characters_at_start
        limit = effective_cast_limit(own, None, limits)
        found = Findings()
        roles = len(draft.cast.roles)
        if roles > limit:
            found.entries.append(
                subject.entry(
                    "cast_limit_exceeded",
                    Outcome.rejected,
                    field="cast.roles",
                    rule=f"a story starts with at most {limit} characters, player and optional "
                    f"roles included (FR-SCN-4); this scenario has {roles} roles",
                )
            )
        if own is not None and limits.ceiling is not None and own > limits.ceiling:
            found.entries.append(
                subject.entry(
                    "cast_limit_exceeded",
                    Outcome.rejected,
                    field="cast.max_characters_at_start",
                    rule=f"max_characters_at_start may not exceed the deployment's ceiling of "
                    f"{limits.ceiling}",
                )
            )
        return found


CHECK = CastLimitCheck()

__all__ = ["CHECK", "CastLimitCheck"]
