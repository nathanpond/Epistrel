"""Attribution claims: a speaker reporting what someone believes or said (FR-BELIEF-15,
FR-TURN-6; #167).

"Bobby thinks it was Marcus" does not assert that it was Marcus: it asserts that Bobby believes
it. An attribution claim is the inner proposition P with a ``ClaimAttribution``: its holder (the
text and, once resolved, the character), its ``mode`` (``believes``, or ``said``: "Bobby *said*
it was Marcus", a belief about a speech act) and whether it is ``factive`` ("Jenni knows it was
George" also asserts P). Two sources mark one:

- **declared**: #150's beat schema (``PropositionSketch.attribution``, ``{holder, mode,
  factive}``);
- **detected**: #148's lexicon (``detectors/attribution.json``): ``cue(text, start)`` finds an
  attribution verb (thinks, believes, figured, reckons, knows, said, …) right before a detected
  claim's span in its clause, with the holder words before it ("Bobby", "I"). ``I`` is the
  speaker itself (a self-report of belief, "I figured it was the delivery guy").

The orchestrator's verifier checks an attribution claim against the speaker's own nested beliefs
(``core.epistemic.nested``), never as a first-person assertion of P. Pure: no model, no world
knowledge; it logs nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

if TYPE_CHECKING:
    from epistrel.core.resolution.entities import ResolutionContext

Mode = Literal["believes", "said"]
SELF = "self"
"""``holder_text`` of a self-report ("I figured …")."""

_CLAUSE = re.compile(r"[.,;:!?\n]")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


@dataclass(frozen=True, slots=True)
class ClaimAttribution:
    """Whom a claim attributes its proposition to (see the module docstring): ``holder_text``
    as said (``SELF`` for the speaker), ``holder_id`` the character it resolved to (``None``:
    not resolved), the ``mode`` and whether it is ``factive``."""

    holder_text: str
    mode: Mode = "believes"
    factive: bool = False
    holder_id: str | None = None

    def resolved(self, holder_id: str | None) -> ClaimAttribution:
        return replace(self, holder_id=holder_id)

    def payload(self) -> dict[str, Any]:
        return {"holder_id": self.holder_id, "mode": self.mode, "factive": self.factive}


@dataclass(frozen=True, slots=True)
class AttributionTable:
    verbs: dict[str, tuple[Mode, bool]]
    selves: frozenset[str]
    complementizers: frozenset[str]


@cache
def table() -> AttributionTable:
    """The English attribution table (``core/verify/detectors/attribution.json``)."""
    source = resources.files("epistrel.core.verify").joinpath("detectors/attribution.json")
    found = json.loads(source.read_text("utf-8"))
    return AttributionTable(
        verbs={
            str(v).casefold(): (str(e["mode"]), bool(e["factive"]))  # type: ignore[misc]
            for v, e in found["verbs"].items()
        },
        selves=frozenset(str(s).casefold() for s in found["self"]),
        complementizers=frozenset(str(c).casefold() for c in found["complementizers"]),
    )


def cue(text: str, start: int) -> ClaimAttribution | None:
    """The attribution a claim starting at ``start`` in ``text`` sits under: the clause before
    it ends in ``<holder> <attribution verb> [that]`` (``None``: no cue). The holder is the run
    of capitalized words right before the verb (a name), or ``SELF`` for "I"."""
    before = text[: max(0, start)]
    clause = _CLAUSE.split(before)[-1]
    words = _WORD.findall(clause)
    if not words:
        return None
    t = table()
    if words[-1].casefold() in t.complementizers:
        words = words[:-1]
    if len(words) < 2:
        return None
    verb = words[-1].casefold()
    if verb not in t.verbs:
        return None
    # The holder: the run of capitalized words (a name, "I") right before the verb.
    holder: list[str] = []
    for word in reversed(words[:-1]):
        if not word[0].isupper():
            break
        holder.insert(0, word)
    if not holder:
        return None
    mode, factive = t.verbs[verb]
    text_of = " ".join(holder).strip()
    if text_of.casefold() in t.selves:
        text_of = SELF
    return ClaimAttribution(text_of, mode, factive)


async def resolve_holder(
    conn: AsyncConnection,
    story_id: str,
    found: ClaimAttribution,
    speaker: str,
    context: ResolutionContext | None,
) -> ClaimAttribution:
    """``found`` with its holder resolved to a character of the story: the speaker for
    ``SELF``, a character id as given, else the character the holder words name (#139's
    ``resolve_reference`` in the speaker's view, then the entity's character); unresolved
    (``holder_id = None``) otherwise."""
    from epistrel.core.resolution.entities import resolve_reference
    from epistrel.core.tables import characters, entities

    if found.holder_text == SELF:
        return found.resolved(speaker)
    candidate: str | None = found.holder_text
    if context is not None:
        mention = resolve_reference(found.holder_text, speaker, context)
        candidate = mention.entity_id or found.holder_text
    if candidate is None:
        return found
    character = await conn.scalar(
        select(characters.c.id).where(
            characters.c.story_id == story_id, characters.c.id == candidate
        )
    )
    if character is None:
        character = await conn.scalar(
            select(entities.c.character_id).where(
                entities.c.story_id == story_id,
                entities.c.id == candidate,
                entities.c.character_id.is_not(None),
            )
        )
    return found.resolved(None if character is None else str(character))


__all__ = ["SELF", "AttributionTable", "ClaimAttribution", "Mode", "cue", "resolve_holder", "table"]
