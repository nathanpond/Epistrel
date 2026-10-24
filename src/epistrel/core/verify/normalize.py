"""Claims become propositions, or they stay ``uncertain`` (FR-PROP-6,9,12; docs/02 §4.9; #140).

A detected or declared claim arrives as a ``ClaimSketch``: subject, relation and object as text
(or as ids a detector already resolved), a typed literal, a time expression, a polarity and the
span it came from. #148's detectors and the declared turn structure produce sketches.
``normalize`` turns one into a ``Proposition`` or an ``Unnormalizable`` with its reason, and
never guesses (FR-PROP-12):

- **Entities** go through #139's ``resolve_reference`` with the speaker's viewpoint. An
  ambiguous or unknown mention is ``unresolved_entity``. Given the turn's ``Introductions``
  (``introduce``; #479), an argument that names nothing the story holds, in a ``place`` or
  ``item`` slot of a registered predicate, takes a minted id instead (``core.verify.introduce``:
  the entity is created only if the claim is canonized). An unresolved argument of a weak
  relation (unregistered, or ``auto``: FR-PROP-4 (c)) is marked ``weak_relation``, with
  ``touches_character`` when another argument resolved to a modeled character other than the
  speaker; ``Unnormalizable.untracked_weak`` is what #479's surfacing reads (ADR-163, ADR-165).
- **The predicate** is the registry's predicate of that name (``registry.auto_name`` of the
  relation text). Failing that, ``RELATION_ALIASES`` maps everyday relation names to core
  predicates ("lives in" → ``located_at``, "has a sister" → ``sibling_of``). An unregistered
  relation on a **declared** claim answers ``RegisterPredicate``: the caller registers it with
  the weakest semantics (#131's ``auto_register``, in its own transaction) and normalizes again,
  which is what ``normalize_declared`` does. On a detected claim it is ``unresolved_predicate``.
- **Argument types**: each entity's kind must be the predicate's subject or object type
  (``any`` admits every kind), a literal must parse to the object type, an object-less
  predicate takes no object, and the qualifiers must be the declared ones. Anything else is
  ``argument_type``. A literal object said as an indefinite (``open_phrase``: "something",
  "some colour") is ``argument_type`` too (``open_literal``, #453): it leaves the value open
  (FR-COMMIT-5), so it is never parsed into a literal. A salient one becomes a thunk through
  ``core.commit.sketch.open_sketch``; otherwise it is texture.
- **Time**: a time expression goes through #139's ``resolve_time`` against the claim's
  position, the story's ``TimeBasis`` and the subject's timeline. With no expression, the
  default comes from the predicate's temporal kind: an ``event`` is an instant at the position's
  minute; a ``fluent`` or ``permanent`` predicate holds from the position on. An expression that
  does not resolve, or an empty interval (no ``[t, t)`` is ever built), is ``unresolved_time``.

The result is in the predicate's canonical direction (``registry.canonical``). Nothing here
reads ``facts`` or ``propositions``, and nothing calls a model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.errors import InvalidRequest
from epistrel.core.propositions.model import (
    ANY,
    NONE,
    Granularity,
    Instant,
    Interval,
    InvalidValidTime,
    Layer,
    LiteralKind,
    Polarity,
    Predicate,
    Proposition,
    QualifierValue,
    TemporalKind,
    TypedLiteral,
)
from epistrel.core.propositions.registry import (
    Registry,
    auto_name,
    auto_register,
    canonical,
    load,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.entities import ResolutionContext, resolve_reference
from epistrel.core.resolution.time import (
    Position,
    SubjectTimeline,
    TimeBasis,
    resolve_time,
    story_time_at,
)
from epistrel.core.resolution.types import Reason, ReasonCode, ResolvedTime, Uncertain
from epistrel.core.verify.rules import Verdict, VerdictReason, uncertain

if TYPE_CHECKING:
    from epistrel.core.verify.introduce import Introductions

RELATION_ALIASES: Mapping[str, str] = {
    "lives_in": "located_at",
    "lived_in": "located_at",
    "lives_at": "located_at",
    "is_in": "located_at",
    "is_at": "located_at",
    "was_in": "located_at",
    "was_at": "located_at",
    "is_located_in": "located_at",
    "has_sister": "sibling_of",
    "has_brother": "sibling_of",
    "has_sibling": "sibling_of",
    "sister_of": "sibling_of",
    "brother_of": "sibling_of",
    "is_married_to": "married_to",
    "married": "married_to",
    "is_dead": "dead",
    "died": "dead",
    "is_alive": "alive",
    "was_born": "born",
    "born_on": "born",
    "has": "holds",
    "carries": "holds",
    "is_holding": "holds",
    "works_for": "employed_by",
    "works_at": "employed_by",
    "is_employed_by": "employed_by",
    "studied_at": "enrolled_at",
    "attends": "enrolled_at",
    "is_enrolled_at": "enrolled_at",
    "is_member_of": "member_of",
    "plays_for": "member_of",
    "is_friends_with": "friend_of",
    "friends_with": "friend_of",
    "is_named": "name",
    "is_called": "name",
    "mother_of": "parent_of",
    "father_of": "parent_of",
    "son_of": "child_of",
    "daughter_of": "child_of",
}
"""Relation names (``registry.auto_name`` forms) that mean a core predicate. #149 maps a role's
relationship names through it; an author predicate is matched by its exact name."""


@dataclass(frozen=True, slots=True)
class ClaimSketch:
    """A claim before normalization: see the module docstring. ``subject_id``/``object_id``
    stand for already-resolved arguments; ``declared`` marks a claim of the declared turn
    structure (its unregistered relation auto-registers)."""

    subject_text: str = ""
    relation_text: str = ""
    object_text: str | None = None
    literal: TypedLiteral | None = None
    time_text: str | None = None
    polarity: Polarity = Polarity.AFFIRMED
    qualifiers: Mapping[str, QualifierValue] = field(default_factory=dict)
    span: tuple[int, int] | None = None
    declared: bool = False
    subject_id: str | None = None
    object_id: str | None = None
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class Unnormalizable:
    """A claim that cannot become a proposition: its ``reason`` (a ``VerdictReason``), a short
    ``detail`` (a code, no story text) and the resolver's ``Uncertain`` when one said so."""

    reason: VerdictReason
    detail: str = ""
    resolution: Uncertain | None = None
    weak_relation: bool = False
    """#479: an argument of a weak relation (unregistered, or auto-registered with the weakest
    semantics, FR-PROP-4 (c)) named nothing the story tracks (``untracked_weak``)."""
    touches_character: bool = False
    """#479: another argument resolved to a modeled character (or the player's) other than
    the speaker."""

    @property
    def untracked_weak(self) -> bool:
        """Whether this is uncertain only because a weak relation names something no entity
        holds, with no other character involved: the claim #479's surfacing applies to
        (``orchestrator.verify.extraction``). "No entity holds it" is the resolver finding
        nothing: no mention, a name no alias matches, or a pronoun with no antecedent; never
        an ambiguous mention, which has candidates."""
        found = None if self.resolution is None else self.resolution
        return (
            self.weak_relation
            and not self.touches_character
            and self.reason is VerdictReason.UNRESOLVED_ENTITY
            and found is not None
            and not found.candidates
            and _untracked(found.reason)
        )

    def verdict(self) -> Verdict:
        """The ``uncertain`` verdict the claim gets, never a guess."""
        return uncertain(self.reason)


