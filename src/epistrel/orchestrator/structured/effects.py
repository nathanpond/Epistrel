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
come through #174's ``TurnStage`` (``StagedBeat.effects``; a ``simultaneous`` turn's primaries
are each folded alone over the snapshot, and a shared State they break together is #175's
conflict, ``state_conflicts``). Each effect is authorized before the bounds (#188,
``core.states.authorize``: its beat's asserter, ``source_of``; the turn's beats as the evidence
a justification cites, ``evidence_of``; #189: the action preconditions over the canon
``read_preconditions`` read through the turn's keyed lookups before each dry-run): an
unpermitted one is ``state_unpermitted``, repaired like a refused one; one above a cap is
rewritten down to it deterministically before anything else sees it (``cap``:
``state_capped``, its declared amount kept for the response). Each ``do`` and narration beat
is read for a State change it narrates with no effect
(``core.states.effects.narrated``). #190 (FR-STATE-13): completeness in both directions
(``completeness_of``, ``core.states.completeness.diff``) over the turn's verified beats, their
detected claims and declared effects: a narrated change no effect covers (another amount or
direction too) is the same ``state_undeclared`` finding, merged by beat and State; a declared
effect on a player-visible State nothing narrates is ``state_unnarrated``; and a value stated
after an earlier beat's effect is judged against what that effect leaves (``folded``). Each
finding becomes a checked claim of its beat (``claims_of``) whose
``VerifyResult.state_finding`` makes it ``needs_repair`` with the finding's tag:
``state_constraint``, ``effect_unresolved``, ``state_undeclared`` (a ``state_change`` denial,
#144's reserved slot) or ``state_unnarrated``. The repair ladder repairs it in character
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

**Read-only external values** (#209, FR-EXT-9, FR-RESP-7's external half): an effect on a State
owned by a provider that is not ``write_capable`` (``Book.read_only``; a transfer's own side, or
its other side when that is one) is never applied: the turn stages a ``requested_effect`` event
(``input``; ``{state, scope_key, delta|value, reason, effect_id, status: requested}``) at the
declaring beat (``requested_of``, ``stage_requested``) and writes no ``state_changes`` row; the
response lists it with ``owner: external``, ``status: pending`` and its ``effect_id`` (the key a
consumer's outcome report names, #217). Its dry-run runs against the value pinned for the turn
(``Book.pin``, #205), and the rebase re-check leaves it out (nothing of it is committed). A
beat asserting such a change's outcome ("the purchase went through", "your balance is now
250") is ``external_outcome_unconfirmed`` (``core.states.effects.unconfirmed_outcomes``),
repaired to the attempt; the composed message is checked the same way (``outcome_check``,
#152). The next turns' narrator inputs list the requests of the last ``PENDING_TURNS`` turns
with no recorded outcome (``pending_outcomes``); no character's context does (FR-EXT-21 (e)).
An external world clock's pacing and skips are requested the same way (``kind:
time_advance``, ``time_requests``).

**Write-capable external values** (#210, FR-EXT-10,15): an effect on a State whose provider
is ``write_capable`` (``Book.outbox``) is requested the same way, but Epistrel dispatches it:
instead of a ``requested_effect`` event, the commit writes an ``external_effect_outbox`` row
(``intended``, its id the ``effect_id``, at the declaring beat) and enqueues its dispatch job
in the turn's own transaction (``write_outbox``; ``core.outbox``,
``orchestrator.jobs.handlers.outbox``). The response lists it ``pending`` with its
``outbox_id`` (the consumer does not apply it), and its outcome prose is held back like a
read-only one's until a confirmation is recorded. Until #212's staged turns, a
``synchronous`` provider's effects take this asynchronous path too.

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
import re
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import outbox as core_outbox
from epistrel.core import providers as core_providers
from epistrel.core import states
from epistrel.core.concurrency import RebaseCheckFailed
from epistrel.core.enums import (
    Provenance,
    StateChangeKind,
    StateChangeSource,
    StateOwner,
    StateScope,
    StateVisibility,
)
from epistrel.core.epistemic.conflicts import Conflict, StateClaim, detect_state_conflicts
from epistrel.core.errors import ModuleOff, StateNotFound
from epistrel.core.events.types import REQUESTED_EFFECT, RequestedEffect
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, active_operation, step_of
from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.vocabulary import STATE_VALUE
from epistrel.core.readset import StructuredReadSet, register_rebase_check
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.states import (
    StateChange,
    StateDefinition,
    Undefined,
    UndefinedType,
    completeness,
)
from epistrel.core.states import authorize as permission
from epistrel.core.states import effects as rules
from epistrel.core.states import preconditions as action_preconditions
from epistrel.core.states import verify as state_verify
from epistrel.core.states.authorize import Evidence, Source
from epistrel.core.states.effects import Finding, Planned
from epistrel.core.states.model import WORLD_CLOCK, Value, number, to_json
from epistrel.core.tables import characters, entities, events, operations
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.other_agent import PLAYER_NARRATOR
from epistrel.core.verify.permission import Asserter, state_change
from epistrel.core.verify.rules import OwnedValue, Verdict, VerdictKind
from epistrel.orchestrator.structured.render import TrackedState
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import VerifyResult

if TYPE_CHECKING:
    from epistrel.core.epistemic.stage import TurnStage
    from epistrel.core.events.operation import Operation
    from epistrel.core.world_clock.timing import TurnTiming

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
    preconditions: action_preconditions.Canon | None = None
    """#189: the canon the attempt read for its effects' action preconditions
    (``read_preconditions``; ``None``: not read yet, so none is checked)."""
    clock: Any = None
    """#194: the world clock as the attempt starts (``orchestrator.structured.clocking.
    ClockRead``; ``None``: the story does not run it): the render schemas read its level."""
    read_only: frozenset[str] = frozenset()
    """#209: the externally owned States whose provider is not ``write_capable``: an effect on
    one is requested of the consumer, never applied (``requested_of``)."""
    outbox: frozenset[str] = frozenset()
    """#210: the externally owned States whose provider is ``write_capable``: an effect on one
    is dispatched through the outbox, never applied by Epistrel (``write_outbox``)."""

    @property
    def requested(self) -> frozenset[str]:
        """Every externally owned State an effect is requested of rather than applied to."""
        return self.read_only | self.outbox

    def pin(self, external: Mapping[tuple[str, str | None], Value | None]) -> None:
        """#205, #209: the turn's pinned external values (``PinnedReads.by_name``), which the
        dry-run reads as an externally owned State's value at the turn's start (``None``:
        unknown)."""
        self.external = dict(external)
        for key, value in external.items():
            if key[0] in self.definitions:
                self.values[key] = Undefined if value is None else value

    @property
    def clock_level(self) -> str | None:
        """#194: the clock's level (``None``: off): a time advance needs it."""
        return None if self.clock is None else str(self.clock.level)

    @property
    def clock_fixed(self) -> bool:
        """#194: whether the anchor fixes the time of day (a ``to`` advance needs it)."""
        return bool(self.clock is not None and self.clock.fixed)

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
    read_only: set[str] = set()
    outbox: set[str] = set()
    for d in found:
        if d.owner is StateOwner.EXTERNAL and d.provider_id is not None:
            provider = await core_providers.find_provider(conn, d.provider_id)
            if provider is None or not provider.write_capable:
                read_only.add(d.name)
            else:
                # #210: a write-capable provider's value goes through the outbox.
                outbox.add(d.name)
    book.read_only = frozenset(read_only)
    book.outbox = frozenset(outbox)
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


def source_of(beat: TurnBeat) -> Source:
    """Who asserts ``beat``'s effects, as #188's ``authorize`` reads it (``asserter_of``)."""
    asserter = asserter_of(beat)
    return Source(asserter.kind, asserter.character_id)  # type: ignore[arg-type]


def _staged_source(narrator: bool, character_id: str | None) -> Source:
    if narrator or character_id is None:
        return Source("director")
    return Source("character", character_id)


def planned(beats: Sequence[TurnBeat], stage: TurnStage | None = None) -> list[Planned]:
    """The turn's declared effects in beat order: ``beats``' own, and a staged beat's (#174)
    where ``beats`` has no beat of that number, each with its asserter (#188)."""
    by_number: dict[int, tuple[str | None, tuple[Any, ...], Source]] = {}
    if stage is not None:
        for s in stage.beats:
            by_number[s.beat] = (
                None if s.narrator else s.character_id,
                tuple(s.effects),
                _staged_source(s.narrator, s.character_id),
            )
    for b in beats:
        by_number[b.beat] = (None if _narrator(b) else b.character_id, b.effects, source_of(b))
    return [
        Planned(number, n, actor, effect, source)
        for number, (actor, effects, source) in sorted(by_number.items())
        for n, effect in enumerate(effects)
    ]


def evidence_of(beats: Sequence[TurnBeat], stage: TurnStage | None = None) -> dict[int, Evidence]:
    """The turn's beats a justification may cite (#188): every beat verification kept (present,
    not an attempt), with the #148 categories its text holds."""
    found: dict[int, Evidence] = {}
    if stage is not None:
        for s in stage.beats:
            found[s.beat] = Evidence(
                s.beat,
                s.kind,
                permission.categories_of(s.text),
                s.narrator or s.kind == "narration",
            )
    for b in beats:
        if b.attempt:
            found.pop(b.beat, None)
            continue
        found[b.beat] = Evidence(
            b.beat, b.kind, permission.categories_of(b.text), _narrator(b) or b.kind == "narration"
        )
    return found


def _alone(planned_: Sequence[Planned]) -> list[list[Planned]]:
    """A ``simultaneous`` turn's effects by asserting primary (the narrator's and the input's
    together), each folded alone over the snapshot."""
    groups: dict[str | None, list[Planned]] = {}
    for p in planned_:
        groups.setdefault(p.actor, []).append(p)
    return list(groups.values())


def _dry_run(
    book: Book,
    beats: Sequence[TurnBeat],
    stage: TurnStage | None,
    *,
    simultaneous: bool = False,
) -> list[Finding]:
    ordered = planned(beats, stage)
    evidence = evidence_of(beats, stage)
    context = (
        None
        if book.preconditions is None
        else action_preconditions.context_of(book.preconditions, beats, stage)
    )
    if not simultaneous:
        return rules.dry_run(
            book.definitions, book.values, ordered, evidence=evidence, preconditions=context
        )[1]
    found: list[Finding] = []
    for group in _alone(ordered):
        found += rules.dry_run(
            book.definitions, book.values, group, evidence=evidence, preconditions=context
        )[1]
    return sorted(found, key=lambda f: (f.beat, f.effect if f.effect is not None else -1))


async def read_preconditions(
    conn: AsyncConnection,
    read_set: StructuredReadSet,
    book: Book,
    beats: Sequence[TurnBeat],
    stage: TurnStage | None,
    *,
    turn: int,
) -> None:
    """#189 (FR-STATE-11): read, through the turn's keyed lookups, the canon the action
    preconditions of ``beats``' (and ``stage``'s) declared effects need (``core.states
    .preconditions.read``) into ``book``, before the dry-run that checks them. Nothing is read
    when effects are off or no effect is declared."""
    if not book.on:
        return
    effects = []
    for p in planned(beats, stage):
        definition = book.definitions.get(p.effect.state)
        if definition is None:
            continue
        key = rules.scope_of(p.effect, definition, p.actor)
        effects.append(replace(rules.effect_of(p.effect, definition, key), beat=p.beat))
    if not effects:
        return
    book.preconditions = await action_preconditions.read(
        conn, read_set, book.story_id, turn, effects, beats, stage
    )


def _declared(book: Book, beats: Sequence[TurnBeat]) -> list[completeness.Declared]:
    """``beats``' declared effects as ``completeness.diff`` takes them (#190)."""
    found: list[completeness.Declared] = []
    kept = {b.beat for b in beats if b.beat > 1 and not b.attempt}
    for p in planned(beats):
        definition = book.definitions.get(p.effect.state)
        if definition is None or p.beat not in kept:
            continue
        key = rules.scope_of(p.effect, definition, p.actor)
        found.append(
            completeness.Declared(
                p.beat,
                p.index,
                replace(rules.effect_of(p.effect, definition, key), beat=p.beat),
                p.effect.reason,
                p.effect.silent,
            )
        )
    return found


def completeness_of(
    book: Book,
    beats: Sequence[TurnBeat],
    stage: TurnStage | None = None,
    claims: Sequence[CheckedClaim] = (),
) -> completeness.Diff:
    """#190 (FR-STATE-13): ``core.states.completeness.diff`` over the turn's verified beats
    (``beats``; an earlier primary's staged beats are prose an effect's narration may sit in),
    their detected claims and their declared effects; never a viewpoint-filtered message."""
    views = [
        completeness.Beat(
            b.beat,
            b.kind,
            b.text,
            None if _narrator(b) else b.character_id,
            _narrator(b),
            b.attempt,
            len(b.advances),
            b.flashback,
        )
        for b in beats
        if b.beat > 1
    ]
    numbers = {b.beat for b in beats}
    staged = [] if stage is None else [s.text for s in stage.beats if s.beat not in numbers]
    return completeness.diff(
        [(c.claim_id, c.claim) for c in claims if not is_state_claim(c)],
        _declared(book, beats),
        book.definitions,
        beats=views,
        texts=staged,
        entity_names=book.entity_names,
    )


def _merged(
    book: Book, found: list[Finding], diff: completeness.Diff, numbers: frozenset[int]
) -> list[Finding]:
    """#190: ``found`` (the dry-run's and #187's narrated-change findings) with ``diff``'s:
    an ``undeclared`` change is the same finding as #187's ``state_undeclared`` (by beat and
    State: one claim, one repair), and an ``unnarrated`` effect is ``state_unnarrated`` unless
    the dry-run already refused it."""
    out = list(found)
    at = {(f.beat, f.state): n for n, f in enumerate(out) if f.effect is None}
    for u in diff.undeclared:
        if u.ref.beat not in numbers:
            continue
        definition = book.definitions.get(u.ref.state)
        merged = Finding(
            rules.STATE_UNDECLARED,
            u.ref.beat,
            u.ref.state,
            None if definition is None else definition.id,
            span=u.ref.span,
            constraint=rules.AMOUNT if u.mismatch else None,
            claim_id=u.claim_id,
        )
        key = (u.ref.beat, u.ref.state)
        if key in at:
            earlier = out[at[key]]
            if earlier.tag == rules.STATE_UNDECLARED and earlier.claim_id is None:
                out[at[key]] = replace(
                    merged, span=earlier.span or merged.span, constraint=merged.constraint
                )
            continue
        at[key] = len(out)
        out.append(merged)
    refused = {(f.beat, f.effect) for f in found if f.effect is not None}
    for d in diff.unnarrated:
        if d.beat not in numbers or (d.beat, d.index) in refused:
            continue
        definition = book.definitions.get(d.effect.state)
        out.append(
            Finding(
                rules.STATE_UNNARRATED,
                d.beat,
                d.effect.state,
                None if definition is None else definition.id,
                d.effect.scope_key,
                effect=d.index,
                narration=completeness.narration_line(d, definition),
            )
        )
    return out


def findings(
    book: Book,
    beats: Sequence[TurnBeat],
    stage: TurnStage | None = None,
    *,
    only: frozenset[int] | None = None,
    simultaneous: bool = False,
    claims: Sequence[CheckedClaim] = (),
) -> list[Finding]:
    """What the dry-run, the narrated-change read and #190's completeness diff (over
    ``claims``, the beats' detected claims) find in ``beats`` (those of ``only``, when
    given), each with its repair line and refusal (see the module docstring)."""
    if not book.on:
        return []
    refused = _dry_run(book, beats, stage, simultaneous=simultaneous)
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
    found = _merged(book, found, completeness_of(book, beats, stage, claims), frozenset(by_number))
    found += outcome_findings(book, beats, stage)
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
    refusal = (
        rules.attempt_text(beat.kind, actor, definition)
        if finding.tag == rules.EXTERNAL_OUTCOME_UNCONFIRMED
        else rules.refusal_text(beat.kind, actor, definition, finding.constraint)
    )
    return replace(
        finding,
        span=span,
        line=rules.constraint_line(finding, definition, narrator=narrator),
        refusal=refusal,
    )


# -- read-only external values (#209) ------------------------------------------------------------


PENDING_TURNS = 3
"""How many earlier turns' unanswered requests the narrator's inputs list (``pending_outcomes``)."""
TIME_ADVANCE = "time_advance"
"""A requested external clock advance's ``kind``."""


@dataclass(frozen=True, slots=True)
class Requested:
    """One change the turn asks the consumer to apply (see the module docstring): its stable
    ``effect_id``, ``kind`` (``change``, ``transfer``, ``time_advance``), the State and scope key,
    the change (``delta`` or ``value``; ``minutes`` for a clock advance), the render's reason,
    the declaring beat and step, the value pinned for the turn (``current``, for the response)
    and whether the State is ``hidden`` (its request is listed for ``omniscient`` only)."""

    effect_id: str
    kind: str
    state: str
    scope_key: str | None
    reason: str
    beat: int
    delta: Any = None
    value: Any = None
    minutes: int | None = None
    step: int = 0
    current: Any = None
    hidden: bool = False
    dispatch: bool = False
    """#210: dispatched through the outbox (a write-capable provider's value), not applied by
    the consumer."""

    def payload(self) -> RequestedEffect:
        """The ``requested_effect`` event's payload."""
        return RequestedEffect(
            effect_id=self.effect_id,
            kind=self.kind,
            state=self.state,
            scope_key=self.scope_key,
            delta=self.delta,
            value=self.value,
            minutes=self.minutes,
            reason=self.reason,
        )

    def response(self) -> dict[str, Any]:
        """The response's ``state_effects`` entry: pending, owned by the consumer."""
        row: dict[str, Any] = {
            "state": self.state,
            "scope_key": self.scope_key,
            "old_value": self.current,
            "new_value": None,
            "reason": self.reason,
            "source": StateChangeSource.TURN.value,
            "contingency": "unconditional",
            "status": "pending",
            "owner": StateOwner.EXTERNAL.value,
            "effect_id": self.effect_id,
        }
        if self.dispatch:
            row["outbox_id"] = self.effect_id
        if self.kind == TIME_ADVANCE:
            row["kind"] = TIME_ADVANCE
            row["minutes"] = self.minutes
        elif self.delta is not None:
            row["delta"] = self.delta
        else:
            row["value"] = self.value
        return row


def _requested(
    book: Book,
    definition: StateDefinition,
    scope_key: str | None,
    p: Planned,
    *,
    kind: str,
    delta: Any = None,
    value: Any = None,
) -> Requested:
    current = book.values.get((definition.name, scope_key), Undefined)
    return Requested(
        effect_id=new_ulid(),
        kind=kind,
        state=definition.name,
        scope_key=scope_key,
        reason=p.effect.reason,
        beat=p.beat,
        delta=delta,
        value=value,
        current=None if isinstance(current, UndefinedType) else to_json(current),
        hidden=definition.visibility is not StateVisibility.PLAYER_VISIBLE,
        dispatch=definition.name in book.outbox,
    )


def requested_of(book: Book, beats: Sequence[TurnBeat]) -> list[Requested]:
    """The changes ``beats``' surviving effects ask the consumer to apply: each effect on a
    read-only external State, and a transfer's other side when that is one (see the module
    docstring), in beat order."""
    if not book.on or not book.requested:
        return []
    found: list[Requested] = []
    for p in planned(beats):
        definition = book.definitions.get(p.effect.state)
        if definition is None:
            continue
        own_scope = rules.scope_of(p.effect, definition, p.actor)
        transfer = p.effect.kind == permission.TRANSFER
        if definition.name in book.requested:
            found.append(
                _requested(
                    book,
                    definition,
                    own_scope,
                    p,
                    kind="transfer" if transfer else "change",
                    delta=p.effect.delta,
                    value=None if p.effect.delta is not None else p.effect.value,
                )
            )
        other = p.effect.counterparty
        if not transfer or other is None or p.effect.delta is None:
            continue
        name, scope_key, declared = other
        other_definition = book.definitions.get(name)
        if other_definition is None or name not in book.requested:
            continue
        moved = declared if declared is not None else to_json(-Decimal(str(p.effect.delta)))
        found.append(
            _requested(
                book,
                other_definition,
                None if other_definition.scope is StateScope.STORY else scope_key,
                p,
                kind="transfer",
                delta=moved,
            )
        )
    return found


def time_requests(timing: TurnTiming) -> list[Requested]:
    """An external world clock's advances for the turn (#194's pacing increments and declared
    skips, #196's stops already cut them) as requests of kind ``time_advance`` (FR-CLOCK-5): the
    consumer's clock is never moved by Epistrel."""
    return [
        Requested(
            effect_id=new_ulid(),
            kind=TIME_ADVANCE,
            state=WORLD_CLOCK,
            scope_key=None,
            reason=a.reason,
            beat=a.beat_position,
            minutes=int(a.minutes),
            step=a.step_position,
        )
        for a in timing.advances
        if a.minutes
    ]


def stage_requested(op: Operation, requested: Sequence[Requested]) -> list[Requested]:
    """Stage one ``requested_effect`` event per request, at its beat and step (an effect's
    step is its beat's, ``core.membership.step_of``); answers them with their steps."""
    staged: list[Requested] = []
    for r in requested:
        step = r.step if r.kind == TIME_ADVANCE else step_of(r.beat, op.operation_id)
        found = replace(r, step=step)
        if found.dispatch:
            # #210: an outbox row, written by the commit (``write_outbox``), not an event.
            staged.append(found)
            continue
        op.append(
            REQUESTED_EFFECT,
            found.payload(),
            provenance=Provenance.ENGINE,
            character_id=None,
            beat=found.beat,
            step=found.step,
        )
        staged.append(found)
    return staged


def is_requested(book: Book, p: Planned) -> bool:
    """Whether ``p``'s own side is requested (of the consumer, or through the outbox),
    never applied."""
    return p.effect.state in book.requested


def pending_of(
    book: Book, beats: Sequence[TurnBeat], stage: TurnStage | None
) -> list[rules.Pending]:
    """The turn's requested changes as ``core.states.effects`` reads their outcome in prose."""
    if not book.on or not book.requested:
        return []
    found: list[rules.Pending] = []
    for p in planned(beats, stage):
        definition = book.definitions.get(p.effect.state)
        if definition is None or definition.name not in book.requested:
            continue
        key = (definition.name, rules.scope_of(p.effect, definition, p.actor))
        current = book.values.get(key, Undefined)
        leaves: Value | UndefinedType = Undefined
        if p.effect.delta is not None and isinstance(current, Decimal):
            leaves = current + Decimal(str(p.effect.delta))
        elif p.effect.delta is None and p.effect.value is not None:
            leaves = number(p.effect.value) or Undefined
        found.append(
            rules.Pending(
                definition.name,
                definition.id,
                key[1],
                rules.terms_of(definition, book.entity_names).words,
                current,
                leaves,
            )
        )
    return found


def outcome_findings(
    book: Book, beats: Sequence[TurnBeat], stage: TurnStage | None = None
) -> list[Finding]:
    """Each beat's sentences asserting a requested change's outcome (FR-EXT-15; see the
    module docstring)."""
    pending = pending_of(book, beats, stage)
    if not pending:
        return []
    found: list[Finding] = []
    for b in beats:
        if b.beat <= 1 or b.attempt:
            continue
        found += rules.unconfirmed_outcomes(b.beat, b.kind, b.text, pending)
    return found


def outcome_check(
    book: Book, beats: Sequence[TurnBeat]
) -> Callable[[str], list[tuple[int, int]]] | None:
    """#152's check of a composed message (``orchestrator.compose.compose``): the spans of the
    text asserting a requested change's outcome; ``None`` when the turn requests none."""
    pending = pending_of(book, beats, None)
    if not pending:
        return None

    def check(text: str) -> list[tuple[int, int]]:
        return [span for _, span in rules.outcome_spans(text, pending)]

    return check


async def pending_outcomes(
    conn: AsyncConnection, story_id: str, turn: int, names: Mapping[str, str]
) -> tuple[str, ...]:
    """The narrator's ``pending_outcomes`` for turn ``turn`` (FR-EXT-9, FR-EXT-21 (e)): the
    requests of the ``PENDING_TURNS`` turns before it on the active timeline with no recorded
    outcome (none is recorded before #217's intake, so every one), oldest first, one line each
    (``core.states.effects.pending_line``; ``names`` the cast's names by id). A hint for the
    narrator only; never a character's."""
    e = events.c
    rows = await conn.execute(
        select(e.payload)
        .select_from(events.join(operations, operations.c.operation_id == e.operation_id))
        .where(
            e.story_id == story_id,
            e.type == REQUESTED_EFFECT,
            e.narrative_turn >= turn - PENDING_TURNS,
            e.narrative_turn < turn,
            active_operation(operations),
        )
        .order_by(e.seq)
    )
    lines = []
    for row in rows:
        payload = dict(row.payload or {})
        scope_key = payload.get("scope_key")
        holder = names.get(str(scope_key)) if scope_key else None
        lines.append(rules.pending_line(payload, holder))
    return tuple(lines)


def outbox_rows(book: Book, requested: Sequence[Requested]) -> list[core_outbox.Intended]:
    """#210: the outbox rows of ``requested``'s dispatched effects (``stage_requested`` gave
    them their steps): the provider and its value from the State's declaration."""
    found: list[core_outbox.Intended] = []
    for r in requested:
        definition = book.definitions.get(r.state)
        if not r.dispatch or definition is None or definition.provider_id is None:
            continue
        effect: dict[str, Any] = {"kind": r.kind, "state": r.state, "reason": r.reason}
        if r.delta is not None:
            effect["delta"] = r.delta
        else:
            effect["value"] = r.value
        found.append(
            core_outbox.Intended(
                id=r.effect_id,
                provider_name=definition.provider_id,
                value_name=definition.external_value_name or definition.name,
                scope_key=r.scope_key,
                effect=effect,
                beat=r.beat,
                step=r.step,
            )
        )
    return found


async def write_outbox(
    conn: AsyncConnection, op: Operation, book: Book, requested: Sequence[Requested]
) -> int:
    """#210 (FR-EXT-10): in the turn's commit transaction, write each dispatched effect's
    ``intended`` outbox row and enqueue its dispatch job (the enqueue's NOTIFY wakes a worker
    when the turn commits); a failure anywhere leaves neither the turn nor the rows."""
    from epistrel.orchestrator.jobs.handlers import outbox as dispatcher

    rows = outbox_rows(book, requested)
    written = await core_outbox.write_intended(conn, op, rows)
    assert op.story_id is not None  # noqa: S101 - a turn belongs to a story
    for row in rows:
        await dispatcher.enqueue(conn, row.id, op.story_id)
    return written


_AMOUNT = r"(?<![\w.,]){}(?![\w]|[.,]\d)"


def _renumbered(text: str, declared: Decimal, capped: Decimal) -> str:
    """``text`` with the first literal of ``declared``'s magnitude ("100000", "100,000")
    rewritten to ``capped``'s, in the same form (the narration repaired to match the cap)."""
    old, new = abs(declared), abs(capped)
    if old != old.to_integral_value() or new != new.to_integral_value():
        forms = [(f"{old.normalize():f}", f"{new.normalize():f}")]
    else:
        forms = [(f"{int(old):,}", f"{int(new):,}"), (str(int(old)), str(int(new)))]
    for was, now in forms:
        hit = re.search(_AMOUNT.format(re.escape(was)), text)
        if hit is not None:
            return text[: hit.start()] + now + text[hit.end() :]
    return text


def capped_beat(beat: TurnBeat, found: Sequence[Finding]) -> TurnBeat:
    """``beat`` with each of its ``state_capped`` findings' effects rewritten down to the cap
    (#188's deterministic repair): the effect's delta (or a set's value) becomes what the cap
    leaves, its ``declared`` amount is kept, a counterparty's own delta follows, and the
    narration's literal amount is rewritten to match."""
    effects = list(beat.effects)
    text = beat.text
    for f in found:
        if f.tag != rules.STATE_CAPPED or f.effect is None or f.amount is None:
            continue
        if not 0 <= f.effect < len(effects):
            continue
        e = effects[f.effect]
        first = e.delta if e.delta is not None else e.value
        declared = e.declared if e.declared is not None else first
        if e.delta is not None:
            was = Decimal(str(e.delta))
            other = e.counterparty
            if other is not None and other[2] is not None:
                other = (other[0], other[1], to_json(-f.amount))
            effects[f.effect] = replace(
                e, delta=to_json(f.amount), declared=declared, counterparty=other
            )
        else:
            if isinstance(f.attempted, UndefinedType) or not isinstance(f.current, Decimal):
                continue
            was = Decimal(str(e.value)) - f.current
            effects[f.effect] = replace(e, value=to_json(f.attempted), declared=declared)
        text = _renumbered(text, was, f.amount)
    return replace(beat, effects=tuple(effects), text=text)


MAX_CAP_PASSES = 32
"""The most effects one turn's caps rewrite (one per pass, in beat order)."""


def cap(
    book: Book,
    beats: Sequence[TurnBeat],
    stage: TurnStage | None = None,
    *,
    simultaneous: bool = False,
) -> tuple[list[TurnBeat], list[Finding]]:
    """``beats`` with every effect above its State's caps rewritten down to them
    (``capped_beat``), and the ``state_capped`` findings it settled; one at a time in beat
    order, so a per-turn cap holds across the turn's effects (a later one capped to what the
    earlier leave)."""
    if not book.on:
        return list(beats), []
    out = list(beats)
    settled: list[Finding] = []
    numbers = {b.beat for b in beats}
    for _ in range(MAX_CAP_PASSES):
        capped = [
            f
            for f in _dry_run(book, out, stage, simultaneous=simultaneous)
            if f.tag == rules.STATE_CAPPED and f.beat in numbers
        ]
        if not capped:
            break
        first = min(capped, key=lambda f: (f.beat, f.effect or 0))
        out = [capped_beat(b, [first]) if b.beat == first.beat else b for b in out]
        settled.append(first)
    return out, settled


# -- a simultaneous turn's shared States (#188) ------------------------------------------------


async def tie_break_book(conn: AsyncConnection, story_id: str, book: Book) -> Book:
    """The States a ``state:<name>`` tie-break reads: the turn's book, or (with State effects
    off) the story's States and their values read now."""
    if book.on:
        return book
    found = await states.definitions(conn, story_id)
    return Book(
        story_id,
        book.level,
        {d.name: d for d in found},
        await states.current_values(conn, story_id, found),
    )


def state_claims(book: Book, beats: Sequence[TurnBeat], order: Sequence[str]) -> list[StateClaim]:
    """A ``simultaneous`` turn's primaries' declared effects on their State values (``order``
    the primaries in request order; the narrator's and the input's take no part), for
    ``core.epistemic.conflicts.detect_state_conflicts``."""
    rank = {c: n + 1 for n, c in enumerate(order)}
    found: list[StateClaim] = []
    for p in planned([b for b in beats if not b.attempt]):
        definition = book.definitions.get(p.effect.state)
        if definition is None or p.actor is None or p.actor not in rank:
            continue
        found.append(
            StateClaim(
                p.beat,
                p.actor,
                rank[p.actor],
                definition.name,
                rules.scope_of(p.effect, definition, p.actor),
                p.index,
            )
        )
    return found


def breaks(book: Book, beats: Sequence[TurnBeat]) -> Callable[[Sequence[StateClaim]], bool]:
    """Whether a shared State value cannot take a group of primaries' effects together: they
    set it to two values, or, folded in request order over the snapshot, one is refused."""
    effects = {(p.beat, p.index): p for p in planned(beats)}

    def check(group: Sequence[StateClaim]) -> bool:
        chosen = [effects[(c.beat, c.index)] for c in group if (c.beat, c.index) in effects]
        sets = {
            str(p.effect.value)
            for p in chosen
            if p.effect.delta is None and p.effect.value is not None
        }
        if len(sets) > 1:
            return True
        _, refused = rules.dry_run(book.definitions, book.values, chosen)
        return bool(refused)

    return check


def state_conflicts(book: Book, beats: Sequence[TurnBeat], order: Sequence[str]) -> list[Conflict]:
    """A ``simultaneous`` turn's conflicts over shared States (#188; see the module
    docstring)."""
    if not book.on:
        return []
    return detect_state_conflicts(state_claims(book, beats, order), breaks=breaks(book, beats))


def tie_break_values(book: Book, name: str, characters: Sequence[str]) -> dict[str, Decimal | None]:
    """Each of ``characters``' value of the number State ``name`` at the turn's start (a story
    State's one value for all; #188's ``state:<name>`` tie-break)."""
    definition = book.definitions.get(name)
    if definition is None:
        return {}
    found: dict[str, Decimal | None] = {}
    for cid in characters:
        key = (name, None if definition.scope.value == "story" else cid)
        value = book.values.get(key, definition.initial_value)
        found[cid] = value if isinstance(value, Decimal) else None
    return found


def refusal_of(
    book: Book, beats: Sequence[TurnBeat], beat: TurnBeat, actor: str | None, conflict: Conflict
) -> str:
    """The losing beat's attempt in a shared-State conflict: its State's refusal narrated (the
    constraint the group's effects, folded in request order, break; two values set at once
    cannot both hold)."""
    claims = [e for e in conflict.effects if isinstance(e, StateClaim)]
    definition = book.definitions.get(claims[0].state) if claims else None
    effects = {(p.beat, p.index): p for p in planned(beats)}
    chosen = [effects[(c.beat, c.index)] for c in claims if (c.beat, c.index) in effects]
    _, refused = rules.dry_run(book.definitions, book.values, chosen)
    constraint = refused[0].constraint if refused else "allowed_values"
    return rules.refusal_text(beat.kind, actor, definition, constraint)


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


def _owned(
    definition: StateDefinition, subject: str, value: Value | UndefinedType
) -> OwnedValue | None:
    if definition.id is None or isinstance(value, UndefinedType):
        return None
    return OwnedValue(
        proposition=state_verify.proposition(
            definition.id, subject, state_verify.value_literal(value)
        ),
        ref=f"{state_verify.REF_PREFIX}{definition.id}",
        kind=definition.type.value,
        tolerance=float(definition.constraints.tolerance),
        entity_id=definition.entity_id,
    )


def folded(
    claims: Sequence[CheckedClaim],
    beats: Sequence[TurnBeat],
    book: Book,
    stage: TurnStage | None = None,
) -> list[CheckedClaim]:
    """#190 (#191's handoff): a value claim (``state_value``, on a canon holder's objective
    path) in a beat after one whose effect changed that value in the same turn is judged
    against the value those earlier effects leave, not the turn's starting value: the turn's
    effects are not committed while it is verified ("Mira takes 300 gold." then "Cal has 200
    gold left." is supported)."""
    if not book.on:
        return list(claims)
    ordered = [replace(p, source=None) for p in planned(beats, stage)]
    passed, _ = rules.dry_run(book.definitions, book.values, ordered)
    if not passed:
        return list(claims)
    by_id = {d.id: d for d in book.definitions.values() if d.id is not None}
    out: list[CheckedClaim] = []
    for c in claims:
        p = c.claim.proposition
        result = c.result
        beat = c.claim.beat
        if (
            p is None
            or p.predicate_id != STATE_VALUE
            or result is None
            or result.speech is not None
            or result.kind not in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED)
            or beat is None
        ):
            out.append(c)
            continue
        definition = by_id.get(state_verify.state_of(p) or "")
        if definition is None:
            out.append(c)
            continue
        key = (definition.name, None if definition.scope.value == "story" else p.subject_id)
        earlier = [x for x in passed if x.key == key and x.beat < beat]
        owned = None if not earlier else _owned(definition, p.subject_id, earlier[-1].new)
        if owned is None:
            out.append(c)
            continue
        verdict = state_verify.verdict(p, [owned])
        out.append(replace(c, result=replace(result, verdict=verdict, kind=verdict.kind)))
    return out


def is_state_claim(claim: CheckedClaim) -> bool:
    """Whether ``claim`` is one of ``claims_of``'s."""
    return claim.result is not None and claim.result.state_finding is not None


# -- the commit ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Committed:
    """One effect the commit applied: for a transfer (#188) ``other`` is its tracked other
    side's row, and ``counterparty`` ``untracked`` or ``external`` when that side is not one;
    ``declared`` the amount the render declared when a cap repaired it down."""

    beat: int
    state: str
    scope_key: str | None
    applied: states.Applied
    other: states.Applied | None = None
    counterparty: str | None = None
    declared: Any = None

    def notes(self) -> dict[str, dict[str, Any]]:
        """What the response adds to this effect's rows (#188), by change id."""
        own: dict[str, Any] = {}
        if self.declared is not None:
            own.update(capped=True, declared=self.declared)
        if self.counterparty is not None:
            own["counterparty"] = self.counterparty
        found = {self.applied.change_id: own} if own else {}
        if self.other is not None and self.declared is not None:
            found[self.other.change_id] = {"capped": True}
        return found


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
        delta = Decimal(str(effect.delta))
        kind = StateChangeKind.DELTA
        if effect.kind == permission.TRANSFER:
            kind = StateChangeKind.TRANSFER_OUT if delta < 0 else StateChangeKind.TRANSFER_IN
        return StateChange(
            state=definition.name,
            reason=effect.reason,
            source=StateChangeSource.TURN,
            kind=kind,
            delta=delta,
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


def _other_change(
    own: StateChange, p: Planned, book: Book
) -> tuple[StateChange | None, str | None]:
    """A transfer's other side (#188): the tracked side's change (``transfer_in`` for an
    outgoing one, and the reverse), or ``None`` with ``untracked``/``external``."""
    other = p.effect.counterparty
    if p.effect.kind != permission.TRANSFER or other is None or own.delta is None:
        return None, None
    name, scope_key, declared = other
    kind = permission.counterparty_kind(book.definitions, name)
    if kind is not None:
        return None, kind
    definition = book.definitions[name]
    delta = -Decimal(own.delta) if declared is None else Decimal(str(declared))
    return (
        StateChange(
            state=name,
            reason=own.reason,
            source=StateChangeSource.TURN,
            kind=StateChangeKind.TRANSFER_OUT if delta < 0 else StateChangeKind.TRANSFER_IN,
            delta=delta,
            scope_key=None if definition.scope.value == "story" else scope_key,
            asserter_role=own.asserter_role,
            position=own.position,
        ),
        None,
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
        own = _change(op, p, definition, role)
        other, counterparty = _other_change(own, p, book)
        written: list[states.Applied] | states.Applied | states.Violation
        if is_requested(book, p):
            # #209: requested of the consumer (``requested_of``), never applied; a transfer's
            # tracked other side is the story's own and is written alone.
            if other is None:
                continue
            own, other, counterparty = other, None, StateOwner.EXTERNAL.value
        try:
            if p.effect.kind == permission.TRANSFER and own.state == definition.name:
                written = await states.apply_transfer(conn, op, own, other)
            else:
                written = await states.apply_change(conn, op, own)
        except (StateNotFound, ModuleOff) as exc:
            raise RebaseCheckFailed("the turn's States changed under it") from exc
        if isinstance(written, states.Violation):
            raise RebaseCheckFailed("a State moved since the turn's snapshot")
        found, *rest = written if isinstance(written, list) else [written]
        done.append(
            Committed(
                p.beat,
                own.state,
                own.scope_key,
                found,
                other=rest[0] if rest else None,
                counterparty=counterparty,
                declared=p.effect.declared,
            )
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
    evidence: Mapping[int, Evidence] = field(default_factory=dict)


_RECORDED: dict[int, tuple[weakref.ref[StructuredReadSet], _Recorded]] = {}


def remember(read_set: StructuredReadSet, book: Book, beats: Sequence[TurnBeat]) -> None:
    """Record the turn's State reads for its attempt: the effects it will commit and the
    interpretive members of each State they touch (``recheck``)."""
    # #209: a requested change commits nothing, so the rebase re-check leaves it out.
    found = tuple(p for p in planned(beats) if not is_requested(book, p))
    touched = {p.effect.state for p in found}
    key = id(read_set)
    _RECORDED[key] = (
        weakref.ref(read_set, lambda _: _RECORDED.pop(key, None)),
        _Recorded(
            read_set.attempt,
            book.story_id,
            found,
            {n: book.definitions[n].interpretive() for n in touched if n in book.definitions},
            evidence_of(beats),
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


async def recheck_authorization(conn: AsyncConnection, op: Operation) -> bool:
    """#188's rebase re-check (``state_authorization``, FR-STATE-10, FR-CONC-16): the turn's
    effects authorized again under the commit-time definitions (a source revoked, a
    justification list or a cap changed through ``update_definition``); any refusal, or an
    effect a cap would now cut, regenerates the turn; it is never repaired inside the commit."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet):
        return True
    found = recorded(source)
    if found is None or not found.planned:
        return True
    current = {d.name: d for d in await states.definitions(conn, found.story_id)}
    if any(p.effect.state not in current for p in found.planned):
        return False
    values = await states.current_values(conn, found.story_id, list(current.values()))
    _, refused = rules.dry_run(current, values, found.planned, evidence=found.evidence)
    unpermitted = [f for f in refused if f.tag in (rules.STATE_UNPERMITTED, rules.STATE_CAPPED)]
    if unpermitted:
        log.debug("rebase re-check: %d effects no longer authorized", len(unpermitted))
    return not unpermitted


register_rebase_check(recheck_authorization)


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
    conn: AsyncConnection,
    story_id: str,
    operation_id: str,
    *,
    omniscient: bool,
    done: Sequence[Committed] = (),
    requested: Sequence[Requested] = (),
) -> list[dict[str, Any]]:
    """The response's ``state_effects`` (FR-RESP-7; see the module docstring); ``done`` the
    commit's effects, whose notes (#188: ``capped`` with the ``declared`` amount, a transfer's
    ``counterparty: untracked|external``, the row's ``kind`` for a transfer side) it adds."""
    notes: dict[str, dict[str, Any]] = {}
    for c in done:
        notes.update(c.notes())
    found = await states.turn_effects(
        conn, story_id, [operation_id], omniscient=omniscient, with_ids=True
    )
    out: list[dict[str, Any]] = []
    for e in found.get(operation_id, []):
        row = {k: e[k] for k in RESPONSE_FIELDS}
        if e["kind"] in TRANSFER_KINDS:
            row["kind"] = e["kind"]
        row.update(notes.get(e["change_id"], {}))
        out.append(row)
    # #209: the changes requested of the consumer, pending (a hidden State's for omniscient).
    out += [r.response() for r in requested if omniscient or not r.hidden]
    return out


TRANSFER_KINDS = frozenset({StateChangeKind.TRANSFER_IN.value, StateChangeKind.TRANSFER_OUT.value})


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
    "PENDING_TURNS",
    "STATE_EFFECTS",
    "TIME_ADVANCE",
    "Book",
    "Committed",
    "Requested",
    "apply_declared",
    "asserter_of",
    "breaks",
    "cap",
    "capped_beat",
    "claims_of",
    "committed",
    "completeness_of",
    "covered",
    "evidence_of",
    "findings",
    "folded",
    "is_requested",
    "is_state_claim",
    "level_of",
    "load",
    "outbox_rows",
    "outcome_check",
    "outcome_findings",
    "pending_of",
    "pending_outcomes",
    "planned",
    "read_preconditions",
    "recheck",
    "recheck_authorization",
    "recorded",
    "refusal_of",
    "remember",
    "report",
    "requested_of",
    "source_of",
    "stage_requested",
    "state_claims",
    "state_conflicts",
    "tie_break_book",
    "tie_break_values",
    "time_requests",
    "write_outbox",
]
