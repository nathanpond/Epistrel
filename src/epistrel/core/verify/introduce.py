"""New places and objects: the ids a turn mints for things no entity holds yet (FR-VERIFY-11,
FR-PROP-6; docs/02 §4.9; #144, #479).

A render may introduce incidental scene texture (FR-VERIFY-11): "the inn", "a cracked window".
When a claim names a place or object that no entity holds, ``normalize`` asks the turn's
``Introductions`` for an id instead of answering ``uncertain(unresolved_entity)``. The claim
then normalizes, is checked like any other (``novel`` on first mention), and passes
#144's permission check. Only a permitted claim that is canonized creates the entity:
``core.canon.canonize`` receives the minted ids its claim uses (``introduces``) and writes each
one, with this id, in the turn's commit. From then on the entity is in the story's index, so
later claims about it resolve to it and are checked against it (a contradiction is caught like
any other). A denied, uncertain or repaired claim creates nothing. Its minted id lives only in
this turn's memory and in the turn's own records of the claim.

**When an id is minted** (``Introductions.resolve``, the guard rails of the owner's decision on
#479), all of the following hold:

- **Nothing the story holds matches.** ``normalize`` asks only after #139's resolver found no
  entity for the argument in the story's index: no mention at all (``no_mention``), or a
  capitalized name that matches no alias (``unknown_name``). An ambiguous name, a pronoun
  with no antecedent and a relational description that found nobody are never minted.
- **The slot is a place or object.** The predicate's argument type on that side is ``place``
  or ``item`` (``MINTED_KINDS``). Literal slots (quantities, durations, dates), ``person``,
  ``organization``, ``any`` and unregistered predicates (no argument types) never mint.
- **Not an indefinite.** "something", "a mug", "some ale" leave the argument open
  (``normalize.open_phrase``, FR-COMMIT-5); they are never minted.
- **Reuse within the turn.** The name is first resolved, with the same resolver, against the
  entities this turn has already minted. A hit of the same kind reuses that id. A hit of
  another kind, or an ambiguous one, mints nothing. So "the inn" twice in one turn is one
  entity, and so is "the old inn" after "the inn" (a descriptor match).

The name is the claim's text with underscores read as spaces (an extraction model writes
``the_inn``; ``entity_name``) and surrounding quotes and punctuation stripped. Paraphrases
that share no word ("the inn" and "Crossroads Inn") still mint two entities. Merging them is
later work (#481).

Nothing here reads the database or calls a model. Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from epistrel.core.canon.canonize import NewEntity
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import Proposition
from epistrel.core.resolution.entities import (
    EntityEntry,
    EntityIndex,
    ResolutionContext,
    descriptors_of,
    resolve_reference,
)
from epistrel.core.verify.normalize import open_phrase

MINTED_KINDS: frozenset[str] = frozenset({"place", "item"})
"""The argument types whose unresolved argument mints an entity: a place or an object."""
MINTED_DETAILS: frozenset[str] = frozenset({"no_mention", "unknown_name"})
"""The resolver results that mean "no entity holds this name" (see the module docstring)."""

_EDGES = "\"'`.,;:!?()[]{} \t\n"
_SPACES = re.compile(r"\s+")


def entity_name(text: str) -> str:
    """``text`` as an entity name: underscores read as spaces, whitespace collapsed,
    surrounding quotes and punctuation stripped."""
    return _SPACES.sub(" ", text.replace("_", " ")).strip(_EDGES)


@dataclass(slots=True)
class Introductions:
    """One turn's minted ids (see the module docstring): shared by every normalization of
    the turn (declared, detected, extracted and the composed message's) and read at its
    commit."""

    minted: dict[str, EntityEntry] = field(default_factory=dict)
    _index: EntityIndex = field(default_factory=lambda: EntityIndex(()))

    def resolve(self, text: str, kind: str, detail: str, speaker: str) -> str | None:
        """The id for ``text`` in a ``kind`` slot that the story's index could not resolve
        (``detail``: the resolver's code): a minted one reused, a new one, or ``None`` when
        the guard rails say no (see the module docstring)."""
        if kind not in MINTED_KINDS or detail not in MINTED_DETAILS:
            return None
        name = entity_name(text)
        if not name or open_phrase(name) or not descriptors_of(name):
            return None
        if self.minted:
            mention = resolve_reference(name, speaker, ResolutionContext(self._index))
            if mention.entity_id is not None:
                found = self.minted.get(mention.entity_id)
                return found.id if found is not None and found.kind == kind else None
            if mention.uncertain() is not None and mention.candidates:
                return None  # ambiguous among this turn's own: never a guess
        entry = EntityEntry(new_ulid(), kind, name, descriptors=descriptors_of(name))
        self.minted[entry.id] = entry
        self._index = EntityIndex(self.minted.values())
        return entry.id

    def names(self) -> dict[str, str]:
        """Each minted id's name (a claim that names one renders with it)."""
        return {i: e.canonical_name for i, e in self.minted.items()}

    def introduces(self, claim: Proposition) -> tuple[NewEntity, ...]:
        """The minted entities ``claim`` names, in argument order: what its canonization
        creates (``core.canon.canonize``'s ``introduces``)."""
        ids = [claim.subject_id, claim.object_id, *(claim.object_set or ())]
        ids += [v for v in claim.qualifiers.values() if isinstance(v, str)]
        found = [self.minted[i] for i in dict.fromkeys(i for i in ids if i in self.minted)]
        return tuple(NewEntity(e.id, e.canonical_name) for e in found)


__all__ = ["MINTED_DETAILS", "MINTED_KINDS", "Introductions", "entity_name"]
