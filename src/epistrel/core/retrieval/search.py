"""The search that never mixes vector spaces (FR-EMBED-3, FR-STORE-30; docs/02 §6; #138).

``search(conn, story_id, query, k=…, lexical=…, embed=…)`` answers a story's memories for a
query through two channels, each over **active hot** memories only:

- **vector**: the query embedded by ``embed`` (an ``Embedder`` the Orchestrator injects, so
  Core imports no model code), compared by cosine distance with the memories whose vector is
  stamped with the deployment's pinned version (``core.embeddings``). A vector in any other
  version is never compared, whatever its dimension (``vector_search``).
- **lexical**: the swappable backend (``core.retrieval.lexical``).

``SearchResult.degradation`` says when the vector channel was left out:

- ``embedding_pending``: the story's vectors are not all in the pinned version yet (a model
  change's re-embed job has not finished it): lexical only, and no query is embedded;
- ``embedding_unavailable``: embedding the query failed (the endpoint is down, unconfigured,
  or answered another dimension than the pin's): lexical only.

No ``embed`` (the story's ``episodic`` module is off, or nothing is embedded yet) is lexical
only with **no** marker: nothing was degraded. Fusing the channels into one ranking, the token
budget and the alias channel are ``core.retrieval.pipeline``'s (#141), which runs
``vector_search`` as its vector arm.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import embeddings
from epistrel.core.enums import RetrievalTier
from epistrel.core.membership import active_only_sql
from epistrel.core.retrieval.lexical import (
    LexicalFilters,
    LexicalSearch,
    position_clause,
    position_params,
    quarantine_clause,
)
from epistrel.core.tables.memories import Vector

log = logging.getLogger(__name__)

Embedder = Callable[[str], Awaitable[Sequence[float]]]
"""One query text to its vector (``ModelAccessLayer.embeddings.query_embedder()``)."""

EMBEDDING_PENDING = "embedding_pending"
EMBEDDING_UNAVAILABLE = "embedding_unavailable"

_TO_VECTOR = Vector().bind_processor(None)


@dataclass(frozen=True)
class Hit:
    """One memory a channel found, with its channel score (higher is better: ``1 - cosine
    distance`` for vectors, ``ts_rank`` for lexical)."""

    memory_id: str
    score: float


@dataclass(frozen=True)
class SearchResult:
    """Both channels' hits, best first, and why a channel was left out (``degradation``)."""

    vector: tuple[Hit, ...] = ()
    lexical: tuple[Hit, ...] = ()
    degradation: tuple[str, ...] = field(default_factory=tuple)
    embedding_model_version: str | None = None
    """The version the vector channel compared within (``None`` when it did not run)."""


async def vector_search(
    conn: AsyncConnection,
    story_id: str,
    vector: Sequence[float],
    *,
    version: str,
    k: int,
    through: tuple[int, int] | None = None,
    include_quarantined: bool = False,
) -> list[Hit]:
    """Up to ``k`` of the story's active hot memories nearest ``vector``, among those stamped
    ``version`` only, none positioned after ``through`` (``(turn, beat)``, when given) and none
    quarantined (#145) unless ``include_quarantined``.
    Ordered by the HNSW index (cosine distance) in a materialized step, then re-sorted
    exactly: with pgvector's iterative scan in ``relaxed_order`` (#141 sets it) the index may
    answer slightly out of order."""
    if k < 1:
        return []
    found = await conn.execute(
        text(
            "WITH nearest AS MATERIALIZED ("  # noqa: S608 - fixed fragments only
            "SELECT m.id, m.embedding <=> CAST(:v AS vector) AS distance FROM memories m "
            f"WHERE m.story_id = :story AND {active_only_sql('m')} "
            f"AND m.retrieval_tier = '{RetrievalTier.HOT.value}' "
            "AND m.embedding IS NOT NULL AND m.embedding_model_version = :version "
            f"AND {position_clause('m')} "
            f"AND {quarantine_clause('m')} "
            "ORDER BY distance LIMIT :k) "
            "SELECT id, distance FROM nearest ORDER BY distance, id"
        ),
        {
            "v": _TO_VECTOR(list(vector)),
            "story": story_id,
            "version": version,
            "k": k,
            "include_quarantined": include_quarantined,
            **position_params(through),
        },
    )
    return [Hit(str(r.id), 1.0 - float(r.distance)) for r in found]


async def search(
    conn: AsyncConnection,
    story_id: str,
    query: str,
    *,
    k: int,
    lexical: LexicalSearch,
    embed: Embedder | None = None,
    filters: LexicalFilters | None = None,
) -> SearchResult:
    """Both channels for ``query`` (see the module docstring)."""
    lexical_hits = tuple(
        Hit(memory_id, rank)
        for memory_id, rank in await lexical.search(conn, story_id, query, k, filters)
    )
    if embed is None:
        return SearchResult(lexical=lexical_hits)
    pin = await embeddings.read_pin(conn)
    if await embeddings.is_pending(conn, story_id, pin):
        return SearchResult(lexical=lexical_hits, degradation=(EMBEDDING_PENDING,))
    if not pin.pinned:
        return SearchResult(lexical=lexical_hits)
    try:
        vector = await embed(query)
        embeddings.check_dimension(pin, len(vector), model=str(pin.version))
    except Exception as exc:  # any failure of the injected embedder degrades, never fails
        log.info("query embedding failed story=%s error=%s", story_id, type(exc).__name__)
        return SearchResult(lexical=lexical_hits, degradation=(EMBEDDING_UNAVAILABLE,))
    hits = await vector_search(conn, story_id, vector, version=str(pin.version), k=k)
    return SearchResult(
        vector=tuple(hits), lexical=lexical_hits, embedding_model_version=pin.version
    )


__all__ = [
    "EMBEDDING_PENDING",
    "EMBEDDING_UNAVAILABLE",
    "Embedder",
    "Hit",
    "SearchResult",
    "search",
    "vector_search",
]
