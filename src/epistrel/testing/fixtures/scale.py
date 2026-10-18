"""The scale fixture of the performance suites (docs/04 §11: P-01, P-02, P-06; NFR-PERF-1,2,
NFR-SCALE-1; #153): one structured story of ~50k memories and ~100k events, deterministic for a
seed, built in a database of its own and cached as a ``pg_dump``.

It extends #141's long-story builder (``tests/retrieval_support.build_story``), which writes its
filler memories through the same row builder and bulk writer (``memory_row``,
``insert_memories``). The story is built in the following steps.

- **Story and cast**: ``create_story_from`` with ``characters`` characters (the first is the
  player), put on the seeded ``Structured (M4)`` profile, with 200 person entities as canon's
  subjects. Those are the cast's own entities plus named people who are not characters. The
  story also has 40 places and 60 items.
- **Events**: the turns are real ``turn`` operations committed ``TURN_BATCH`` to a transaction
  (``OperationBatch``). Each turn has its input beat and its reply beat. Its commit step writes
  the turn's share of canon through ``canon.commit_fact``, and each fact appends its
  ``fact_committed`` event. With ``events = 2·turns + facts``, the default 100k events are
  25,000 turns of 2 beats plus 50,000 facts. The projections (#137) are maintained as in play.
- **Canon** (``canon_plan``): ``facts`` facts over the 200 subjects, mixing a functional fluent
  (``located_at``, in disjoint windows), a ``multi`` fluent (``knows``) and a ``multi``
  possession (``owns``). P-02's claims are drawn from the same plan (``claims``).
- **Memories** (``memory_plan``): 80% episodic, 15% semantic and 5% reflection. They are spread
  over the turns, two beats a turn, as rows of those turns' operations. Each text has 40 to 120
  words, 5% of them proper nouns. Episodic memories are shared (no holder) and name two or three
  characters as participants and, most of the time, an event type. A semantic memory or a
  reflection is held by a character, on a Zipf-like spread (exponent ``HOLDER_SKEW``) capped at
  ``HOLDER_CAP``, so no character's unpinned hot set passes its 2,000 budget. Every memory has
  the stub's vector for its text in the pinned version. Each semantic memory covers the episodic
  memories just before it (``covered_by``, as consolidation would). Each character pins
  ``PINS_PER_CHARACTER`` of its held memories. The counters (``hot_counts``) are recounted by
  #147's own ``reconcile``. Nothing is retired, so no retrieval rehydrates.
- **Index**: the HNSW index is dropped before the bulk load and rebuilt after it by
  ``embeddings.retype``, the DDL a pin runs. P-06 reads that build's time and the index's size
  from ``ScaleFixture.index``.

``fixture_version()`` hashes this module's source, and ``cache_key`` combines it with the
alembic head and the spec. ``cached_database`` restores a matching dump from ``cache_dir``, or
builds the fixture and dumps it there. The dump is made with ``pg_dump``/``pg_restore`` from
PATH, or with the pgvector image's binaries through ``docker run`` when PATH has none.
``grow`` adds memories to a built story, which is P-06's 1k → 10k → 50k → 100k curve.

The builder writes rows straight into ``memories`` (``core.membership.insert_derived``), so they
are not indexed for erasure and they enqueue no job. The database is a scratch one, dropped with
the suite.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import embeddings
from epistrel.core.canon import facts as canon
from epistrel.core.enums import (
    MemoryKind,
    MemoryTrustTier,
    OperationKind,
    RecordProvenance,
)
from epistrel.core.events import Operation, OperationBatch
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.memory import retirement
from epistrel.core.propositions import registry as predicate_registry
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.core.tables import operations, stories
from epistrel.core.tables.memories import memories
from epistrel.core.turns import stage_turn
from epistrel.orchestrator.embeddings.adapter import stub_vector
from epistrel.testing import perf

FIXTURE_SEED = 20261008
DIMENSION = 768
"""The stub embedding's dimension (a common real model's)."""
PRINCIPAL = "p-perf-fixture"
SUBJECTS = 200
PLACES = 40
ITEMS = 60
TURN_BATCH = 250
MEMORY_BATCH = 1_000
WORDS = (40, 120)
PROPER_NOUN_SHARE = 0.05
PINS_PER_CHARACTER = 20
HOLDER_SKEW = 0.5
HOLDER_CAP = 1_900
"""The most unpinned memories one character holds (``memory.hot_set.memories_per_character``
is 2,000)."""
COVERS = 6
"""The episodic memories just before a semantic memory that it covers."""
EVENT_TYPES = ("dinner", "party", "walk", "work", "game", "market", "journey", None, None)
LOCATED_AT = CORE_BY_NAME["located_at"].id
KNOWS = CORE_BY_NAME["knows"].id
OWNS = CORE_BY_NAME["owns"].id
WINDOW = 20
MAINTENANCE_WORK_MEM = "1GB"
"""The index build's ``maintenance_work_mem``, with no parallel workers (P-06 publishes it with
the curve)."""
"""Story-minutes between the starts of one subject's ``located_at`` windows (each 10 long)."""

