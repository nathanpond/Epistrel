"""The State-effects channel of a structured turn (FR-TURN-11, FR-STATE-5,6, FR-RESP-7,
FR-VERIFY-12, FR-CONC-16; docs/02 §4.4, §7.1; #187). The pure rules are
``core.states.effects``'; this module applies them to a turn.

**The book** (``load``). When the turn starts (each attempt), its States are read once: the
story's ``states.level`` (``on`` is the only level effects run at; ``off`` and Lite's
``prompt_only`` refuse them as ``invalid_structure``, named by module), every active State's
declaration, and each State's folded value on the active timeline (``core.states
.current_values``: a regenerate's replaced variant left out). The renders' schemas take the
declarations (``Book.tracked``, ``render.effect_errors``), the narrator's prompt every State
with its value and constraints (``Book.narrator_section``), and the dry-run the values.

**The dry-run** (``findings``), after verification and before #151's ladder: every declared
effect of the turn's beats is folded in beat order (``core.states.effects.dry_run``) over the
snapshot plus the earlier effects that passed; in a ``sequential`` turn the earlier primaries'
come through #174's ``TurnStage`` (``StagedBeat.effects``). #188's ``authorize()`` is the
seam before the bounds once it lands: until then only the bounds are checked. Each ``do`` and
narration beat is read for a State change it narrates with no effect
(``core.states.effects.narrated``). Each finding becomes a checked claim of its beat
(``claims_of``) whose ``VerifyResult.state_finding`` makes it ``needs_repair`` with the
finding's tag: ``state_constraint``, ``effect_unresolved`` or ``state_undeclared`` (a
``state_change`` denial, #144's reserved slot). The repair ladder repairs it in character
(``orchestrator.repair.ladder``); a beat the ladder cannot run for (the repair module off,
the turn the safe fallback) is settled deterministically (``ladder.settled``, the pipeline's
``settle_states``): a refused effect
narrated as refused, an unresolved or undeclared change removed, and nothing changes.

**The commit** (``apply_declared``, the turn's first commit step, inside #150's single
transaction before its beats' records are written): each surviving effect goes through
``core.states.apply_change`` with ``source = turn``, the asserting character's #176 role and
the beat's position (its step from ``core.membership.step_of``, #168). A ``Violation`` there
means the State moved since the snapshot: it is never repaired inside the commit; the step
raises ``core.concurrency.RebaseCheckFailed`` and the turn regenerates (#47).

**The rebase re-check** (``recheck``, a ``core.readset.REBASE_CHECKS`` entry; FR-CONC-16): the
turn's State reads, recorded for its attempt (``remember``: the effects it will commit and the
interpretive members of each State they touch), are re-run under the story lock against
current state and configuration. A State gone, an interpretive change to one of its
declarations (#185's ``update_definition``: constraints, authorization, transfer, rules,
visibility), ``states.level`` no longer ``on``, or a concurrent change that makes an effect
break its bounds fails it, and the turn regenerates. A prospective change (aliases, unit,
silent) or a change to another State passes. The turn depends on #185's ``state_definitions``
configuration member, so its ``config_revisions`` record the revision its State checks ran
under.

**The response** (``committed``, FR-RESP-7): the changes the turn committed, ``{state,
scope_key, old_value, new_value, reason, source, contingency, status}``, a ``hidden`` State's
only for ``omniscient`` (C-12's rule; history's turn-level ``state_effects`` reads the same,
``core.states.turn_effects``).

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import states
from epistrel.core.concurrency import RebaseCheckFailed
from epistrel.core.enums import StateChangeKind, StateChangeSource, StateOwner
from epistrel.core.errors import ModuleOff, StateNotFound
from epistrel.core.membership import Position, step_of
from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.readset import StructuredReadSet, register_rebase_check
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.states import StateChange, StateDefinition, Undefined, UndefinedType
from epistrel.core.states import effects as rules
from epistrel.core.states.effects import Finding, Planned
from epistrel.core.states.model import Value
from epistrel.core.tables import characters, entities
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.other_agent import PLAYER_NARRATOR
from epistrel.core.verify.permission import Asserter, state_change
from epistrel.core.verify.rules import Verdict, VerdictKind
from epistrel.orchestrator.structured.render import TrackedState
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import VerifyResult

if TYPE_CHECKING:
    from epistrel.core.epistemic.stage import TurnStage
    from epistrel.core.events.operation import Operation

log = logging.getLogger(__name__)

STATE_EFFECTS = "state_effects"
"""``turn_report.state_effects`` and the response's field."""


