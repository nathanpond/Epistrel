"""perception routing: the topology and channel predicates, per-perceiver memories (#161)

FR-TURN-2,9,10,13, NFR-PRIV-3 (docs/02 §4.4): ``core.epistemic.perception`` routes a beat to
the characters who perceive it, and ``core.memory.records`` writes one memory per perceiver.

- The core predicates ``adjacent_to`` (places that adjoin, with a ``barrier``), ``on_channel``
  (a character on a remote channel), ``spoke_to`` and ``spoke`` (an utterance's observable
  envelope) are inserted with their fixed ids, unless already there (0048 seeds the vocabulary
  as the code has it, so a database created after this release has them already).
- ``memories`` gains ``fidelity`` (``content`` or ``envelope``: how much of the beat the holder
  perceived; ``NULL`` for every memory routing did not write) and ``channel`` (how it reached
  the holder: ``self``, ``room``, ``eavesdrop``, ``remote``, ``adjacent`` or ``think``).
- ``characters`` gains ``languages`` (the language codes the character understands; ``NULL``:
  every language).
- ``placement`` gains ``since_beat``: the beat of the turn its fact was committed at, so a move
  in beat 2 takes effect from beat 3.

Downgrade drops the columns; the predicate rows stay (a predicate is never deleted, 0048's
guard), as an older release's re-sync leaves rows it does not know alone.

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.propositions.codec import predicate_row
from epistrel.core.propositions.vocabulary import (
    ADJACENT_TO,
    CORE_BY_ID,
    ON_CHANNEL,
    SPOKE,
    SPOKE_TO,
)

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_PREDICATES = (ADJACENT_TO, ON_CHANNEL, SPOKE_TO, SPOKE)
_JSON = frozenset({"qualifiers", "exclusions", "implications"})
FIDELITIES = ("content", "envelope")
CHANNELS = ("self", "room", "eavesdrop", "remote", "adjacent", "think")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    rows = [predicate_row(CORE_BY_ID[i]) for i in NEW_PREDICATES]
    table = sa.table(
        "predicates",
        *(
            sa.column(name, postgresql.JSONB(none_as_null=True) if name in _JSON else None)
            for name in rows[0]
        ),
    )
    op.execute(postgresql.insert(table).values(rows).on_conflict_do_nothing(index_elements=["id"]))

    op.add_column("memories", sa.Column("fidelity", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("channel", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_memories_fidelity",
        "memories",
        f"fidelity IS NULL OR fidelity IN ({_quoted(FIDELITIES)})",
    )
    op.create_check_constraint(
        "ck_memories_channel", "memories", f"channel IS NULL OR channel IN ({_quoted(CHANNELS)})"
    )
    op.add_column("characters", sa.Column("languages", postgresql.ARRAY(sa.Text()), nullable=True))
    op.add_column("placement", sa.Column("since_beat", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("placement", "since_beat")
    op.drop_column("characters", "languages")
    op.drop_constraint("ck_memories_channel", "memories", type_="check")
    op.drop_constraint("ck_memories_fidelity", "memories", type_="check")
    op.drop_column("memories", "channel")
    op.drop_column("memories", "fidelity")
