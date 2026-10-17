"""Q-02's resolution accuracy: the metric and a generated fixture (FR-RET-2,3, NFR-QUAL-6; #139).

docs/04 Q-02 measures referential and temporal resolution (S-THOSEGUYS generalised). #139
contributes the metric and the fixture; #157 registers the Q-02 suite (and #141's retrieval
joins the run). The metric is **report-only**: docs/04 sets no floor.

- ``generate(seed, n)`` builds a deterministic fixture: a cast (names, genders, a sibling
  pair) and ``n`` queries, each a text with its speaker, context beats, the mentions it should
  resolve (an entity id, or ``None`` where the right answer is ``uncertain``), its temporal
  expression's rule and its occasion's event type. The queries cycle through S-THOSEGUYS's
  shapes: a named speaker's "those guys at the game last week", a pronoun with one compatible
  antecedent, a pronoun with two (the complement: ``uncertain``), a relational description, an
  unknown name, a calendar expression without a calendar.
- ``run(fixture)`` resolves every query through ``core.resolution`` (no model) and
  ``accuracy`` scores it: **per-mention accuracy** (a mention is right when its entity is the
  expected one, or both are unresolved) and **per-query all-correct** (every mention, the time
  rule or reason, and the event type right).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from epistrel.core.resolution.entities import (
    ContextBeat,
    EntityEntry,
    EntityIndex,
    Relation,
    ResolutionContext,
    resolve_mentions,
)
from epistrel.core.resolution.events import resolve_event_type
from epistrel.core.resolution.time import TimeBasis, resolve_time
from epistrel.core.resolution.types import ResolvedTime

MEN = ("Bobby", "Tom", "Raj", "Luis", "Owen", "Felix", "Hugo", "Ivan")
WOMEN = ("Jenni", "Mara", "Priya", "Ines", "Wendy", "Zoe", "Lena", "Ruth")
EVENTS = (("game", "game"), ("party", "party"), ("dinner", "dinner"), ("fight", "fight"))
TIMES = (("last week", "last_week"), ("a month ago", "ago"), ("yesterday", "yesterday"))


@dataclass(frozen=True, slots=True)
class Expected:
    surface: str
    entity_id: str | None


@dataclass(frozen=True, slots=True)
class Query:
    id: str
    text: str
    speaker: str
    beats: tuple[ContextBeat, ...]
    mentions: tuple[Expected, ...]
    time_rule: str | None
    """The grammar rule the time expression resolves by, or a reason code (``no_calendar``)."""
    event_type: str | None


@dataclass(frozen=True, slots=True)
class Fixture:
    entries: tuple[EntityEntry, ...]
    relations: tuple[Relation, ...]
    queries: tuple[Query, ...]
    basis: TimeBasis = field(default_factory=TimeBasis)


def _id(n: int) -> str:
    return f"01JQ02{n:020d}"


def generate(seed: int = 0, n: int = 60) -> Fixture:
    """A deterministic Q-02 fixture of ``n`` queries (see the module docstring)."""
    rnd = random.Random(seed)  # noqa: S311 - a reproducible fixture, not a secret
    men, women = list(MEN), list(WOMEN)
    rnd.shuffle(men)
    rnd.shuffle(women)
    people = [(name, "masculine") for name in men[:4]] + [(name, "feminine") for name in women[:4]]
    entries = tuple(
        EntityEntry(_id(i), "person", name, gender) for i, (name, gender) in enumerate(people)
    )
    ids = {e.canonical_name: e.id for e in entries}
    m, w = men[:4], women[:4]
    relations = (Relation(ids[m[0]], "sibling_of", ids[w[0]]),)
    queries: list[Query] = []
    for i in range(n):
        shape = i % 6
        a, b = rnd.sample(m, 2)
        x = rnd.choice(w)
        event, event_type = rnd.choice(EVENTS)
        time, time_rule = rnd.choice(TIMES)
        qid = f"q02-{seed}-{i}"
        if shape == 0:
            text = f"I saw those guys at the {event} {time}."
            queries.append(
                Query(qid, text, ids[x], (), (Expected("I", ids[x]),), time_rule, event_type)
            )
        elif shape == 1:
            text = f"Then {a} met {x} at the {event}. He left {time}."
            mentions: tuple[Expected, ...] = (
                Expected(a, ids[a]),
                Expected(x, ids[x]),
                Expected("He", ids[a]),
            )
            queries.append(Query(qid, text, ids[b], (), mentions, time_rule, event_type))
        elif shape == 2:
            text = f"Then {a} told {b} he said it at the {event}."
            mentions = (Expected(a, ids[a]), Expected(b, ids[b]), Expected("he", None))
            queries.append(Query(qid, text, ids[x], (), mentions, None, event_type))
        elif shape == 3:
            text = f"Then {m[0]}'s sister came to the {event} {time}."
            mentions = (Expected(m[0], ids[m[0]]), Expected("sister", ids[w[0]]))
            queries.append(Query(qid, text, ids[x], (), mentions, time_rule, event_type))
        elif shape == 4:
            beats = (ContextBeat(f"{a} waved at the {event}.", ids[x]),)
            text = f"Then Quillon Vexby said he saw it {time}."
            mentions = (Expected("Quillon Vexby", None), Expected("he", ids[a]))
            queries.append(Query(qid, text, ids[x], beats, mentions, time_rule, event_type))
        else:
            text = f"Then {a} was at the {event} in 2012."
            queries.append(
                Query(qid, text, ids[x], (), (Expected(a, ids[a]),), "no_calendar", event_type)
            )
    # Shape 4's event lives in its context beat: the query's own text names none.
    fixed = [
        q if not q.beats else Query(q.id, q.text, q.speaker, q.beats, q.mentions, q.time_rule, None)
        for q in queries
    ]
    return Fixture(entries, relations, tuple(fixed))


@dataclass(frozen=True, slots=True)
class QueryResult:
    query: Query
    mentions_right: tuple[bool, ...]
    time_right: bool
    event_right: bool

    @property
    def all_correct(self) -> bool:
        return all(self.mentions_right) and self.time_right and self.event_right


@dataclass(frozen=True, slots=True)
class Accuracy:
    """Q-02's report-only numbers."""

    mentions: int
    mentions_right: int
    queries: int
    queries_all_correct: int

    @property
    def per_mention(self) -> float:
        return self.mentions_right / self.mentions if self.mentions else 1.0

    @property
    def per_query(self) -> float:
        return self.queries_all_correct / self.queries if self.queries else 1.0