# -- the book ------------------------------------------------------------------------------------


@dataclass(slots=True)
class Book:
    """The turn's States as its attempt read them (see the module docstring)."""

    story_id: str
    level: str | None = None
    definitions: dict[str, StateDefinition] = field(default_factory=dict)
    values: dict[rules.Key, Value | UndefinedType] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    entity_names: dict[str, str] = field(default_factory=dict)
    external: dict[tuple[str, str | None], Value | None] = field(default_factory=dict)
    """#205: the turn's pinned external values by ``(State name, scope key)``
    (``PinnedReads.by_name``): the narrator sees an externally owned State only when pinned."""

    @property
    def on(self) -> bool:
        """Whether the turn's renders may declare effects (``states.level = on``)."""
        return self.level == states.ON

    def tracked(self) -> dict[str, TrackedState]:
        """The render schema's States (what an effect may name)."""
        return {
            name: TrackedState(d.type.value, d.scope.value, d.visibility.value)
            for name, d in self.definitions.items()
        }

    def terms(self, *, narrator: bool) -> list[rules.Terms]:
        """How prose names the States a beat's speaker may know: every State for the
        narrator, the player-visible ones for a character."""
        return [
            rules.terms_of(d, self.entity_names)
            for d in self.definitions.values()
            if narrator or rules.visible_to_characters(d)
        ]

    def state_words(self) -> tuple[str, ...]:
        """Every player-visible State's terms (#176's reactive escalation reads them)."""
        return tuple(w for t in self.terms(narrator=False) for w in t.words)

    def narrator_section(self) -> tuple[tuple[str, ...], str]:
        """The narrator's ``## Tracked values``: every State with its value at the turn's start
        and its constraints (an external one's value is in its ``## External data`` block,
        #208), and the effect syntax (nothing when effects are off)."""
        if not self.on or not self.definitions:
            return (), ""
        found = list(self.definitions.values())
        lines: list[str] = []
        for d in found:
            if d.owner is StateOwner.EXTERNAL:
                # #205: only a pinned external State is listed; #208: its value is in its fenced
                # ``## External data`` block, never on this line.
                pinned = {
                    scope: Undefined if v is None else v
                    for (name, scope), v in self.external.items()
                    if name == d.name
                }
                if pinned:
                    lines.append(rules.narrator_line(d, pinned, self.names, external=True))
                continue
            values = {scope: v for (name, scope), v in self.values.items() if name == d.name}
            lines.append(rules.narrator_line(d, values, self.names))
        return tuple(lines), rules.syntax(found, character=False)


def level_of(settings: ProfileSettings) -> str | None:
    """The level ``states`` runs at under ``settings`` (``None``: off, or undelivered)."""
    level = schema.level_of(settings, states.MODULE)
    if level is None or not schema.is_delivered(states.MODULE, level):
        return None
    return level


async def load(conn: AsyncConnection, story_id: str, settings: ProfileSettings) -> Book:
    """The turn's ``Book`` (see the module docstring)."""
    level = level_of(settings)
    if level is None:
        return Book(story_id)
    found = await states.definitions(conn, story_id)
    book = Book(
        story_id,
        level,
        {d.name: d for d in found},
        await states.current_values(conn, story_id, found),
    )
    rows = await conn.execute(
        select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
    )
    book.names = {str(r.id): str(r.name) for r in rows}
    tracked = [d.entity_id for d in found if d.entity_id is not None]
    if tracked:
        named = await conn.execute(
            select(entities.c.id, entities.c.canonical_name).where(
                entities.c.story_id == story_id, entities.c.id.in_(tracked)
            )
        )
        book.entity_names = {str(r.id): str(r.canonical_name) for r in named}
    return book