@dataclass(frozen=True, slots=True)
class RegisterPredicate:
    """The caller registers ``relation`` as an auto predicate (#131) with these argument types,
    in its transaction, then normalizes again."""

    relation: str
    subject_type: str = ANY
    object_type: str = ANY


Normalized = Proposition | Unnormalizable | RegisterPredicate

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?\d+\.\d+$")
_QUANTITY = re.compile(r"^(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>[^\W\d]\w*)$")
_BOOLS = {"true": True, "yes": True, "false": False, "no": False}


def predicate_for(registry: Registry, relation: str) -> Predicate | None:
    """The registered predicate ``relation`` names: by exact name, then ``RELATION_ALIASES``."""
    try:
        name = auto_name(relation)
    except InvalidRequest:
        return None
    found = registry.named(name)
    if found is not None:
        return found
    alias = RELATION_ALIASES.get(name)
    return None if alias is None else registry.get(CORE_BY_NAME[alias].id)


def _entity(
    text: str | None, given: str | None, speaker: str, context: ResolutionContext
) -> str | Unnormalizable:
    if given is not None:
        return given
    if not text:
        return Unnormalizable(VerdictReason.UNRESOLVED_ENTITY, "no_mention")
    mention = resolve_reference(text, speaker, context)
    if mention.entity_id is not None:
        return mention.entity_id
    found = mention.uncertain()
    detail = "no_mention" if found is None else found.reason.code.value
    return Unnormalizable(VerdictReason.UNRESOLVED_ENTITY, detail, found)


