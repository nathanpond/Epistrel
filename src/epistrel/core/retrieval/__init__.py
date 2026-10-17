"""Memory retrieval (FR-RET, FR-EMBED-3; #138, #141): the swappable lexical backend
(``lexical``: the ``LexicalSearch`` protocol and its ``tsvector`` implementation) and the
search that never mixes vector spaces (``search``). #141 adds the hybrid ranking, the token
budget and the alias channel over these.
"""

from epistrel.core.retrieval.lexical import (
    BACKENDS,
    LexicalFilters,
    LexicalSearch,
    TsvectorLexicalSearch,
    lexical_backend,
)
from epistrel.core.retrieval.search import (
    EMBEDDING_PENDING,
    EMBEDDING_UNAVAILABLE,
    Embedder,
    Hit,
    RetrievalResult,
    search,
    vector_search,
)

__all__ = [
    "BACKENDS",
    "EMBEDDING_PENDING",
    "EMBEDDING_UNAVAILABLE",
    "Embedder",
    "Hit",
    "LexicalFilters",
    "LexicalSearch",
    "RetrievalResult",
    "TsvectorLexicalSearch",
    "lexical_backend",
    "search",
    "vector_search",
]