# -- the dry-run ---------------------------------------------------------------------------------


def _narrator(beat: TurnBeat) -> bool:
    return beat.narrator or beat.character_id is None


def planned(beats: Sequence[TurnBeat], stage: TurnStage | None = None) -> list[Planned]:
    """The turn's declared effects in beat order: ``beats``' own, and a staged beat's (#174)
    where ``beats`` has no beat of that number."""
    by_number: dict[int, tuple[str | None, tuple[Any, ...]]] = {}
    if stage is not None:
        for s in stage.beats:
            by_number[s.beat] = (None if s.narrator else s.character_id, tuple(s.effects))
    for b in beats:
        by_number[b.beat] = (None if _narrator(b) else b.character_id, b.effects)
    return [
        Planned(number, n, actor, effect)
        for number, (actor, effects) in sorted(by_number.items())
        for n, effect in enumerate(effects)
    ]


def findings(
    book: Book,
    beats: Sequence[TurnBeat],
    stage: TurnStage | None = None,
    *,
    only: frozenset[int] | None = None,
) -> list[Finding]:
    """What the dry-run and the narrated-change read find in ``beats`` (those of ``only``,
    when given), each with its repair line and refusal (see the module docstring)."""
    if not book.on:
        return []
    _, refused = rules.dry_run(book.definitions, book.values, planned(beats, stage))
    by_number = {b.beat: b for b in beats}
    found = [f for f in refused if f.beat in by_number]
    for b in beats:
        if b.beat <= 1 or b.attempt:
            continue
        found += rules.narrated(
            b.beat,
            b.kind,
            b.text,
            b.effects,
            book.terms(narrator=_narrator(b)),
            book.definitions,
        )
    if only is not None:
        found = [f for f in found if f.beat in only]
    return [_decorated(book, by_number[f.beat], f) for f in found]


def _decorated(book: Book, beat: TurnBeat, finding: Finding) -> Finding:
    definition = book.definitions.get(finding.state)
    narrator = _narrator(beat)
    span = finding.span
    if span is None:
        span = rules.span_of(beat.text, finding.state, book.terms(narrator=narrator))
    actor = None if beat.character_id is None else book.names.get(beat.character_id)
    return replace(
        finding,
        span=span,
        line=rules.constraint_line(finding, definition, narrator=narrator),
        refusal=rules.refusal_text(beat.kind, actor, definition, finding.constraint),
    )


def asserter_of(beat: TurnBeat) -> Asserter:
    """Who asserts ``beat``'s change: the director for the narrator, the player for its
    ``co_author`` narration (#180), the narrating render for a reaction (#176), else the
    beat's character."""
    if _narrator(beat):
        return Asserter("director")
    cid = str(beat.character_id)
    if beat.narrated_by == PLAYER_NARRATOR:
        return Asserter("co_author", cid)
    if beat.narrated_by is not None:
        return Asserter("reactive", beat.narrated_by, reactor=cid)
    return Asserter("character", cid)


def claims_of(
    found: Sequence[Finding], beats: Sequence[TurnBeat], operation_id: str
) -> list[CheckedClaim]:
    """Each finding as a checked claim of its beat (see the module docstring)."""
    by_number = {b.beat: b for b in beats}
    claims: list[CheckedClaim] = []
    for n, f in enumerate(found):
        beat = by_number[f.beat]
        start, end = f.span if f.span is not None else (0, len(beat.text))
        asserter = asserter_of(beat)
        undeclared = f.tag == rules.STATE_UNDECLARED
        kind = VerdictKind.NOVEL if undeclared else VerdictKind.UNCERTAIN
        claims.append(
            CheckedClaim(
                claim=DetectedClaim(
                    span=(f.beat, start, end),
                    layer="declared" if f.effect is not None else "detector",
                    category="declared" if f.effect is not None else "quantity",
                    channel=beat.kind,
                    speaker=NARRATOR if _narrator(beat) else str(beat.character_id),
                ),
                result=VerifyResult(
                    claim=None,
                    verdict=Verdict(kind),
                    kind=kind,
                    asserter=asserter,
                    permission=state_change(asserter, validated=False) if undeclared else None,
                    state_finding=f,
                ),
                claim_id=f"{operation_id}:state:{f.beat}:{n}",
            )
        )
    return claims