_FIRST = (  # noqa: SIM905 - a name list reads best as text
    "Wren Ada Bram Cora Dell Esme Finn Gale Hale Iris Joss Kit Lena Mott Nell Orin Pell Quin "
    "Rhea Sable Tamsin Ulric Vane Willa Yara Zeke Alder Briar Calla Dorian"
).split()
_LAST = (  # noqa: SIM905
    "Ashdown Brook Carrow Dunmore Ellery Fenwick Garrow Holt Ivers Jessop Kell Larch"
).split()
_PLACES = (  # noqa: SIM905
    "Harbor Mill Tower Market Ford Chapel Orchard Quarry Bridge Inn Hollow Gate Barn Wharf "
    "Archive Cellar Forge Meadow Ridge Well"
).split()
_ITEMS = (  # noqa: SIM905
    "lantern compass ledger ring knife map key satchel flute coin cloak kettle"
).split()
_VOCABULARY = (  # noqa: SIM905
    "the a of and to in by with from over under after before while when as it they she he "
    "road river bridge market tower harbor forest meadow stone ember quiet window letter coin "
    "rope wagon gate north south east west morning evening rain wind ash oak silver iron candle "
    "map ledger tavern keeper traveller guard merchant scholar ferry orchard well crossing "
    "cellar stair archive bell ribbon compass thread kettle shelf walked spoke waited laughed "
    "argued promised remembered carried opened closed watched followed warned trusted"
).split()


# -- the spec and the built fixture --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScaleSpec:
    """What to build: ``memories`` memories and ``events`` events (``facts`` of them canon's
    ``fact_committed`` events, the rest two beats a turn) for ``characters`` characters."""

    memories: int = 50_000
    events: int = 100_000
    characters: int = 12
    facts: int = 50_000
    seed: int = FIXTURE_SEED
    dimension: int = DIMENSION

    def __post_init__(self) -> None:
        if not 2 <= self.characters <= len(_FIRST):
            raise ValueError(f"characters is between 2 and {len(_FIRST)}")
        if self.memories < 1 or self.facts < 0 or self.dimension < 1:
            raise ValueError("memories and dimension are positive, facts not negative")
        if self.events < self.facts + 2:
            raise ValueError("events cover the facts' events and at least one turn")

    @property
    def turns(self) -> int:
        """The turns committed: each turn is two beat events."""
        return (self.events - self.facts) // 2

    @classmethod
    def scaled(cls, memories: int, **overrides: Any) -> ScaleSpec:
        """The default spec's proportions at ``memories`` memories (as many facts, twice as
        many events)."""
        values: dict[str, Any] = {"memories": memories, "events": 2 * memories, "facts": memories}
        values.update(overrides)
        return cls(**values)


@dataclass(frozen=True, slots=True)
class IndexStats:
    """One HNSW build: its wall-clock seconds, the index's size and the vectors it holds."""

    build_s: float
    bytes: int
    vectors: int


@dataclass
class ScaleStory:
    """A built story: its id, the cast (name to character id, the player first), the entities
    by name (``people`` holds every subject, the cast's own entities included), the head turn
    and the counts written."""

    story_id: str
    characters: dict[str, str]
    people: dict[str, str]
    places: dict[str, str]
    items: dict[str, str]
    head: int
    memories: int = 0
    facts: int = 0
    events: int = 0

    @property
    def player(self) -> str:
        return next(iter(self.characters.values()))


@dataclass
class ScaleFixture:
    """The built fixture: its spec, story, pinned embedding version, build timings by step
    (seconds) and the last index build."""

    spec: ScaleSpec
    story: ScaleStory
    version: str
    timings: dict[str, float] = field(default_factory=dict)
    index: IndexStats | None = None

    def document(self) -> dict[str, Any]:
        return {
            "spec": asdict(self.spec),
            "story": asdict(self.story),
            "version": self.version,
            "timings": dict(self.timings),
            "index": None if self.index is None else asdict(self.index),
            "fixture_version": fixture_version(),
        }

    @classmethod
    def of(cls, document: Mapping[str, Any]) -> ScaleFixture:
        index = document.get("index")
        return cls(
            spec=ScaleSpec(**document["spec"]),
            story=ScaleStory(**document["story"]),
            version=str(document["version"]),
            timings={str(k): float(v) for k, v in dict(document.get("timings") or {}).items()},
            index=None if index is None else IndexStats(**index),
        )


def version_of(dimension: int) -> str:
    """The pinned embedding version of the stub at ``dimension`` (its own identity)."""
    return f"stub:sha256:{dimension}"


