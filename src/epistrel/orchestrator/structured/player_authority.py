"""Player authority over the other characters in a structured turn (FR-RESP-11, FR-AUTH-3,
FR-VERIFY-11; docs/02 §4.9; #180). ``core.verify.other_agent`` finds the claims; this module
applies the story's ``player_authority`` to them, one switch on the effective setting.

Only for input whose actor is a player's own character: input for a character the platform plays
is checked like its render (FR-AUTH-3). ``split`` runs before the renders, on the input's ``do``
and on its ``say``'s attributed quotations, and takes every claim span out of the player's own
text (what the ``player_input`` event stores, what detection reads as the player's, what working
memory and canonization see). Each claim:

- about another human's character: always a request, which no render can answer, so dropped
  (``other_player_character``);
- a stated thought ("Jenni thinks about betraying me"): never written as that character's
  ``think`` (``player_cannot_write_think``), the span dropped and the rest of the input standing;
- about a character not in the scene (#161's co-located set; the whole cast with no
  placements): dropped (``not_present``);
- under **``own_character``** (the default): a **request** (``Request``). It is passed to the
  named character's render as ``player_requests`` (``## The player asks of you``: comply or
  refuse, in character), stored tagged on the ``player_input`` event (``requests``), never
  detected as fact or canonized; a requested character the request did not name renders
  (``requested_renders``: appended after the named primaries in mention order under
  ``per_character`` renders, the turn's default render under ``combined``). Whether it complied
  is read from its verified ``do`` beats (``outcomes``: a ``do`` sharing the request's content
  words ``complied``, else ``refused``; ``unresolved`` when it did not render);
- under **``co_author``**: **narration** (``Narrated``): the character's own observable beat
  (``beat_do``, or ``beat_say`` with ``intent = unspecified`` for attributed speech), beats 2..k
  of the turn after the player's own, ``narrated_by = "player"``, its first-person words
  rewritten to the player character's name. A fixed ``silent`` character, or a fixed
  ``reactive`` one given speech, is not narrated (``role_fixed``: #176's bounds stand). The
  beat's claims are asserted ``co_author`` (``core.verify.permission``'s exception: its
  observable actions over existing canon, with provenance ``player``; anything else about the
  character, its backstory, is ``player_authority``, never canonized) and ``verify`` checks the
  beat against that character's viewpoint after detection, like its own render: a ``do`` whose
  claim needs repair is dropped (``contradicted``), as is one the character could not perceive
  being in (``not_perceived``) or whose claims about anything but the character and the player it
  could not have known (#165, ``could_not_know``, under per-character partitions). A narrated
  ``say`` the speaker's beliefs contradict is a sincere error: labelled by #166, never repaired,
  never a lie. Narrated beats never go to the repair ladder; a dropped one is a ``render_rejected``
  record (rung 1, no model call).

``turn_report.player_authority`` = ``{mode, requests: [{text, character_id, kind, outcome}],
narrated: [{beat, character_id, kind}], rejected: [{text, character_id, code}]}``; the counter
``epistrel_player_authority_total{mode, outcome}``. An input that only narrates others leaves the
player's own beat empty (its event kept, with its ``requests``). Logs carry ids and counts only
(invariant 3).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.epistemic import perception, replay
from epistrel.core.epistemic import roles as role_rules
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.epistemic import transitions as belief_transitions
from epistrel.core.events.types import RenderRejected
from epistrel.core.membership import Position
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.readset import StructuredReadSet
from epistrel.core.settings_schema import DEFAULT_PLAYER_AUTHORITY, StorySettings
from epistrel.core.turns import CastEntry
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify import other_agent
from epistrel.core.verify.other_agent import PLAYER_NARRATOR, OtherAgentClaim
from epistrel.observability.metrics import PLAYER_AUTHORITY

log = logging.getLogger(__name__)

OWN_CHARACTER, CO_AUTHOR = "own_character", "co_author"
MODES: tuple[str, ...] = (OWN_CHARACTER, CO_AUTHOR)

PLAYER_CANNOT_WRITE_THINK = "player_cannot_write_think"
COULD_NOT_KNOW = "could_not_know"
NOT_PERCEIVED = "not_perceived"
NOT_PRESENT = "not_present"
OTHER_PLAYER_CHARACTER = "other_player_character"
ROLE_FIXED = "role_fixed"
CONTRADICTED = "contradicted"
CODES: tuple[str, ...] = (
    PLAYER_CANNOT_WRITE_THINK,
    COULD_NOT_KNOW,
    NOT_PERCEIVED,
    NOT_PRESENT,
    OTHER_PLAYER_CHARACTER,
    ROLE_FIXED,
    CONTRADICTED,
)
"""Why a span of player input about another character was dropped."""

COMPLIED, REFUSED, UNRESOLVED = "complied", "refused", "unresolved"
REJECTED = "player_authority"
"""The ``render_rejected`` constraint kind of a dropped narrated beat."""

_STOP_WORDS = (
    "a an the and or but so to of in on at with for from up down over into onto off out "
    "then now his her their its my me i you he she they it him them is are was were be"
)
_STOP = frozenset(_STOP_WORDS.split())


def mode_of(story: StorySettings) -> str:
    """The story's effective ``player_authority`` (``own_character`` unless set)."""
    found = getattr(story, "player_authority", None)
    return str(found) if found in MODES else DEFAULT_PLAYER_AUTHORITY