def _unit_of(claim: CheckedClaim) -> str | None:
    sketch = claim.claim.sketch
    literal = None if sketch is None else sketch.literal
    unit = None if literal is None else literal.unit
    return None if not unit else str(unit).casefold()


def _names_term(unit: str, words: Sequence[str]) -> bool:
    return any(unit in (w, w.rstrip("s"), f"{w}s") or w in (unit, f"{unit}s") for w in words)


def covered(
    claims: Sequence[CheckedClaim], beats: Sequence[TurnBeat], book: Book
) -> list[CheckedClaim]:
    """``claims`` with each lone quantity #148 detected ("300 gold") that names a State the
    beat declares an effect on, or one of its State findings is about, taken as that effect's
    (FR-STATE-5): its amount is declared through the channel (or repaired as the finding), so
    it is never repaired twice; such a claim's verdict is the effect's (``None`` here)."""
    if not book.on:
        return list(claims)
    by_number = {b.beat: b for b in beats}
    about: dict[int, set[str]] = {}
    for c in claims:
        if is_state_claim(c) and c.claim.beat is not None and c.result is not None:
            finding = c.result.state_finding
            if finding is not None:
                about.setdefault(c.claim.beat, set()).add(finding.state)
    out: list[CheckedClaim] = []
    for c in claims:
        unit = _unit_of(c) if c.claim.category == "quantity" else None
        beat = None if c.claim.beat is None else by_number.get(c.claim.beat)
        if unit is not None and beat is not None and c.result is not None:
            named = {e.state for e in beat.effects} | about.get(beat.beat, set())
            words = [w for t in book.terms(narrator=True) if t.state in named for w in t.words]
            if _names_term(unit, words):
                out.append(replace(c, result=None))
                continue
        out.append(c)
    return out


def is_state_claim(claim: CheckedClaim) -> bool:
    """Whether ``claim`` is one of ``claims_of``'s."""
    return claim.result is not None and claim.result.state_finding is not None


# -- the commit ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Committed:
    """One effect the commit applied."""

    beat: int
    state: str
    scope_key: str | None
    applied: states.Applied


def _change(
    op: Operation, p: Planned, definition: StateDefinition, role: str | None
) -> StateChange:
    effect = p.effect
    scope = rules.scope_of(effect, definition, p.actor)
    position = Position(
        int(op.narrative_turn or 0),
        int(op.turn_variant or 0),
        0,
        p.beat,
        step_of(p.beat, op.operation_id),
    )
    if effect.delta is not None:
        return StateChange(
            state=definition.name,
            reason=effect.reason,
            source=StateChangeSource.TURN,
            kind=StateChangeKind.DELTA,
            delta=Decimal(str(effect.delta)),
            scope_key=scope,
            asserter_role=role,
            position=position,
        )
    return StateChange(
        state=definition.name,
        reason=effect.reason,
        source=StateChangeSource.TURN,
        kind=StateChangeKind.SET,
        value=effect.value,
        scope_key=scope,
        asserter_role=role,
        position=position,
    )


