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
  being in (``not_perceived``). A narrated ``say`` the speaker's beliefs contradict is player
  authority (#564, FR-RESP-14): never repaired, so labelled a sincere error (never a
  hallucination, never a lie) and the speaker's beliefs unchanged. Narrated beats never go to
  the repair ladder; a dropped one is a ``render_rejected`` record (rung 1, no model call).

**Co-author knowledge** (FR-RESP-14; #598, ADR-174). Under per-character partitions, a narrated
``do`` is held to what its character could know (#165's ``replay.could_have_known``) only
through what the action **presupposes** (``presuppositions``), never through what it brings
about:

- **brought about** (exempt): #148's detectors' results of the action: a transfer the character
  takes part in (giver or recipient), a state result (``consumption``, ``injury``,
  ``consciousness``), the scene participation of what the action mentions (``entity_mention``),
  and anything about the character itself or the player;
- **presupposed** (checked): every other claim about the world or another character ("Jenni
  nods at Bobby, who holds the key": ``holds(Bobby, key)``), and what the action's results
  need to have held before it: a transfer's giver held the item, and an item the character
  takes *from* a place or a person was there ("Jenni takes the key from the drawer":
  ``located_at(key, drawer)``; from a character: ``holds(that character, key)``), up to the
  beat.

A ``protected_content`` hit (#148: the player's words match a record hidden from the character)
is no presupposition and does not drop the beat under partitions: it stays denied, never
canonized, and the presuppositions the words state are judged as when canon settles nothing, so
neither the beat's fate nor its notices answer whether such a record exists (NFR-PRIV-4). So
does a claim contradicted only by canon hidden from the character (#169's ``hidden_conflict``):
it stays ``contradicted`` (never canonized over the hidden fact), and as a presupposition it is
judged like any other, so a fiat and, under ``confirm``, a notice, exactly as when nothing is
hidden.

A presupposition the character has no path to is **accepted** under both values of the story's
``co_author_knowledge``: the beat commits and the character's knowledge of it is written in the
turn's commit as an ``author_fiat`` transition citing the turn's ``player_input`` event at the
narrated beat (``fiats_committed``), so its later renders are verified as knowing it (and later
renders of the same turn, through ``replay.stage_path``). Under ``confirm`` (the default) each
one also gives one ``implausible_knowledge`` notice ``{id, kind, character_id, claim_text,
turn, correctable}``: ``claim_text`` is the player's own span, so a notice says nothing of canon,
of the claim's truth, or of who else knows it, and is raised whether or not canon settles the
claim (NFR-PRIV-4). It is stored on the turn's ``player_input`` event (``notices``) and
returned by ``respond`` and the history read, which add ``external_effects`` (whether the turn
has dispatched or confirmed effects, FR-EXT-14). A turn written by ``correct`` (``corrected``)
has notices with ``correctable: false``. ``free`` writes the fiats and no notice.
``co_author_knowledge`` is interpretive (FR-CONC-15): a turn that accepted a presupposition and
whose story's value changed while it ran fails its rebase check (``recheck``, a
``core.readset.REBASE_CHECKS`` entry) and regenerates under the new value.

``turn_report.player_authority`` = ``{mode, requests: [{text, character_id, kind, outcome}],
narrated: [{beat, character_id, kind}], rejected: [{text, character_id, code}]}``, plus, when the
turn accepted a presupposition, ``fiats: [{beat, character_id}]`` and ``notices: [{kind,
character_id, correctable}]``; the counter ``epistrel_player_authority_total{mode,
outcome}`` (``author_fiat`` per fiat). An input that only narrates others leaves the player's
own beat empty (its event kept, with its ``requests``). Logs carry ids and counts only
(invariant 3).
"""

from __future__ import annotations

import logging
import re
import weakref
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import timeline_effects
from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import perception, replay
from epistrel.core.epistemic import roles as role_rules
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.epistemic import transitions as belief_transitions
from epistrel.core.epistemic.errors import (
    AntecedentInvalid,
    ConfidenceInconsistent,
    SelfAttestation,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.events.types import RenderRejected
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, step_of
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.propositions.vocabulary import HOLDS, LOCATED_AT
from epistrel.core.readset import StructuredReadSet, register_rebase_check
from epistrel.core.settings_schema import (
    DEFAULT_CO_AUTHOR_KNOWLEDGE,
    DEFAULT_PLAYER_AUTHORITY,
    StorySettings,
)
from epistrel.core.tables import stories
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

CONFIRM, FREE = "confirm", "free"
KNOWLEDGE: tuple[str, ...] = (CONFIRM, FREE)
"""#598: the values of ``co_author_knowledge`` (FR-RESP-14)."""
IMPLAUSIBLE_KNOWLEDGE = "implausible_knowledge"
"""#598: the kind of a co-author knowledge notice."""
AUTHOR_FIAT = "author_fiat"
"""#598: the ``epistrel_player_authority_total`` outcome of an accepted presupposition."""
RESULT_CATEGORIES: frozenset[str] = frozenset(
    {"consumption", "injury", "consciousness", "entity_mention"}
)
"""#598: the detector categories whose claims a narrated action brings about (its state
results and the scene participation of what it mentions); a ``transfer`` the character takes
part in is one too (``presuppositions``)."""
TRANSFER = "transfer"
ACQUIRING: frozenset[str] = frozenset({"possession", TRANSFER})
"""#598: the categories whose affirmed ``holds`` of the character is an acquisition, whose source
(``from`` a place or a person) it presupposes."""
_SOURCE = re.compile(r"\b(?:from|out of|off of|off)\s+(?:(?:the|a|an|his|her|their)\s+)?", re.I)
"""A source marker and its determiner, before the mention of where an item is taken from."""

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


def knowledge_of(story: StorySettings) -> str:
    """The story's effective ``co_author_knowledge`` (``confirm`` unless set; #598)."""
    found = getattr(story, "co_author_knowledge", None)
    return str(found) if found in KNOWLEDGE else DEFAULT_CO_AUTHOR_KNOWLEDGE


@dataclass(frozen=True, slots=True)
class Fiat:
    """#598: one presupposition of a narrated beat its character had no path to, accepted as
    its ``author_fiat`` knowledge: the beat, the character, the proposition, the player's span
    (``text``, as typed) and the id of its notice under ``confirm``."""

    beat: int
    character_id: str
    proposition: Proposition
    text: str
    notice_id: str = field(default_factory=new_ulid)

    def notice(self, turn: int, *, correctable: bool) -> dict[str, Any]:
        """The ``implausible_knowledge`` notice: the player's words only (NFR-PRIV-4)."""
        return {
            "id": self.notice_id,
            "kind": IMPLAUSIBLE_KNOWLEDGE,
            "character_id": self.character_id,
            "claim_text": self.text,
            "turn": turn,
            "correctable": correctable,
        }


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
    knowledge: str = DEFAULT_CO_AUTHOR_KNOWLEDGE
    """#598: the story's ``co_author_knowledge`` the turn ran under."""
    fiats: list[Fiat] = field(default_factory=list)
    """#598: the accepted presuppositions (``verify``), written by ``fiats_committed``."""
    corrected: bool = False
    """#598: the turn is a correction (``correct``): its notices are not correctable."""

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

    def notices(self, turn: int) -> tuple[dict[str, Any], ...]:
        """#598: the turn's ``implausible_knowledge`` notices (none under ``free``)."""
        if self.knowledge != CONFIRM:
            return ()
        return tuple(f.notice(turn, correctable=not self.corrected) for f in self.fiats)

    def report(self) -> dict[str, Any]:
        found: dict[str, Any] = {
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
        if self.fiats:
            # #598: ids and flags only (the notice's text is the player's, in the event).
            found["fiats"] = [{"beat": f.beat, "character_id": f.character_id} for f in self.fiats]
            found["notices"] = [
                {k: v for k, v in n.items() if k in ("kind", "character_id", "correctable")}
                for n in self.notices(0)
            ]
        return found

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
        for _ in self.fiats:
            PLAYER_AUTHORITY.labels(mode=self.mode, outcome=AUTHOR_FIAT).inc()


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


def _before(proposition: Proposition) -> Proposition:
    """``proposition`` held up to its start: what an action's result needed beforehand."""
    found = proposition.valid_time
    if isinstance(found, Interval) and found.start is not None:
        return proposition.model_copy(update={"valid_time": Interval(end=found.start)})
    return proposition


def _sources(text: str, beat_claims: Sequence[Any], names: Mapping[str, str]) -> list[str]:
    """The entities ``text`` takes something *from* (``_SOURCE``): a mention the detector
    resolved (its ``entity_mention`` claim) or a cast member's name right after the marker."""
    found: list[str] = []
    lowered = text.casefold()
    for match in _SOURCE.finditer(text):
        at = match.end()
        source: str | None = None
        for c in beat_claims:
            claim = c.claim
            if (
                claim.category == "entity_mention"
                and claim.proposition is not None
                and at <= claim.span[1] <= at + 1
            ):
                source = claim.proposition.subject_id
                break
        if source is None:
            source = next(
                (
                    cid
                    for cid, name in names.items()
                    if name and lowered.startswith(name.casefold(), at)
                ),
                None,
            )
        if source is not None and source not in found:
            found.append(source)
    return found


def presuppositions(
    beat: Any,
    beat_claims: Sequence[Any],
    *,
    exempt: Collection[str],
    names: Mapping[str, str] | None = None,
) -> list[Proposition]:
    """What the narrated ``do`` ``beat`` presupposes (see the module docstring): its claims
    (``beat_claims``, the pipeline's checked claims) about anything but ``exempt`` (the narrated
    character, the player, the actor) that the action does not bring about, and what its
    results needed to hold before it. ``names``: the cast's names by id (a source named by a
    character)."""
    cid = str(beat.character_id)
    transfers = [
        c.claim
        for c in beat_claims
        if c.claim.category == TRANSFER and c.claim.proposition is not None
    ]
    takes_part = any(
        t.giver == cid or (t.proposition is not None and t.proposition.subject_id == cid)
        for t in transfers
    )
    found: list[Proposition] = []

    def add(proposition: Proposition) -> None:
        if proposition.subject_id in exempt:
            return
        if any(_same(proposition, p) for p in found):
            return
        found.append(proposition)

    for c in beat_claims:
        claim = c.claim
        proposition = claim.proposition
        if proposition is None or claim.protected:
            continue  # a hidden record the words match: canon's, never the player's claim
        if claim.category in RESULT_CATEGORIES or (claim.category == TRANSFER and takes_part):
            if (
                claim.category == TRANSFER
                and proposition.predicate_id == HOLDS
                and proposition.polarity is Polarity.NEGATED
            ):
                # The giver held the item until the transfer.
                add(_before(proposition.model_copy(update={"polarity": Polarity.AFFIRMED})))
            continue
        add(proposition)
    for c in beat_claims:
        claim = c.claim
        proposition = claim.proposition
        if (
            proposition is None
            or claim.category not in ACQUIRING
            or proposition.subject_id != cid
            or proposition.predicate_id != HOLDS
            or proposition.polarity is not Polarity.AFFIRMED
            or proposition.object_id is None
        ):
            continue
        item = proposition.object_id
        for source in _sources(beat.text, beat_claims, names or {}):
            if source in (cid, item):
                continue
            if source in (names or {}):
                held = proposition.model_copy(update={"subject_id": source})
            else:
                held = proposition.model_copy(
                    update={"subject_id": item, "predicate_id": LOCATED_AT, "object_id": source}
                )
            add(_before(held))
    return found


def _hidden_only(c: Any) -> bool:
    """Whether checked claim ``c`` needs repair only because of canon hidden from the narrated
    character: a protected-content hit, or #169's ``hidden_conflict``. Under partitions neither
    drops a narrated ``do``: its fate must not answer whether such canon exists (NFR-PRIV-4)."""
    result = c.result
    return bool(c.claim.protected or (result is not None and result.hidden_conflict))


def _same(a: Proposition, b: Proposition) -> bool:
    return (a.subject_id, a.predicate_id, a.object_id, a.polarity) == (
        b.subject_id,
        b.predicate_id,
        b.object_id,
        b.polarity,
    )


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
    names: Mapping[str, str] | None = None,
) -> None:
    """Check each narrated beat of ``checked`` (the pipeline's ``Checked``) against its
    character's viewpoint and drop the failing ones; accept each presupposition its character
    could not know as a fiat (``found.fiats``; see the module docstring). ``names``: the cast's
    names by id."""
    narrated = [b for b in checked.beats if is_narrated(b)]
    if not narrated:
        return
    level = scene_loader.level_of(settings)
    partitions = module_on(settings, belief_transitions.PARTITIONS_MODULE)
    failed: dict[int, str] = {}
    texts = {n.beat: n.claim.text for n in found.narrated}
    async with db.connect() as conn:
        scene = None
        if level != perception.NONE:
            scene = (await scene_loader.load_scene(conn, story_id, turn))[0]
        registry = await load_registry(conn, story_id)
        for beat in narrated:
            cid = str(beat.character_id)
            mine = [c for c in checked.checked if c.claim.beat == beat.beat]
            if beat.kind == "do" and any(
                c.result is not None
                and c.result.needs_repair
                and not (partitions and _hidden_only(c))
                for c in mine
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
            exempt = {cid, actor_id, *([player_id] if player_id else [])}
            at = Position(turn, 0, 0, beat.beat)
            for proposition in presuppositions(beat, mine, exempt=exempt, names=names or {}):
                answer = await replay.could_have_known(
                    conn,
                    story_id,
                    cid,
                    proposition,
                    at,
                    min_level=replay.Level.BELIEVED,
                    settings=settings,
                    registry=registry,
                    read_set=read_set,
                )
                if isinstance(answer, replay.NoPath):
                    # FR-RESP-14: accepted, as the character's knowledge by the author's fiat.
                    found.fiats.append(
                        Fiat(beat.beat, cid, proposition, texts.get(beat.beat, beat.text))
                    )
                    replay.stage_path(cid, proposition, at, f"{AUTHOR_FIAT}:{beat.beat}")
    if found.fiats:
        log.debug("player authority: %d presuppositions accepted", len(found.fiats))
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


async def fiats_committed(
    conn: AsyncConnection,
    op: Any,
    found: Split | None,
    *,
    input_event_id: str | None,
    settings: Any,
    registry: Registry,
) -> int:
    """Write each accepted presupposition (``found.fiats``) as its character's ``author_fiat``
    knowledge in the turn's commit (see the module docstring): a ``believed`` transition citing
    the turn's ``player_input`` event at the narrated beat. Returns how many were written; a
    refusal by the antecedent guard is counted, never raised."""
    if found is None or not found.fiats or input_event_id is None:
        return 0
    turn = int(op.narrative_turn or 0)
    variant = int(op.turn_variant or 0)
    wrote = 0
    for fiat in found.fiats:
        try:
            await belief_transitions.transition(
                conn,
                op,
                holder=fiat.character_id,
                kind=belief_transitions.Kind.AUTHOR_FIAT,
                proposition=fiat.proposition,
                antecedent=belief_transitions.Antecedent.record("events", input_event_id),
                position=Position(
                    turn, variant, 0, fiat.beat, step_of(fiat.beat, str(op.operation_id))
                ),
                confidence=belief_transitions.Confidence(BeliefConfidenceLevel.BELIEVED),
                settings=settings,
                registry=registry,
            )
        except SelfAttestation, AntecedentInvalid, ConfidenceInconsistent:
            continue  # counted as refused by transition() itself
        except InvalidRequest:
            belief_transitions.refused()
            continue
        wrote += 1
    log.debug("player authority: %d fiats written", wrote)
    return wrote


_ASSUMED: dict[int, tuple[weakref.ref[StructuredReadSet], int, str]] = {}


def remember(read_set: StructuredReadSet, found: Split) -> None:
    """#598: the ``co_author_knowledge`` a structured turn's current attempt accepted a
    presupposition under (nothing when it accepted none), for ``recheck``."""
    key = id(read_set)
    if not found.fiats:
        _ASSUMED.pop(key, None)
        return
    _ASSUMED[key] = (
        weakref.ref(read_set, lambda _: _ASSUMED.pop(key, None)),
        read_set.attempt,
        found.knowledge,
    )


async def recheck(conn: AsyncConnection, op: Any) -> bool:
    """The rebase re-check of co-author knowledge (``core.readset.REBASE_CHECKS``; FR-CONC-16):
    a turn whose attempt accepted a presupposition under one ``co_author_knowledge`` and whose
    story now has the other regenerates, so its notices follow the new value."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet) or op.story_id is None:
        return True
    entry = _ASSUMED.get(id(source))
    if entry is None or entry[0]() is not source or entry[1] != source.attempt:
        return True
    found = await conn.scalar(select(stories.c.settings).where(stories.c.id == op.story_id))
    return knowledge_of(StorySettings.from_row(found)) == entry[2]


register_rebase_check(recheck)


_MODES: dict[int, tuple[weakref.ref[StructuredReadSet], int, str]] = {}


def remember_mode(read_set: StructuredReadSet, found: Split) -> None:
    """#214 (S-CONFIG-RACE (c)): the ``player_authority`` a structured turn's current attempt
    split its input under, when the split applied it (a request, a narrated beat or a dropped
    span), for ``recheck_mode``."""
    key = id(read_set)
    if not found.active:
        _MODES.pop(key, None)
        return
    _MODES[key] = (
        weakref.ref(read_set, lambda _: _MODES.pop(key, None)),
        read_set.attempt,
        found.mode,
    )


async def recheck_mode(conn: AsyncConnection, op: Any) -> bool:
    """The rebase re-check of ``player_authority`` (``core.readset.REBASE_CHECKS``;
    FR-CONC-16, #214): a turn whose attempt split its input about another character under one
    mode, and whose story now has the other, regenerates under the new one (a ``co_author``
    narration becomes a request under ``own_character``, and the reverse)."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet) or op.story_id is None:
        return True
    entry = _MODES.get(id(source))
    if entry is None or entry[0]() is not source or entry[1] != source.attempt:
        return True
    found = await conn.scalar(select(stories.c.settings).where(stories.c.id == op.story_id))
    return mode_of(StorySettings.from_row(found)) == entry[2]


register_rebase_check(recheck_mode)


def captured_mode(op: Any, story: Any) -> str:
    """The ``player_authority`` the attempt runs under: the value its configuration captured
    (#214: a regeneration after a change runs under the new one), else the story's."""
    found = (getattr(op, "config_revisions", None) or {}).get("player_authority")
    return str(found) if found in MODES else mode_of(story)


async def notices_of(
    db: AsyncConnection | AsyncEngine,
    story_id: str,
    operation_id: str,
    notices: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """#598: a turn's stored notices as a response returns them, with ``external_effects``:
    whether the turn has a dispatched or confirmed external effect, which a correction cannot
    undo (FR-EXT-14; ``core.timeline_effects.with_effects``)."""
    if not notices:
        return []
    if isinstance(db, AsyncConnection):
        effects = await timeline_effects.with_effects(db, story_id, [operation_id])
    else:
        async with db.connect() as conn:
            effects = await timeline_effects.with_effects(conn, story_id, [operation_id])
    return [{**n, "external_effects": operation_id in effects} for n in notices]


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
    "AUTHOR_FIAT",
    "CODES",
    "COMPLIED",
    "CONFIRM",
    "CO_AUTHOR",
    "FREE",
    "IMPLAUSIBLE_KNOWLEDGE",
    "KNOWLEDGE",
    "MODES",
    "OWN_CHARACTER",
    "PLAYER_CANNOT_WRITE_THINK",
    "REFUSED",
    "UNRESOLVED",
    "Fiat",
    "Narrated",
    "Rejection",
    "Request",
    "Split",
    "claims_of",
    "fiats_committed",
    "is_narrated",
    "knowledge_of",
    "mode_of",
    "narrated_beats",
    "notices_of",
    "outcomes",
    "present_of",
    "presuppositions",
    "recheck",
    "rejected_events",
    "remember",
    "requested_renders",
    "split",
    "verify",
]