@dataclass(frozen=True, slots=True)
class Request:
    """One span of the input under ``own_character``: what the player asked of a character."""

    claim: OtherAgentClaim
    sketch: str
    """The span with its first-person words rewritten to the player character's name."""

    @property
    def character_id(self) -> str:
        return self.claim.character_id

    def payload(self) -> dict[str, Any]:
        return {**self.claim.payload(), "tag": "request"}


@dataclass(frozen=True, slots=True)
class Narrated:
    """One span of the input under ``co_author``: the character's narrated beat."""

    claim: OtherAgentClaim
    text: str
    beat: int

    @property
    def character_id(self) -> str:
        return self.claim.character_id

    @property
    def kind(self) -> str:
        return "say" if self.claim.kind == "say" else "do"


@dataclass(frozen=True, slots=True)
class Rejection:
    character_id: str
    text: str
    code: str
    beat: int | None = None

    def report(self) -> dict[str, Any]:
        return {"text": self.text, "character_id": self.character_id, "code": self.code}


@dataclass(slots=True)
class Split:
    """One input's split (see the module docstring): the player's own ``say``/``do`` (``None``
    when nothing of its own is left), its requests, its narrated beats and its dropped spans."""

    mode: str
    say: str | None
    do: str | None
    requests: list[Request] = field(default_factory=list)
    narrated: list[Narrated] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    counted: bool = False

    @property
    def active(self) -> bool:
        return bool(self.requests or self.narrated or self.rejected)

    def requests_for(self, character_id: str) -> list[str]:
        """The ``## The player asks of you`` lines of ``character_id``'s render."""
        return [r.sketch for r in self.requests if r.character_id == character_id]

    def requested(self) -> list[str]:
        """The requested characters, in mention order."""
        return list(dict.fromkeys(r.character_id for r in self.requests))

    def payloads(self) -> tuple[dict[str, Any], ...]:
        """The ``player_input`` event's ``requests``."""
        return tuple(r.payload() for r in self.requests)

    def report(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "requests": [
                {
                    "text": r.claim.text,
                    "character_id": r.character_id,
                    "kind": r.claim.kind,
                    "outcome": self.outcomes[n] if n < len(self.outcomes) else UNRESOLVED,
                }
                for n, r in enumerate(self.requests)
            ],
            "narrated": [
                {"beat": b.beat, "character_id": b.character_id, "kind": b.kind}
                for b in self.narrated
            ],
            "rejected": [r.report() for r in self.rejected],
        }

    def count(self) -> None:
        """``epistrel_player_authority_total`` for the turn (once)."""
        if self.counted:
            return
        self.counted = True
        for n, _ in enumerate(self.requests):
            outcome = self.outcomes[n] if n < len(self.outcomes) else UNRESOLVED
            PLAYER_AUTHORITY.labels(mode=self.mode, outcome=outcome).inc()
        for _ in self.narrated:
            PLAYER_AUTHORITY.labels(mode=self.mode, outcome="narrated").inc()
        for r in self.rejected:
            PLAYER_AUTHORITY.labels(mode=self.mode, outcome=r.code).inc()


def _others(cast_: Sequence[CastEntry], actor_id: str) -> dict[str, str]:
    return {c.name: c.id for c in cast_ if c.id != actor_id}


def claims_of(
    say: str | None, do: str | None, cast_: Sequence[CastEntry], actor_id: str
) -> list[OtherAgentClaim]:
    """The input's other-agent claims (none unless the actor is a player's character)."""
    actor = next((c for c in cast_ if c.id == actor_id), None)
    if actor is None or not actor.is_player:
        return []
    return other_agent.detect(say, do, _others(cast_, actor_id))


