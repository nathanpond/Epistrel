"""Hidden commitments and determined-but-unknown facts (FR-COMMIT-4,6,13, FR-EPI-8; docs/02 §4.4;
#169).

**A salient-but-hidden fact** (FR-COMMIT-4). When a character notices something the others do
not, the world decides it once and only that character knows. ``commit_hidden`` writes, in the
turn's commit transaction, the canon fact scoped away from everyone else (``visibility =
characters: [noticers]``, #159's scope) and, for each noticer with a partition, an
``observation`` transition (#160) of it citing the committing beat's event: an engine-made
witness transition at the beat's step + 1, ``known``, held from the beat, valid when the fact
is. When the noticer perceived only a signal (``observed = signal``: a pattern of car doors,
not who slams them) the truth is scoped ``engine``, away from everyone the noticer included,
and the observation is the signal's own (a separate ``commit_hidden`` of the signal with
``observed = fact``). The player's character has no partition: it is scoped in, and observes
nothing. With ``partitions`` off (every M4 profile) it degrades to #146's commit: the fact
with the caller's recorded scope and no observation.

**Determined, never per character** (FR-COMMIT-6, FR-EPI-8). A determined proposition has one
truth value at a valid-time for everyone; characters differ only in whether it is in their
partition. Canon has one holder (``core.canon.facts.refuse_holder``: any other is 422
``viewpoint_cannot_assign_truth``), a functional key has one current object over overlapping
time (``single_value_violations`` states the invariant), and a character's divergent view is an
owned belief record, never a second value. "Unknown to Jenni" is an absence in her partition,
never "undetermined for Jenni", which is a global thunk's state (``core.commit.lifecycle``).

**A reveal** (``reveal``). A sincere ``say`` or a ``do`` by a character the fact is scoped to,
asserting it, widens the fact: ``supersede`` with the scope plus the beat's content-fidelity
perceivers (an envelope or a partial eavesdrop is not access). Visibility is access, not
acceptance: a hearer whose testimony rejects it still gains access; their transitions are
#161's routing's and #162/#164's (nothing is written twice here). A ``deceptive`` ``say``
never widens, and a claim about one proposition widens only its fact. The reveal discharges
the hidden fact's ledger entries (``lifecycle.discharge_hidden``) and marks the lies it settles
(``divergences.revealed_by_event_id``, #166). Nothing here asks whether the claim is true: the
caller hands over claims that match the fact.

**Identity** (``commit_identity``, #171): a narrated ``same_as`` is held, not shared: its canon
fact is scoped to the narration's perceivers and the characters it names, so "the stranger is
Bobby" is canon's and Bobby's, never Jenni's from elsewhere; a reveal widens it like any hidden
fact.

``hidden_facts`` is ``GET /stories/{id}/inspect/ledger``'s ``hidden_facts`` (omniscient only):
the current canon facts scoped away from the shared view, the engine's true ages aside. Log
lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon import facts as canon
from epistrel.core.canon.facts import refuse_holder
from epistrel.core.enums import RecordProvenance
from epistrel.core.membership import active_only
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.propositions.vocabulary import AGE
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables.divergences import divergences
from epistrel.core.tables.propositions import propositions
from epistrel.core.visibility import OBJECTIVE, SHARED_SCOPES, Visibility

log = logging.getLogger(__name__)

Observed = Literal["fact", "signal"]
ENGINE = Visibility(scope="engine")
PARTITIONS_MODULE = "partitions"


@dataclass(frozen=True, slots=True)
class HiddenCommit:
    """What ``commit_hidden`` wrote: the canon fact, its scope and the noticers' observation
    transitions (none with partitions off, for ``observed = signal`` and for a player)."""

    fact_id: str
    visibility: Visibility
    observation_transition_ids: tuple[str, ...] = ()

    @property
    def observation_transition_id(self) -> str | None:
        return self.observation_transition_ids[0] if self.observation_transition_ids else None

    def report(self) -> dict[str, Any]:
        """A ``turn_report.commit.hidden_commits`` item (ids only)."""
        return {
            "fact_id": self.fact_id,
            "scope": self.visibility.scope,
            "characters": list(self.visibility.characters or ()),
            "observation_transition_ids": list(self.observation_transition_ids),
        }


@dataclass(frozen=True, slots=True)
class Reveal:
    """A hidden fact a beat revealed: the widened version, the version it superseded, its new
    scope and the divergences it settled."""

    fact_id: str
    superseded_id: str
    visibility: Visibility
    divergence_ids: tuple[str, ...] = ()
    discharged: int = 0

    def report(self) -> dict[str, Any]:
        """A ``turn_report.commit.reveals`` item (ids only)."""
        return {
            "fact_id": self.fact_id,
            "superseded_id": self.superseded_id,
            "characters": list(self.visibility.characters or ()),
            "divergence_ids": list(self.divergence_ids),
            "ledger_discharged": self.discharged,
        }


# -- the pure rules -------------------------------------------------------------------------------


def scope_of(noticers: Iterable[str], observed: Observed = "fact") -> Visibility:
    """A hidden fact's scope: its noticers (``observed = fact``), or ``engine`` when they
    perceived only a signal or nobody noticed it."""
    found = tuple(sorted(set(noticers)))
    if observed == "signal" or not found:
        return ENGINE
    return Visibility(scope="characters", characters=found)


def widened(visibility: Visibility, perceivers: Iterable[str]) -> Visibility:
    """``visibility`` with ``perceivers`` added (a reveal's new scope); a shared scope stays
    shared, and ``engine`` is never widened by a character's beat."""
    if visibility.scope != "characters":
        return visibility
    return Visibility(scope="characters", characters=(*(visibility.characters or ()), *perceivers))


def knows(visibility: Visibility, character_id: str) -> bool:
    """Whether ``character_id`` may read a fact of ``visibility`` (and so reveal it)."""
    if visibility.scope in SHARED_SCOPES:
        return True
    return visibility.scope == "characters" and character_id in (visibility.characters or ())


@dataclass(frozen=True, slots=True)
class KeyedValue:
    """One current canon value as DETERMINED-SINGLE-VALUE reads it: its functional key, its
    value and its valid-time (story-minutes, ``None`` open)."""

    key: Hashable
    value: Hashable
    start: int | None
    end: int | None


def _overlap(a: KeyedValue, b: KeyedValue) -> bool:
    low = max(x for x in (a.start, b.start, -(2**62)) if x is not None)
    highs = [x for x in (a.end, b.end) if x is not None]
    return not highs or low < min(highs)


def single_value_violations(values: Iterable[KeyedValue]) -> list[tuple[KeyedValue, KeyedValue]]:
    """FR-EPI-8's invariant: every pair of current values of one functional key with different
    values over overlapping valid-time (empty when it holds)."""
    by_key: dict[Hashable, list[KeyedValue]] = {}
    for value in values:
        by_key.setdefault(value.key, []).append(value)
    found: list[tuple[KeyedValue, KeyedValue]] = []
    for group in by_key.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a.value != b.value and _overlap(a, b):
                    found.append((a, b))
    return found


# -- writing -------------------------------------------------------------------------------------


async def commit_identity(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    proposition: Proposition,
    *,
    perceivers: Sequence[str],
    selves: Sequence[str],
    event_id: str | None,
    beat: int | None,
    provenance: RecordProvenance = RecordProvenance.DIRECTOR,
    settings: Any = None,
    registry: Registry | None = None,
) -> HiddenCommit:
    """A narrated ``same_as`` under partitions (FR-PROP-7; #171): identity is held, so the canon
    fact is scoped to who perceived the narration (``perceivers``, routing's co-located
    characters) and to the modeled characters it names (``selves``: a character always knows who
    it is). Routing writes the perceivers' observations (#164's sink observes a narration's
    claims); each of ``selves`` that did not perceive it gets its own here, as
    ``commit_hidden``'s noticers do. Widening it later is #169's reveal (``reveal``). With
    ``partitions`` off it is an ordinary objective fact."""
    from epistrel.core.epistemic import partitions, testimony, transitions

    story_id = str(operation.story_id)
    registry = registry or await load(conn, story_id)
    sources = [event_id] if event_id is not None else []
    on = await partitions_on(conn, story_id, settings)
    fact = await canon.commit_fact(
        conn,
        story_id,
        proposition,
        provenance=provenance,
        visibility=scope_of([*perceivers, *selves]) if on else OBJECTIVE,
        operation=operation,
        source_event_ids=sources,
        beat=beat,
        registry=registry,
    )
    written: list[str] = []
    if on and event_id is not None:
        position = testimony.witness_position(
            operation.narrative_turn or 0, operation.turn_variant or 0, beat or 0
        )
        for self_id in sorted(set(selves) - set(perceivers)):
            if await partitions.is_player(conn, story_id, self_id):
                continue
            result = await transitions.transition(
                conn,
                operation,
                holder=self_id,
                kind=transitions.Kind.OBSERVATION,
                proposition=fact.proposition,
                antecedent=transitions.Antecedent.record("events", event_id),
                position=position,
                confidence=transitions.Confidence(transitions.Level.KNOWN),
                settings=settings,
                registry=registry,
            )
            written.append(result.transition.id)
    log.debug("identity commit: fact=%s observations=%d", fact.id, len(written))
    return HiddenCommit(fact.id, fact.visibility, tuple(written))


async def partitions_on(conn: AsyncConnection, story_id: str, settings: Any = None) -> bool:
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration

    found = settings
    if found is None:
        found = (await effective_configuration(conn, story_id, validate=False)).settings
    return module_on(found, PARTITIONS_MODULE)


async def commit_hidden(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    proposition: Proposition,
    *,
    noticers: Sequence[str],
    observed: Observed = "fact",
    event_id: str | None,
    beat: int | None,
    provenance: RecordProvenance = RecordProvenance.RENDER,
    recorded: Visibility = OBJECTIVE,
    holder: str = "canon",
    settings: Any = None,
    registry: Registry | None = None,
) -> HiddenCommit:
    """Commit ``proposition`` as a hidden fact ``noticers`` noticed at ``beat`` (its event
    ``event_id``), with each noticer's observation (see the module docstring). With
    ``partitions`` off the fact keeps the ``recorded`` scope and nobody observes it. 422
    ``viewpoint_cannot_assign_truth`` for a ``holder`` other than ``canon``."""
    from epistrel.core.epistemic import partitions, testimony, transitions

    refuse_holder(holder)
    story_id = str(operation.story_id)
    registry = registry or await load(conn, story_id)
    sources = [event_id] if event_id is not None else []
    if not await partitions_on(conn, story_id, settings):
        fact = await canon.commit_fact(
            conn,
            story_id,
            proposition,
            provenance=provenance,
            visibility=recorded,
            operation=operation,
            source_event_ids=sources,
            beat=beat,
            registry=registry,
        )
        return HiddenCommit(fact.id, fact.visibility)
    scope = scope_of(noticers, observed)
    fact = await canon.commit_fact(
        conn,
        story_id,
        proposition,
        provenance=provenance,
        visibility=scope,
        operation=operation,
        source_event_ids=sources,
        beat=beat,
        registry=registry,
    )
    written: list[str] = []
    if observed == "fact" and event_id is not None:
        position = testimony.witness_position(
            operation.narrative_turn or 0, operation.turn_variant or 0, beat or 0
        )
        for noticer in sorted(set(noticers)):
            if await partitions.is_player(conn, story_id, noticer):
                continue  # the player's character has no partition
            result = await transitions.transition(
                conn,
                operation,
                holder=noticer,
                kind=transitions.Kind.OBSERVATION,
                proposition=fact.proposition,
                antecedent=transitions.Antecedent.record("events", event_id),
                position=position,
                confidence=transitions.Confidence(transitions.Level.KNOWN),
                settings=settings,
                registry=registry,
            )
            written.append(result.transition.id)
    log.debug("hidden commit: fact=%s observations=%d", fact.id, len(written))
    return HiddenCommit(fact.id, fact.visibility, tuple(written))


async def _hidden_matches(
    conn: AsyncConnection, story_id: str, claim: Proposition, speaker: str
) -> list[canon.Fact]:
    """The current canon facts ``claim`` asserts (its key and polarity) that are scoped away
    from the shared view and that ``speaker`` may read."""
    found = await canon.canon(
        conn,
        story_id,
        viewpoint=OMNISCIENT,
        subject=claim.subject_id,
        predicate=claim.predicate_id,
    )
    return [
        f
        for f in found
        if f.visibility.scope == "characters"
        and knows(f.visibility, speaker)
        and f.proposition.normalized_key() == claim.normalized_key()
        and f.proposition.polarity is claim.polarity
    ]


async def _settled_divergences(
    conn: AsyncConnection, story_id: str, fact: canon.Fact, registry: Registry
) -> list[str]:
    """The open divergences (#166) a reveal of ``fact`` settles: a lie whose speaker believed
    the fact's proposition, or a lie about the fact's functional key."""
    d = divergences.alias("d")
    believed = propositions.alias("b")
    asserted = propositions.alias("a")
    rows = await conn.execute(
        select(
            d.c.id,
            believed.c.subject_id.label("b_subject"),
            believed.c.predicate_id.label("b_predicate"),
            believed.c.object_id.label("b_object"),
            asserted.c.subject_id.label("a_subject"),
            asserted.c.predicate_id.label("a_predicate"),
        )
        .select_from(
            d.join(asserted, asserted.c.id == d.c.asserted_proposition_id).outerjoin(
                believed, believed.c.id == d.c.believed_proposition_id
            )
        )
        .where(d.c.story_id == story_id, d.c.revealed_by_event_id.is_(None), active_only(d))
        .order_by(d.c.id)
    )
    predicate = registry.get(fact.predicate_id)
    functional = predicate is not None and predicate.max_cardinality == 1
    target = fact.proposition
    found: list[str] = []
    for r in rows:
        same_belief = (r.b_subject, r.b_predicate, r.b_object) == (
            target.subject_id,
            target.predicate_id,
            target.object_id,
        )
        same_key = functional and (r.a_subject, r.a_predicate) == (
            target.subject_id,
            target.predicate_id,
        )
        if same_belief or same_key:
            found.append(str(r.id))
    return found


async def reveal(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    claims: Sequence[Proposition],
    *,
    speaker: str,
    perceivers: Iterable[str],
    beat: int | None,
    event_id: str | None,
    intent: str = "unspecified",
    registry: Registry | None = None,
) -> list[Reveal]:
    """Widen every hidden fact ``claims`` reveal (see the module docstring): ``speaker``'s beat
    ``beat`` (its event ``event_id``) whose content reached ``perceivers``. A ``deceptive``
    beat reveals nothing; a fact everyone it reached already reads is left as it is."""
    from epistrel.core.commit import lifecycle

    if intent == "deceptive":
        return []
    story_id = str(operation.story_id)
    registry = registry or await load(conn, story_id)
    reached = sorted(set(perceivers) - {speaker})
    found: list[Reveal] = []
    seen: set[str] = set()
    for claim in claims:
        for fact in await _hidden_matches(conn, story_id, claim, speaker):
            if fact.id in seen:
                continue
            seen.add(fact.id)
            scope = widened(fact.visibility, reached)
            if scope == fact.visibility:
                continue
            successor = await canon.supersede(
                conn,
                story_id,
                fact.id,
                fact.proposition,
                operation=operation,
                visibility=scope,
                valid_time=fact.valid_time,
                source_event_ids=[event_id] if event_id is not None else (),
                beat=beat,
                registry=registry,
            )
            discharged = await lifecycle.discharge_hidden(
                conn, operation, fact.proposition_id, beat=beat
            )
            settled = await _settled_divergences(conn, story_id, fact, registry)
            if settled and event_id is not None:
                await conn.execute(
                    update(divergences)
                    .where(divergences.c.story_id == story_id, divergences.c.id.in_(settled))
                    .values(revealed_by_event_id=event_id)
                )
            found.append(
                Reveal(
                    successor.id,
                    fact.id,
                    successor.visibility,
                    tuple(settled) if event_id is not None else (),
                    discharged,
                )
            )
    log.debug("reveal: %d facts widened", len(found))
    return found


async def determined_otherwise(
    conn: AsyncConnection, story_id: str, claim: Proposition
) -> str | None:
    """FR-COMMIT-6, FR-EPI-8: the resolved thunk whose reference ``claim`` fills with another
    value than the one the world already fixed ("the mailman knocked" once Bobby's knock is
    committed, known to Bobby alone), or ``None``. Such a claim is a second value, never a
    resolution: the turn's screen contradicts it without citing the fixed value."""
    from sqlalchemy import not_

    from epistrel.core.commit import lifecycle
    from epistrel.core.commit.sketch import fills
    from epistrel.core.enums import ThunkState
    from epistrel.core.tables.commitments import thunks

    t = thunks.alias("t")
    rows = await conn.execute(
        select(*lifecycle.thunk_columns(t))
        .where(
            t.c.story_id == story_id,
            t.c.predicate_id == claim.predicate_id,
            t.c.state == ThunkState.RESOLVED.value,
            not_(lifecycle.open_clause(t)),
            active_only(t),
        )
        .order_by(t.c.id)
    )
    for row in rows:
        thunk = lifecycle.thunk_of(row)
        if thunk.resolved_fact_id is None or not fills(thunk.sketch, claim):
            continue
        fact = await canon.read_version(conn, story_id, str(thunk.resolved_fact_id))
        if fact.proposition.normalized_key() != claim.normalized_key():
            return thunk.id
    return None


# -- reading -------------------------------------------------------------------------------------


async def hidden_facts(conn: AsyncConnection, story_id: str) -> list[canon.Fact]:
    """The story's current hidden canon (see the module docstring), by valid-time then id."""
    return [
        f
        for f in await canon.canon(conn, story_id, viewpoint=OMNISCIENT)
        if f.visibility.scope not in SHARED_SCOPES and f.predicate_id != AGE
    ]


__all__ = [
    "ENGINE",
    "HiddenCommit",
    "KeyedValue",
    "Observed",
    "Reveal",
    "commit_hidden",
    "commit_identity",
    "determined_otherwise",
    "hidden_facts",
    "knows",
    "partitions_on",
    "refuse_holder",
    "reveal",
    "scope_of",
    "single_value_violations",
    "widened",
]
