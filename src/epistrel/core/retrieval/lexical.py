"""The lexical search backend (NFR-PORT-2, FR-RET; #138), swappable like the embedding adapter.

``LexicalSearch.search(conn, story_id, query, k, filters)`` answers up to ``k`` of the story's
memories that match ``query`` as ``(memory_id, rank)``, best first, over **active** memories
only (``core.membership.active_only``) and, unless the filters name the ``retired`` tier, only
**hot** ones (a retired memory is searched only by #141's rehydration, FR-MEM-8). It is the
hybrid retrieval's lexical arm (``core.retrieval.pipeline``) and the channel a search falls back
to while the story's vectors are being re-embedded or the embedding endpoint is down
(``core.retrieval.search``).

One backend ships: ``tsvector``, PostgreSQL full-text search under the ``english`` text-search
configuration over the generated, GIN-indexed ``memories.search`` column (#141, migration
0056). A query matches a memory holding **any** of its words (``plainto_tsquery``'s lexemes
joined by ``|``: a conversational question never has all its words in one memory), ranked by
``ts_rank`` (a memory holding more of them ranks higher), ties by id.
``EPISTREL_LEXICAL_BACKEND`` names the backend (``BACKENDS``); a test-only in-memory backend
proves the swap (C-07 M4).

``LexicalFilters`` narrows a search by memory kind, by narrative position (``through``: no
memory positioned after it, #141's "nothing after the query's position") and by retrieval tier
(``tiers``: hot by default; #141's rehydration searches ``retired`` records, FR-MEM-8). A
quarantined memory (#145, FR-GUARD-4) is never found unless ``include_quarantined`` (the
omniscient debugging read's flag).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import MemoryKind, RetrievalTier
from epistrel.core.membership import active_only_sql
from epistrel.core.tables.memories import SEARCH_CONFIGURATION

TEXT_SEARCH_CONFIGURATION = SEARCH_CONFIGURATION


@dataclass(frozen=True)
class LexicalFilters:
    """Narrowing a lexical search: only memories of these ``kinds`` (all when ``None``), at or
    before the narrative position ``through`` (``(turn, beat)``; position-less memories always
    qualify; no bound when ``None``), in these retrieval ``tiers``."""

    kinds: tuple[MemoryKind, ...] | None = None
    through: tuple[int, int] | None = None
    tiers: tuple[RetrievalTier, ...] = (RetrievalTier.HOT,)
    include_quarantined: bool = False


def quarantine_clause(alias: str) -> str:
    """The SQL fragment "not quarantined, unless ``:include_quarantined``" over memories
    aliased ``alias`` (#145)."""
    return f"(CAST(:include_quarantined AS boolean) OR {alias}.quarantined_at IS NULL)"


def position_clause(alias: str) -> str:
    """The SQL fragment "at or before ``(:through_turn, :through_beat)``" over memories aliased
    ``alias`` (no bound while ``:through_turn`` is ``NULL``)."""
    a = alias
    return (
        f"(CAST(:through_turn AS integer) IS NULL OR {a}.narrative_turn IS NULL "
        f"OR {a}.narrative_turn < CAST(:through_turn AS integer) "
        f"OR ({a}.narrative_turn = CAST(:through_turn AS integer) "
        f"AND ({a}.beat IS NULL OR {a}.beat <= CAST(:through_beat AS integer))))"
    )


def position_params(through: tuple[int, int] | None) -> dict[str, int | None]:
    return {
        "through_turn": None if through is None else int(through[0]),
        "through_beat": None if through is None else int(through[1]),
    }


ANY_WORD_QUERY = (
    f"CAST(replace(CAST(plainto_tsquery('{SEARCH_CONFIGURATION}', :query) AS text), "
    "' & ', ' | ') AS tsquery)"
)
"""A tsquery matching a document that holds any of ``:query``'s lexemes."""


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
        filters = filters or LexicalFilters()
        kinds = None if filters.kinds is None else [kind.value for kind in filters.kinds]
        tiers = [tier.value for tier in filters.tiers]
        if not tiers:
            return []
        found = await conn.execute(
            text(
                "SELECT m.id, ts_rank(m.search, q) AS rank "  # noqa: S608 - fixed fragments only
                f"FROM memories m, (SELECT {ANY_WORD_QUERY} AS q) words "
                f"WHERE m.story_id = :story AND {active_only_sql('m')} "
                # The columns are compared as their enums, not cast to text, so the tier and
                # kind filters can use the story's b-tree indexes (#451: a search of the
                # retired tier reads the retired rows, not every row matching a word).
                "AND m.retrieval_tier = ANY(CAST(:tiers AS retrieval_tier[])) "
                "AND (CAST(:kinds AS memory_kind[]) IS NULL "
                "OR m.kind = ANY(CAST(:kinds AS memory_kind[]))) "
                f"AND {position_clause('m')} "
                f"AND {quarantine_clause('m')} "
                "AND m.search @@ q "
                # Ties by position, newest first, then text, then id (#154, #476: ids are
                # random within a millisecond, so an id tie-break is not deterministic across
                # runs).
                "ORDER BY rank DESC, m.narrative_turn DESC NULLS LAST, m.beat DESC NULLS LAST, "
                "m.text, m.id LIMIT :k"
            ),
            {
                "story": story_id,
                "query": query,
                "k": k,
                "kinds": kinds,
                "tiers": tiers,
                "include_quarantined": filters.include_quarantined,
                **position_params(filters.through),
            },
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
    "ANY_WORD_QUERY",
    "BACKENDS",
    "TEXT_SEARCH_CONFIGURATION",
    "LexicalFilters",
    "LexicalSearch",
    "TsvectorLexicalSearch",
    "lexical_backend",
    "position_clause",
    "position_params",
    "quarantine_clause",
]
