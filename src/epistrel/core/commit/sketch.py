"""A thunk's proposition: a #140 sketch with one open argument (FR-COMMIT-5,10; #146). Pure.

A reference like "someone knocked at the door" names a proposition whose subject the world has
not decided. ``open_sketch`` turns such a ``ClaimSketch`` into a ``ThunkSketch``: the relation
resolved to a registered predicate (#140's ``predicate_for``), every argument but one resolved
to an entity (#139's ``resolve_reference``) or a literal, and the one left open recorded with
its field ``None`` and ``open_slot`` naming it (``subject``, ``object`` or ``literal``). There is
no entity id for an open slot, so an undetermined value has none (no anonymous entity is ever
made).

**The open-argument lexicon** (``OPEN_PHRASES``, ``open_phrase``): indefinite pronouns
(``someone``, ``somebody``, ``something``, ``anyone`` …) and indefinite noun phrases (``a
<noun>``, ``an <noun>``, ``some <noun>``, ``an unknown …``, ``a stranger``). It is applied to a
sketch's argument texts only, before resolution: an indefinite says the speaker does not
identify the thing, so "a stranger" is open even when the story has an entity named
"stranger". An object-taking predicate whose object text is missing is open in its object too.
A sketch with no open argument is no thunk (``None``: an ordinary claim); one with two, or with
an argument that neither resolves nor is indefinite, is ``Unnormalizable`` and stays texture.

**Matching** (``fills``): a proposition fills a thunk when it has the thunk's predicate and
polarity, the thunk's known arguments, and a value in the open slot. That is the whole test a
dependence (``core.commit.lifecycle.depend``) and the turn-time lookup (``core.commit.matching``)
apply; nothing here reads the database.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.enums import OpenSlot
from epistrel.core.propositions.model import NONE, Polarity, Proposition, TypedLiteral
from epistrel.core.propositions.registry import Registry
from epistrel.core.resolution.entities import ResolutionContext, resolve_reference
from epistrel.core.resolution.time import Position, TimeBasis, resolve_time, story_time_at
from epistrel.core.resolution.types import ResolvedTime
from epistrel.core.verify.normalize import (
    LITERAL_TYPES,
    ClaimSketch,
    Unnormalizable,
    parse_literal,
    predicate_for,
)
from epistrel.core.verify.rules import VerdictReason

OPEN_PHRASES: frozenset[str] = frozenset(
    {
        "someone",
        "somebody",
        "something",
        "somewhere",
        "anyone",
        "anybody",
        "anything",
        "a stranger",
        "a person",
        "a figure",
        "a voice",
        "an unknown",
    }
)
"""Indefinite pronouns and noun phrases that leave an argument open (see the module docstring);
``open_phrase`` also takes any ``a``/``an``/``some`` + noun phrase."""

_INDEFINITE = re.compile(r"^(?:a|an|some|an unknown|a mysterious|an unseen)\s+[^\W\d][\w' -]*$")

TEXT_MAX = 500
"""The longest argument text a sketch keeps."""


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def open_phrase(text: str | None) -> bool:
    """Whether ``text`` is an indefinite that leaves its argument open."""
    if not text:
        return False
    folded = _folded(text).strip(".,;:!?\"'")
    return folded in OPEN_PHRASES or bool(_INDEFINITE.match(folded))


class Candidate(BaseModel):
    """A possible value of the open slot (#148 reads them as protected content): an entity, or
    a text with no entity yet, and how likely it is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str | None = None
    text: str = Field(default="", max_length=TEXT_MAX)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ThunkSketch(BaseModel):
    """A thunk's proposition (stored whole as ``thunks.proposition_sketch``): the sketch's
    texts, the resolved predicate and known arguments, the open slot (its field ``None``), the
    candidates and the span it came from (``(beat, start, end)``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_text: str | None = Field(default=None, max_length=TEXT_MAX)
    relation_text: str = Field(default="", max_length=TEXT_MAX)
    object_text: str | None = Field(default=None, max_length=TEXT_MAX)
    time_text: str | None = Field(default=None, max_length=TEXT_MAX)
    predicate_id: str
    subject_id: str | None = None
    object_id: str | None = None
    literal: TypedLiteral | None = None
    polarity: Polarity = Polarity.AFFIRMED
    open_slot: OpenSlot
    candidates: tuple[Candidate, ...] = ()
    span: tuple[int, int, int] | None = None
    parent_fact_id: str | None = None
    """An incidental detail's thunk (FR-COMMIT-9, #154): the consequential fact whose object's
    attribute it leaves open (``core.commit.boundary.depend_on_detail``)."""

    def document(self) -> dict[str, object]:
        """The stored ``proposition_sketch``."""
        return self.model_dump(mode="json")

    @property
    def key(self) -> tuple[str, str | None, str | None, str]:
        """What a second sketch of the same reference dedupes on: the predicate, the known
        arguments and the open slot."""
        literal = None if self.literal is None else self.literal.model_dump_json()
        known = self.object_id if literal is None else literal
        return (self.predicate_id, self.subject_id, known, self.open_slot.value)

    def describe(self, names: dict[str, str] | None = None, predicate: str | None = None) -> str:
        """``<subject> <relation> <object>`` as said (an open slot by its own text, ``?`` when
        it has none; a known entity by its name in ``names``; the relation's text, else the
        predicate's name): the ``## Open threads`` line's text."""
        names = names or {}

        def arg(entity: str | None, text: str | None, slot: OpenSlot) -> str:
            if slot is self.open_slot:
                return text or "?"
            if entity is not None:
                return names.get(entity, text or "")
            return text or ""

        subject = arg(self.subject_id, self.subject_text, OpenSlot.SUBJECT)
        if self.literal is not None:
            obj = str(self.literal.value)
        elif self.open_slot is OpenSlot.LITERAL:
            obj = self.object_text or "?"
        else:
            obj = arg(self.object_id, self.object_text, OpenSlot.OBJECT)
        relation = self.relation_text or (predicate or "").replace("_", " ")
        return " ".join(p for p in (subject, relation, obj) if p)


def _entity(
    text: str | None, given: str | None, speaker: str, context: ResolutionContext
) -> str | None:
    if given is not None:
        return given
    if not text:
        return None
    return resolve_reference(text, speaker, context).entity_id


def valid_time_of(
    sketch: ClaimSketch, position: Position, basis: TimeBasis
) -> int | Unnormalizable:
    """The story-minute a reference is about: its time expression's start when it has one
    (#139), else the position's."""
    if not sketch.time_text:
        return story_time_at(position, basis)
    resolved = resolve_time(sketch.time_text, position, None, basis=basis)
    if not isinstance(resolved, ResolvedTime):
        return Unnormalizable(VerdictReason.UNRESOLVED_TIME, resolved.reason.code.value, resolved)
    found = resolved.valid_time
    start = getattr(found, "at", None)
    if start is None:
        bound = getattr(found, "start", None)
        start = None if bound is None else bound.outer
    return story_time_at(position, basis) if start is None else int(start)


def open_sketch(
    sketch: ClaimSketch,
    speaker: str,
    *,
    registry: Registry,
    context: ResolutionContext,
    candidates: Sequence[Candidate] = (),
    span: tuple[int, int, int] | None = None,
) -> ThunkSketch | Unnormalizable | None:
    """``sketch`` as a thunk's proposition said by ``speaker`` (see the module docstring):
    ``None`` when no argument is open, ``Unnormalizable`` when it cannot be one."""
    predicate = predicate_for(registry, sketch.relation_text)
    if predicate is None:
        return Unnormalizable(VerdictReason.UNRESOLVED_PREDICATE, "unregistered")
    opened: list[OpenSlot] = []
    subject_id: str | None = None
    if sketch.subject_id is None and open_phrase(sketch.subject_text):
        opened.append(OpenSlot.SUBJECT)
    else:
        subject_id = _entity(sketch.subject_text, sketch.subject_id, speaker, context)
        if subject_id is None:
            return Unnormalizable(VerdictReason.UNRESOLVED_ENTITY, "subject")
    object_id: str | None = None
    literal = sketch.literal
    if predicate.object_type == NONE:
        if sketch.object_text or sketch.object_id is not None or literal is not None:
            return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "takes_no_object")
    elif literal is not None:
        pass
    elif predicate.object_type in LITERAL_TYPES:
        if sketch.object_text and not open_phrase(sketch.object_text):
            literal = parse_literal(sketch.object_text, predicate.object_type)
            if literal is None:
                return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "literal_unparsed")
        else:
            opened.append(OpenSlot.LITERAL)
    elif sketch.object_id is None and (not sketch.object_text or open_phrase(sketch.object_text)):
        opened.append(OpenSlot.OBJECT)
    else:
        object_id = _entity(sketch.object_text, sketch.object_id, speaker, context)
        if object_id is None:
            return Unnormalizable(VerdictReason.UNRESOLVED_ENTITY, "object")
    if not opened:
        return None
    if len(opened) > 1:
        return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "two_open")
    if subject_id is None and object_id is None and literal is None:
        return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "nothing_known")

    def clip(text: str | None) -> str | None:
        return None if text is None else text[:TEXT_MAX]

    return ThunkSketch(
        subject_text=clip(sketch.subject_text) or None,
        relation_text=sketch.relation_text[:TEXT_MAX],
        object_text=clip(sketch.object_text),
        time_text=clip(sketch.time_text),
        predicate_id=predicate.id,
        subject_id=subject_id,
        object_id=object_id,
        literal=literal,
        polarity=sketch.polarity,
        open_slot=opened[0],
        candidates=tuple(candidates),
        span=span,
    )


def fills(sketch: ThunkSketch, claim: Proposition) -> bool:
    """Whether ``claim`` fills ``sketch``'s open slot with its known arguments unchanged (see
    the module docstring)."""
    if claim.predicate_id != sketch.predicate_id or claim.polarity is not sketch.polarity:
        return False
    slot = sketch.open_slot
    if slot is not OpenSlot.SUBJECT and claim.subject_id != sketch.subject_id:
        return False
    if slot is OpenSlot.OBJECT:
        return claim.object_id is not None
    if slot is OpenSlot.LITERAL:
        return claim.literal is not None
    if sketch.literal is not None:
        return claim.literal is not None and claim.object_key() == _literal_key(sketch, claim)
    return claim.object_id == sketch.object_id


def _literal_key(sketch: ThunkSketch, claim: Proposition) -> object:
    return claim.model_copy(update={"literal": sketch.literal}).object_key()


__all__ = [
    "OPEN_PHRASES",
    "TEXT_MAX",
    "Candidate",
    "ThunkSketch",
    "fills",
    "open_phrase",
    "open_sketch",
    "valid_time_of",
]