def _untracked(reason: Reason) -> bool:
    """The resolver found nothing for the argument (see ``Unnormalizable.untracked_weak``)."""
    if reason.code is ReasonCode.UNRESOLVED_ENTITY:
        return reason.detail == "no_mention"
    return reason.code in (ReasonCode.UNKNOWN_NAME, ReasonCode.NO_ANTECEDENT)


def _introduced(
    failed: Unnormalizable,
    text: str | None,
    given: str | None,
    kind: str,
    speaker: str,
    introduce: Introductions | None,
) -> str | Unnormalizable:
    """An unresolved argument's minted id (``core.verify.introduce``, #479), else ``failed``."""
    if introduce is None or given is not None or not text:
        return failed
    # The resolver's own reason: ``no_mention`` (nothing named) or ``unknown_name`` may mint.
    reason = None if failed.resolution is None else failed.resolution.reason
    detail = failed.detail
    if reason is not None:
        detail = reason.detail if reason.code is ReasonCode.UNRESOLVED_ENTITY else reason.code
    found = introduce.resolve(text, kind, detail, speaker)
    return failed if found is None else found


def _other_character(found: str, speaker: str, context: ResolutionContext) -> bool:
    entry = context.index.get(found)
    if entry is None or entry.character_id is None or found == speaker:
        return False
    if speaker == "narrator" and entry.character_id in context.composing:
        return False  # #484: a composed render's own character is not "another" here
    return not (speaker == "player" and found == context.player_id)


def _weak(
    failed: Unnormalizable,
    predicate: Predicate | None,
    resolved: tuple[str | None, ...],
    speaker: str,
    context: ResolutionContext,
) -> Unnormalizable:
    """``failed`` marked for #479 when the relation is weak (see ``Unnormalizable``)."""
    if predicate is not None and predicate.layer is not Layer.AUTO:
        return failed
    touches = any(r is not None and _other_character(r, speaker, context) for r in resolved)
    return replace(failed, weak_relation=True, touches_character=touches)


def _kind_ok(expected: str, kind: str | None) -> bool:
    return expected == ANY or kind is None or kind == expected


def parse_literal(text: str, kind: str) -> TypedLiteral | None:
    """``text`` as a literal of ``kind`` (an argument type), or ``None`` when it does not
    parse. A quantity is ``<number> <unit>``; a bare number for ``any`` is an ``int`` or a
    ``float``."""
    value = text.strip()
    try:
        if kind in (LiteralKind.INT, LiteralKind.DURATION) and _INT.match(value):
            return TypedLiteral(kind=LiteralKind(kind), value=int(value))
        if kind == LiteralKind.FLOAT and (_INT.match(value) or _FLOAT.match(value)):
            return TypedLiteral(kind=LiteralKind.FLOAT, value=float(value))
        if kind == LiteralKind.QUANTITY:
            match = _QUANTITY.match(value)
            if match is None:
                return None
            number = match.group("value")
            amount: int | float = int(number) if _INT.match(number) else float(number)
            return TypedLiteral(
                kind=LiteralKind.QUANTITY, value=amount, unit=match.group("unit").casefold()
            )
        if kind == LiteralKind.BOOL and value.casefold() in _BOOLS:
            return TypedLiteral(kind=LiteralKind.BOOL, value=_BOOLS[value.casefold()])
        if kind == LiteralKind.DATE:
            date.fromisoformat(value)
            return TypedLiteral(kind=LiteralKind.DATE, value=value)
        if kind == LiteralKind.STR and value:
            return TypedLiteral(kind=LiteralKind.STR, value=value)
        if kind == ANY and _INT.match(value):
            return TypedLiteral(kind=LiteralKind.INT, value=int(value))
        if kind == ANY and _FLOAT.match(value):
            return TypedLiteral(kind=LiteralKind.FLOAT, value=float(value))
    except ValueError:
        return None
    return None