def _query(query: Query, fixture: Fixture, index: EntityIndex) -> QueryResult:
    context = ResolutionContext(index, beats=query.beats, relations=fixture.relations)
    found = resolve_mentions(query.text, query.speaker, context)
    by_surface: dict[str, list[str | None]] = {}
    for mention in found:
        surface = query.text[mention.span[0] : mention.span[1]]
        by_surface.setdefault(surface, []).append(mention.entity_id)
    right = []
    for expected in query.mentions:
        got = by_surface.get(expected.surface, [])
        right.append(bool(got) and got.pop(0) == expected.entity_id)
    time = resolve_time(query.text, 0, basis=fixture.basis)
    if query.time_rule is None:
        time_right = not isinstance(time, ResolvedTime)
    elif isinstance(time, ResolvedTime):
        time_right = time.rule == query.time_rule
    else:
        time_right = time.reason.code.value == query.time_rule
    event_right = resolve_event_type(query.text) == query.event_type
    return QueryResult(query, tuple(right), time_right, event_right)


def run(fixture: Fixture) -> list[QueryResult]:
    """Every query of ``fixture`` resolved through ``core.resolution``."""
    index = EntityIndex(fixture.entries)
    return [_query(q, fixture, index) for q in fixture.queries]


def accuracy(results: Sequence[QueryResult]) -> Accuracy:
    """Per-mention accuracy and per-query all-correct over ``results``."""
    return Accuracy(
        mentions=sum(len(r.mentions_right) for r in results),
        mentions_right=sum(sum(r.mentions_right) for r in results),
        queries=len(results),
        queries_all_correct=sum(r.all_correct for r in results),
    )


__all__ = ["Accuracy", "Expected", "Fixture", "Query", "QueryResult", "accuracy", "generate", "run"]