async def apply_declared(
    conn: AsyncConnection,
    op: Operation,
    beats: Sequence[TurnBeat],
    book: Book,
    *,
    roles: Mapping[str, str] | None = None,
) -> list[Committed]:
    """Write every surviving effect of ``beats`` in ``op``'s commit transaction (see the module
    docstring); ``roles``: each character's #176 role for the turn (``primary`` otherwise)."""
    done: list[Committed] = []
    for p in planned(beats):
        definition = book.definitions.get(p.effect.state)
        if definition is None:
            raise RebaseCheckFailed("a State the turn changes is gone")
        role = None if p.actor is None else (roles or {}).get(p.actor, "primary")
        try:
            found = await states.apply_change(conn, op, _change(op, p, definition, role))
        except (StateNotFound, ModuleOff) as exc:
            raise RebaseCheckFailed("the turn's States changed under it") from exc
        if isinstance(found, states.Violation):
            raise RebaseCheckFailed("a State moved since the turn's snapshot")
        done.append(
            Committed(p.beat, definition.name, rules.scope_of(p.effect, definition, p.actor), found)
        )
    log.debug("state effects: %d committed", len(done))
    return done


# -- the rebase re-check -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Recorded:
    attempt: int
    story_id: str
    planned: tuple[Planned, ...]
    interpretive: Mapping[str, list[Any]]


_RECORDED: dict[int, tuple[weakref.ref[StructuredReadSet], _Recorded]] = {}


def remember(read_set: StructuredReadSet, book: Book, beats: Sequence[TurnBeat]) -> None:
    """Record the turn's State reads for its attempt: the effects it will commit and the
    interpretive members of each State they touch (``recheck``)."""
    found = tuple(planned(beats))
    touched = {p.effect.state for p in found}
    key = id(read_set)
    _RECORDED[key] = (
        weakref.ref(read_set, lambda _: _RECORDED.pop(key, None)),
        _Recorded(
            read_set.attempt,
            book.story_id,
            found,
            {n: book.definitions[n].interpretive() for n in touched if n in book.definitions},
        ),
    )


def recorded(read_set: StructuredReadSet) -> _Recorded | None:
    entry = _RECORDED.get(id(read_set))
    if entry is None or entry[0]() is not read_set or entry[1].attempt != read_set.attempt:
        return None
    return entry[1]


async def recheck(conn: AsyncConnection, op: Operation) -> bool:
    """The rebase re-check (see the module docstring); ``False`` regenerates."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet):
        return True
    found = recorded(source)
    if found is None or not found.planned:
        return True
    if await states.state_level(conn, found.story_id) != states.ON:
        return False
    current = {d.name: d for d in await states.definitions(conn, found.story_id)}
    for name, members in found.interpretive.items():
        if name not in current or current[name].interpretive() != members:
            log.debug("rebase re-check: a State's declaration changed")
            return False
    values = await states.current_values(conn, found.story_id, list(current.values()))
    _, refused = rules.dry_run(current, values, found.planned)
    if refused:
        log.debug("rebase re-check: %d effects no longer fit", len(refused))
    return not refused


register_rebase_check(recheck)


# -- the response --------------------------------------------------------------------------------

RESPONSE_FIELDS = (
    "state",
    "scope_key",
    "old_value",
    "new_value",
    "reason",
    "source",
    "contingency",
    "status",
)


async def committed(
    conn: AsyncConnection, story_id: str, operation_id: str, *, omniscient: bool
) -> list[dict[str, Any]]:
    """The response's ``state_effects`` (FR-RESP-7; see the module docstring)."""
    found = await states.turn_effects(conn, story_id, [operation_id], omniscient=omniscient)
    return [{k: e[k] for k in RESPONSE_FIELDS} for e in found.get(operation_id, [])]


def report(book: Book, beats: Sequence[TurnBeat], repaired: Sequence[Finding]) -> dict[str, Any]:
    """``turn_report.state_effects``: counts and codes only."""
    tags: dict[str, int] = {}
    for f in repaired:
        tags[f.tag] = tags.get(f.tag, 0) + 1
    return {
        "level": book.level,
        "declared": sum(len(b.effects) for b in beats),
        "repaired": dict(sorted(tags.items())),
    }


__all__ = [
    "STATE_EFFECTS",
    "Book",
    "Committed",
    "apply_declared",
    "asserter_of",
    "claims_of",
    "committed",
    "covered",
    "findings",
    "is_state_claim",
    "level_of",
    "load",
    "planned",
    "recheck",
    "recorded",
    "remember",
    "report",
]
