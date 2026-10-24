"""Per-character knowledge over a built scale story (#182): the partitions layer P-04's
multi-character variant and P-02's ``could_have_known`` span measure (docs/04 §11; NFR-PERF-2,4).

``apply`` writes it into a story ``scale`` built (``ScaleFixture``/``ScaleStory``), after the
build or after a cached dump is restored, so the scale fixture's own dump and cache key are
untouched. Per character of the cast except the player (who has no partition, FR-EPI-12):

- **private memories**: ``private_share`` (30%) of the memories the character holds get the
  visibility ``{"scope": "characters", "characters": [holder]}`` (a ``think`` is private in the
  same way); the rest stay as built. The write runs with ``epistrel.erasure = on``, the one
  setting under which ``epistrel_memories_guard`` lets ``visibility`` change: the fixture rewrites
  rows it built, as an erasure rewrites them;
- **beliefs**: ``beliefs`` (200) beliefs, each one ``author`` transition (#160) citing an author
  act of the fixture's own ``import`` operation (position-less, so it precedes every step),
  held from a turn spread over the story. Half restate a planned canon fact, a quarter deny one
  (false beliefs), a quarter place a subject in a gap between its windows (what canon does not
  say);
- **exposures**: ``exposures`` (50) unresolved exposures (#164), each to a ``located_at`` of a
  subject in a gap canon does not cover, heard from the player at the input beat of a turn
  spread over the story (``exposure_position(turn, variant, 1)``, the player's ``player_input``
  event as the source event).

Everything is deterministic for the scale spec and ``KnowledgeSpec.seed``. ``plan`` is pure;
``probes`` draws ``could_have_known`` checks from the plan: ``believed`` (a belief licenses
it), ``canon`` (a planned fact, which the character may read), ``exposed`` (an unresolved
exposure's dry run licenses it) and ``no_path`` (a gap no record covers: every path is read).

The settings passed are the story's effective configuration with per-character partitions on
(``partitioned``): the fixture writes beliefs under them without changing the story's profile,
so the suites sharing the scale fixture (P-01, P-06) read it as before. Nothing in the Engine
imports this module.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import BeliefConfidenceLevel, OperationKind
from epistrel.core.epistemic import exposure, transitions
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.tables import events
from epistrel.core.tables.memories import memories
from epistrel.testing.fixtures.scale import (
    KNOWS,
    LOCATED_AT,
    WINDOW,
    Planned,
    ScaleSpec,
    ScaleStory,
    canon_plan,
    person_names,
    proposition,
    turn_operations,
)

PRINCIPAL = "p-perf-knowledge"
SEED = 20261024
PRIVATE_SHARE = 0.30
BELIEFS = 200
EXPOSURES = 50

ProbeKind = Literal["believed", "canon", "exposed", "no_path"]
PROBE_KINDS: tuple[ProbeKind, ...] = ("believed", "canon", "exposed", "no_path")


@dataclass(frozen=True, slots=True)
class KnowledgeSpec:
    """How much knowledge each non-player character gets."""

    private_share: float = PRIVATE_SHARE
    beliefs: int = BELIEFS
    exposures: int = EXPOSURES
    seed: int = SEED


@dataclass(frozen=True, slots=True)
class PlannedBelief:
    holder: str
    planned: Planned
    negated: bool
    turn: int


@dataclass(frozen=True, slots=True)
class PlannedExposure:
    witness: str
    planned: Planned
    turn: int


@dataclass
class KnowledgePlan:
    """What ``apply`` writes, per holder (character id)."""

    beliefs: dict[str, list[PlannedBelief]] = field(default_factory=dict)
    exposures: dict[str, list[PlannedExposure]] = field(default_factory=dict)
    used: dict[str, frozenset[tuple[str, str, str]]] = field(default_factory=dict)
    """Every key each holder has a belief or an exposure of."""


@dataclass(frozen=True, slots=True)
class Written:
    """What ``apply`` wrote."""

    private_memories: int
    beliefs: int
    exposures: int
    operation_id: str


def player_of(story: ScaleStory) -> str:
    """The story's player character: the first of the cast's names (``scale.person_names``).
    Not ``ScaleStory.player``: a cached fixture's sidecar is written with sorted keys, so its
    ``characters`` lose the cast's order."""
    first = person_names(len(story.characters))[0]
    return story.characters[first]


def holders(story: ScaleStory) -> list[str]:
    """The characters that get a partition: the cast less the player, in the cast's order."""
    names = person_names(len(story.characters))
    return [story.characters[n] for n in names[1:]]


def key_of(planned: Planned) -> tuple[str, str, str]:
    """A planned record's functional key (predicate, subject, object): what the replay matches
    on (time aside)."""
    return (planned[0], planned[1], planned[2])


