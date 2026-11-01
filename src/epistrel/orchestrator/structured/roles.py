"""Participation roles in a structured turn: who renders, whose reactions another render may
narrate, escalation and budget reduction (FR-MULTI-7,9,10, NFR-PERF-7 (6), FR-VERIFY-14;
docs/02 §4.4; #176). The pure rules are ``core.epistemic.roles``; this module applies them.

Only under ``multi_character.renders = per_character``: under ``combined`` (every M4 profile) and
in a Lite story the stored roles are accepted and inert, and a request's ``roles`` or
``reactive_interjections`` is 422 (``refuse``).

**Resolution** (``resolve``, when the turn renders): every present character (#161's co-located
set: the scene's present names; with no placements, the whole cast) gets its role for the turn
(``core.epistemic.roles.resolve_roles``); the planner's reduced primaries are ``reactive``
(``reduced``), its deferred fixed primaries stay ``primary`` and are not narrated. The player's
character and a non-player actor have none. ``turn_report.roles`` = ``{cid: {role, source}}`` and
the response's ``roles`` are the oracle; a request's ``primary`` override names a character to
render (``promoted``, after the named ones).

**Reactions** (``collect``, ``apply``): a primary's render declares reactions on its ``reactions``
channel (``render.ReactionDecl``), and ``core.epistemic.roles.prose_reactions`` finds those its
``do`` prose narrates (the sentence is taken out of the primary's beat; a beat left empty becomes
the reaction). Each is judged (``roles.judge``): accepted, it becomes its own beat after the
primary's (a ``do``, or a brief interjection's ``say`` with ``intent = unspecified``) attributed
to the reactor with ``narrated_by`` = the primary, which does not count against
``render.max_beats``; a reaction that needs a render of its own **escalates** the reactor (at
most ``MAX_ESCALATIONS_PER_TURN``, no recursion: an escalated render's reactions never escalate)
when the turn's remaining budget fits its render, its first output check and its extraction
(``afford``, funded from what no reservation holds); otherwise it is limited to a non-verbal
reaction and listed in ``escalation_deferred`` (``fixed_role`` or ``budget``; not an error). A
fixed ``silent`` character narrated as reacting is dropped and listed ``fixed_role``.

**Verification** (``verify``, after #148's detection and #142's verification of the beats): a
reaction's claims are asserted by ``Asserter("reactive", narrator, reactor=…)`` (#144's reactive
exception), and each reaction is checked against the **reactor's** viewpoint, in order: its
claims' verdicts (a denied, contradicted or protected claim, FR-VERIFY-14), the reactor's
perception of the trigger (``reacts_to``, else the beat just before; #161's routing), then
could-have-known (#165, ``min_level = believed``) over every claim in it about someone else. A
failing reaction is dropped (rung 1) and staged as ``render_rejected``. An accepted reaction is
the reactor's ``do`` and never writes its ``think``, ``latent`` or beliefs (the observation sink
skips a narrated beat's own actor); the reactor remembers it as an episodic ``do`` and forms its
beliefs about what it witnessed through ordinary exposures (#164).

Counters: ``epistrel_escalations_total{outcome}``, ``epistrel_primaries_reduced_total``. Log
lines carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.epistemic import perception, replay
from epistrel.core.epistemic import roles as rules
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.epistemic.roles import (
    Bounds,
    Outcome,
    Participation,
    Reaction,
    RoleOf,
)
from epistrel.core.events.types import RenderRejected
from epistrel.core.genesis.settings_map import PARTICIPATION
from epistrel.core.membership import Position
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.readset import StructuredReadSet
from epistrel.core.settings_schema import StorySettings
from epistrel.core.turns import CastEntry
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify.other_agent import PLAYER_NARRATOR
from epistrel.core.verify.permission import Asserter
from epistrel.observability.metrics import ESCALATIONS, PRIMARIES_REDUCED
from epistrel.orchestrator.structured.multi import inactive_value

log = logging.getLogger(__name__)

REACTION_REJECTED = "reaction"
"""The ``render_rejected`` constraint kind of a dropped narrated reaction (#176)."""
OBSERVABLE = frozenset({"say", "do", "narration"})


def interjections_of(story: StorySettings, request: Any = None) -> bool:
    """Whether reactive narration may give a brief interjection this turn: the request's
    ``reactive_interjections``, else the story's, else the scenario's that genesis kept inactive
    (#181), else off."""
    override = getattr(request, "reactive_interjections", None)
    if override is not None:
        return bool(override)
    if "reactive_interjections" in story.model_fields_set:
        return bool(story.reactive_interjections)
    return inactive_value(story, "reactive_interjections") is True


def max_words(settings: ProfileSettings) -> int:
    """The profile dial ``multi_character.interjection_max_words`` (#181; default 6)."""
    return settings.multi_character.interjection_max_words


def stored_of(
    cast: Sequence[CastEntry], story: StorySettings | None = None
) -> dict[str, Participation]:
    """Each non-player cast member's stored role (a minor with no explicit role is
    ``reactive``, the column's default). A character still at that default whose scenario role's
    participation genesis kept inactive (``participation.<role id>``: a story created before
    M5 delivered per-character renders) takes the role's (#181), never fixed."""
    found: dict[str, Participation] = {}
    for c in cast:
        if c.is_player:
            continue
        role, fixed = c.participation_role, c.participation_fixed
        if story is not None and c.role_id and role == rules.REACTIVE and not fixed:
            kept = inactive_value(story, f"{PARTICIPATION}.{c.role_id}")
            if kept in rules.ROLES:
                role = str(kept)
        found[c.id] = Participation(role, fixed)
    return found


def promoted(
    cast: Sequence[CastEntry], request: Any, named: Sequence[str], actor_id: str
) -> tuple[str, ...]:
    """The request's ``roles`` validated (422; ``core.epistemic.roles.validate_overrides``):
    the characters a ``primary`` override names to render."""
    overrides = getattr(request, "roles", None)
    excluded = {c.id for c in cast if c.is_player} | {actor_id}
    return rules.validate_overrides(
        overrides, cast=[c.id for c in cast], named=named, excluded=excluded
    )


@dataclass(slots=True)
class TurnRoles:
    """One turn's roles and what reactive narration made of them (``turn_report.roles`` and
    the response's ``roles``, ``escalation_deferred``, ``deferred_characters``,
    ``reduced_characters``)."""

    roles: dict[str, RoleOf]
    present: frozenset[str]
    names: Mapping[str, str]
    interjections: bool = False
    max_words: int = rules.INTERJECTION_MAX_WORDS
    player_id: str | None = None
    rendering: set[str] = field(default_factory=set)
    reduced: tuple[str, ...] = ()
    deferred: list[dict[str, Any]] = field(default_factory=list)
    escalation_deferred: list[dict[str, Any]] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[tuple[TurnBeat, str]] = field(default_factory=list)
    limited: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    state_terms: tuple[str, ...] = ()
    """#187: how prose names the story's tracked States (``states.level = on`` only): a reaction
    naming one escalates (``core.epistemic.roles.judge``)."""

    def bounds(self) -> Bounds:
        return Bounds(
            roles=self.roles,
            present=self.present,
            interjections=self.interjections,
            max_words=self.max_words,
            player_id=self.player_id,
            primaries=frozenset(self.rendering),
            state_terms=self.state_terms,
        )

    def silent(self) -> frozenset[str]:
        return frozenset(c for c, r in self.roles.items() if r.role == rules.SILENT)

    def reactive(self) -> list[str]:
        """The reactive-eligible characters (whose reactions a primary may narrate)."""
        return [
            c
            for c, r in self.roles.items()
            if r.role == rules.REACTIVE and c in self.present and c not in self.rendering
        ]

    def others_present(self) -> list[str]:
        """The ``## Others present`` lines: ``<name> (id <id>)``."""
        return [f"{self.names.get(c, c)} (id {c})" for c in self.reactive()]

    def defer(self, character_id: str, reason: str, needed: str | None) -> None:
        if any(e["character_id"] == character_id for e in self.escalation_deferred):
            return
        entry: dict[str, Any] = {"character_id": character_id, "reason": reason}
        if needed is not None:
            entry["needed"] = needed
        self.escalation_deferred.append(entry)
        ESCALATIONS.labels(outcome=f"deferred_{reason}").inc()

    def escalate(self, character_id: str) -> None:
        self.escalated.append(character_id)
        self.rendering.add(character_id)
        self.roles[character_id] = RoleOf(
            rules.PRIMARY, rules.ESCALATED, self.roles[character_id].fixed
        )
        ESCALATIONS.labels(outcome="escalated").inc()

    def report(self) -> dict[str, Any]:
        return {
            "roles": {c: r.payload() for c, r in self.roles.items()},
            "reactions": list(self.accepted),
            "rejected": [{"beat": b.beat, "reason": r} for b, r in self.rejected],
            "limited": self.limited,
            "dropped": dict(sorted(self.dropped.items())),
            "escalated": list(self.escalated),
            "escalation_deferred": list(self.escalation_deferred),
            "reduced_characters": list(self.reduced),
            "deferred_characters": list(self.deferred),
        }


def resolve(
    cast: Sequence[CastEntry],
    *,
    present_names: Sequence[str],
    rendering: Sequence[str],
    actor_id: str,
    request: Any,
    story: StorySettings,
    settings: ProfileSettings,
    reduced: Sequence[str] = (),
    deferred: Sequence[str] = (),
    deferred_fixed: Sequence[str] = (),
) -> TurnRoles:
    """The turn's roles (see the module docstring)."""
    stored = stored_of(cast, story)
    player_id = next((c.id for c in cast if c.is_player), None)
    wanted = set(present_names)
    present = [c.id for c in cast if not c.is_player and (not wanted or c.name in wanted)]
    excluded = {actor_id} | ({player_id} if player_id else set())
    found = rules.resolve_roles(
        present=present,
        named=[*rendering, *deferred],
        stored=stored,
        overrides=getattr(request, "roles", None),
        excluded=excluded,
        minors=[c.id for c in cast if c.minor],
    )
    for cid in reduced:
        if cid in found or cid in present:
            found[cid] = RoleOf(
                rules.REACTIVE, rules.REDUCED, stored.get(cid, Participation()).fixed
            )
            PRIMARIES_REDUCED.inc()
    for cid in deferred:
        if cid in found:
            found[cid] = RoleOf(
                rules.PRIMARY,
                rules.FIXED if cid in deferred_fixed else rules.DEFAULT,
                cid in deferred_fixed,
            )
    return TurnRoles(
        roles=found,
        present=frozenset([*present, *rendering]),
        names={c.id: c.name for c in cast},
        interjections=interjections_of(story, request),
        max_words=max_words(settings),
        player_id=player_id,
        rendering=set(rendering) | set(deferred),
        reduced=tuple(reduced),
        deferred=[
            {"character_id": c, "reason": rules.BUDGET, "fixed": c in deferred_fixed}
            for c in deferred
        ],
    )


# -- reactions -----------------------------------------------------------------------------------


def _reactor(ref: str, roles: TurnRoles) -> str:
    if ref in roles.names:
        return ref
    folded = ref.strip().casefold()
    return next((c for c, n in roles.names.items() if n.casefold() == folded), ref)


def collect(
    beats: Sequence[TurnBeat],
    declared: Sequence[Any],
    *,
    primary: str,
    start: int,
    roles: TurnRoles,
) -> list[Reaction]:
    """The reactions primary ``primary``'s render narrates: its declared ``reactions`` (in
    order) and those its ``do`` prose narrates (see the module docstring)."""
    found: list[Reaction] = []
    own = [b for b in beats if b.character_id == primary and not b.narrator]
    for decl in declared:
        trigger = None
        if decl.reacts_to is not None and decl.reacts_to < len(own):
            trigger = own[decl.reacts_to].beat
        found.append(
            Reaction(
                character_id=_reactor(decl.character_id, roles),
                text=" ".join(decl.text.split()),
                narrated_by=primary,
                kind=decl.kind,
                reacts_to=trigger,
            )
        )
    others = {
        roles.names[c]: c
        for c in roles.names
        if c != primary and c in roles.present | ({roles.player_id} if roles.player_id else set())
    }
    for beat in own:
        if beat.kind != "do":
            continue
        for cid, sentence, _, _ in rules.prose_reactions(beat.text, others, roles.state_terms):
            found.append(
                Reaction(
                    character_id=cid,
                    text=sentence,
                    narrated_by=primary,
                    reacts_to=beat.beat,
                    source="prose",
                    beat=beat.beat,
                )
            )
    del start
    return found


def apply(
    beats: list[TurnBeat],
    sketches: dict[int, list[Any]],
    reactions: Sequence[Reaction],
    roles: TurnRoles,
    *,
    afford: Callable[[], bool],
    escalating: bool = True,
) -> tuple[list[TurnBeat], list[str]]:
    """Judge ``reactions`` and lay the accepted (or limited) ones into ``beats`` (see the module
    docstring); answers the new beats and the characters to escalate, in order of appearance.
    ``afford``: whether one more primary render fits the turn's budget (it holds it when it
    does). ``escalating``: off for an escalated render's own reactions (no recursion)."""
    bounds = roles.bounds()
    taken: dict[int, list[tuple[int, int]]] = {}
    added: list[tuple[Reaction, str, str]] = []
    escalate: list[str] = []
    for reaction in reactions:
        bound = rules.judge(reaction, bounds)
        cid = reaction.character_id
        text = reaction.text
        kind = rules.NONVERBAL
        if bound.outcome is Outcome.DROP:
            roles.dropped[bound.reason] = roles.dropped.get(bound.reason, 0) + 1
            if bound.reason == rules.FIXED_ROLE:
                roles.defer(cid, rules.FIXED_ROLE, None)
        elif bound.outcome is Outcome.ACCEPT:
            kind = bound.kind
        else:
            reason = bound.reason
            if bound.outcome is Outcome.ESCALATE:
                if cid in escalate or cid in roles.escalated:
                    reason = ""
                elif (
                    escalating
                    and len(roles.escalated) + len(escalate) < rules.MAX_ESCALATIONS_PER_TURN
                    and afford()
                ):
                    escalate.append(cid)
                    reason = ""
                else:
                    roles.defer(cid, rules.BUDGET, bound.reason)
            else:
                roles.defer(cid, rules.FIXED_ROLE, bound.category)
            del reason
            # The narrated reaction is limited to what reactive narration allows.
            text = rules.limited_text(roles.names.get(cid, "Someone"))
            roles.limited += 1
        if reaction.source == "prose" and reaction.beat is not None:
            for b in beats:
                if b.beat == reaction.beat:
                    start = b.text.find(reaction.text)
                    if start >= 0:
                        taken.setdefault(b.beat, []).append((start, start + len(reaction.text)))
        if bound.outcome is not Outcome.DROP:
            added.append((reaction, text, kind))
    out: list[TurnBeat] = []
    emptied: set[int] = set()
    for b in beats:
        spans = taken.get(b.beat)
        if not spans:
            out.append(b)
            continue
        rest = rules.without(b.text, spans)
        if rest:
            out.append(replace(b, text=rest))
        else:
            # The whole beat was narrated for others: its number goes to its first reaction.
            emptied.add(b.beat)
            sketches.pop(b.beat, None)
    next_beat = max((b.beat for b in beats), default=1) + 1
    for reaction, text, kind in added:
        if reaction.beat is not None and reaction.beat in emptied:
            number = reaction.beat
            emptied.discard(reaction.beat)
        else:
            number = next_beat
            next_beat += 1
        out.append(
            TurnBeat(
                kind="say" if kind == rules.INTERJECTION else "do",
                beat=number,
                text=text,
                character_id=reaction.character_id,
                narrated_by=reaction.narrated_by,
            )
        )
        sketches.setdefault(number, [])
        roles.accepted.append(
            {
                "character_id": reaction.character_id,
                "narrated_by": reaction.narrated_by,
                "beat": number,
                "kind": kind,
                "source": reaction.source,
                "trigger": None if reaction.reacts_to == number else reaction.reacts_to,
            }
        )
    out.sort(key=lambda b: b.beat)
    return out, escalate


def asserter_of(beats: Mapping[int, TurnBeat]) -> Callable[[Any], Asserter | None]:
    """A narrated reaction's claims' asserter (#144's reactive exception); ``None`` for every
    other beat's (its default asserter), the ``co_author`` player's narration included (#180)."""

    def of(claim: Any) -> Asserter | None:
        beat = beats.get(claim.beat or -1)
        if (
            beat is None
            or beat.narrated_by is None
            or beat.character_id is None
            or beat.narrated_by == PLAYER_NARRATOR
        ):
            return None
        return Asserter("reactive", beat.narrated_by, reactor=beat.character_id)

    return of


def _trigger_of(beat: TurnBeat, beats: Sequence[TurnBeat], roles: TurnRoles) -> int:
    found = next((a for a in roles.accepted if a["beat"] == beat.beat), None)
    trigger = None if found is None else found.get("trigger")
    if trigger is not None:
        return int(trigger)
    before = [
        b.beat
        for b in beats
        if b.beat < beat.beat and b.kind in OBSERVABLE and b.narrated_by is None
    ]
    return max(before, default=1)


async def verify(
    db: AsyncEngine,
    story_id: str,
    *,
    turn: int,
    checked: Any,
    roles: TurnRoles,
    settings: ProfileSettings,
    actor_id: str,
    say: str | None,
    do: str | None,
    read_set: StructuredReadSet | None = None,
) -> None:
    """Check each narrated reaction of ``checked`` against its reactor's viewpoint and drop the
    failing ones (see the module docstring): ``checked`` is the pipeline's ``Checked``."""
    reacted = [
        b for b in checked.beats if b.narrated_by is not None and b.narrated_by != PLAYER_NARRATOR
    ]
    if not reacted:
        return
    level = scene_loader.level_of(settings)
    by_beat = {b.beat: b for b in checked.beats}
    failed: dict[int, str] = {}
    async with db.connect() as conn:
        scene = None
        if level != perception.NONE:
            scene = (await scene_loader.load_scene(conn, story_id, turn))[0]
        registry = await load_registry(conn, story_id)
        for beat in reacted:
            reactor = str(beat.character_id)
            mine = [c for c in checked.checked if c.claim.beat == beat.beat]
            if any(c.result is not None and c.result.needs_repair for c in mine):
                failed[beat.beat] = "verification"
                continue
            if scene is not None:
                trigger = _trigger_of(beat, checked.beats, roles)
                source = by_beat.get(trigger)
                route: perception.RouteBeat | None = None
                if trigger <= 1 or source is None:
                    if say or do:
                        route = perception.RouteBeat(
                            kind="say" if say else "do", beat=1, actor=actor_id
                        )
                else:
                    route = perception.RouteBeat(
                        kind=cast(perception.RouteKind, source.kind),
                        beat=source.beat,
                        actor=None if source.narrator else source.character_id,
                        qualifiers=source.qualifiers,
                    )
                if route is not None:
                    seen = perception.route_perception(route, scene, level=level)
                    if reactor not in {p.character_id for p in seen}:
                        failed[beat.beat] = "unperceived"
                        continue
            for c in mine:
                proposition = c.claim.proposition
                if proposition is None or proposition.subject_id == reactor:
                    continue
                answer = await replay.could_have_known(
                    conn,
                    story_id,
                    reactor,
                    proposition,
                    Position(turn, 0, 0, beat.beat),
                    min_level=replay.Level.BELIEVED,
                    settings=settings,
                    registry=registry,
                    read_set=read_set,
                )
                if isinstance(answer, replay.NoPath):
                    failed[beat.beat] = "could_have_known"
                    break
    for number, reason in failed.items():
        beat = by_beat[number]
        roles.rejected.append((beat, reason))
        roles.accepted = [a for a in roles.accepted if a["beat"] != number]
        checked.beats = [b for b in checked.beats if b.beat != number]
        checked.checked = [c for c in checked.checked if c.claim.beat != number]
        checked.requests = [r for r in checked.requests if r.beat != number]
        checked.latent = [(b, c) for b, c in checked.latent if b.beat != number]
        checked.mentions.pop(number, None)
        checked.salient = [s for s in checked.salient if s[0].index != number]
    if failed:
        log.debug("reactions: %d dropped", len(failed))


def rejected_events(roles: TurnRoles | None) -> list[tuple[str, Any]]:
    """The ``render_rejected`` audit record of each dropped reaction (rung 1)."""
    if roles is None:
        return []
    return [
        (
            "render_rejected",
            RenderRejected(
                beat=beat.beat,
                attempt=0,
                rung=1,
                text=beat.text,
                constraints=({"kind": REACTION_REJECTED, "reason": reason},),
            ),
        )
        for beat, reason in roles.rejected
    ]


__all__ = [
    "REACTION_REJECTED",
    "TurnRoles",
    "apply",
    "asserter_of",
    "collect",
    "interjections_of",
    "max_words",
    "promoted",
    "rejected_events",
    "resolve",
    "stored_of",
    "verify",
]