LITERAL_TYPES = frozenset(k.value for k in LiteralKind)

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


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def open_phrase(text: str | None) -> bool:
    """Whether ``text`` is an indefinite that leaves its argument open."""
    if not text:
        return False
    folded = _folded(text).strip(".,;:!?\"'")
    return folded in OPEN_PHRASES or bool(_INDEFINITE.match(folded))


def default_time(predicate: Predicate, position: Position, basis: TimeBasis) -> Interval | Instant:
    """The valid-time of a claim that states none (see the module docstring)."""
    now = story_time_at(position, basis)
    if predicate.temporal_kind is TemporalKind.EVENT:
        return Instant.of(now, Granularity.MINUTE)
    return Interval.of(now, None)


def normalize(
    sketch: ClaimSketch,
    speaker: str,
    position: Position,
    *,
    registry: Registry,
    context: ResolutionContext,
    basis: TimeBasis,
    subject_timeline: SubjectTimeline | None = None,
    introduce: Introductions | None = None,
) -> Normalized:
    """``sketch`` as a proposition said by ``speaker`` (an entity id, ``narrator`` or
    ``player``) at ``position`` (see the module docstring). With ``introduce``, an unresolved
    place or object argument takes a minted id (``core.verify.introduce``)."""
    subject = _entity(sketch.subject_text, sketch.subject_id, speaker, context)
    predicate = predicate_for(registry, sketch.relation_text)
    if isinstance(subject, Unnormalizable):
        other: str | Unnormalizable | None = None
        if sketch.object_text or sketch.object_id is not None:
            other = _entity(sketch.object_text, sketch.object_id, speaker, context)
        bound = (other if isinstance(other, str) else None,)
        if predicate is None:
            return _weak(subject, predicate, bound, speaker, context)
        subject = _introduced(
            subject,
            sketch.subject_text,
            sketch.subject_id,
            predicate.subject_type,
            speaker,
            introduce,
        )
        if isinstance(subject, Unnormalizable):
            return _weak(subject, predicate, bound, speaker, context)
    has_object = bool(sketch.object_text) or sketch.object_id is not None
    if predicate is None:
        if not sketch.declared:
            return Unnormalizable(VerdictReason.UNRESOLVED_PREDICATE, "unregistered")
        object_kind = ANY
        if sketch.literal is not None:
            object_kind = sketch.literal.kind.value
        elif has_object:
            found = _entity(sketch.object_text, sketch.object_id, speaker, context)
            if isinstance(found, Unnormalizable):
                return _weak(found, predicate, (subject,), speaker, context)
            entry = context.index.get(found)
            object_kind = ANY if entry is None else entry.kind
        else:
            object_kind = NONE
        entry = context.index.get(subject)
        try:
            auto_name(sketch.relation_text)
        except InvalidRequest:
            return Unnormalizable(VerdictReason.UNRESOLVED_PREDICATE, "unnamed_relation")
        return RegisterPredicate(
            sketch.relation_text, ANY if entry is None else entry.kind, object_kind
        )
    subject_entry = context.index.get(subject)
    if not _kind_ok(predicate.subject_type, None if subject_entry is None else subject_entry.kind):
        return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "subject_type")
    object_id: str | None = None
    literal: TypedLiteral | None = sketch.literal
    if predicate.object_type == NONE:
        if has_object or literal is not None:
            return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "takes_no_object")
    elif literal is not None:
        if predicate.object_type not in (ANY, literal.kind.value):
            return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "literal_type")
    elif predicate.object_type in LITERAL_TYPES:
        if open_phrase(sketch.object_text):  # #453: an undetermined value has none
            return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "open_literal")
        literal = parse_literal(sketch.object_text or "", predicate.object_type)
        if literal is None:
            return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "literal_unparsed")
    elif has_object:
        found = _entity(sketch.object_text, sketch.object_id, speaker, context)
        if isinstance(found, Unnormalizable):
            found = _introduced(
                found,
                sketch.object_text,
                sketch.object_id,
                predicate.object_type,
                speaker,
                introduce,
            )
        if isinstance(found, Unnormalizable):
            parsed = (
                parse_literal(sketch.object_text or "", ANY)
                if predicate.object_type == ANY and sketch.object_id is None
                else None
            )
            if parsed is None:
                return _weak(found, predicate, (subject,), speaker, context)
            literal = parsed
        else:
            entry = context.index.get(found)
            if not _kind_ok(predicate.object_type, None if entry is None else entry.kind):
                return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "object_type")
            object_id = found
    else:
        return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "needs_object")
    if sketch.time_text:
        resolved = resolve_time(sketch.time_text, position, subject_timeline, basis=basis)
        if not isinstance(resolved, ResolvedTime):
            return Unnormalizable(
                VerdictReason.UNRESOLVED_TIME, resolved.reason.code.value, resolved
            )
        valid_time: Interval | Instant = resolved.valid_time
    else:
        valid_time = default_time(predicate, position, basis)
    try:
        proposition = Proposition(
            subject_id=subject,
            predicate_id=predicate.id,
            object_id=object_id,
            literal=literal,
            qualifiers=dict(sketch.qualifiers),
            polarity=sketch.polarity,
            valid_time=valid_time,
            event_id=sketch.event_id,
        )
    except InvalidValidTime:
        return Unnormalizable(VerdictReason.UNRESOLVED_TIME, "invalid_valid_time")
    except ValueError:
        return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "proposition")
    if predicate.qualifier_errors(proposition):
        return Unnormalizable(VerdictReason.ARGUMENT_TYPE, "qualifiers")
    return canonical(proposition, registry)