def _draw(rng: random.Random, pool: Sequence[Planned], used: set[tuple[str, str, str]]) -> Planned:
    """A planned record from ``pool`` whose key the holder has no record of yet."""
    while True:
        found = rng.choice(pool)
        if key_of(found) not in used:
            used.add(key_of(found))
            return found


def _gap(planned: Planned) -> Planned:
    """``planned`` (a ``located_at``) moved to the gap after its window: canon says nothing
    there (windows are ``WINDOW // 2`` long, ``WINDOW`` apart)."""
    _, subject, place, _, hi = planned
    if hi is None:  # located_at is always windowed
        raise AssertionError(subject)
    return (LOCATED_AT, subject, place, hi + 1, hi + WINDOW // 2 - 1)


def plan(spec: ScaleSpec, story: ScaleStory, knowledge: KnowledgeSpec) -> KnowledgePlan:
    """The beliefs and exposures of every holder (see the module docstring), each of a key
    (``key_of``) the holder has no other record of, so every probe finds the path it is built
    for. Pure."""
    canon = canon_plan(spec)
    located = [p for p in canon if p[0] == LOCATED_AT]
    known = [p for p in canon if p[0] == KNOWS] or located
    last = max(1, story.head)
    found = KnowledgePlan()
    for n, holder in enumerate(holders(story)):
        rng = random.Random(f"knowledge:{knowledge.seed}:{spec.seed}:{n}")  # noqa: S311
        used: set[tuple[str, str, str]] = set()
        beliefs: list[PlannedBelief] = []
        for k in range(knowledge.beliefs):
            turn = 1 + (k * last) // max(1, knowledge.beliefs)
            share = k % 4
            if share in (0, 1):
                beliefs.append(PlannedBelief(holder, _draw(rng, known, used), False, turn))
            elif share == 2:
                beliefs.append(PlannedBelief(holder, _draw(rng, located, used), True, turn))
            else:
                beliefs.append(PlannedBelief(holder, _gap(_draw(rng, located, used)), False, turn))
        found.beliefs[holder] = beliefs
        found.exposures[holder] = [
            PlannedExposure(
                holder,
                _gap(_draw(rng, located, used)),
                1 + (k * last) // max(1, knowledge.exposures),
            )
            for k in range(knowledge.exposures)
        ]
        found.used[holder] = frozenset(used)
    return found


async def partitioned(db: AsyncEngine | AsyncConnection, story_id: str) -> ProfileSettings:
    """The story's effective settings with per-character partitions on (the story's own
    profile is left as it is)."""
    if isinstance(db, AsyncConnection):
        found = (await effective_configuration(db, story_id, validate=False)).settings
    else:
        async with db.connect() as conn:
            found = (await effective_configuration(conn, story_id, validate=False)).settings
    document = found.model_dump(mode="json")
    document["character_knowledge"]["partitions"] = True
    return ProfileSettings.model_validate(document)


async def privatize(db: AsyncEngine, story: ScaleStory, share: float, seed: int = SEED) -> int:
    """Make ``share`` of each holder's held memories visible to it alone; answers how many."""
    if share <= 0:
        return 0
    m = memories.c
    written = 0
    for n, holder in enumerate(holders(story)):
        async with db.connect() as conn:
            ids = sorted(
                str(r)
                for r in await conn.scalars(
                    select(m.id).where(
                        m.story_id == story.story_id, m.holder_character_id == holder
                    )
                )
            )
        rng = random.Random(f"private:{seed}:{n}")  # noqa: S311 - a reproducible fixture
        chosen = rng.sample(ids, round(len(ids) * share))
        if not chosen:
            continue
        async with db.begin() as conn:
            # The guard lets ``visibility`` change only under the erasure setting.
            await conn.execute(text("SET LOCAL epistrel.erasure = 'on'"))
            await conn.execute(
                update(memories)
                .where(m.story_id == story.story_id, m.id.in_(chosen))
                .values(visibility={"scope": "characters", "characters": [holder]})
            )
        written += len(chosen)
    return written


async def _input_events(
    conn: AsyncConnection, story_id: str, turns: Mapping[int, str]
) -> dict[int, tuple[str, int]]:
    """Each turn's ``player_input`` event id and the turn's variant."""
    e = events.c
    found = await conn.execute(
        select(e.operation_id, e.id, e.turn_variant).where(
            e.story_id == story_id,
            e.type == "player_input",
            e.operation_id.in_(list(turns.values())),
        )
    )
    by_op = {str(r.operation_id): (str(r.id), int(r.turn_variant or 0)) for r in found}
    return {t: by_op[op] for t, op in turns.items() if op in by_op}


async def apply(
    db: AsyncEngine,
    spec: ScaleSpec,
    story: ScaleStory,
    knowledge: KnowledgeSpec = KnowledgeSpec(),  # noqa: B008 - frozen
) -> Written:
    """Write ``plan``'s beliefs and exposures in one ``import`` operation, and make the private
    share of the holders' memories private (see the module docstring)."""
    planned = plan(spec, story, knowledge)
    settings = await partitioned(db, story.story_id)
    turns = await turn_operations(db, story.story_id)
    counts: dict[str, int] = {"beliefs": 0, "exposures": 0}
    async with Operation(
        db,
        story_id=story.story_id,
        kind=OperationKind.IMPORT,
        principal=PRINCIPAL,
        bumps_revision=True,
    ) as op:
        for holder in planned.beliefs:
            op.append("belief_authored", {"holder_character_id": holder})

        async def step(conn: AsyncConnection) -> None:
            e = events.c
            acts = {
                str(r.character): str(r.id)
                for r in await conn.execute(
                    select(e.id, e.payload["holder_character_id"].astext.label("character")).where(
                        e.operation_id == op.operation_id, e.type == "belief_authored"
                    )
                )
            }
            registry = await load_registry(conn, story.story_id)
            inputs = await _input_events(conn, story.story_id, turns)
            for holder, beliefs in planned.beliefs.items():
                cited = transitions.Antecedent.record("events", acts[holder])
                for b in beliefs:
                    await transitions.transition(
                        conn,
                        op,
                        holder=holder,
                        kind=transitions.Kind.AUTHOR,
                        proposition=proposition(story, b.planned, negated=b.negated),
                        antecedent=cited,
                        position=Position(b.turn),
                        confidence=transitions.Confidence(BeliefConfidenceLevel.BELIEVED, None),
                        settings=settings,
                        registry=registry,
                    )
                    counts["beliefs"] += 1
            exposed: list[exposure.Exposed] = []
            for holder, items in planned.exposures.items():
                for x in items:
                    heard = proposition(story, x.planned)
                    stored = await store.write_proposition(conn, story.story_id, heard, registry)
                    turn = min((t for t in inputs if t >= x.turn), default=max(inputs))
                    event_id, variant = inputs[turn]
                    exposed.append(
                        exposure.Exposed(
                            witness=holder,
                            proposition_id=stored,
                            proposition=heard,
                            source=player_of(story),
                            source_event_id=event_id,
                            position=exposure.exposure_position(turn, variant, 1),
                        )
                    )
            counts["exposures"] = len(await exposure.record_exposures(conn, op, exposed))

        op.add_commit_step(step)
        await op.commit()
    private = await privatize(db, story, knowledge.private_share, knowledge.seed)
    return Written(private, counts["beliefs"], counts["exposures"], op.operation_id)


@dataclass(frozen=True, slots=True)
class Probe:
    """One ``could_have_known`` check: who, what, at which position, and the path it is built
    to find (``kind``)."""

    kind: ProbeKind
    holder: str
    proposition: Proposition
    at: Position


def probes(spec: ScaleSpec, story: ScaleStory, knowledge: KnowledgeSpec, n: int) -> list[Probe]:
    """``n`` checks, a quarter of each kind (``PROBE_KINDS``), at the turn after the story's
    head (every record is before it). A ``canon`` probe restates a planned ``located_at`` the
    holder has no belief or exposure of, inside its window; a ``no_path`` probe places such a
    subject there before story-time began, which no record covers. Deterministic."""
    planned = plan(spec, story, knowledge)
    located = [p for p in canon_plan(spec) if p[0] == LOCATED_AT]
    rng = random.Random(f"probes:{knowledge.seed}:{spec.seed}")  # noqa: S311
    who = list(planned.beliefs)
    at = Position(story.head + 1)
    out: list[Probe] = []
    for k in range(n):
        kind = PROBE_KINDS[k % len(PROBE_KINDS)]
        holder = rng.choice(who)
        if kind == "believed":
            b = rng.choice(planned.beliefs[holder])
            target = proposition(story, b.planned, negated=b.negated)
        elif kind == "exposed":
            target = proposition(story, rng.choice(planned.exposures[holder]).planned)
        else:
            fresh = rng.choice(located)
            while key_of(fresh) in planned.used[holder]:
                fresh = rng.choice(located)
            _, subject, place, _, _ = fresh
            if kind == "canon":
                target = proposition(story, fresh)
            else:
                target = Proposition(
                    subject_id=story.people[subject],
                    predicate_id=LOCATED_AT,
                    object_id=story.places[place],
                    valid_time=Interval.of(-WINDOW, -1),
                    polarity=Polarity.AFFIRMED,
                )
        out.append(Probe(kind, holder, target, at))
    rng.shuffle(out)
    return out


__all__ = [
    "BELIEFS",
    "EXPOSURES",
    "PRIVATE_SHARE",
    "PROBE_KINDS",
    "KnowledgePlan",
    "KnowledgeSpec",
    "Probe",
    "ProbeKind",
    "Written",
    "apply",
    "holders",
    "key_of",
    "partitioned",
    "plan",
    "player_of",
    "privatize",
    "probes",
]