def split(
    say: str | None,
    do: str | None,
    *,
    mode: str,
    cast_: Sequence[CastEntry],
    actor_id: str,
    present: frozenset[str] | None = None,
    roles: Mapping[str, role_rules.RoleOf | role_rules.Participation] | None = None,
    start: int = 2,
) -> Split:
    """Split the input (see the module docstring); ``present``: the co-located characters
    (``None``: everyone); ``roles``: the turn's participation roles (#176); narrated beats are
    numbered from ``start``."""
    found = claims_of(say, do, cast_, actor_id)
    out = Split(mode=mode, say=say, do=do)
    if not found:
        return out
    by_id = {c.id: c for c in cast_}
    actor = by_id[actor_id]
    taken: dict[str, list[tuple[int, int]]] = {"say": [], "do": []}
    beat = start
    for claim in found:
        taken[claim.channel].append((claim.start, claim.end))
        target = by_id[claim.character_id]
        role = (roles or {}).get(claim.character_id)
        code: str | None = None
        if target.is_player:
            code = OTHER_PLAYER_CHARACTER
        elif claim.kind == "think":
            code = PLAYER_CANNOT_WRITE_THINK
        elif present is not None and claim.character_id not in present:
            code = NOT_PRESENT
        elif (
            mode == CO_AUTHOR
            and role is not None
            and role.fixed
            and (role.role == role_rules.SILENT or claim.kind == "say")
            and role.role != role_rules.PRIMARY
        ):
            code = ROLE_FIXED
        if code is not None:
            out.rejected.append(Rejection(claim.character_id, claim.text, code))
            continue
        rewritten = other_agent.first_person(claim.text, actor.name)
        if mode == CO_AUTHOR:
            out.narrated.append(Narrated(claim, rewritten, beat))
            beat += 1
        else:
            out.requests.append(Request(claim, rewritten))
    out.say = other_agent.without(say, taken["say"]) or None if say else None
    out.do = other_agent.without(do, taken["do"]) or None if do else None
    log.debug(
        "player authority: %d requests, %d narrated, %d dropped",
        len(out.requests),
        len(out.narrated),
        len(out.rejected),
    )
    return out


def requested_renders(
    cast_: Sequence[CastEntry],
    say: str | None,
    do: str | None,
    *,
    story: StorySettings,
    actor_id: str,
    rendering: Sequence[str],
) -> list[CastEntry]:
    """The characters an ``own_character`` input asks something of that ``rendering`` leaves
    out, in mention order (never the player's character, nor a fixed non-primary role)."""
    if mode_of(story) != OWN_CHARACTER:
        return []
    by_id = {c.id: c for c in cast_}
    found: list[CastEntry] = []
    for claim in claims_of(say, do, cast_, actor_id):
        target = by_id[claim.character_id]
        if (
            claim.kind == "think"
            or target.is_player
            or target.id in rendering
            or any(c.id == target.id for c in found)
            or (target.participation_fixed and target.participation_role != role_rules.PRIMARY)
        ):
            continue
        found.append(target)
    return found


def narrated_beats(found: Split) -> list[TurnBeat]:
    """The ``co_author`` input's narrated beats, in input order."""
    return [
        TurnBeat(
            kind="say" if n.kind == "say" else "do",
            beat=n.beat,
            text=n.text,
            character_id=n.character_id,
            narrated_by=PLAYER_NARRATOR,
        )
        for n in found.narrated
    ]


def is_narrated(beat: TurnBeat) -> bool:
    """Whether ``beat`` is the player's ``co_author`` narration."""
    return beat.narrated_by == PLAYER_NARRATOR


def _words(text: str, names: Sequence[str]) -> set[str]:
    skip = {n.casefold() for n in names} | _STOP
    return {
        w
        for w in re.findall(r"[a-z][a-z'\u2019]+", text.casefold())
        if w not in skip and len(w) > 2
    }


def outcomes(found: Split, beats: Sequence[TurnBeat], names: Sequence[str]) -> None:
    """Each request's outcome over the turn's verified beats (see the module docstring)."""
    found.outcomes = []
    for request in found.requests:
        cid = request.character_id
        own = [
            b
            for b in beats
            if b.character_id == cid and b.narrated_by is None and b.kind in ("say", "do")
        ]
        if not own:
            found.outcomes.append(UNRESOLVED)
            continue
        wanted = _words(request.sketch, names)
        complied = any(
            b.kind == "do" and len(wanted & _words(b.text, names)) >= max(1, min(2, len(wanted)))
            for b in own
        )
        found.outcomes.append(COMPLIED if complied else REFUSED)


