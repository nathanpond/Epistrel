"""The seed check (FR-SCN-5,8; #111): every knowledge seed declares its holder, its audience and
its disclosure. A draft may leave them out (#108); a published seed may not.

One error per missing field, its code naming the field (``seed_missing_holder``,
``seed_missing_audience``, ``seed_missing_disclosure``), at that field
(``cast.roles[0].seeded_memories[1].audience``), with the seed's server-assigned id and, for a
role's seed, the role. Only seed items need the three: the world's seeded memories, each role's,
and each planned reveal's truth (``ScenarioDraft.seeds``). An audience token that is not a
level names a role or a named group, which FR-SCN-8 lets the author add freely, so no token is
refused; a seed's holder is checked against the roles when the draft is saved (#108).
"""

from __future__ import annotations

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.scenario import draft_of, seeds_at
from epistrel.core.catalog.report import Outcome

SEED_FIELDS = ("holder", "audience", "disclosure")
RULE = "a published seed declares its holder, audience and disclosure (FR-SCN-8)"


class SeedsCheck(Check):
    """See the module docstring."""

    name = "seeds"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        found = Findings()
        for at in seeds_at(draft_of(subject)):
            for name in SEED_FIELDS:
                if getattr(at.seed, name) is None:
                    found.entries.append(
                        subject.entry(
                            f"seed_missing_{name}",
                            Outcome.rejected,
                            field=f"{at.field}.{name}",
                            role=at.role,
                            seed_id=at.seed.id,
                            rule=RULE,
                        )
                    )
        return found


CHECK = SeedsCheck()

__all__ = ["CHECK", "SEED_FIELDS", "SeedsCheck"]
