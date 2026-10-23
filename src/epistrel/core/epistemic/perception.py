"""Perception routing: who perceives a beat, and how much of it (FR-TURN-2,9,10,13, NFR-PRIV-3;
docs/02 §4.4; #161).

Pure and deterministic over a ``SceneState`` (#137's authoritative placements, the location
topology and its barriers, the open remote channels and who understands which language;
``core.epistemic.scene`` loads it) and a beat's explicit qualifiers. **Presence is only a
candidate audience**: the consumer's scene roster is never an input, so a manipulated roster
changes nothing, a co-located character omitted from it still perceives, and nothing private is
granted on non-authoritative input. This module is the codebase's one perception decision:
memory writes (``core.memory.records``' sink), the audience filter (#177) and working memory
(#170) all call it (``python -m tools.gates check-perception-authority``).

**The rule** (``route_perception``). A beat happens at its actor's authoritative location (a
narration at its declared ``location``, by default the scene's). Same location → perceive,
different → not; an unplaced character is absent, and an adjoining room never perceives without
an explicit ``eavesdrop_by``, whatever the barrier. The actor always perceives its own beat
(``self``). A ``think`` reaches its thinker only (``think``), a ``latent`` its renderer only.

- At perception ``co_location`` every co-located character perceives the whole beat
  (``content``) and every qualifier is ignored: a whisper is heard by the whole room
  (FR-TURN-2's split applies "with ``full``").
- At ``full`` an utterance's **envelope** (that speech occurred, the speaker, the apparent
  target, the visible manner) reaches everyone co-located and its **content** reaches its
  audience: the room by default; narrowed by ``whisper_to`` (the targets) or ``aside`` (the
  speaker alone), the rest of the room getting the envelope; widened by ``eavesdrop_by``
  (exactly whom the render names, with its ``partial_text`` when the render limited what they
  caught) and ``remote_channel`` (the channel's participants, wherever they are; a co-located
  non-participant gets the envelope "<Speaker> speaks into the phone"). A ``language`` a
  perceiver does not understand (``characters.languages``) leaves them the envelope. A ``do``
  routes as an observation (content) to all co-located; a sound-category ``do`` (``audible``,
  the ``audible.json`` lexicon) also reaches the adjoining places through any barrier but a
  ``wall`` as an envelope ("You hear a knock from the hallway").
- At ``none`` (every M4 profile) nothing is routed: every cast member perceives everything
  (#129's gating; the shared-holder writer of #135 writes the memories).

**Qualifier validation** (``validate_qualifiers``, FR-TURN-13). Narrowing needs none (it can
only remove perceivers), except that a whisper target who is not in the room is dropped
(``target_absent``; every target dropped → the beat is an aside). An eavesdropper needs a
plausible path: their location adjoins the beat's (directly or through a ``located_in``
ancestor of either) with a barrier sound can be caught through (``none``, ``door_open``,
``door_closed``, ``window``, ``thin_wall``); a ``wall`` is ``sealed_barrier`` and no adjacency
``no_path``. An eavesdropper who is in the room already is ignored silently. A remote channel
needs the speaker on it (``on_channel``) and at least one other participant (``no_channel``); a
render naming more participants than the channel has gets the intersection. An id that is no
character of the story is ``unknown_character``, a narration ``location`` that is no place
``unknown_location`` (the beat falls back to the scene's location). An invalid qualifier is
**repaired, not honored**: it is dropped and the beat routes as unqualified (the pipeline feeds
the repairs to #151's rung 1).

**A turn** (``route_turn``) routes its beats in order; a move a beat stages (its committed
placement change, #137's ``placement.since_beat``) takes effect from the next beat, so "Bobby
leaves" in beat 2 removes him from beat 3 while he still perceives beat 2 at the origin.

**Envelopes** (``envelope_text``) are rendered deterministically per qualifier, never by a
model: "<Speaker> whispers something to <Target>", "<Speaker> says something to <Target> in a
language you don't understand", "<Speaker> speaks into the phone", "<Speaker> says something,
<manner>" when there is no addressee, and "You hear <sound> from <place>".

**Sinks** (``PERCEPTION_SINKS``). Each routed turn's ``PerceptionResult`` list is handed to every
registered sink in the commit transaction: ``core.memory.records`` writes the memories here
(#161) and ``core.epistemic.observation`` the observation transitions (#164); #162's testimony
and #164's exposures run from the pipeline on the same results.
Routing results themselves are never stored: a history read re-routes from the stored beats
and their persisted qualifiers.

Log lines carry ids and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal, Protocol

from epistrel.core.propositions.vocabulary import (
    ADJACENT_TO,
    BARRIERS,
    DEFAULT_BARRIER,
    ON_CHANNEL,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

Level = Literal["none", "co_location", "full"]
Fidelity = Literal["content", "envelope"]
Channel = Literal["self", "room", "eavesdrop", "remote", "adjacent", "think"]
RouteKind = Literal["say", "do", "think", "latent", "narration"]
EnvelopeKind = Literal["whisper", "aside", "language", "phone", "manner", "sound"]
RepairReason = Literal[
    "no_path",
    "sealed_barrier",
    "no_channel",
    "unknown_character",
    "target_absent",
    "unknown_location",
]

NONE: Level = "none"
CO_LOCATION: Level = "co_location"
FULL: Level = "full"
LEVELS: tuple[Level, ...] = (NONE, CO_LOCATION, FULL)

CONTENT: Fidelity = "content"
ENVELOPE: Fidelity = "envelope"

SOUND_BLOCKING: frozenset[str] = frozenset({"wall"})
"""The barriers a sound-category ``do`` does not cross (it reaches the next room through every
other barrier, a closed door included, as an envelope)."""
EAVESDROP_BARRIERS: frozenset[str] = frozenset(BARRIERS) - {"wall"}
"""The barriers an eavesdropper can listen through (FR-TURN-13)."""

ANCESTRY_MAX = 8
"""How far up ``located_in`` an eavesdrop path looks (a cycle stops it as well)."""


# -- the inputs ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteChannel:
    """A ``remote_channel`` qualifier: the channel and, optionally, whom the render names on it
    (``()``: every participant)."""

    channel_id: str
    participants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Qualifiers:
    """A beat's explicit perception qualifiers (#150's beat schema, amended here): who it is
    addressed to and how, and the deviations from the co-location rule the render established.
    Persisted on the ``beat_*`` event payload (``payload``) so a history read re-routes it."""

    addressee: str | None = None
    manner: str | None = None
    whisper_to: tuple[str, ...] = ()
    aside: bool = False
    eavesdrop_by: tuple[str, ...] = ()
    remote_channel: RemoteChannel | None = None
    language: str | None = None
    partial_text: str | None = None
    location: str | None = None

    @property
    def empty(self) -> bool:
        return self == Qualifiers()

    def payload(self) -> dict[str, Any]:
        """The qualifiers as the event payload stores them (only what was declared)."""
        found: dict[str, Any] = {}
        for name in ("addressee", "manner", "language", "partial_text", "location"):
            value = getattr(self, name)
            if value is not None:
                found[name] = value
        if self.whisper_to:
            found["whisper_to"] = list(self.whisper_to)
        if self.aside:
            found["aside"] = True
        if self.eavesdrop_by:
            found["eavesdrop_by"] = list(self.eavesdrop_by)
        if self.remote_channel is not None:
            channel: dict[str, Any] = {"channel_id": self.remote_channel.channel_id}
            if self.remote_channel.participants:
                channel["participants"] = list(self.remote_channel.participants)
            found["remote_channel"] = channel
        return found

    @classmethod
    def of(cls, payload: Mapping[str, Any] | None) -> Qualifiers:
        """The qualifiers of a stored payload (``payload``'s inverse)."""
        if not payload:
            return cls()
        channel = payload.get("remote_channel")
        return cls(
            addressee=payload.get("addressee"),
            manner=payload.get("manner"),
            whisper_to=tuple(payload.get("whisper_to") or ()),
            aside=bool(payload.get("aside", False)),
            eavesdrop_by=tuple(payload.get("eavesdrop_by") or ()),
            remote_channel=None
            if not channel
            else RemoteChannel(
                str(channel["channel_id"]), tuple(channel.get("participants") or ())
            ),
            language=payload.get("language"),
            partial_text=payload.get("partial_text"),
            location=payload.get("location"),
        )


def pair(a: str, b: str) -> tuple[str, str]:
    """An adjacency's key: the two places in canonical order (``adjacent_to`` is symmetric)."""
    return (a, b) if a <= b else (b, a)


@dataclass(frozen=True, slots=True)
class SceneState:
    """What routing reads, all authoritative (#137's projections and canon): each placed
    character's leaf location, the containment of places (``located_in``), the adjacency of
    places with its barrier, the open remote channels and their participants, the languages a
    character understands (absent: every language), the story's characters and places, and the
    scene's location (the player character's)."""

    location: str | None = None
    placements: Mapping[str, str] = field(default_factory=dict)
    containment: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    adjacency: Mapping[tuple[str, str], str] = field(default_factory=dict)
    channels: Mapping[str, frozenset[str]] = field(default_factory=dict)
    languages: Mapping[str, frozenset[str]] = field(default_factory=dict)
    characters: frozenset[str] = frozenset()
    places: frozenset[str] = frozenset()

    def at(self, character_id: str | None) -> str | None:
        return None if character_id is None else self.placements.get(character_id)

    def co_located(self, location: str | None) -> frozenset[str]:
        """The characters authoritatively at ``location`` (none for ``None``)."""
        if location is None:
            return frozenset()
        return frozenset(c for c, where in self.placements.items() if where == location)

    def barrier(self, a: str, b: str) -> str | None:
        """The barrier between two adjoining places; ``None`` when they do not adjoin."""
        return self.adjacency.get(pair(a, b))

    def understands(self, character_id: str, language: str | None) -> bool:
        if language is None:
            return True
        known = self.languages.get(character_id)
        return known is None or language.casefold() in known

    def ancestry(self, location: str) -> tuple[str, ...]:
        """``location`` and its ``located_in`` ancestors, nearest first."""
        found = [location]
        frontier = [location]
        for _ in range(ANCESTRY_MAX):
            nxt = [p for loc in frontier for p in self.containment.get(loc, ()) if p not in found]
            if not nxt:
                break
            found.extend(dict.fromkeys(nxt))
            frontier = nxt
        return tuple(found)

    def moved(self, moves: Iterable[tuple[str, str | None]]) -> SceneState:
        """The state after ``moves`` (a character to a location; ``None``: unplaced)."""
        placements = dict(self.placements)
        for character_id, location in moves:
            if location is None:
                placements.pop(character_id, None)
            else:
                placements[character_id] = location
        return replace(self, placements=placements)


@dataclass(frozen=True, slots=True)
class RouteBeat:
    """One beat as routing takes it: its kind, number, actor (``None`` for narration and the
    narrator's ``latent``), its qualifiers, the sound an audible ``do`` makes (``audible_sound``)
    and the moves it stages (what its committed placement changes do from the next beat on).
    ``source`` is the caller's own beat, passed through to the sinks untouched."""

    kind: RouteKind
    beat: int
    actor: str | None
    qualifiers: Qualifiers = field(default_factory=Qualifiers)
    sound: str | None = None
    moves: tuple[tuple[str, str | None], ...] = ()
    source: object | None = None
    claims: tuple[Any, ...] = ()
    """The propositions a ``do`` or a narration states (#164: ``core.epistemic.observation``
    records them as each content perceiver's observation); empty for every other beat."""


def with_declared(scene: SceneState, claims: Iterable[Any]) -> SceneState:
    """``scene`` with the channels and barriers ``claims`` declare (affirmed ``on_channel`` and
    ``adjacent_to`` propositions: "she picks up the phone", "he closes the door"), so a later
    beat of the same turn may use them before they are canon (FR-TURN-13: "a channel declared by
    a prior ``do``")."""
    channels = {k: set(v) for k, v in scene.channels.items()}
    adjacency = dict(scene.adjacency)
    for claim in claims:
        if claim is None or claim.polarity.value != "affirmed":
            continue
        if claim.predicate_id == ON_CHANNEL and claim.literal is not None:
            channels.setdefault(str(claim.literal.value), set()).add(str(claim.subject_id))
        elif claim.predicate_id == ADJACENT_TO and claim.object_id is not None:
            barrier = str(claim.qualifiers.get("barrier") or DEFAULT_BARRIER)
            if barrier in BARRIERS:
                adjacency[pair(str(claim.subject_id), str(claim.object_id))] = barrier
    return replace(
        scene, channels={k: frozenset(v) for k, v in channels.items()}, adjacency=adjacency
    )


# -- the outputs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Envelope:
    """What an envelope perceiver is told happened (``envelope_text`` renders it)."""

    kind: EnvelopeKind
    speaker: str | None
    target: tuple[str, ...] = ()
    manner: str | None = None
    sound: str | None = None
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class Perceived:
    """One perceiver of a beat: the fidelity (``content``: the words or the action;
    ``envelope``: only that it happened, ``envelope`` says what), the channel it came by, and
    the part of the words the render limited an eavesdropper to (``partial_text``)."""

    character_id: str
    fidelity: Fidelity
    channel: Channel
    partial_text: str | None = None
    envelope: Envelope | None = None


@dataclass(frozen=True, slots=True)
class Repair:
    """A qualifier dropped because it could not be honored (FR-TURN-13): the beat, the
    qualifier (``whisper_to``, ``eavesdrop_by``, ``remote_channel`` or ``location``), why, and
    the character it named."""

    beat: int
    kind: str
    reason: RepairReason
    character_id: str | None = None

    def report(self) -> dict[str, Any]:
        found: dict[str, Any] = {"beat": self.beat, "kind": self.kind, "reason": self.reason}
        if self.character_id is not None:
            found["character_id"] = self.character_id
        return found


@dataclass(frozen=True, slots=True)
class PerceptionResult:
    """One routed beat: the beat, the qualifiers it was routed under (after validation), the
    level, where it happened, its perceivers (actor first, then by id; a non-perceiver is not
    listed) and the qualifiers repaired."""

    beat: RouteBeat
    qualifiers: Qualifiers
    level: Level
    location: str | None
    perceived: tuple[Perceived, ...]
    repairs: tuple[Repair, ...] = ()

    def of(self, character_id: str) -> Perceived | None:
        return next((p for p in self.perceived if p.character_id == character_id), None)

    @property
    def perceivers(self) -> frozenset[str]:
        return frozenset(p.character_id for p in self.perceived)


# -- validation -------------------------------------------------------------------------------


def _location_of(beat: RouteBeat, scene: SceneState, qualifiers: Qualifiers) -> str | None:
    if beat.kind == "narration" or beat.actor is None:
        if qualifiers.location is not None and qualifiers.location in scene.places:
            return qualifiers.location
        return scene.location
    return scene.at(beat.actor)


def eavesdrop_path(scene: SceneState, listener: str, location: str) -> RepairReason | None:
    """``None`` when a character at ``listener`` (a location) has a plausible perception path
    to ``location`` (FR-TURN-13: see the module docstring), else why not."""
    sealed = False
    for a in scene.ancestry(listener):
        for b in scene.ancestry(location):
            if a == b:
                continue
            barrier = scene.barrier(a, b)
            if barrier is None:
                continue
            if barrier in EAVESDROP_BARRIERS:
                return None
            sealed = True
    return "sealed_barrier" if sealed else "no_path"


def validate_qualifiers(
    beat: RouteBeat, scene: SceneState, level: Level = FULL
) -> tuple[Qualifiers, tuple[Repair, ...]]:
    """``beat``'s qualifiers as routing may honor them, and the repairs made (see the module
    docstring). Below ``full`` every qualifier is ignored, so nothing is repaired either."""
    q = beat.qualifiers
    if level != FULL or q.empty:
        return q, ()
    repairs: list[Repair] = []
    known = scene.characters
    if beat.kind != "say":  # narrowing applies to speech only
        q = replace(q, whisper_to=(), aside=False)
    location = scene.location if beat.kind == "narration" else scene.at(beat.actor)
    if q.location is not None and q.location not in scene.places:
        repairs.append(Repair(beat.beat, "location", "unknown_location"))
        q = replace(q, location=None)
    if beat.kind == "narration":
        location = _location_of(beat, scene, q)
    room = scene.co_located(location)
    if q.whisper_to:
        kept: list[str] = []
        for target in dict.fromkeys(q.whisper_to):
            if target == beat.actor:
                continue
            if target not in known:
                repairs.append(Repair(beat.beat, "whisper_to", "unknown_character", target))
            elif target not in room:
                repairs.append(Repair(beat.beat, "whisper_to", "target_absent", target))
            else:
                kept.append(target)
        q = replace(q, whisper_to=tuple(kept), aside=q.aside or not kept)
    if q.eavesdrop_by:
        listeners: list[str] = []
        for listener in dict.fromkeys(q.eavesdrop_by):
            if listener == beat.actor:
                continue
            if listener not in known:
                repairs.append(Repair(beat.beat, "eavesdrop_by", "unknown_character", listener))
                continue
            if listener in room:
                continue  # already perceives: ignored silently
            where = scene.at(listener)
            reason: RepairReason | None = "no_path"
            if where is not None and location is not None:
                reason = eavesdrop_path(scene, where, location)
            if reason is None:
                listeners.append(listener)
            else:
                repairs.append(Repair(beat.beat, "eavesdrop_by", reason, listener))
        q = replace(q, eavesdrop_by=tuple(listeners))
    if q.remote_channel is not None:
        channel = q.remote_channel
        members = scene.channels.get(channel.channel_id, frozenset())
        others = members - {beat.actor} if beat.actor is not None else frozenset()
        if channel.participants:
            others &= frozenset(channel.participants)
        if beat.actor is None or beat.actor not in members or not others:
            repairs.append(Repair(beat.beat, "remote_channel", "no_channel"))
            q = replace(q, remote_channel=None)
        else:
            q = replace(q, remote_channel=RemoteChannel(channel.channel_id, tuple(sorted(others))))
    return q, tuple(repairs)


# -- routing ----------------------------------------------------------------------------------


def _ordered(found: Mapping[str, Perceived], actor: str | None) -> tuple[Perceived, ...]:
    return tuple(found[c] for c in sorted(found, key=lambda c: (c != actor, c)))


def route_perception(
    beat: RouteBeat,
    scene_state: SceneState,
    qualifiers: Qualifiers | None = None,
    level: Level = FULL,
) -> list[Perceived]:
    """Who perceives ``beat`` and at what fidelity (see the module docstring); ``qualifiers``
    are the validated ones (``validate_qualifiers``; ``None``: the beat's own, validated
    here). Pure: the same inputs always give the same answer."""
    scene = scene_state
    if qualifiers is None:
        qualifiers, _ = validate_qualifiers(beat, scene, level)
    q = qualifiers if level == FULL else Qualifiers(location=qualifiers.location)
    actor = beat.actor
    found: dict[str, Perceived] = {}

    def give(character_id: str, fidelity: Fidelity, channel: Channel, **extra: Any) -> None:
        held = found.get(character_id)
        if held is not None and (held.fidelity == CONTENT or fidelity == ENVELOPE):
            return
        found[character_id] = Perceived(character_id, fidelity, channel, **extra)

    if beat.kind == "think":
        return [] if actor is None else [Perceived(actor, CONTENT, "think")]
    if beat.kind == "latent":
        return [] if actor is None else [Perceived(actor, CONTENT, "self")]
    if level == NONE:
        for character_id in sorted(scene.characters):
            give(character_id, CONTENT, "self" if character_id == actor else "room")
        if actor is not None:
            give(actor, CONTENT, "self")
        return list(_ordered(found, actor))
    location = _location_of(beat, scene, q)
    room = scene.co_located(location)
    if actor is not None:
        give(actor, CONTENT, "self")
    speaker = actor
    if level == CO_LOCATION or beat.kind != "say":
        for character_id in sorted(room):
            give(character_id, CONTENT, "room")
        if level == FULL:
            _widen(beat, scene, q, location, give)
            if beat.kind == "do" and beat.sound is not None and location is not None:
                _sound(beat, scene, location, room, give)
        return list(_ordered(found, actor))
    # A ``say`` at ``full``: the envelope to the room, the content to its audience.
    targets = tuple(t for t in q.whisper_to if t in room)
    if q.aside or q.whisper_to:
        audience = frozenset(targets)
        held_back: Envelope = (
            Envelope("whisper", speaker, targets, q.manner)
            if targets
            else Envelope("aside", speaker, (), q.manner)
        )
    elif q.remote_channel is not None:
        audience = frozenset()
        held_back = Envelope("phone", speaker)
    else:
        audience = room
        held_back = Envelope("manner", speaker, _addressed(q), q.manner)
    for character_id in sorted(room):
        if character_id == actor:
            continue
        if character_id in audience:
            _hear(character_id, beat, scene, q, "room", give)
        else:
            give(character_id, ENVELOPE, "room", envelope=held_back)
    _widen(beat, scene, q, location, give)
    return list(_ordered(found, actor))


def _addressed(q: Qualifiers) -> tuple[str, ...]:
    return () if q.addressee is None else (q.addressee,)


def _hear(
    character_id: str,
    beat: RouteBeat,
    scene: SceneState,
    q: Qualifiers,
    channel: Channel,
    give: Callable[..., None],
    partial: str | None = None,
) -> None:
    """``character_id`` hears a ``say``'s words, unless their language is not theirs."""
    if beat.kind == "say" and not scene.understands(character_id, q.language):
        target = q.whisper_to or _addressed(q)
        give(
            character_id,
            ENVELOPE,
            channel,
            envelope=Envelope("language", beat.actor, tuple(target), q.manner),
        )
        return
    give(character_id, CONTENT, channel, partial_text=partial)


def _widen(
    beat: RouteBeat,
    scene: SceneState,
    q: Qualifiers,
    location: str | None,
    give: Callable[..., None],
) -> None:
    del location
    for listener in q.eavesdrop_by:
        if listener != beat.actor:
            _hear(listener, beat, scene, q, "eavesdrop", give, q.partial_text)
    if q.remote_channel is not None:
        for participant in q.remote_channel.participants:
            if participant != beat.actor:
                _hear(participant, beat, scene, q, "remote", give)


def _sound(
    beat: RouteBeat,
    scene: SceneState,
    location: str,
    room: frozenset[str],
    give: Callable[..., None],
) -> None:
    """A sound-category ``do`` heard in the places adjoining ``location`` (not through a wall)."""
    for (a, b), barrier in sorted(scene.adjacency.items()):
        if location not in (a, b) or barrier in SOUND_BLOCKING:
            continue
        other = b if a == location else a
        for character_id in sorted(scene.co_located(other) - room):
            give(
                character_id,
                ENVELOPE,
                "adjacent",
                envelope=Envelope("sound", beat.actor, sound=beat.sound, origin=location),
            )


def route_turn(
    beats: Sequence[RouteBeat], scene: SceneState, level: Level
) -> list[PerceptionResult]:
    """Every beat of a turn routed in beat order, each beat's staged moves applied after it
    (see the module docstring)."""
    results: list[PerceptionResult] = []
    current = scene
    for beat in sorted(beats, key=lambda b: b.beat):
        qualifiers, repairs = validate_qualifiers(beat, current, level)
        perceived = route_perception(beat, current, qualifiers, level)
        location = _location_of(beat, current, qualifiers) if beat.kind != "think" else None
        results.append(
            PerceptionResult(beat, qualifiers, level, location, tuple(perceived), repairs)
        )
        if beat.moves:
            current = current.moved(beat.moves)
    return results


def noticers(
    beat: RouteBeat, scene: SceneState, candidates: Iterable[str], level: Level = FULL
) -> tuple[str, ...]:
    """Who of ``candidates`` notices a hidden ``latent`` (#169, FR-COMMIT-4): the rendering
    character always; otherwise a candidate present where it happens (the renderer's location,
    a narrator's latent the scene's, or its declared ``location``), and at ``none`` every
    candidate in the scene. A candidate elsewhere notices nothing."""
    wanted = sorted(set(candidates))
    if level == NONE:
        present = scene.characters
    else:
        present = scene.co_located(_location_of(beat, scene, beat.qualifiers))
    return tuple(c for c in wanted if c == beat.actor or c in present)


def co_located_set(scene: SceneState, character_id: str | None = None) -> frozenset[str]:
    """The characters authoritatively co-located with ``character_id`` (with the scene's
    location when ``None``), the character included: #55's ``present`` set (FR-GUARD-11)."""
    location = scene.location if character_id is None else scene.at(character_id)
    return scene.co_located(location)


# -- envelopes ---------------------------------------------------------------------------------


def _names(ids: Sequence[str], names: Mapping[str, str]) -> str:
    shown = [names.get(i, "someone") for i in ids]
    if len(shown) <= 1:
        return shown[0] if shown else "someone"
    return ", ".join(shown[:-1]) + " and " + shown[-1]


def envelope_text(
    envelope: Envelope, names: Mapping[str, str], places: Mapping[str, str] | None = None
) -> str:
    """What an envelope perceiver is told (deterministic, per qualifier: see the module
    docstring). ``names`` are the characters' names, ``places`` the places'."""
    speaker = names.get(envelope.speaker or "", "Someone")
    if envelope.kind == "whisper":
        return f"{speaker} whispers something to {_names(envelope.target, names)}"
    if envelope.kind == "language":
        if envelope.target:
            return (
                f"{speaker} says something to {_names(envelope.target, names)} in a language "
                "you don't understand"
            )
        return f"{speaker} says something in a language you don't understand"
    if envelope.kind == "phone":
        return f"{speaker} speaks into the phone"
    if envelope.kind == "sound":
        origin = (places or {}).get(envelope.origin or "", "nearby")
        return f"You hear {envelope.sound or 'a sound'} from {origin}"
    if envelope.target:
        return f"{speaker} says something to {_names(envelope.target, names)}"
    manner = (envelope.manner or "").strip()
    if envelope.kind == "aside" and not manner:
        manner = "under their breath"
    return f"{speaker} says something, {manner}" if manner else f"{speaker} says something"


# -- the audible lexicon --------------------------------------------------------------------------


@cache
def _audible() -> tuple[tuple[re.Pattern[str], str], ...]:
    source = resources.files("epistrel.core.verify").joinpath("detectors/audible.json")
    table: dict[str, Any] = json.loads(source.read_text("utf-8"))
    found = []
    for entry in table["lemmas"]:
        forms = "|".join(sorted((re.escape(f) for f in entry["forms"]), key=lambda f: -len(f)))
        found.append((re.compile(rf"\b(?:{forms})\b", re.IGNORECASE), str(entry["noun"])))
    return tuple(found)


def sound_of(text: str) -> str | None:
    """The sound a ``do`` beat's text makes (the first sound-category verb it uses, as the
    lexicon's noun: "a knock"), or ``None``."""
    hits = [(m.start(), noun) for pattern, noun in _audible() if (m := pattern.search(text))]
    return min(hits)[1] if hits else None


# -- sinks ------------------------------------------------------------------------------------


class SinkOperation(Protocol):
    """What a sink needs of the writing operation (an ``Operation`` inside its commit)."""

    operation_id: str
    story_id: str | None
    narrative_turn: int | None
    turn_variant: int | None
    base_revision: int | None


PerceptionSink = Callable[
    ["AsyncConnection", SinkOperation, Sequence[PerceptionResult]], Awaitable[None]
]
"""Called in the commit transaction with the turn's routed beats."""

PERCEPTION_SINKS: list[PerceptionSink] = []


def register_perception_sink(sink: PerceptionSink) -> PerceptionSink:
    """Hand every routed turn to ``sink`` (once per sink), in registration order."""
    if sink not in PERCEPTION_SINKS:
        PERCEPTION_SINKS.append(sink)
    return sink


def remove_perception_sink(sink: PerceptionSink) -> None:
    if sink in PERCEPTION_SINKS:
        PERCEPTION_SINKS.remove(sink)


async def deliver(
    conn: AsyncConnection, operation: SinkOperation, results: Sequence[PerceptionResult]
) -> None:
    """Run every registered sink over ``results`` (in the caller's transaction)."""
    for sink in list(PERCEPTION_SINKS):
        await sink(conn, operation, results)


__all__ = [
    "CONTENT",
    "CO_LOCATION",
    "EAVESDROP_BARRIERS",
    "ENVELOPE",
    "FULL",
    "LEVELS",
    "NONE",
    "PERCEPTION_SINKS",
    "SOUND_BLOCKING",
    "Channel",
    "Envelope",
    "Fidelity",
    "Level",
    "Perceived",
    "PerceptionResult",
    "PerceptionSink",
    "Qualifiers",
    "RemoteChannel",
    "Repair",
    "RepairReason",
    "RouteBeat",
    "SceneState",
    "SinkOperation",
    "co_located_set",
    "deliver",
    "eavesdrop_path",
    "envelope_text",
    "noticers",
    "pair",
    "register_perception_sink",
    "remove_perception_sink",
    "route_perception",
    "route_turn",
    "sound_of",
    "validate_qualifiers",
    "with_declared",
]
