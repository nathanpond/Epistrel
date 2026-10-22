"""a retrieval's touch of ``memories.last_accessed_at`` stays a HOT update: the guard fires
after the row is written, and the table leaves room on each page (#452; NFR-PERF-1)

P-04's structured run measured the retrieve stage at 250 to 500 ms per turn, against 150 ms. Most
of it was the touch: ``UPDATE memories SET last_accessed_at`` on the 40 or so memories a
retrieval returns, about 200 ms. Two things made every touched row a full new version, with an
entry in each of the table's indexes, the HNSW graph and three GIN indexes among them:

- ``memories_guard`` was a ``BEFORE`` row trigger. PL/pgSQL takes ``NEW`` into a record, which
  detoasts its values, and returning it wrote a tuple whose ``embedding`` and ``search`` were no
  longer the old bytes. Postgres then counts those indexed columns as changed, so the update is
  never heap-only. The guard now fires ``AFTER UPDATE OR DELETE``: the function, its default-deny
  comparison and its refusals are unchanged (an exception raised after the row is written still
  aborts the statement), and the stored tuple is the one the statement made. A ``BEFORE``
  trigger's ``WHEN`` clause could not do it: it cannot reference a whole ``NEW`` row with a
  generated column (``search``), and naming the immutable columns one by one would make every
  later column mutable by default.
- At the default fillfactor (100) a seeded page has no room for a second version of its row, so
  even an untriggered update leaves the page. ``fillfactor = 70`` keeps about 2.4 kB of an 8 kB
  page free: one more version of a memory row (about 1.7 kB with a 768-dimension vector), which
  page pruning reclaims before the next turn's touch. It applies to pages written from now on;
  an existing store gains it as memories are written, or at once with ``VACUUM FULL memories``.

On P-04's seed, a retrieval's touch went from about 200 ms to about 3 ms (99.8 % of the updates
heap-only) and the retrieve stage's p50 from 281 to 142 ms.

Downgrade restores the ``BEFORE`` trigger and the default fillfactor.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-22

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FILLFACTOR = 70


def _guard(timing: str) -> None:
    op.execute("DROP TRIGGER memories_guard ON memories")
    op.execute(
        f"CREATE TRIGGER memories_guard {timing} UPDATE OR DELETE ON memories "
        "FOR EACH ROW EXECUTE FUNCTION epistrel_memories_guard()"
    )


def upgrade() -> None:
    _guard("AFTER")
    op.execute(f"ALTER TABLE memories SET (fillfactor = {FILLFACTOR})")


def downgrade() -> None:
    op.execute("ALTER TABLE memories RESET (fillfactor)")
    _guard("BEFORE")