async def normalize_declared(
    conn: AsyncConnection,
    story_id: str,
    sketch: ClaimSketch,
    speaker: str,
    position: Position,
    *,
    registry: Registry,
    context: ResolutionContext,
    basis: TimeBasis,
    subject_timeline: SubjectTimeline | None = None,
    introduce: Introductions | None = None,
) -> tuple[Proposition | Unnormalizable, Registry]:
    """``normalize``, applying a ``RegisterPredicate`` in ``conn``'s transaction (a story
    configuration change, #131) and normalizing again; answers the result and the registry it
    was normalized under."""
    found = normalize(
        sketch,
        speaker,
        position,
        registry=registry,
        context=context,
        basis=basis,
        subject_timeline=subject_timeline,
        introduce=introduce,
    )
    if not isinstance(found, RegisterPredicate):
        return found, registry
    await auto_register(
        conn,
        story_id,
        found.relation,
        subject_type=found.subject_type,
        object_type=found.object_type,
    )
    registry = await load(conn, story_id)
    again = normalize(
        sketch,
        speaker,
        position,
        registry=registry,
        context=context,
        basis=basis,
        subject_timeline=subject_timeline,
        introduce=introduce,
    )
    if isinstance(again, RegisterPredicate):  # pragma: no cover - it is registered now
        return Unnormalizable(VerdictReason.UNRESOLVED_PREDICATE, "unregistered"), registry
    return again, registry


__all__ = [
    "LITERAL_TYPES",
    "OPEN_PHRASES",
    "RELATION_ALIASES",
    "ClaimSketch",
    "Normalized",
    "RegisterPredicate",
    "Unnormalizable",
    "default_time",
    "normalize",
    "normalize_declared",
    "open_phrase",
    "parse_literal",
    "predicate_for",
]
