"""The lexical search backend (NFR-PORT-2, FR-RET; #138), swappable like the embedding adapter.

``LexicalSearch.search(conn, story_id, query, k, filters)`` answers up to ``k`` of the story's
memories that match ``query`` as ``(memory_id, rank)``, best first, over **active hot** memories
only (``core.membership.active_only``; a retired memory is never searched, FR-MEM-8). It is the
channel a search falls back to while the story's vectors are being re-embedded or the embedding
endpoint is down (``core.retrieval.search``).

One backend ships: ``tsvector``, PostgreSQL full-text search under the ``english`` text-search
configuration (``to_tsvector('english', text) @@ plainto_tsquery('english', query)``, ranked by
``ts_rank``, ties by id). It computes the document vector per query; #141's migration adds the
generated ``tsvector`` column and its GIN index, and this backend reads them then.
``EPISTREL_LEXICAL_BACKEND`` names the backend (``BACKENDS``); a test-only in-memory backend
proves the swap (C-07 M4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import MemoryKind, RetrievalTier
from epistrel.core.membership import active_only_sql

TEXT_SEARCH_CONFIGURATION = "english"


@dataclass(frozen=True)
class LexicalFilters:
    """Narrowing a lexical search: only memories of these ``kinds`` (all when ``None``)."""

    kinds: tuple[MemoryKind, ...] | None = None


@runtime_checkable
class LexicalSearch(Protocol):
    """One lexical search backend."""

    name: str

    async def search(
        self,
        conn: AsyncConnection,
        story_id: str,
        query: str,
        k: int,
        filters: LexicalFilters | None = None,
    ) -> list[tuple[str, float]]: ...


class TsvectorLexicalSearch:
    """PostgreSQL full-text search over ``memories.text`` (see the module docstring)."""

    name = "tsvector"

    async def search(
        self,
        conn: AsyncConnection,
        story_id: str,
        query: str,
        k: int,
        filters: LexicalFilters | None = None,
    ) -> list[tuple[str, float]]:
        if k < 1 or not query.strip():
            return []
        kinds = (
            None
            if filters is None or filters.kinds is None
            else [kind.value for kind in filters.kinds]
        )
        document = f"to_tsvector('{TEXT_SEARCH_CONFIGURATION}', m.text)"
        found = await conn.execute(
            text(
                f"SELECT m.id, ts_rank({document}, q) AS rank "  # noqa: S608 - fixed fragments only
                f"FROM memories m, plainto_tsquery('{TEXT_SEARCH_CONFIGURATION}', :query) q "
                f"WHERE m.story_id = :story AND {active_only_sql('m')} "
                f"AND m.retrieval_tier = '{RetrievalTier.HOT.value}' "
                "AND (CAST(:kinds AS text[]) IS NULL "
                "OR m.kind::text = ANY(CAST(:kinds AS text[]))) "
                f"AND {document} @@ q "
                "ORDER BY rank DESC, m.id LIMIT :k"
            ),
            {"story": story_id, "query": query, "k": k, "kinds": kinds},
        )
        return [(str(r.id), float(r.rank)) for r in found]


BACKENDS: dict[str, Callable[[], LexicalSearch]] = {
    TsvectorLexicalSearch.name: TsvectorLexicalSearch
}
"""The lexical backends ``EPISTREL_LEXICAL_BACKEND`` may name."""


def lexical_backend(name: str) -> LexicalSearch:
    """The backend ``name`` (``KeyError`` for an unknown one; settings validation refuses it
    first)."""
    return BACKENDS[name]()


__all__ = [
    "BACKENDS",
    "TEXT_SEARCH_CONFIGURATION",
    "LexicalFilters",
    "LexicalSearch",
    "TsvectorLexicalSearch",
    "lexical_backend",
]
