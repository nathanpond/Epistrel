"""The eligibility check (FR-SCN-3,5; #111): the character of every ``locked`` and ``default`` role
passes the role's eligibility filters. One ``eligibility_failed`` error per failing filter, at
that filter's field (``cast.roles[i].eligibility.labels``, ``….content_ratings``,
``….age_range``), naming the character version in ``source``:

- **labels**: the character holds every label the role lists (labels compare lower-cased);
- **content ratings**: when the role lists any, the character's rating is one of them, by the
  rating's id, so a retired rating still matches;
- **age range**: the character's true age (FR-CAT-12) is within ``[min, max]``, both ends
  inclusive and either open.

A filter the character cannot be judged by (it has no labels, rating or true age) fails. A role
whose character is missing or unpublished is the role-character check's to report.
"""

from __future__ import annotations

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import FilledRole, filled
from epistrel.core.catalog.checks.character_documents import labels_of, true_age_of
from epistrel.core.catalog.checks.scenario import role_field
from epistrel.core.catalog.report import Entry, Outcome
from epistrel.core.catalog.scenario_models import RoleEligibility


def _failed(subject: Subject, member: FilledRole, filter_name: str, rule: str) -> Entry:
    return subject.entry(
        "eligibility_failed",
        Outcome.rejected,
        field=role_field(member.index, "eligibility", filter_name),
        role=member.role.id,
        source=member.source,
        rule=rule,
    )


def _range(value: tuple[int | None, int | None]) -> str:
    youngest, oldest = value
    low = "any" if youngest is None else str(youngest)
    high = "any" if oldest is None else str(oldest)
    return f"{low} to {high}"


def eligibility_failures(
    subject: Subject, member: FilledRole, eligibility: RoleEligibility
) -> list[Entry]:
    """The entries of each filter ``member``'s character fails (see the module docstring)."""
    document = member.document
    failed: list[Entry] = []
    missing = sorted(set(eligibility.labels) - labels_of(document))
    if missing:
        failed.append(
            _failed(
                subject,
                member,
                "labels",
                "eligibility: the character holds every label the role lists (missing: "
                + ", ".join(missing)
                + ")",
            )
        )
    if eligibility.content_ratings and member.rating_id not in eligibility.content_ratings:
        held = "none" if member.rating_id is None else member.rating_id
        failed.append(
            _failed(
                subject,
                member,
                "content_ratings",
                "eligibility: the character's content rating is one the role accepts (the "
                f"character's: {held})",
            )
        )
    if eligibility.age_range is not None:
        youngest, oldest = eligibility.age_range
        age = true_age_of(document)
        if (
            age is None
            or (youngest is not None and age < youngest)
            or (oldest is not None and age > oldest)
        ):
            failed.append(
                _failed(
                    subject,
                    member,
                    "age_range",
                    "eligibility: the character's true age is within the role's age range "
                    f"({_range(eligibility.age_range)}, inclusive)",
                )
            )
    return failed


class EligibilityCheck(Check):
    """See the module docstring."""

    name = "eligibility"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        found = Findings()
        for member in await filled(subject, ctx):
            eligibility = member.role.eligibility
            if eligibility is None or member.version is None:
                continue
            found.entries.extend(eligibility_failures(subject, member, eligibility))
        return found


CHECK = EligibilityCheck()

__all__ = ["CHECK", "EligibilityCheck", "eligibility_failures"]
