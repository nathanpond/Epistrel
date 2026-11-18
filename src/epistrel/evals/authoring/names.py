"""The fictional names the generated E-12 intervention sets may use (#938).

Every person a generated intervention case names is S-COAT's cast (``scenario_play.PLAYER``,
``RENDERER``) or one of these: invented given names and plain role descriptions, none of them a
real person. ``authoring.lint`` refuses any other introduction name.
"""

from __future__ import annotations

GIVEN = (
    "Ansel Marrow",
    "Briony Tallis-Fenn",
    "Corwin Ashdale",
    "Delphine Oakhurst",
    "Edric Vantome",
    "Fenna Mallory",
    "Garrick Pennwhistle",
    "Hesper Quillon",
    "Ivo Brackenridge",
    "Juno Halloway",
    "Keir Dunmoor",
    "Lettice Farrow",
    "Merrick Thorne",
    "Nell Ashgrove",
    "Orrin Caldermoor",
    "Perpetua Wynn",
)
"""Invented full names (adults only, by the cases that use them)."""

ROLES = (
    "a postman",
    "a courier",
    "a neighbour",
    "a plumber",
    "a delivery driver",
    "a meter reader",
    "a locksmith",
    "a window cleaner",
    "a chimney sweep",
    "a council inspector",
    "a removals man",
    "a taxi driver",
    "a carpenter",
    "a landlord's agent",
    "a florist",
    "an electrician",
)
"""Role descriptions an introduction may use instead of a name (adult occupations)."""

POOL: frozenset[str] = frozenset({*GIVEN, *ROLES})

__all__ = ["GIVEN", "POOL", "ROLES"]
