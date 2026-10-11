"""The four seeded content ratings (FR-RATE-1): Everyone → Teen → Mature → Explicit.

Each inherits guardrails from the next less restrictive one. Migration ``0005_ratings`` inserts
these rows with the fixed head and version ids below, writes no ``config_changes`` rows, and
leaves the deployment's ``config_revision`` at 0. The policy sets are empty until the policy
store lands (#49). The text is starter prose for an operator to replace; once a database is
seeded, editing this module changes nothing in it — change seeds through a new migration.
"""

from __future__ import annotations

from dataclasses import dataclass

STARTER_NOTE = "Starter guidance — edit for your deployment."


@dataclass(frozen=True, slots=True)
class SeedRating:
    id: str
    version_id: str
    name: str
    display_order: int
    description: str
    prompt_guidance: str
    image_guidance: str
    inherits_from: tuple[str, ...]
    policy_ids: tuple[str, ...] = ()


EVERYONE_ID = "00000000000000000000RAT001"
TEEN_ID = "00000000000000000000RAT002"
MATURE_ID = "00000000000000000000RAT003"
EXPLICIT_ID = "00000000000000000000RAT004"

DEFAULT_RATING_ID = EVERYONE_ID
"""The seeded deployment default for a story created without a rating (FR-RATE-1)."""

SEED_RATINGS: tuple[SeedRating, ...] = (
    SeedRating(
        id=EVERYONE_ID,
        version_id="00000000000000000000RTV001",
        name="Everyone",
        display_order=10,
        description=(
            "Suitable for all ages. No sexual content, no graphic violence, no strong language. "
            + STARTER_NOTE
        ),
        prompt_guidance=(
            "Keep the story suitable for all ages: no sexual content, no graphic violence, and "
            "no strong language. " + STARTER_NOTE
        ),
        image_guidance="All-ages imagery only. " + STARTER_NOTE,
        inherits_from=(TEEN_ID,),
    ),
    SeedRating(
        id=TEEN_ID,
        version_id="00000000000000000000RTV002",
        name="Teen",
        display_order=20,
        description=(
            "Mild violence and mild language; romance without sexual content. " + STARTER_NOTE
        ),
        prompt_guidance=(
            "Mild violence and mild language are acceptable; romance stays non-sexual. "
            + STARTER_NOTE
        ),
        image_guidance="No nudity and no gore. " + STARTER_NOTE,
        inherits_from=(MATURE_ID,),
    ),
    SeedRating(
        id=MATURE_ID,
        version_id="00000000000000000000RTV003",
        name="Mature",
        display_order=30,
        description=(
            "Strong violence, strong language and mature themes; no explicit sexual content. "
            + STARTER_NOTE
        ),
        prompt_guidance=(
            "Strong violence, strong language and mature themes are acceptable; sexual content "
            "is not explicit. " + STARTER_NOTE
        ),
        image_guidance="No explicit sexual imagery. " + STARTER_NOTE,
        inherits_from=(EXPLICIT_ID,),
    ),
    SeedRating(
        id=EXPLICIT_ID,
        version_id="00000000000000000000RTV004",
        name="Explicit",
        display_order=40,
        description=(
            "Explicit adult content between adults, within the deployment's global policies. "
            + STARTER_NOTE
        ),
        prompt_guidance=(
            "Explicit adult content between adults is acceptable within the deployment's global "
            "policies. " + STARTER_NOTE
        ),
        image_guidance="Adult imagery within the deployment's global policies. " + STARTER_NOTE,
        inherits_from=(),
    ),
)

SEED_PRINCIPAL = "system"
"""``created_by`` of the seeded versions (the workers' principal, ``core.context``)."""