def fixture_version() -> str:
    """A hash of this builder's source: a change to the fixture's shape changes it."""
    source = inspect.getsource(sys.modules[__name__]) + perf.SEED_VERSION
    return hashlib.sha256(source.encode()).hexdigest()[:12]


# -- names and texts -------------------------------------------------------------------------------


def person_names(n: int) -> list[str]:
    """``n`` distinct "First Last" names, in a fixed order (the cast's first)."""
    names = [f"{first} {last}" for last in _LAST for first in _FIRST]
    if n > len(names):
        raise ValueError(f"at most {len(names)} people")
    return names[:n]


def _numbered(base: Sequence[str], n: int, suffix: str) -> list[str]:
    return [
        base[i % len(base)] + ("" if i < len(base) else f" {suffix}{i // len(base)}")
        for i in range(n)
    ]


def text_of(rng: random.Random, names: Sequence[str]) -> str:
    """40 to 120 words from the vocabulary, about 5% of them proper nouns from ``names``."""
    count = rng.randint(*WORDS)
    words = [
        rng.choice(names) if names and rng.random() < PROPER_NOUN_SHARE else rng.choice(_VOCABULARY)
        for _ in range(count)
    ]
    sentence = " ".join(words)
    return sentence[0].upper() + sentence[1:] + "."


# -- canon -----------------------------------------------------------------------------------------

Planned = tuple[str, str, str, int | None, int | None]
"""One planned fact: ``(predicate id, subject name, object name, valid from, valid to)``."""