async def verify(
    db: AsyncEngine,
    story_id: str,
    *,
    turn: int,
    checked: Any,
    found: Split,
    settings: ProfileSettings,
    actor_id: str,
    player_id: str | None,
    read_set: StructuredReadSet | None = None,
) -> None:
    """Check each narrated beat of ``checked`` (the pipeline's ``Checked``) against its
    character's viewpoint and drop the failing ones (see the module docstring)."""
    narrated = [b for b in checked.beats if is_narrated(b)]
    if not narrated:
        return
    level = scene_loader.level_of(settings)
    partitions = module_on(settings, belief_transitions.PARTITIONS_MODULE)
    failed: dict[int, str] = {}
    async with db.connect() as conn:
        scene = None
        if level != perception.NONE:
            scene = (await scene_loader.load_scene(conn, story_id, turn))[0]
        registry = await load_registry(conn, story_id)
        for beat in narrated:
            cid = str(beat.character_id)
            mine = [c for c in checked.checked if c.claim.beat == beat.beat]
            if beat.kind == "do" and any(
                c.result is not None and c.result.needs_repair for c in mine
            ):
                failed[beat.beat] = CONTRADICTED
                continue
            if scene is not None:
                route = perception.RouteBeat(kind="do", beat=1, actor=actor_id)
                seen = perception.route_perception(route, scene, level=level)
                if cid not in {p.character_id for p in seen}:
                    failed[beat.beat] = NOT_PERCEIVED
                    continue
            if not partitions or beat.kind != "do":
                continue
            for c in mine:
                proposition = c.claim.proposition
                if proposition is None or proposition.subject_id in (cid, player_id, actor_id):
                    continue
                answer = await replay.could_have_known(
                    conn,
                    story_id,
                    cid,
                    proposition,
                    Position(turn, 0, 0, beat.beat),
                    min_level=replay.Level.BELIEVED,
                    settings=settings,
                    registry=registry,
                    read_set=read_set,
                )
                if isinstance(answer, replay.NoPath):
                    failed[beat.beat] = COULD_NOT_KNOW
                    break
    if not failed:
        return
    by_beat = {b.beat: b for b in checked.beats}
    for number, code in failed.items():
        beat = by_beat[number]
        found.rejected.append(Rejection(str(beat.character_id), beat.text, code, beat=number))
        found.narrated = [n for n in found.narrated if n.beat != number]
        checked.beats = [b for b in checked.beats if b.beat != number]
        checked.checked = [c for c in checked.checked if c.claim.beat != number]
        checked.requests = [r for r in checked.requests if r.beat != number]
        checked.latent = [(b, c) for b, c in checked.latent if b.beat != number]
        checked.mentions.pop(number, None)
        checked.salient = [s for s in checked.salient if s[0].index != number]
    log.debug("player authority: %d narrated beats dropped", len(failed))


def rejected_events(found: Split | None) -> list[tuple[str, Any]]:
    """The ``render_rejected`` audit record of each dropped narrated beat (rung 1)."""
    if found is None:
        return []
    return [
        (
            "render_rejected",
            RenderRejected(
                beat=r.beat,
                attempt=0,
                rung=1,
                text=r.text,
                constraints=({"kind": REJECTED, "reason": r.code},),
            ),
        )
        for r in found.rejected
        if r.beat is not None
    ]


def present_of(cast_: Sequence[CastEntry], present_names: Sequence[str]) -> frozenset[str] | None:
    """The co-located characters by id (#161's present names; ``None``: no placements, so
    everyone)."""
    if not present_names:
        return None
    wanted = set(present_names)
    return frozenset(c.id for c in cast_ if c.name in wanted)


__all__ = [
    "CODES",
    "COMPLIED",
    "CO_AUTHOR",
    "MODES",
    "OWN_CHARACTER",
    "PLAYER_CANNOT_WRITE_THINK",
    "REFUSED",
    "UNRESOLVED",
    "Narrated",
    "Rejection",
    "Request",
    "Split",
    "claims_of",
    "is_narrated",
    "mode_of",
    "narrated_beats",
    "outcomes",
    "present_of",
    "rejected_events",
    "requested_renders",
    "split",
    "verify",
]
