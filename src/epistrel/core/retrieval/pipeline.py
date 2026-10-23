"""Viewpoint-scoped retrieval for the structured prompt (FR-RET-2..7, FR-MEM-6,8, NFR-PERF-1;
docs/02 §4.2; #141).

``retrieve(conn, story_id, query, …)`` answers what a render's context holds of memory and canon
for a query ``{text, position, actor}``. It runs on the caller's connection, inside its
transaction (a turn's: snapshot consistency with its read set), calls no model itself (the
Orchestrator injects ``embed``, one query to its vector, and ``embed_batch``, several texts in
one call) and reads derived rows only through ``core.membership``'s helpers: a dormant or
discarded memory is never returned. The stages, each timed in ``timings`` (milliseconds; the
embedding calls are excluded and counted in ``model_calls``):

1. **resolve** (FR-RET-2): ``resolve_query``, pure over #139's resolvers, finds the text's
   mentions (as ``actor`` says them, against the context at ``position``), its first temporal
   expression as story-time (a subject-anchored one, "when he was seven", reads the subject's
   timeline through ``KeyedLookup``) and its event type. Q-02's eval runs the same function.
   The context reads only the relationships the text's relational descriptions need (#451:
   none without a relation noun, else its possessors', ``load_context(text=…)``).
2. **occasion** (FR-RET-3): when the text gives a time window or an event type, the episode
   layer (episodic memories, ``event_type``/``participants``) is queried by
   ``(story-time window as turns, event_type, participants && the resolved entities)``, up to
   ``MAX_EPISODES`` episodes, latest first; with no window, those best matching the text first
   (``ts_rank``, any of its words), the latest first among equals (#458: "the runaway goose at
   the wedding" is that wedding, not the latest one). The located episodes' participants
   resolve the group ("those guys"), and their **assessments** are the semantic memories and
   reflections whose ``participants`` include one of them, formed from the window's start on.
   Neither a window nor a type: the step is skipped (``skipped: ["occasion"]``).
3. **hybrid arms** (FR-RET-4): the **vector** arm (``search.vector_search`` over the HNSW
   index, ``SET LOCAL hnsw.ef_search`` and ``hnsw.iterative_scan = relaxed_order``, only
   vectors in the pinned version, #138's never-mix rule), the **lexical** arm (the
   ``LexicalSearch`` backend over ``memories.search``; a hit scoring under
   ``LEXICAL_RELATIVE_FLOOR`` of the best hit is dropped, #458) and the **alias** arm (the
   aliases of the entities the text names, as exact phrases, any casing; the speaker's own
   "I"/"me" names no topic and is left out, #458). The lexical and alias arms run when
   ``memory.episodic.hybrid_search`` is on, and the lexical one also whenever the vector arm
   cannot (``degradation``: ``embedding_pending``/``embedding_unavailable``, #138). Each arm
   takes its top ``retrieval_vector_k``/``retrieval_lexical_k`` (50).
4. **fuse and rank** (FR-RET-5): reciprocal-rank fusion (``retrieval_rrf_k``) then
   ``score = w_sim·fused + w_rec·recency + w_imp·decayed importance`` (``core.retrieval.rank``,
   the ``memory.episodic.weight_*`` dials); the best ``memory.episodic.k`` that are not already
   episodes or assessments are ``memories``, each with its score. Nothing positioned after
   ``position`` is ever a candidate.
5. **profiles** (FR-RET-6): the standing profile of each resolved or located character (#137,
   through ``KeyedLookup.profiles``, one batched read, #451) and the canon about the resolved
   entities (#133's ``canon_at`` with its visibility filter, through
   ``KeyedLookup.canon_context``); the speaker is left out of both.
6. **assemble** (FR-RET-7, FR-MEM-6): ``core.retrieval.assemble`` fills
   ``memory.episodic.retrieval_token_budget`` by tier: the ``CONTEXT_SOURCES`` hook's ledger
   items and pins, the occasions, the profiles and canon, the ranked memories.

**Rehydration** (FR-MEM-8): a retired memory the occasion window or the lexical lookup finds is
read from its row and returned without changing its ``retrieval_tier``, up to
``retrieval_rehydrate_max_rows`` (20) a query. When the vector arm ran, the lexically found ones
are compared with the query too: by their stored vector when it is in the pinned version, else
re-embedded in one batched call (counted in ``model_calls``), the vector used for this query
only and never stored, unless the story's ``memory.hot_set.retain_retired_vectors`` is on
(#147): then a touching retrieval stores it on the retired row, which stays retired.

**The viewpoint** (FR-RET-1, FR-EPI-5,12; #159). With the story's ``partitions`` module on,
every arm above composes one viewpoint filter (``core.retrieval.viewpoint``): the occasion and
assessment reads, the vector arm (in the HNSW query), the lexical arm over both tiers (so a
retired row is filtered before it is rehydrated or re-embedded), the alias arm and the reads by
id return only memories whose visibility admits the viewpoint, and canon and the profiles are
read as the viewpoint may read them. The ``beliefs`` stage then returns the viewpoint
character's own beliefs (its partition, never another's) about the resolved and located
entities, held at ``suspected`` or above at the query's position, strongest first, at most
``BELIEFS_MAX``; each is a retrieved row (``beliefs``, its ``tx_from``). ``omniscient`` reads
everything and no beliefs; the player's character (``characters.is_player``) has no partition
and reads ``objective`` and ``public`` records only (FR-EPI-12). With ``partitions`` off (every
Lite story, M4's structured profile) memories are not filtered and canon keeps its own
visibility filter, as in M4.

**Quarantine** (#145, FR-GUARD-4,5): a quarantined memory is no candidate of any stage (the
occasion and assessment reads, the vector, lexical and alias arms, rehydration), so it is in no
context; ``include_quarantined`` (the omniscient debugging read's flag) lets it back in, marked
``quarantined``.

**Read set and access.** With ``read_set`` given, every returned memory is recorded in it as a
retrieved row (``memories``, its ``membership_tx``), and the canon and profiles it read too:
semantic retrieval is context, never keyed (#136). With ``touch``, every returned memory's
``last_accessed_at`` is set in one statement (a turn touches; the debugging read does not).

**Gated** by the ``episodic`` module (``memory.episodic.enabled``): off, ``retrieve`` reads the
story's configuration and nothing else, and answers an empty result with ``skipped:
["episodic"]`` (a Lite story's prompt never sees any of this). ``memory.episodic.reranking`` is
not delivered.

The cost of one retrieval is a fixed number of statements, whatever the story's size: each arm
is one indexed query (HNSW for the vector arm, GIN on ``search`` for the lexical and alias arms,
GIN on ``participants`` and ``(story_id, event_type, narrative_turn)`` for the occasion step),
the profiles a fixed number whatever the characters (#451), plus one statement per resolved
entity for canon; the rows the resolve stage reads grow with the text's relational
descriptions, not with the canon (#451). Logs carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Text, and_, cast, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock, embeddings
from epistrel.core.config_changes import section
from epistrel.core.enums import MemoryKind, RetrievalTier
from epistrel.core.epistemic import modeling
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import POSITION_MAX, active_only, active_only_sql
from epistrel.core.membership import Position as NarrativePosition
from epistrel.core.memory.records import EPISODIC_MODULE, episodic_on
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Polarity
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution import context as resolution
from epistrel.core.resolution.entities import (
    PLAYER,
    EntityIndex,
    ResolutionContext,
    resolve_mentions,
)
from epistrel.core.resolution.events import resolve_event_type
from epistrel.core.resolution.time import (
    Position,
    SubjectTimeline,
    TimeBasis,
    find_time_expressions,
    grammar,
    resolve_time,
    story_time_at,
)
from epistrel.core.resolution.types import (
    Mention,
    MentionKind,
    ResolvedTime,
    TimeExpression,
    TimeResolution,
)
from epistrel.core.retrieval import rank as ranking
from epistrel.core.retrieval.assemble import Context, ContextItem, Tier, assemble
from epistrel.core.retrieval.filtered import _seal
from epistrel.core.retrieval.lexical import (
    LexicalFilters,
    LexicalSearch,
    TsvectorLexicalSearch,
    position_clause,
    position_params,
    quarantine_clause,
)
from epistrel.core.retrieval.search import (
    EMBEDDING_PENDING,
    EMBEDDING_UNAVAILABLE,
    Embedder,
    vector_search,
)
from epistrel.core.retrieval.viewpoint import (
    SHARED_READER,
    Viewpoint,
    scope_for,
    scope_params,
    scope_sql,
)
from epistrel.core.settings_schema import (
    OMNISCIENT,
    EmbeddingSettings,
    MemorySettings,
    RetrievalSettings,
)
from epistrel.core.tables import deployment_state, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.memories import SEARCH_CONFIGURATION, Vector, memories
from epistrel.core.visibility import visible_clause

if TYPE_CHECKING:
    from epistrel.core.canon.facts import Fact
    from epistrel.core.epistemic.partitions import Belief
    from epistrel.core.projections.structured import ProfileView

log = logging.getLogger(__name__)

BatchEmbedder = Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]]
"""Several texts to their vectors in one call (``ModelAccessLayer.embeddings``)."""

MAX_EPISODES = 5
"""The most episodes the occasion step locates."""
MAX_ALIASES = 32
"""The most alias phrases the alias arm matches in one query."""
CANON_PER_ENTITY = 20
"""The most canon facts read about one resolved entity."""
BELIEFS_MAX = 20
"""The most beliefs the ``beliefs`` arm returns (#159)."""
PARTITIONS_MODULE = "partitions"

ARM_VECTOR = "vector"
ARM_LEXICAL = "lexical"
ARM_ALIAS = "alias"
ARM_OCCASION = "occasion"
ARM_ASSESSMENT = "assessment"
ARM_BELIEFS = "beliefs"

STAGES: tuple[str, ...] = (
    "resolve",
    "occasion",
    "vector",
    "lexical",
    "fuse",
    "profiles",
    "beliefs",
    "assemble",
)
"""The stages ``timings`` reports (and ``total``)."""

_TO_VECTOR = Vector().bind_processor(None)


# -- the query and its resolution ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """What a retrieval is for: the ``text`` (a player's input, a beat), the narrative
    ``position`` it is asked at (``(turn, beat)``; ``None``: after the head) and its ``actor``
    (an entity id, or ``narrator``/``player``; ``None``: the player)."""

    text: str
    position: tuple[int, int] | None = None
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class QueryResolution:
    """The resolve stage's answer: the mentions, the first temporal expression and its
    resolution, the event type, the resolved entities (each once, in mention order), whether a
    group mention was found, and the subject a subject-anchored expression needs (``anchor``)."""

    mentions: tuple[Mention, ...] = ()
    expression: TimeExpression | None = None
    time: TimeResolution | None = None
    event_type: str | None = None
    entities: tuple[str, ...] = ()
    group: bool = False
    anchor: str | None = None

    @property
    def window(self) -> tuple[int | None, int | None] | None:
        """The resolved time's possible story-minutes ``[start, end)``, or ``None``."""
        if isinstance(self.time, ResolvedTime):
            return self.time.valid_time.possible()
        return None


def _rule_kind(name: str) -> str | None:
    return next((r.kind for r in grammar().rules if r.name == name), None)


def _anchor_of(expression: TimeExpression, mentions: Sequence[Mention]) -> str | None:
    """The subject a subject-anchored expression ("when he was seven") is about: the resolved
    mention inside it, else the last resolved one before it."""
    inside = [
        m
        for m in mentions
        if m.entity_id is not None
        and m.span[0] >= expression.span[0]
        and m.span[1] <= expression.span[1]
    ]
    if inside:
        return inside[0].entity_id
    before = [m for m in mentions if m.entity_id is not None and m.span[1] <= expression.span[0]]
    return before[-1].entity_id if before else None


def resolve_query(
    text: str,
    *,
    speaker: str,
    position: Position,
    context: ResolutionContext,
    basis: TimeBasis,
    timeline: SubjectTimeline | None = None,
    extra_event_types: Iterable[str] = (),
) -> QueryResolution:
    """The resolve stage, pure (see the module docstring): ``timeline`` is the ``anchor``
    subject's, when a first pass found the expression subject-anchored."""
    mentions = tuple(resolve_mentions(text, speaker, context))
    expressions = find_time_expressions(text)
    expression = expressions[0] if expressions else None
    anchor = None
    time: TimeResolution | None = None
    if expression is not None:
        if _rule_kind(expression.rule) == "age":
            anchor = _anchor_of(expression, mentions)
        time = resolve_time(expression, position, timeline, basis=basis)
    entities = tuple(dict.fromkeys(m.entity_id for m in mentions if m.entity_id is not None))
    return QueryResolution(
        mentions=mentions,
        expression=expression,
        time=time,
        event_type=resolve_event_type(text, extra_event_types),
        entities=entities,
        group=any(m.kind is MentionKind.GROUP for m in mentions),
        anchor=anchor,
    )


def turn_window(
    window: tuple[int | None, int | None], basis: TimeBasis
) -> tuple[int | None, int | None]:
    """The turns whose story-time span overlaps ``window`` (``[start, end)`` story-minutes;
    ``None`` open): turn ``t`` spans ``[zero + t·m, zero + (t+1)·m)`` under M4's basis."""
    lo, hi = window
    m, zero = basis.minutes_per_turn, basis.minute_zero
    first = None if lo is None else math.floor((lo - zero) / m)
    last = None if hi is None else math.ceil((hi - zero) / m) - 1
    return first, last


def beat_occasion(text: str, mentions: Iterable[Mention]) -> tuple[str | None, tuple[str, ...]]:
    """A beat's ``(event_type, participants)`` for #135's episodic writer (#150 calls it with
    the beat's resolved mentions): #139's event type of the text and the entities its mentions
    resolved to, each once."""
    found = tuple(dict.fromkeys(m.entity_id for m in mentions if m.entity_id is not None))
    return resolve_event_type(text), found


# -- results -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    """One returned memory: the row's fields a context needs, which arms found it, and (for a
    ranked one) its score and the terms it came from."""

    id: str
    kind: MemoryKind
    text: str
    narrative_turn: int | None
    beat: int | None
    character_id: str | None
    event_type: str | None
    participants: tuple[str, ...]
    retrieval_tier: RetrievalTier
    importance: float | None
    membership_tx: datetime
    arms: tuple[str, ...] = ()
    quarantined: bool = False
    """Quarantined (#145): returned only with ``include_quarantined``."""
    score: float | None = None
    similarity: float | None = None
    recency: float | None = None
    decayed_importance: float | None = None

    @property
    def rehydrated(self) -> bool:
        return self.retrieval_tier is RetrievalTier.RETIRED


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """What ``retrieve`` answers (see the module docstring)."""

    episodes: tuple[RetrievedMemory, ...] = ()
    assessments: tuple[RetrievedMemory, ...] = ()
    memories: tuple[RetrievedMemory, ...] = ()
    participants: tuple[str, ...] = ()
    profiles: tuple[ProfileView, ...] = ()
    canon: tuple[Fact, ...] = ()
    beliefs: tuple[Belief, ...] = ()
    """The viewpoint character's own beliefs about what the query names (#159's ``beliefs``
    arm; empty for ``omniscient``, a reader, the player, and with ``partitions`` off)."""
    context: Context = field(default_factory=Context)
    resolution: QueryResolution = field(default_factory=QueryResolution)
    turn_window: tuple[int | None, int | None] | None = None
    timings: Mapping[str, float] = field(default_factory=dict)
    degradation: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    model_calls: int = 0
    embedding_model_version: str | None = None
    position: tuple[int, int] = (0, 0)

    @property
    def rehydrated(self) -> tuple[str, ...]:
        """The retired memories returned (read from the row, their tier unchanged)."""
        rows = (*self.episodes, *self.assessments, *self.memories)
        return tuple(m.id for m in rows if m.rehydrated)


# -- the context sources hook (#146's ledger, #147's pins) ---------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """What a context source is asked: the story, the query's position and viewpoint, and the
    entities the query resolved."""

    story_id: str
    position: tuple[int, int]
    viewpoint: str
    entities: tuple[str, ...]


ContextSource = Callable[[AsyncConnection, SourceRequest], Awaitable[Sequence[ContextItem]]]
"""A tier-1 source: its items (``ledger`` or ``pin``, tier ``LEDGER_AND_PINS``), in priority
order (pins by decayed importance)."""

CONTEXT_SOURCES: list[ContextSource] = []
"""Run in registration order (``register_context_source``)."""


def register_context_source(source: ContextSource) -> ContextSource:
    if source not in CONTEXT_SOURCES:
        CONTEXT_SOURCES.append(source)
    return source


def remove_context_source(source: ContextSource) -> None:
    if source in CONTEXT_SOURCES:
        CONTEXT_SOURCES.remove(source)


# -- parameters ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Params:
    """One retrieval's dials: the profile's (``memory.episodic``) and the deployment's."""

    k: int
    hybrid: bool
    weights: ranking.Weights
    recency_half_life: int
    token_budget: int
    vector_k: int
    lexical_k: int
    rrf_k: int
    rehydrate_max: int
    ef_search: int
    importance_half_life: int

    @classmethod
    def of(cls, settings: ProfileSettings, document: Mapping[str, Any] | None) -> Params:
        e = settings.memory.episodic
        r = section(RetrievalSettings, document)
        return cls(
            k=e.k,
            hybrid=e.hybrid_search,
            weights=ranking.Weights(e.weight_similarity, e.weight_recency, e.weight_importance),
            recency_half_life=e.recency_half_life_turns,
            token_budget=e.retrieval_token_budget,
            vector_k=r.retrieval_vector_k,
            lexical_k=r.retrieval_lexical_k,
            rrf_k=r.retrieval_rrf_k,
            rehydrate_max=r.retrieval_rehydrate_max_rows,
            ef_search=section(EmbeddingSettings, document).embedding_hnsw_ef_search,
            importance_half_life=section(
                MemorySettings, document
            ).memory_importance_half_life_turns,
        )


class _Clock:
    """Stage timings in milliseconds, with the embedding calls taken out."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.mark = self.started
        self.excluded = 0.0
        self.total_excluded = 0.0
        self.stages: dict[str, float] = {}

    def lap(self, stage: str) -> None:
        now = time.perf_counter()
        elapsed = max(0.0, now - self.mark - self.excluded)
        self.stages[stage] = self.stages.get(stage, 0.0) + elapsed * 1000.0
        self.mark = now
        self.excluded = 0.0

    def exclude(self, seconds: float) -> None:
        self.excluded += seconds
        self.total_excluded += seconds

    def timings(self) -> dict[str, float]:
        total = (time.perf_counter() - self.started - self.total_excluded) * 1000.0
        found = {stage: round(self.stages.get(stage, 0.0), 3) for stage in STAGES}
        found["total"] = round(max(0.0, total), 3)
        return found


# -- reads -----------------------------------------------------------------------------------------

_COLUMNS = (
    memories.c.id,
    memories.c.kind,
    memories.c.text,
    memories.c.narrative_turn,
    memories.c.beat,
    memories.c.character_id,
    memories.c.event_type,
    memories.c.participants,
    memories.c.retrieval_tier,
    memories.c.importance,
    memories.c.importance_at,
    memories.c.membership_tx,
    memories.c.quarantined_at,
    memories.c.holder_character_id,
)


def _through(through: tuple[int, int]) -> Any:
    m = memories.c
    turn, beat = through
    return or_(
        m.narrative_turn.is_(None),
        m.narrative_turn < turn,
        and_(m.narrative_turn == turn, or_(m.beat.is_(None), m.beat <= beat)),
    )


def _not_quarantined(include: bool) -> list[Any]:
    """The quarantine filter (#145): none unless ``include``."""
    return [] if include else [memories.c.quarantined_at.is_(None)]


def _scoped(scope: str | None) -> list[Any]:
    """The viewpoint filter (#159, ``core.retrieval.viewpoint``): none when ``scope`` is
    ``None``."""
    found = None if scope is None else visible_clause(memories, scope)
    return [] if found is None else [found]


def _row(r: Any, arms: tuple[str, ...] = ()) -> RetrievedMemory:
    return RetrievedMemory(
        id=str(r.id),
        kind=MemoryKind(r.kind),
        text=r.text,
        narrative_turn=r.narrative_turn,
        beat=r.beat,
        character_id=None if r.character_id is None else str(r.character_id),
        event_type=r.event_type,
        participants=tuple(str(p) for p in r.participants),
        retrieval_tier=RetrievalTier(r.retrieval_tier),
        importance=None if r.importance is None else float(r.importance),
        membership_tx=r.membership_tx,
        arms=arms,
        quarantined=r.quarantined_at is not None,
    )


async def _episodes(
    conn: AsyncConnection,
    story_id: str,
    *,
    through: tuple[int, int],
    turns: tuple[int | None, int | None] | None,
    event_type: str | None,
    entities: Sequence[str],
    include_quarantined: bool = False,
    text_query: str | None = None,
    scope: str | None = None,
) -> list[Any]:
    m = memories.c
    where = [
        m.story_id == story_id,
        active_only(memories),
        m.kind == MemoryKind.EPISODIC.value,
        _through(through),
        *_not_quarantined(include_quarantined),
        *_scoped(scope),
    ]
    if turns is not None:
        first, last = turns
        where.append(m.narrative_turn.is_not(None))
        if first is not None:
            where.append(m.narrative_turn >= first)
        if last is not None:
            where.append(m.narrative_turn <= last)
    if event_type is not None:
        where.append(m.event_type == event_type)
    if entities:
        where.append(m.participants.overlap(list(entities)))
    # Ties by position, then text, then id (#476: ids differ between two replays).
    latest = (m.narrative_turn.desc().nulls_last(), m.beat.desc().nulls_last(), m.text, m.id)
    order: tuple[Any, ...] = latest
    if turns is None and text_query is not None and text_query.strip():
        # No window locates the occasion: the episodes that best match the text come first
        # (#458), the latest first among equals.
        words = cast(
            func.replace(
                cast(func.plainto_tsquery(SEARCH_CONFIGURATION, text_query), Text), " & ", " | "
            ),
            TSQUERY,
        )
        order = (func.ts_rank(m.search, words).desc(), *latest)
    found = await conn.execute(select(*_COLUMNS).where(*where).order_by(*order).limit(MAX_EPISODES))
    return list(found)


async def _assessments(
    conn: AsyncConnection,
    story_id: str,
    *,
    through: tuple[int, int],
    since_turn: int | None,
    participants: Sequence[str],
    k: int,
    include_quarantined: bool = False,
    scope: str | None = None,
) -> list[Any]:
    m = memories.c
    where = [
        m.story_id == story_id,
        active_only(memories),
        m.kind.in_([MemoryKind.SEMANTIC.value, MemoryKind.REFLECTION.value]),
        m.participants.overlap(list(participants)),
        _through(through),
        *_not_quarantined(include_quarantined),
        *_scoped(scope),
    ]
    if since_turn is not None:
        where.append(or_(m.narrative_turn.is_(None), m.narrative_turn >= since_turn))
    found = await conn.execute(
        select(*_COLUMNS)
        .where(*where)
        .order_by(m.narrative_turn.desc().nulls_last(), m.beat.desc().nulls_last(), m.text, m.id)
        .limit(k)
    )
    return list(found)


async def _by_id(
    conn: AsyncConnection,
    story_id: str,
    ids: Sequence[str],
    include_quarantined: bool = False,
    scope: str | None = None,
) -> dict[str, Any]:
    if not ids:
        return {}
    found = await conn.execute(
        select(*_COLUMNS).where(
            memories.c.story_id == story_id,
            active_only(memories),
            memories.c.id.in_(list(ids)),
            *_not_quarantined(include_quarantined),
            *_scoped(scope),
        )
    )
    return {str(r.id): r for r in found}


async def _alias_arm(
    conn: AsyncConnection,
    story_id: str,
    aliases: Sequence[str],
    *,
    through: tuple[int, int],
    k: int,
    include_quarantined: bool = False,
    scope: str | None = None,
) -> list[str]:
    """The hot active memories whose text holds one of ``aliases`` as a phrase (any casing),
    latest first (none quarantined unless ``include_quarantined``; only those ``scope``
    admits)."""
    if not aliases or k < 1:
        return []
    phrases = " || ".join(
        f"phraseto_tsquery('{SEARCH_CONFIGURATION}', :a{i})" for i in range(len(aliases))
    )
    found = await conn.execute(
        text(
            "SELECT m.id FROM memories m "  # noqa: S608 - fixed fragments and bound values only
            f"WHERE m.story_id = :story AND {active_only_sql('m')} "
            f"AND m.retrieval_tier = '{RetrievalTier.HOT.value}' "
            f"AND {position_clause('m')} "
            f"AND {quarantine_clause('m')} "
            f"AND {scope_sql('m')} "
            f"AND m.search @@ ({phrases}) "
            "ORDER BY m.narrative_turn DESC NULLS LAST, m.beat DESC NULLS LAST, m.text, m.id "
            "LIMIT :k"
        ),
        {
            "story": story_id,
            "k": k,
            "include_quarantined": include_quarantined,
            **position_params(through),
            **scope_params(scope),
            **{f"a{i}": a for i, a in enumerate(aliases)},
        },
    )
    return [str(r.id) for r in found]


async def _stored_similarity(
    conn: AsyncConnection, story_id: str, ids: Sequence[str], vector: Sequence[float], version: str
) -> dict[str, float]:
    if not ids:
        return {}
    found = await conn.execute(
        text(
            "SELECT m.id, m.embedding <=> CAST(:v AS vector) AS distance FROM memories m "
            "WHERE m.story_id = :story AND m.id = ANY(CAST(:ids AS text[])) "
            "AND m.embedding IS NOT NULL AND m.embedding_model_version = :version"
        ),
        {"v": _TO_VECTOR(list(vector)), "story": story_id, "ids": list(ids), "version": version},
    )
    return {str(r.id): 1.0 - float(r.distance) for r in found}


async def _retain(
    conn: AsyncConnection, story_id: str, vectors: Mapping[str, Sequence[float]], version: str
) -> None:
    """Store the vectors a rehydration computed (``memory.hot_set.retain_retired_vectors``,
    #147): on the retired rows that still have none, stamped with the pinned version; a vector
    of another dimension is not stored. The rows stay retired."""
    pin = await embeddings.read_pin(conn)
    for memory_id, vector in sorted(vectors.items()):
        if pin.dimension is not None and len(vector) != pin.dimension:
            continue
        await conn.execute(
            update(memories)
            .where(
                memories.c.story_id == story_id,
                memories.c.id == memory_id,
                memories.c.retrieval_tier == RetrievalTier.RETIRED.value,
                memories.c.embedding.is_(None),
            )
            .values(embedding=list(vector), embedding_model_version=version)
        )


LEXICAL_RELATIVE_FLOOR = 0.6
"""A lexical hit ranks among the arm's hits only when its score is at least this share of the
best hit's (#458)."""


def _relevant_lexical(hits: Sequence[tuple[str, float]]) -> list[tuple[str, float]]:
    """The lexical hits (best first) without those far weaker than the best (#458): a memory
    sharing one common word with the query is no match beside one holding all its words, and
    reciprocal-rank fusion would rank the two close enough for recency to reorder them."""
    if not hits:
        return []
    best = max(score for _, score in hits)
    return [(i, score) for i, score in hits if score >= best * LEXICAL_RELATIVE_FLOOR]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


async def _is_player(conn: AsyncConnection, story_id: str, character_id: str) -> bool:
    from epistrel.core.epistemic.partitions import is_player

    return await is_player(conn, story_id, character_id)


async def _head(conn: AsyncConnection, story_id: str) -> int:
    found = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    return int(found or 0) - 1


# -- rendering context items ---------------------------------------------------------------------


def _name(index: EntityIndex, entity_id: str | None) -> str:
    entry = None if entity_id is None else index.get(entity_id)
    return entry.canonical_name if entry is not None else (entity_id or "")


def profile_text(profile: ProfileView) -> str:
    """A standing profile as one context line."""
    name = profile.entity.name
    parts = []
    for rel in profile.relationships:
        if rel.direction == "in":
            parts.append(f"{rel.other.name} {rel.predicate} {name}")
        else:
            parts.append(f"{name} {rel.predicate} {rel.other.name}")
    placement = profile.placement
    if placement is not None and placement.location is not None:
        parts.append(f"{name} is at {placement.location.name}")
    elif placement is not None and placement.container is not None:
        parts.append(f"{name} is held by {placement.container.name}")
    if profile.inventory:
        parts.append(f"{name} holds " + ", ".join(i.item.name for i in profile.inventory))
    return f"{name}: " + ("; ".join(parts) if parts else "no standing relationships")


def canon_text(fact: Fact, index: EntityIndex, predicates: Mapping[str, str]) -> str:
    """A canon fact as one context line (subject, predicate, object or literal)."""
    p = fact.proposition
    predicate = predicates.get(p.predicate_id, p.predicate_id)
    if p.object_id is not None:
        obj = _name(index, p.object_id)
    elif p.object_set is not None:
        obj = ", ".join(_name(index, o) for o in p.object_set)
    elif p.literal is not None:
        obj = str(p.literal.value)
    else:
        obj = ""
    negated = "not " if p.polarity is not Polarity.AFFIRMED else ""
    return " ".join(x for x in (_name(index, p.subject_id), f"{negated}{predicate}", obj) if x)


# -- the pipeline ----------------------------------------------------------------------------------


async def _speaker(index: EntityIndex, actor: str | None) -> str:
    if actor is None:
        return PLAYER
    if actor in ("narrator", PLAYER) or index.get(actor) is not None:
        return actor
    by_character = [e.id for e in index.entries.values() if e.character_id == actor]
    if by_character:
        return by_character[0]
    raise InvalidRequest(
        "the query's actor is one of the story's entities or characters",
        {"field": "query.actor", "reason": "unknown_actor"},
    )


async def retrieve(
    conn: AsyncConnection,
    story_id: str,
    query: RetrievalQuery,
    *,
    viewpoint: str = OMNISCIENT,
    embed: Embedder | None = None,
    embed_batch: BatchEmbedder | None = None,
    read_set: StructuredReadSet | None = None,
    lexical: LexicalSearch | None = None,
    settings: ProfileSettings | None = None,
    touch: bool = True,
    extra_event_types: Iterable[str] = (),
    kinds: Iterable[MemoryKind] | None = None,
    include_quarantined: bool = False,
) -> RetrievalResult:
    """Retrieve for ``query`` (see the module docstring). ``kinds`` keeps only memories of
    those kinds (every kind when ``None``): Q-03's arm without the reflection tier (#143).
    ``include_quarantined`` lets quarantined memories back in (the debugging read's flag)."""
    iq = include_quarantined
    wanted = None if kinds is None else frozenset(MemoryKind(k) for k in kinds)
    clk = _Clock()
    if settings is None:
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
    if not await episodic_on(conn, story_id, settings):
        return RetrievalResult(skipped=(EPISODIC_MODULE,), timings=clk.timings())
    document = await conn.scalar(
        select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    params = Params.of(settings, document)
    lexical = lexical or TsvectorLexicalSearch()
    read_set = read_set if read_set is not None else StructuredReadSet()
    lookup = KeyedLookup(conn, read_set)
    head = await _head(conn, story_id)
    position = query.position or (head + 1, 0)
    through = position
    now_turn = position[0]
    # #159: the viewpoint filter every arm composes (``core.retrieval.viewpoint``).
    seen_as = Viewpoint.parse(viewpoint)
    partitioned = module_on(settings, PARTITIONS_MODULE)
    player = (
        partitioned
        and seen_as.kind == "character"
        and seen_as.character_id is not None
        and await _is_player(conn, story_id, seen_as.character_id)
    )
    scope = scope_for(seen_as, partitions=partitioned, player=player)
    # The player's character has no partition and no private records (FR-EPI-12): it reads
    # canon as a reader does (its standing profiles stay the character's view).
    canon_viewpoint = SHARED_READER if player else viewpoint

    # 1. resolve
    index = await resolution.load_index(conn, story_id)
    speaker = await _speaker(index, query.actor)
    basis = await resolution.load_time_basis(conn, story_id)
    context = await resolution.load_context(
        conn,
        story_id,
        position=position,
        speaker=speaker,
        index=index,
        lookup=lookup,
        text=query.text,
    )
    resolved = resolve_query(
        query.text,
        speaker=speaker,
        position=position,
        context=context,
        basis=basis,
        extra_event_types=extra_event_types,
    )
    if resolved.anchor is not None:
        timeline = await resolution.subject_timeline(
            lookup,
            story_id,
            resolved.anchor,
            viewpoint=canon_viewpoint,
            through_position=(now_turn, 0),
        )
        if resolved.expression is not None:
            resolved = replace(
                resolved, time=resolve_time(resolved.expression, position, timeline, basis=basis)
            )
    speaker_entity = speaker if index.get(speaker) is not None else context.player_id
    clk.lap("resolve")

    # 2. occasion
    skipped: list[str] = []
    window = resolved.window
    turns = None if window is None else turn_window(window, basis)
    episode_rows: list[Any] = []
    assessment_rows: list[Any] = []
    participants: tuple[str, ...] = ()
    if turns is None and resolved.event_type is None:
        skipped.append(ARM_OCCASION)
    else:
        episode_rows = await _episodes(
            conn,
            story_id,
            through=through,
            turns=turns,
            event_type=resolved.event_type,
            entities=resolved.entities,
            include_quarantined=iq,
            text_query=query.text,
            scope=scope,
        )
        participants = tuple(
            dict.fromkeys(
                p
                for r in episode_rows
                for p in (
                    *([str(r.character_id)] if r.character_id else []),
                    *(str(x) for x in r.participants),
                )
            )
        )
        if participants:
            assessment_rows = await _assessments(
                conn,
                story_id,
                through=through,
                since_turn=None if turns is None else turns[0],
                participants=participants,
                k=params.k,
                include_quarantined=iq,
                scope=scope,
            )
    if wanted is not None:
        episode_rows = [r for r in episode_rows if MemoryKind(r.kind) in wanted]
        assessment_rows = [r for r in assessment_rows if MemoryKind(r.kind) in wanted]
    clk.lap("occasion")

    # 3. the hybrid arms
    degradation: list[str] = []
    model_calls = 0
    vector_hits: list[tuple[str, float]] = []
    query_vector: Sequence[float] | None = None
    version: str | None = None
    if embed is not None:
        pin = await embeddings.read_pin(conn)
        if await embeddings.is_pending(conn, story_id, pin):
            degradation.append(EMBEDDING_PENDING)
        elif pin.pinned:
            started = time.perf_counter()
            try:
                model_calls += 1
                vector = await embed(query.text)
                embeddings.check_dimension(pin, len(vector), model=str(pin.version))
                query_vector = vector
            except Exception as exc:  # any failure of the injected embedder degrades
                log.info("query embedding failed story=%s error=%s", story_id, type(exc).__name__)
                degradation.append(EMBEDDING_UNAVAILABLE)
            clk.exclude(time.perf_counter() - started)
            if query_vector is not None:
                version = str(pin.version)
                await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(params.ef_search)}"))
                await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                hits = await vector_search(
                    conn,
                    story_id,
                    query_vector,
                    version=version,
                    k=params.vector_k,
                    through=through,
                    include_quarantined=iq,
                    scope=scope,
                )
                vector_hits = [(h.memory_id, h.score) for h in hits]
    else:
        skipped.append(ARM_VECTOR)
    clk.lap("vector")

    vector_ran = query_vector is not None
    lexical_on = params.hybrid or not vector_ran
    lexical_hits: list[tuple[str, float]] = []
    alias_ids: list[str] = []
    retired_hits: list[tuple[str, float]] = []
    rehydrate_left = max(
        0,
        params.rehydrate_max
        - sum(
            1
            for r in (*episode_rows, *assessment_rows)
            if r.retrieval_tier == RetrievalTier.RETIRED.value
        ),
    )
    taken = {str(r.id) for r in (*episode_rows, *assessment_rows)}
    if lexical_on:
        lexical_hits = await lexical.search(
            conn,
            story_id,
            query.text,
            params.lexical_k,
            LexicalFilters(through=through, include_quarantined=iq, scope=scope),
        )
        if rehydrate_left:
            found_retired = await lexical.search(
                conn,
                story_id,
                query.text,
                rehydrate_left + len(taken),
                LexicalFilters(
                    through=through,
                    tiers=(RetrievalTier.RETIRED,),
                    include_quarantined=iq,
                    scope=scope,
                ),
            )
            retired_hits = [h for h in found_retired if h[0] not in taken][:rehydrate_left]
    else:
        skipped.append(ARM_LEXICAL)
    if params.hybrid:
        aliases: list[str] = []
        # The speaker's "I"/"me" names no topic: every memory the speaker is in would match its
        # name (#458). An entity the text names otherwise (a name, "you", "he") counts.
        named = dict.fromkeys(
            m.entity_id
            for m in resolved.mentions
            if m.entity_id is not None and m.kind is not MentionKind.FIRST_PERSON
        )
        for entity_id in named:
            entry = index.get(entity_id)
            if entry is None:
                continue
            aliases.extend([entry.canonical_name, *(a for a, _ in entry.aliases)])
        aliases = list(dict.fromkeys(a for a in aliases if a.strip()))[:MAX_ALIASES]
        alias_ids = await _alias_arm(
            conn,
            story_id,
            aliases,
            through=through,
            k=params.lexical_k,
            include_quarantined=iq,
            scope=scope,
        )
    else:
        skipped.append(ARM_ALIAS)
    clk.lap("lexical")

    # Rehydrated rows found lexically join the vector ranking by their similarity.
    retired_ids = [i for i, _ in retired_hits]
    if vector_ran and retired_ids and query_vector is not None and version is not None:
        similar = await _stored_similarity(conn, story_id, retired_ids, query_vector, version)
        missing = [i for i in retired_ids if i not in similar]
        if missing and embed_batch is not None:
            rows = await _by_id(conn, story_id, missing, iq, scope)
            texts = [rows[i].text for i in missing if i in rows]
            started = time.perf_counter()
            try:
                model_calls += 1
                vectors = await embed_batch(texts)
                computed = dict(zip([i for i in missing if i in rows], vectors, strict=True))
                for memory_id, vector in computed.items():
                    similar[memory_id] = _cosine(query_vector, vector)
                if touch and settings.memory.hot_set.retain_retired_vectors:
                    await _retain(conn, story_id, computed, version)
            except Exception as exc:  # a failed rehydration embedding leaves them lexical
                log.info(
                    "rehydration embedding failed story=%s error=%s", story_id, type(exc).__name__
                )
            clk.exclude(time.perf_counter() - started)
        # A stable sort by score alone: ties keep each arm's own order (its position tie-break)
        # and the hot hits before the rehydrated ones, never the random id order (#476).
        rehydrated = [(i, similar[i]) for i in retired_ids if i in similar]
        vector_hits = sorted([*vector_hits, *rehydrated], key=lambda hit: -hit[1])
    if retired_hits:
        lexical_hits = sorted([*lexical_hits, *retired_hits], key=lambda hit: -hit[1])
    lexical_hits = _relevant_lexical(lexical_hits)

    # 4. fuse and rank
    rankings: list[list[str]] = []
    arms_of: dict[str, list[str]] = {}
    for arm, ids in (
        (ARM_VECTOR, [i for i, _ in vector_hits] if vector_ran else None),
        (ARM_LEXICAL, [i for i, _ in lexical_hits] if lexical_on else None),
        (ARM_ALIAS, alias_ids if params.hybrid else None),
    ):
        if ids is None:
            continue
        rankings.append(ids)
        for memory_id in ids:
            arms_of.setdefault(memory_id, []).append(arm)
    fused = ranking.fuse(rankings, params.rrf_k)
    candidates = [i for i in fused if i not in taken]
    details = await _by_id(conn, story_id, candidates, iq, scope)
    if wanted is not None:
        details = {i: r for i, r in details.items() if MemoryKind(r.kind) in wanted}
    # #163 (FR-EPI-10): a minor holder's records rank below a major character's.
    minors = await modeling.minor_ids(conn, story_id) if details else frozenset()
    ranked = ranking.rank(
        (
            ranking.Candidate(
                i,
                r.narrative_turn,
                r.importance,
                r.importance_at,
                r.beat,
                r.text,
                factor=modeling.MINOR_SCORE_FACTOR
                if r.holder_character_id is not None and str(r.holder_character_id) in minors
                else 1.0,
            )
            for i, r in details.items()
        ),
        fused,
        now=now_turn,
        weights=params.weights,
        recency_half_life=params.recency_half_life,
        importance_half_life=params.importance_half_life,
    )[: params.k]
    ranked_memories = tuple(
        replace(
            _row(details[r.memory_id], tuple(arms_of.get(r.memory_id, ()))),
            score=r.score,
            similarity=r.similarity,
            recency=r.recency,
            decayed_importance=r.importance,
        )
        for r in ranked
    )
    clk.lap("fuse")

    # 5. profiles and canon
    subjects = [
        e for e in dict.fromkeys((*resolved.entities, *participants)) if e != speaker_entity
    ]
    profiles: list[ProfileView] = []
    as_of = min(max(now_turn, 0), head)
    profiled = [e for e in subjects if (entry := index.get(e)) is not None and entry.character_id]
    if head >= 0 and profiled:
        profiles = await lookup.profiles(story_id, profiled, as_of=as_of, viewpoint=viewpoint)
    canon: list[Fact] = []
    if subjects:
        canon = await lookup.canon_context(
            story_id,
            subjects=subjects,
            story_time=story_time_at(position, basis),
            viewpoint=canon_viewpoint,
            through_position=(now_turn, 0),
            limit=CANON_PER_ENTITY,
        )
    clk.lap("profiles")

    # 5b. the viewpoint character's own beliefs (#159): its partition, never another's.
    held: list[Belief] = []
    believer = seen_as.character_id if scope is not None and seen_as.kind == "character" else None
    about = list(dict.fromkeys((*resolved.entities, *participants)))
    if believer is not None and not player and about:
        from epistrel.core.epistemic import partitions as partition_store

        held = await partition_store.partition(
            conn,
            story_id,
            believer,
            through=NarrativePosition(now_turn, 0, 0, position[1], POSITION_MAX),
            entities_=about,
            levels=partition_store.HELD_LEVELS,
            limit=BELIEFS_MAX,
            order="confidence",
        )
        for belief in held:
            read_set.record_retrieved(ARM_BELIEFS, belief.id, belief.tx_from)
    clk.lap("beliefs")

    # 6. assemble
    episodes = tuple(_row(r, (ARM_OCCASION,)) for r in episode_rows)
    assessments = tuple(_row(r, (ARM_ASSESSMENT,)) for r in assessment_rows)
    request = SourceRequest(story_id, position, viewpoint, resolved.entities)
    tier_one: list[ContextItem] = []
    for source in list(CONTEXT_SOURCES):
        for item in await source(conn, request):
            if item.tier is not Tier.LEDGER_AND_PINS or item.kind not in ("ledger", "pin"):
                raise ValueError("a context source answers tier-1 ledger or pin items")
            tier_one.append(item)
    tier_one.sort(key=lambda i: 0 if i.kind == "ledger" else 1)
    predicates: dict[str, str] = {}
    if canon:
        from epistrel.core.propositions.registry import load as load_registry

        registry = await load_registry(conn, story_id)
        predicates = {pid: p.name for pid, p in registry.predicates.items()}
    items = [
        *tier_one,
        *(ContextItem(Tier.OCCASIONS, "episode", m.id, m.text) for m in episodes),
        *(ContextItem(Tier.OCCASIONS, "assessment", m.id, m.text) for m in assessments),
        *(
            ContextItem(Tier.PROFILES, "profile", p.entity.entity_id, profile_text(p))
            for p in profiles
        ),
        *(
            ContextItem(Tier.PROFILES, "canon", f.id, canon_text(f, index, predicates))
            for f in canon
        ),
        *(ContextItem(Tier.MEMORIES, "memory", m.id, m.text) for m in ranked_memories),
    ]
    # #170 (U-31): every item above came through #159's viewpoint filter.
    assembled = assemble(_seal(items, viewpoint), params.token_budget)
    returned = (*episodes, *assessments, *ranked_memories)
    for memory in returned:
        read_set.record_retrieved(memories.name, memory.id, memory.membership_tx)
    if touch and returned:
        await conn.execute(
            update(memories)
            .where(
                memories.c.story_id == story_id,
                memories.c.id.in_([m.id for m in returned]),
            )
            .values(last_accessed_at=clock.now())
        )
    clk.lap("assemble")
    log.debug(
        "retrieved story=%s episodes=%d assessments=%d memories=%d profiles=%d canon=%d",
        story_id,
        len(episodes),
        len(assessments),
        len(ranked_memories),
        len(profiles),
        len(canon),
    )
    return RetrievalResult(
        episodes=episodes,
        assessments=assessments,
        memories=ranked_memories,
        participants=participants,
        profiles=tuple(profiles),
        canon=tuple(canon),
        beliefs=tuple(held),
        context=assembled,
        resolution=resolved,
        turn_window=turns,
        timings=clk.timings(),
        degradation=tuple(degradation),
        skipped=tuple(skipped),
        model_calls=model_calls,
        embedding_model_version=version,
        position=position,
    )


QUERY_TEXT_MAX = 4_000
"""The longest query text the debugging read takes."""


async def read_retrieve(
    conn: AsyncConnection,
    story_id: str,
    *,
    q: str,
    viewpoint: str | None,
    as_of: int | None = None,
    actor_character_id: str | None = None,
    embed: Embedder | None = None,
    embed_batch: BatchEmbedder | None = None,
    lexical: LexicalSearch | None = None,
    include_quarantined: bool = False,
) -> RetrievalResult:
    """``retrieve`` as the debugging read (``GET /stories/{id}/retrieve``, ``Engine.retrieve``)
    asks it: the text ``q`` asked by ``actor_character_id`` (one of the cast; default the
    player) just after turn ``as_of`` (default the head; 422 outside ``0..head``), for
    ``viewpoint`` resolved as every read resolves it, which must be ``omniscient`` (403
    ``omniscient_required``: the read shows ranking over records a character may not see). It
    touches no memory's ``last_accessed_at``. 404 for an unknown story."""
    from epistrel.core.errors import StoryNotFound
    from epistrel.core.settings_schema import StorySettings
    from epistrel.core.tables import characters
    from epistrel.core.viewpoint import require_omniscient, resolve_viewpoint

    row = (
        await conn.execute(
            select(stories.c.next_turn_index, stories.c.settings).where(stories.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    cast = [
        str(c)
        for c in await conn.scalars(
            select(characters.c.id).where(characters.c.story_id == story_id)
        )
    ]
    resolved = resolve_viewpoint(
        viewpoint,
        actor_character_id=actor_character_id,
        story_default=StorySettings.from_row(row.settings).default_output_viewpoint,
        cast_ids=cast,
        field="query.viewpoint",
    )
    require_omniscient(resolved, field="query.viewpoint")
    if not q.strip() or len(q) > QUERY_TEXT_MAX:
        raise InvalidRequest(
            f"q is a text of 1 to {QUERY_TEXT_MAX} characters",
            {"field": "query.q", "reason": "invalid_query"},
        )
    if actor_character_id is not None and actor_character_id not in cast:
        raise InvalidRequest(
            "actor_character_id is one of the story's characters",
            {"field": "query.actor_character_id", "reason": "unknown_character"},
        )
    head = int(row.next_turn_index) - 1
    if as_of is not None and not 0 <= as_of <= head:
        raise InvalidRequest(
            "as_of must be a turn of the story, from 0 to its head",
            {"field": "query.as_of", "head": head},
        )
    position = None if as_of is None else (as_of + 1, 0)
    return await retrieve(
        conn,
        story_id,
        RetrievalQuery(q, position=position, actor=actor_character_id),
        viewpoint=resolved,
        embed=embed,
        embed_batch=embed_batch,
        lexical=lexical,
        touch=False,
        include_quarantined=include_quarantined,
    )


__all__ = [
    "ARM_ALIAS",
    "ARM_ASSESSMENT",
    "ARM_BELIEFS",
    "ARM_LEXICAL",
    "ARM_OCCASION",
    "ARM_VECTOR",
    "BELIEFS_MAX",
    "CONTEXT_SOURCES",
    "MAX_EPISODES",
    "QUERY_TEXT_MAX",
    "STAGES",
    "BatchEmbedder",
    "ContextSource",
    "Params",
    "QueryResolution",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievedMemory",
    "SourceRequest",
    "beat_occasion",
    "canon_text",
    "profile_text",
    "read_retrieve",
    "register_context_source",
    "remove_context_source",
    "resolve_query",
    "retrieve",
    "turn_window",
]

# #146's ``## Open threads`` and #147's pins register themselves on ``CONTEXT_SOURCES`` (after
# everything above is defined, so the import cycle is harmless), the ledger first.
from epistrel.core.commit import threads as _threads  # noqa: E402, F401 - registers a source
from epistrel.core.memory import pins as _pins  # noqa: E402, F401 - registers a source
