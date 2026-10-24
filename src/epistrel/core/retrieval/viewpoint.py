"""The viewpoint filter every retrieval arm composes (FR-RET-1, FR-EPI-5,12; docs/02 §4.2; #159).

A retrieval for character A returns only what A may read: A's partition (its beliefs, the
``beliefs`` arm) and the memories and facts whose visibility admits A, ``objective``, ``public``
or a ``characters`` scope naming A (``core.visibility``: the scope alone decides, ADR-167). It
never returns another character's private memory (a ``think`` of B), a ``latent`` fact scoped
away from A, or B's beliefs, in any arm or tier: the occasion and assessment reads, the vector
arm (the predicate is in the HNSW query, which pgvector's iterative scan keeps filling), the
lexical arm over the hot and the retired tier (so a rehydrated row is filtered before it is
re-embedded), the alias arm, and canon (``KeyedLookup.canon_context``'s viewpoint).

``Viewpoint.parse`` reads #59's string: ``omniscient`` (everything; the narrator's and the
debugging read's), ``character:<id>``, or a reader that is no character (``narrator``,
``director``, ``player``: ``objective`` and ``public`` only; ``narrator`` is a requester of the
VISIBILITY property, never one ``retrieve()`` is asked for). The player's own character
(``characters.is_player``) reads as such a reader too: it has no partition and no private
memory (FR-EPI-12), so ``character:<player>`` sees ``objective`` and ``public`` records only.

**The gate.** The filter applies to memories only when the story's ``partitions`` module is on
(``module_on(settings, "partitions")``, #129; delivered by #181). With it off (shared knowledge,
every Lite story and M4's structured profile) memories are not filtered, as in M4, and canon
keeps its own visibility filter, so a ``characters``-scoped ``latent`` fact is still read only by
the characters it names. ``scope_for`` answers the effective filter: ``None`` (no filter) or
the viewpoint string ``core.visibility.visible_clause`` reads; ``scope_sql``/``scope_params``
are the same predicate as a raw SQL fragment for the arms written in SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT
from epistrel.core.visibility import READER_VIEWPOINTS, SHARED_SCOPES

NARRATOR = "narrator"
SHARED_READER = "player"
"""The viewpoint string that reads ``objective`` and ``public`` only (a reader, never a
character)."""

ViewpointKind = Literal["omniscient", "character", "reader"]


@dataclass(frozen=True, slots=True)
class Viewpoint:
    """A parsed viewpoint: ``omniscient``, a ``character`` (with its id), or a ``reader``
    (``narrator``, ``director``, ``player``)."""

    kind: ViewpointKind
    character_id: str | None = None
    reader: str | None = None

    @classmethod
    def parse(cls, value: str) -> Viewpoint:
        """#59's viewpoint string as a ``Viewpoint``; anything that is neither ``omniscient``
        nor ``character:<id>`` reads as a reader (shared scopes only: fail closed)."""
        text = value.strip()
        if text == OMNISCIENT:
            return cls("omniscient")
        if text.startswith(CHARACTER_VIEWPOINT_PREFIX) and len(text) > len(
            CHARACTER_VIEWPOINT_PREFIX
        ):
            return cls("character", character_id=text.removeprefix(CHARACTER_VIEWPOINT_PREFIX))
        return cls("reader", reader=text if text in READER_VIEWPOINTS else SHARED_READER)

    @property
    def omniscient(self) -> bool:
        return self.kind == "omniscient"

    def string(self) -> str:
        if self.kind == "omniscient":
            return OMNISCIENT
        if self.kind == "character":
            return f"{CHARACTER_VIEWPOINT_PREFIX}{self.character_id}"
        return self.reader or SHARED_READER


def scope_for(viewpoint: Viewpoint, *, partitions: bool, player: bool = False) -> str | None:
    """The memory filter of ``viewpoint`` as a ``visible_clause`` viewpoint string: ``None``
    (no filter: omniscient, or the ``partitions`` module off), the character's own viewpoint,
    or ``player`` (shared scopes only: a reader, or the player's character)."""
    if viewpoint.omniscient or not partitions:
        return None
    if viewpoint.kind == "character" and not player:
        return viewpoint.string()
    return SHARED_READER


def admits(scope: str | None, visibility: dict[str, Any]) -> bool:
    """``scope_for``'s filter for one stored visibility document, in Python (the VISIBILITY
    property's in-memory reads)."""
    if scope is None:
        return True
    kind = visibility.get("scope")
    if kind in SHARED_SCOPES:
        return True
    if kind != "characters" or not scope.startswith(CHARACTER_VIEWPOINT_PREFIX):
        return False
    return scope.removeprefix(CHARACTER_VIEWPOINT_PREFIX) in (visibility.get("characters") or ())


def scope_sql(alias: str) -> str:
    """``scope_for``'s filter as a SQL fragment over the row aliased ``alias`` (bound by
    ``scope_params``): every row while ``:scope_on`` is false."""
    shared = ", ".join(f"'{s}'" for s in SHARED_SCOPES)
    a = alias
    return (
        f"(NOT CAST(:scope_on AS boolean) OR {a}.visibility->>'scope' IN ({shared}) "
        f"OR (CAST(:scope_member AS text) IS NOT NULL "
        f"AND {a}.visibility->>'scope' = 'characters' "
        f"AND ({a}.visibility->'characters') ? CAST(:scope_member AS text)))"
    )


def scope_params(scope: str | None) -> dict[str, Any]:
    """The bind parameters of ``scope_sql`` for ``scope_for``'s filter."""
    member = (
        scope.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
        if scope is not None and scope.startswith(CHARACTER_VIEWPOINT_PREFIX)
        else None
    )
    return {"scope_on": scope is not None, "scope_member": member}


__all__ = [
    "NARRATOR",
    "SHARED_READER",
    "Viewpoint",
    "ViewpointKind",
    "admits",
    "scope_for",
    "scope_params",
    "scope_sql",
]