def canon_plan(spec: ScaleSpec) -> list[Planned]:
    """``spec.facts`` facts over the ``SUBJECTS`` people, by subject: ``located_at`` in disjoint
    windows (``WINDOW``), ``knows`` of distinct people and ``owns`` of distinct items, about
    48/32/20, interleaved so every turn writes a mix. Deterministic for the spec."""
    people = person_names(SUBJECTS)
    places = _numbered(_PLACES, PLACES, "No. ")
    items = _numbered(_ITEMS, ITEMS, "no. ")
    per_subject: list[list[Planned]] = []
    for k, subject in enumerate(people):
        n = spec.facts // SUBJECTS + (1 if k < spec.facts % SUBJECTS else 0)
        rng = random.Random(f"canon:{spec.seed}:{k}")  # noqa: S311 - a reproducible fixture
        knows_n = min(len(people) - 1, round(n * 0.32))
        owns_n = min(len(items), round(n * 0.20))
        located_n = n - knows_n - owns_n
        others = [p for p in people if p != subject]
        rng.shuffle(others)
        held = list(items)
        rng.shuffle(held)
        rows: list[Planned] = [
            (LOCATED_AT, subject, rng.choice(places), j * WINDOW, j * WINDOW + WINDOW // 2)
            for j in range(located_n)
        ]
        rows += [(KNOWS, subject, other, None, None) for other in others[:knows_n]]
        rows += [(OWNS, subject, item, None, None) for item in held[:owns_n]]
        rng.shuffle(rows)
        per_subject.append(rows)
    plan: list[Planned] = []
    depth = max((len(rows) for rows in per_subject), default=0)
    for j in range(depth):
        plan += [rows[j] for rows in per_subject if j < len(rows)]
    return plan


def proposition(story: ScaleStory, planned: Planned, *, negated: bool = False) -> Proposition:
    predicate, subject, obj, lo, hi = planned
    objects = (
        story.places
        if predicate == LOCATED_AT
        else story.items
        if predicate == OWNS
        else story.people
    )
    return Proposition(
        subject_id=story.people[subject],
        predicate_id=predicate,
        object_id=objects[obj],
        valid_time=Interval.of(lo, hi),
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
    )


ClaimKind = Literal["supported", "contradicted", "novel", "uncertain"]
CLAIM_MIX: tuple[tuple[ClaimKind, int], ...] = (
    ("supported", 40),
    ("contradicted", 30),
    ("novel", 20),
    ("uncertain", 10),
)
"""P-02's claims, by the verdict they are built to get (percent)."""


def claims(spec: ScaleSpec, story: ScaleStory, n: int) -> list[tuple[ClaimKind, Proposition]]:
    """``n`` claims over canon, mixed by ``CLAIM_MIX``, each built from a planned fact:

    - ``supported``: a planned ``located_at`` restated inside its window, or a planned ``knows``;
    - ``contradicted``: a planned ``located_at`` or ``knows`` negated (the subject at *another*
      place is only ``uncertain``: no ``located_in`` says the places are disjoint);
    - ``novel``: the subject somewhere in the gap between two of its windows;
    - ``uncertain``: a ``located_at`` window straddling a planned window and the gap after it
      (canon settles one part only).

    Deterministic for the spec."""
    plan = canon_plan(spec)
    located = [p for p in plan if p[0] == LOCATED_AT]
    known = [p for p in plan if p[0] == KNOWS]
    rng = random.Random(f"claims:{spec.seed}")  # noqa: S311 - a reproducible fixture
    kinds: list[ClaimKind] = []
    for kind, share in CLAIM_MIX:
        kinds += [kind] * (n * share // 100)
    kinds += ["supported"] * (n - len(kinds))
    rng.shuffle(kinds)
    out: list[tuple[ClaimKind, Proposition]] = []
    for kind in kinds:
        base = rng.choice(located)
        _, subject, place, lo, hi = base
        if lo is None or hi is None:  # located_at is always windowed
            raise AssertionError(base)
        if kind == "supported":
            if known and rng.random() < 0.25:
                out.append((kind, proposition(story, rng.choice(known))))
            else:
                out.append((kind, proposition(story, (LOCATED_AT, subject, place, lo + 1, hi - 1))))
        elif kind == "contradicted":
            if known and rng.random() < 0.25:
                out.append((kind, proposition(story, rng.choice(known), negated=True)))
            else:
                restated = (LOCATED_AT, subject, place, lo + 1, hi - 1)
                out.append((kind, proposition(story, restated, negated=True)))
        elif kind == "novel":
            out.append((kind, proposition(story, (LOCATED_AT, subject, place, hi + 2, hi + 8))))
        else:
            out.append((kind, proposition(story, (LOCATED_AT, subject, place, lo + 2, hi + 6))))
    return out


# -- queries ---------------------------------------------------------------------------------------

QueryKind = Literal["occasion", "free_text", "proper_noun"]
QUERY_MIX: tuple[tuple[QueryKind, int], ...] = (
    ("occasion", 40),
    ("free_text", 40),
    ("proper_noun", 20),
)
"""P-01's queries by template (percent)."""
_WHEN = ("yesterday", "last week", "three days ago", "a month ago", "two weeks ago")


def queries(spec: ScaleSpec, story: ScaleStory, n: int) -> list[tuple[QueryKind, str]]:
    """``n`` retrieval queries mixed by ``QUERY_MIX``:

    - ``occasion``: a character, an event type and a relative time (the occasion step runs);
    - ``free_text``: 8 to 16 words of the memories' vocabulary;
    - ``proper_noun``: a question about a named person who is not a character, or a place.

    Deterministic for the spec."""
    rng = random.Random(f"queries:{spec.seed}")  # noqa: S311 - a reproducible fixture
    kinds: list[QueryKind] = []
    for kind, share in QUERY_MIX:
        kinds += [kind] * (n * share // 100)
    kinds += ["free_text"] * (n - len(kinds))
    rng.shuffle(kinds)
    cast = list(story.characters)
    strangers = [p for p in story.people if p not in story.characters]
    types = [t for t in EVENT_TYPES if t is not None]
    out: list[tuple[QueryKind, str]] = []
    for kind in kinds:
        if kind == "occasion":
            who = rng.choice(cast).split()[0]
            q = f"{who} at the {rng.choice(types)} {rng.choice(_WHEN)}"
        elif kind == "free_text":
            words = [rng.choice(_VOCABULARY) for _ in range(rng.randint(8, 16))]
            q = " ".join(words).capitalize() + "?"
        else:
            name = (
                rng.choice(strangers or cast)
                if rng.random() < 0.7
                else rng.choice(list(story.places))
            )
            q = f"What do you know about {name}?"
        out.append((kind, q))
    return out


# -- memories --------------------------------------------------------------------------------------


def memory_row(
    *,
    story_id: str,
    operation_id: str,
    line: str,
    turn: int | None,
    beat: int | None = 1,
    kind: MemoryKind = MemoryKind.EPISODIC,
    event_type: str | None = None,
    participants: Sequence[str] = (),
    importance: float | None = None,
    character: str | None = None,
    holder: str | None = None,
    vector: Sequence[float] | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """One ``memories`` row of a render-derived, in-world memory (``insert_memories`` writes
    it), with its vector when given."""
    row: dict[str, Any] = {
        "id": new_ulid(),
        "story_id": story_id,
        "kind": kind.value,
        "text": line,
        "character_id": character,
        "holder_character_id": holder,
        "provenance": RecordProvenance.RENDER.value,
        "trust_tier": MemoryTrustTier.IN_WORLD.value,
        "importance": importance,
        "importance_at": turn if importance is not None else None,
        "operation_id": operation_id,
        "narrative_turn": turn,
        "beat": beat,
        "event_type": event_type,
        "participants": list(participants),
    }
    if vector is not None:
        row.update(embedding=list(vector), embedding_model_version=version)
    return row


async def insert_memories(
    db: AsyncEngine, rows: Sequence[Mapping[str, Any]], *, batch: int = MEMORY_BATCH
) -> int:
    """Write ``rows`` through ``insert_derived``, ``batch`` to a transaction; answers the
    count. Every row of one call has the same columns."""
    for start in range(0, len(rows), batch):
        async with db.begin() as conn:
            await insert_derived(conn, memories, rows[start : start + batch])
    return len(rows)


def _holders(spec: ScaleSpec, cast: Sequence[str], held: int) -> list[str]:
    """Who holds each of ``held`` memories: weights ``1/k^HOLDER_SKEW``, each capped."""
    rng = random.Random(f"holders:{spec.seed}")  # noqa: S311 - a reproducible fixture
    weights = [1.0 / (k + 1) ** HOLDER_SKEW for k in range(len(cast))]
    counts = dict.fromkeys(cast, 0)
    out: list[str] = []
    cap = max(HOLDER_CAP, -(-held // len(cast)) + PINS_PER_CHARACTER)
    for _ in range(held):
        while True:
            who = rng.choices(cast, weights)[0]
            if counts[who] < cap:
                break
        counts[who] += 1
        out.append(who)
    return out


def memory_plan(
    spec: ScaleSpec,
    story: ScaleStory,
    turn_ops: Mapping[int, str],
    *,
    count: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """The rows of memories ``offset`` .. ``offset + count`` (see the module docstring),
    positioned over the story's turns; ``turn_ops`` maps a turn to its operation."""
    count = spec.memories if count is None else count
    total = offset + count
    turns = sorted(turn_ops)
    cast = list(story.characters.values())
    names = list(story.people)
    version = version_of(spec.dimension)
    kinds = [_kind(i) for i in range(offset, total)]
    held = sum(1 for k in kinds if k is not MemoryKind.EPISODIC)
    holders = iter(_holders(replace_seed(spec, offset), cast, held))
    rows: list[dict[str, Any]] = []
    for i, kind in zip(range(offset, total), kinds, strict=True):
        rng = random.Random(f"memory:{spec.seed}:{i}")  # noqa: S311 - a reproducible fixture
        turn = turns[(i * len(turns)) // total] if total > len(turns) else turns[i % len(turns)]
        line = text_of(rng, names)
        actor = rng.choice(cast)
        if kind is MemoryKind.EPISODIC:
            others = rng.sample(
                [c for c in cast if c != actor], k=min(len(cast) - 1, rng.randint(1, 2))
            )
            row = memory_row(
                story_id=story.story_id,
                operation_id=turn_ops[turn],
                line=line,
                turn=turn,
                beat=1 + i % 2,
                kind=kind,
                event_type=rng.choice(EVENT_TYPES),
                participants=[actor, *others],
                importance=round(rng.random(), 3),
                character=actor,
                vector=stub_vector(line, spec.dimension),
                version=version,
            )
        else:
            holder = next(holders)
            other = rng.choice([c for c in cast if c != holder])
            row = memory_row(
                story_id=story.story_id,
                operation_id=turn_ops[turn],
                line=line,
                turn=turn,
                beat=2,
                kind=kind,
                participants=[holder, other],
                importance=round(rng.random(), 3),
                character=holder,
                holder=holder,
                vector=stub_vector(line, spec.dimension),
                version=version,
            )
        rows.append(row)
    return rows


def replace_seed(spec: ScaleSpec, offset: int) -> ScaleSpec:
    """The spec whose holder draw continues at ``offset`` (``grow``'s later steps)."""
    if offset == 0:
        return spec
    return ScaleSpec(
        memories=spec.memories,
        events=spec.events,
        characters=spec.characters,
        facts=spec.facts,
        seed=spec.seed + offset,
        dimension=spec.dimension,
    )


def _kind(i: int) -> MemoryKind:
    """80% episodic, 15% semantic, 5% reflection, interleaved."""
    slot = i % 20
    if slot < 16:
        return MemoryKind.EPISODIC
    return MemoryKind.SEMANTIC if slot < 19 else MemoryKind.REFLECTION


async def _cover_and_pin(
    db: AsyncEngine, story: ScaleStory, rows: Sequence[Mapping[str, Any]]
) -> None:
    """Each semantic memory covers the ``COVERS`` episodic memories before it; each character
    pins its first ``PINS_PER_CHARACTER`` held memories."""
    covers: list[dict[str, Any]] = []
    recent: list[str] = []
    pins: dict[str, list[str]] = {}
    for row in rows:
        kind = MemoryKind(row["kind"])
        if kind is MemoryKind.EPISODIC:
            recent = [*recent[-(COVERS - 1) :], str(row["id"])]
            continue
        if kind is MemoryKind.SEMANTIC and recent:
            covers += [{"mid": mid, "by": [str(row["id"])]} for mid in recent]
            recent = []
        holder = str(row["holder_character_id"])
        chosen = pins.setdefault(holder, [])
        if len(chosen) < PINS_PER_CHARACTER:
            chosen.append(str(row["id"]))
    m = memories.c
    async with db.begin() as conn:
        if covers:
            await conn.execute(
                update(memories).where(m.id == bindparam("mid")).values(covered_by=bindparam("by")),
                covers,
            )
        pinned = sorted(i for ids in pins.values() for i in ids)
        if pinned:
            await conn.execute(
                update(memories)
                .where(m.story_id == story.story_id, m.id.in_(pinned))
                .values(pinned=True, pinned_by=PRINCIPAL)
            )


# -- building --------------------------------------------------------------------------------------


@contextmanager
def _timed(
    timings: dict[str, float], step: str, log: Callable[[str], None] | None
) -> Iterator[None]:
    started = time.perf_counter()
    yield
    timings[step] = round(timings.get(step, 0.0) + time.perf_counter() - started, 3)
    if log is not None:
        log(f"{step}: {timings[step]:.1f} s")


async def _create(db: AsyncEngine, spec: ScaleSpec, name: str) -> ScaleStory:
    cast_names = person_names(spec.characters)
    created = await create_story_from(
        db,
        StorySpec(
            name=name,
            characters=[
                CharacterSpec(
                    name=n,
                    definition=f"{n}, one of the story's people.",
                    true_age=20 + i,
                    is_player=i == 0,
                )
                for i, n in enumerate(cast_names)
            ],
        ),
        principal=PRINCIPAL,
    )
    story_id = created.story_id
    characters = {c.name: c.id for c in created.characters}
    characters = {n: characters[n] for n in cast_names}
    people: dict[str, str] = {}
    async with db.begin() as conn:
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(engine_profile_id=STRUCTURED_M4_PROFILE_ID, engine_profile_version_pin=None)
        )
        for person in person_names(SUBJECTS):
            people[person] = await store.add_entity(
                conn, story_id, "person", person, character_id=characters.get(person)
            )
        places = {
            p: await store.add_entity(conn, story_id, "place", p)
            for p in _numbered(_PLACES, PLACES, "No. ")
        }
        items = {
            i: await store.add_entity(conn, story_id, "item", i)
            for i in _numbered(_ITEMS, ITEMS, "no. ")
        }
    return ScaleStory(story_id, characters, people, places, items, head=0)


async def _turns(
    db: AsyncEngine,
    spec: ScaleSpec,
    story: ScaleStory,
    log: Callable[[str], None] | None,
) -> None:
    """Commit ``spec.turns`` turns, each writing its share of canon in its commit step."""
    plan = canon_plan(spec)
    turns = spec.turns
    registry = await predicate_registry.load(db, story.story_id)

    def share(index: int) -> list[Proposition]:
        start, end = (index * len(plan)) // turns, ((index + 1) * len(plan)) // turns
        return [proposition(story, p) for p in plan[start:end]]

    written = await _commit_turns(
        db, story, turns, seed=f"turns:{spec.seed}", share=share, registry=registry, log=log
    )
    story.facts = written


async def commit_turns(db: AsyncEngine, fixture: ScaleFixture, n: int) -> int:
    """Commit ``n`` more turns (no canon) to the fixture's story; answers the events written
    (P-10's event write throughput)."""
    written = await _commit_turns(
        db,
        fixture.story,
        n,
        seed=f"more-turns:{fixture.spec.seed}:{fixture.story.head}",
        share=lambda _: [],
        registry=None,
    )
    return 2 * n + written


async def _commit_turns(
    db: AsyncEngine,
    story: ScaleStory,
    turns: int,
    *,
    seed: str,
    share: Callable[[int], list[Proposition]],
    registry: Any,
    log: Callable[[str], None] | None = None,
) -> int:
    """Commit ``turns`` turns, ``TURN_BATCH`` to a transaction (``OperationBatch``); turn
    ``i`` writes ``share(i)`` through ``commit_fact`` in its commit step. Answers the facts
    written; moves the story's head and event count."""
    rng = random.Random(seed)  # noqa: S311 - filler text
    cast = list(story.characters.values())
    done = 0
    written = 0
    with perf.summaries_muted():
        while done < turns:
            size = min(TURN_BATCH, turns - done)
            ops = [
                Operation(db, story_id=story.story_id, kind=OperationKind.TURN, principal=PRINCIPAL)
                for _ in range(size)
            ]
            group = OperationBatch(db, story.story_id, ops)
            await group.begin()
            for n, op in enumerate(ops):
                index = done + n
                facts = share(index)
                say = perf.filler(rng, 160)
                stage_turn(
                    op,
                    actor_character_id=story.player,
                    say=say,
                    do=None,
                    reply=perf.filler(rng, 300),
                    render_character_ids=[cast[1 + index % (len(cast) - 1)]],
                    model=perf.SEED_MODEL,
                    prompt_hash=hashlib.sha256(say.encode()).hexdigest(),
                    finish_reason="stop",
                )
                if facts:
                    op.add_commit_step(_facts_step(story.story_id, op, facts, registry))
                written += len(facts)
            await group.commit()
            done += size
            if log is not None and done % (TURN_BATCH * 20) == 0:
                log(f"turns: {done}/{turns}")
    story.head += turns
    story.events += 2 * turns + written
    return written


def _facts_step(
    story_id: str, op: Operation, share: Sequence[Proposition], registry: Any
) -> Callable[[AsyncConnection], Any]:
    async def step(conn: AsyncConnection) -> None:
        for p in share:
            await canon.commit_fact(
                conn,
                story_id,
                p,
                provenance=RecordProvenance.RENDER,
                operation=op,
                registry=registry,
            )

    return step


async def turn_operations(db: AsyncEngine, story_id: str) -> dict[int, str]:
    """The story's committed turns: turn to its operation."""
    o = operations.c
    async with db.connect() as conn:
        found = await conn.execute(
            select(o.narrative_turn, o.operation_id).where(
                o.story_id == story_id,
                o.kind == OperationKind.TURN.value,
                o.status == "committed",
                o.narrative_turn.is_not(None),
            )
        )
        return {int(r.narrative_turn): str(r.operation_id) for r in found}


async def drop_index(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await conn.execute(text(f"DROP INDEX IF EXISTS {embeddings.HNSW_INDEX}"))


async def rebuild_index(
    db: AsyncEngine, dimension: int, *, maintenance_work_mem: str | None = MAINTENANCE_WORK_MEM
) -> IndexStats:
    """Rebuild the HNSW index the way a pin does (``embeddings.retype``), timed, with
    ``maintenance_work_mem`` for the build (published beside P-06's curve); answers the build,
    the index's size and the vectors it holds."""
    started = time.perf_counter()
    async with db.begin() as conn:
        if maintenance_work_mem is not None:
            # A serial build: a parallel one shares its graph through dynamic shared memory,
            # which a container's default 64MB /dev/shm cannot hold at 1GB.
            await conn.execute(
                text(
                    "SELECT set_config('maintenance_work_mem', :m, true), "
                    "set_config('max_parallel_maintenance_workers', '0', true)"
                ),
                {"m": maintenance_work_mem},
            )
        await embeddings.retype(conn, dimension)
    build_s = time.perf_counter() - started
    async with db.connect() as conn:
        size = await conn.scalar(
            text("SELECT pg_relation_size(CAST(:i AS regclass))"), {"i": embeddings.HNSW_INDEX}
        )
        vectors = await conn.scalar(
            text("SELECT count(*) FROM memories WHERE embedding IS NOT NULL")
        )
    async with db.begin() as conn:
        await conn.execute(text("ANALYZE memories"))
    return IndexStats(round(build_s, 3), int(size or 0), int(vectors or 0))


async def build_scale_fixture(
    db: AsyncEngine,
    spec: ScaleSpec | None = None,
    *,
    name: str = "Scale fixture",
    log: Callable[[str], None] | None = None,
) -> ScaleFixture:
    """Build ``spec`` (the default: 50k memories, 100k events, 12 characters, 50k facts) in
    ``db``, a migrated scratch database: pins the deployment at the stub's ``spec.dimension``
    (unless pinned), then the steps of the module docstring. Answers the fixture with each
    step's seconds."""
    spec = spec or ScaleSpec()
    version = version_of(spec.dimension)
    timings: dict[str, float] = {}
    async with db.begin() as conn:
        await embeddings.pin_first(conn, version=version, dimension=spec.dimension)
    await drop_index(db)
    with _timed(timings, "story", log):
        story = await _create(db, spec, name)
    with _timed(timings, "turns_and_canon", log):
        await _turns(db, spec, story, log)
    turn_ops = await turn_operations(db, story.story_id)
    with _timed(timings, "memories", log):
        rows = memory_plan(spec, story, turn_ops)
        story.memories = await insert_memories(db, rows)
        await _cover_and_pin(db, story, rows)
    with _timed(timings, "counters", log):
        async with db.begin() as conn:
            await conn.execute(
                update(stories)
                .where(stories.c.id == story.story_id)
                .values(embedding_model_version=version)
            )
            await retirement.reconcile(conn, story.story_id)
    with _timed(timings, "index", log):
        index = await rebuild_index(db, spec.dimension)
    with _timed(timings, "analyze", log):
        async with db.begin() as conn:
            await conn.execute(text("ANALYZE"))
    return ScaleFixture(spec, story, version, timings, index)


async def grow(
    db: AsyncEngine, fixture: ScaleFixture, to: int, *, rebuild: bool = True
) -> IndexStats | None:
    """Add memories to the fixture's story until it holds ``to`` (P-06's next step), spread
    over its turns, then rebuild the index (timed) unless ``rebuild`` is off. The counters are
    not recounted: past the story's vector budget an overflow episode would open, and the curve
    measures retrieval over the whole set."""
    story = fixture.story
    if to <= story.memories:
        return fixture.index
    turn_ops = await turn_operations(db, story.story_id)
    rows = memory_plan(
        fixture.spec, story, turn_ops, count=to - story.memories, offset=story.memories
    )
    await drop_index(db)
    story.memories += await insert_memories(db, rows)
    if rebuild:
        fixture.index = await rebuild_index(db, fixture.spec.dimension)
    return fixture.index


# -- the cache -------------------------------------------------------------------------------------

PG_IMAGE = "pgvector/pgvector:pg16"
"""The image whose ``pg_dump``/``pg_restore`` run when PATH has none (the test server's)."""


def alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[4]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    return str(head)


def cache_key(spec: ScaleSpec) -> str:
    """The dump's key: the schema (alembic head), the builder (``fixture_version``) and the
    spec."""
    material = json.dumps(
        {"head": alembic_head(), "fixture": fixture_version(), "spec": asdict(spec)},
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _pg_argv(tool: str, url: str, *args: str) -> tuple[list[str], dict[str, str]]:
    """``tool`` against the database at ``url``: PATH's binary, else the pgvector image's
    through ``docker run`` (host networking on Linux; ``host.docker.internal`` elsewhere)."""
    u = make_url(url)
    env = {**os.environ, "PGPASSWORD": u.password or ""}
    host = u.host or "localhost"
    conn = [
        "-h",
        host,
        "-p",
        str(u.port or 5432),
        "-U",
        u.username or "postgres",
        "-d",
        u.database or "",
    ]
    if shutil.which(tool):
        return [tool, *conn, *args], env
    if not shutil.which("docker"):
        raise RuntimeError(f"neither {tool} nor docker is on PATH")
    network = ["--network", "host"]
    if sys.platform != "linux":
        network = []
        if host in ("localhost", "127.0.0.1"):
            conn[1] = "host.docker.internal"
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "-e",
        "PGPASSWORD",
        *network,
        PG_IMAGE,
        tool,
        *conn,
        *args,
    ], env


def dump(url: str, path: Path) -> None:
    """``pg_dump -Fc`` of the database at ``url`` into ``path`` (written whole or not at all)."""
    argv, env = _pg_argv("pg_dump", url, "-Fc", "--no-owner")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    with partial.open("wb") as sink:
        subprocess.run(argv, check=True, stdout=sink, env=env)  # noqa: S603 - fixed argv
    partial.rename(path)


def restore(url: str, path: Path) -> None:
    """``pg_restore`` ``path`` into the empty database at ``url``."""
    argv, env = _pg_argv("pg_restore", url, "--no-owner", "--exit-on-error")
    with path.open("rb") as source:
        subprocess.run(argv, check=True, stdin=source, env=env)  # noqa: S603 - fixed argv


def cached_paths(cache_dir: Path, spec: ScaleSpec) -> tuple[Path, Path]:
    key = cache_key(spec)
    return cache_dir / f"scale-{key}.dump", cache_dir / f"scale-{key}.json"


def read_cached(cache_dir: Path | None, spec: ScaleSpec) -> ScaleFixture | None:
    """The cached fixture's description when its dump and sidecar exist."""
    if cache_dir is None:
        return None
    dump_path, sidecar = cached_paths(cache_dir, spec)
    if not (dump_path.exists() and sidecar.exists()):
        return None
    return ScaleFixture.of(json.loads(sidecar.read_text()))


def write_cached(cache_dir: Path, url: str, fixture: ScaleFixture) -> Path:
    """Dump the database at ``url`` and write the fixture's sidecar; answers the dump."""
    dump_path, sidecar = cached_paths(cache_dir, fixture.spec)
    dump(url, dump_path)
    sidecar.write_text(json.dumps(fixture.document(), indent=2, sort_keys=True) + "\n")
    return dump_path


__all__ = [
    "CLAIM_MIX",
    "DIMENSION",
    "FIXTURE_SEED",
    "QUERY_MIX",
    "IndexStats",
    "ScaleFixture",
    "ScaleSpec",
    "ScaleStory",
    "alembic_head",
    "build_scale_fixture",
    "cache_key",
    "cached_paths",
    "canon_plan",
    "claims",
    "commit_turns",
    "dump",
    "fixture_version",
    "grow",
    "insert_memories",
    "memory_plan",
    "memory_row",
    "person_names",
    "queries",
    "read_cached",
    "rebuild_index",
    "restore",
    "turn_operations",
    "version_of",
    "write_cached",
]
