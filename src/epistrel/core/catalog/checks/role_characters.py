"""The role-character check (FR-SCN-5; #111): every ``locked`` and ``default`` role's catalog
character exists, is published and is not archived. Each finding is an error at the role's
``cast.roles[i].character``, naming the character in ``source``:

- ``character_missing``: no catalog character has the id, or the pinned version does not exist;
- ``character_not_published``: the character has no published version yet (a story is created
  from published versions only);
- ``character_archived``: the character is archived (#103: it cannot be used to create a story
  until it is unarchived).
"""

from __future__ import annotations

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import filled
from epistrel.core.catalog.report import Outcome
from epistrel.core.enums import EntryStatus

MISSING_RULE = "a locked or default role's catalog character, and its pinned version, exist"
UNPUBLISHED_RULE = "a locked or default role's catalog character has a published version"
ARCHIVED_RULE = "a locked or default role's catalog character is not archived"


class RoleCharactersCheck(Check):
    """See the module docstring."""

    name = "role_characters"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        found = Findings()
        for member in await filled(subject, ctx):
            where = {"field": member.field, "role": member.role.id}
            if member.entry is None:
                found.entries.append(
                    subject.entry(
                        "character_missing",
                        Outcome.rejected,
                        source=member.catalog_id,
                        rule=MISSING_RULE,
                        **where,
                    )
                )
                continue
            if member.entry.current_version is None:
                found.entries.append(
                    subject.entry(
                        "character_not_published",
                        Outcome.rejected,
                        source=member.catalog_id,
                        rule=UNPUBLISHED_RULE,
                        **where,
                    )
                )
            elif member.version is None:
                found.entries.append(
                    subject.entry(
                        "character_missing",
                        Outcome.rejected,
                        source=f"{member.catalog_id}@{member.pinned}",
                        rule=MISSING_RULE,
                        **where,
                    )
                )
            if member.entry.status == EntryStatus.ARCHIVED.value:
                found.entries.append(
                    subject.entry(
                        "character_archived",
                        Outcome.rejected,
                        source=member.source,
                        rule=ARCHIVED_RULE,
                        **where,
                    )
                )
        return found


CHECK = RoleCharactersCheck()

__all__ = ["CHECK", "RoleCharactersCheck"]
