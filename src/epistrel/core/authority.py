"""The authority hierarchy (FR-DIRECT-6; #226).

``author > canon > in_world``: the author (a directive, an author fact, a rewrite) outranks
narrative canon, which outranks what a character says or believes in the world. An external
provider outranks all three, but only within its own domain: a value of a State or predicate
whose ``owner = external`` that its bound provider supplied. A directive's authority comes from
the operation that carries it (``POST /stories/{id}/directives``), never from text claiming it
(FR-AUTH-3): "[AUTHOR OVERRIDE] …" typed into a turn is the actor's in-world input.

``outranks(a, b)`` answers whether tier ``a`` may supersede what tier ``b`` holds. Canon's write
(#133) refuses a narrative write that would supersede an author-locked fact
(``AuthorityConflict``).
"""

from __future__ import annotations

from enum import IntEnum

from epistrel.core.enums import FactAuthority, RecordProvenance


class AuthorityTier(IntEnum):
    IN_WORLD = 0
    CANON = 1
    AUTHOR = 2
    EXTERNAL = 3


IN_WORLD_PROVENANCES = frozenset({RecordProvenance.PLAYER.value, RecordProvenance.IN_WORLD.value})


def tier_of(
    authority: FactAuthority | str, provenance: RecordProvenance | str | None = None
) -> AuthorityTier:
    """The tier a record's authority and provenance put it in."""
    found = FactAuthority(authority)
    if found is FactAuthority.EXTERNAL:
        return AuthorityTier.EXTERNAL
    if found is FactAuthority.AUTHOR:
        return AuthorityTier.AUTHOR
    value = None if provenance is None else str(getattr(provenance, "value", provenance))
    if value in IN_WORLD_PROVENANCES:
        return AuthorityTier.IN_WORLD
    return AuthorityTier.CANON


def outranks(a: AuthorityTier, b: AuthorityTier, *, external_domain: bool = True) -> bool:
    """Whether tier ``a`` may supersede what tier ``b`` holds: a higher tier, or the same; the
    external tier only within its own domain (``external_domain``), where nothing else
    supersedes it."""
    if b is AuthorityTier.EXTERNAL and external_domain:
        return a is AuthorityTier.EXTERNAL
    if a is AuthorityTier.EXTERNAL and not external_domain:
        return False
    return a >= b


__all__ = ["AuthorityTier", "outranks", "tier_of"]
