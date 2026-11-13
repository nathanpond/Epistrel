"""The PostgreSQL server settings the engine ships, and the test servers that run with them
(#784 option (a), ADR-191).

``memories`` has a partition per story (ADR-187). A few operator actions change every story's
partition in one transaction: the first embedding pin and a dimension change, an embedding
model change, a migration that alters ``memories``, and ``pg_dump``. Their lock count grows with
the number of stories, so the server's lock table is sized by ADR-188's rule:
``max_locks_per_transaction ≥ 16 * stories / (max_connections + max_prepared_transactions)``,
rounded up to a power of two and never below 64. Postgres keeps per-relation statistics in
dynamic shared memory too, so the container's ``/dev/shm`` needs 64 MB plus 8 MB per 1,000
stories.

``docker-compose.yml``'s ``db`` service runs with ``MAX_LOCKS_PER_TRANSACTION`` and
``SHM_SIZE_MB``. On Postgres's 100 connections and no prepared transactions, they cover
``COVERED_STORIES`` stories. Every test server (``container``) runs with the same settings, so
the tests exercise the shipped configuration. ``tests/test_compose.py`` fails when compose
drifts from this module or below the rule.

Nothing in ``create_app`` or the Engine imports this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PG_IMAGE = "pgvector/pgvector:pg16"
MAX_LOCKS_PER_TRANSACTION = 1024
"""The shipped ``max_locks_per_transaction`` (Postgres's default is 64)."""
SHM_SIZE_MB = 128
"""The shipped ``/dev/shm`` of the database container, in MB (Docker's default is 64)."""
COVERED_STORIES = 6400
"""The stories the shipped settings cover on ``DEFAULT_MAX_CONNECTIONS``: 16 * 6,400 / 100 =
1,024, and 64 MB + 8 MB * 6.4 = 115.2 MB ≤ 128 MB."""
DEFAULT_MAX_CONNECTIONS = 100
"""Postgres's default ``max_connections``; ``max_prepared_transactions`` defaults to 0."""
LOCKS_PER_STORY = 16
"""ADR-188's lock slots per story: a partition and its 14 indexes, rounded up."""
MIN_LOCKS = 64
SHM_BASE_MB = 64
SHM_MB_PER_1000_STORIES = 8


def required_locks(stories: int, connections: int = DEFAULT_MAX_CONNECTIONS) -> int:
    """ADR-188's ``max_locks_per_transaction`` for ``stories`` on ``connections`` (that is,
    ``max_connections + max_prepared_transactions``)."""
    needed = -(-LOCKS_PER_STORY * stories // connections)
    power = MIN_LOCKS
    while power < needed:
        power *= 2
    return power


def required_shm_mb(stories: int) -> float:
    """docs/OPERATIONS.md's ``/dev/shm`` for ``stories``, in MB."""
    return SHM_BASE_MB + SHM_MB_PER_1000_STORIES * stories / 1000


def server_args(settings: Mapping[str, object] | None = None) -> list[str]:
    """``postgres``'s ``-c`` arguments: the shipped lock table, then ``settings`` (which may
    override it)."""
    merged: dict[str, object] = {"max_locks_per_transaction": MAX_LOCKS_PER_TRANSACTION}
    merged.update(settings or {})
    return [arg for name, value in merged.items() for arg in ("-c", f"{name}={value}")]


def container(
    image: str = PG_IMAGE,
    *,
    settings: Mapping[str, object] | None = None,
    command: str | list[str] | None = None,
) -> Any:
    """A testcontainers ``PostgresContainer`` (not started) with the shipped settings:
    ``postgres`` run with ``server_args(settings)`` unless ``command`` replaces it (a caller
    that wraps the server puts ``server_args()`` in its own command), and ``SHM_SIZE_MB`` of
    ``/dev/shm``."""
    from testcontainers.community.postgres import PostgresContainer  # lazy: dev only

    pg = PostgresContainer(image, driver="psycopg", shm_size=f"{SHM_SIZE_MB}m")
    return pg.with_command(command if command is not None else ["postgres", *server_args(settings)])
