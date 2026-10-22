"""``utterances``: every ``say`` of a structured turn as a speech act (FR-TURN-5,6; #150,
migration 0063).

One row per ``say`` beat (a character's, or the player's own ``say``), written in the turn's
commit transaction by ``core.turns.beats.write_utterances``. Being said never makes a claim
true (FR-TURN-5): the row records what the speaker asserted and three **independent** labels
(FR-TURN-6):

- ``objective_relation`` ∈ ``true|false|unresolved``: the verdict of the utterance's
  propositions against canon (#142): all supported → ``true``, any contradicted → ``false``,
  otherwise (undecided, novel, none at all, verification off) ``unresolved``;
- ``belief_relation`` ∈ ``consistent|inconsistent|unknown``: ``unknown`` in M4 (there are no
  belief partitions before M5, which may update it);
- ``intent`` ∈ ``sincere|deceptive|joking|unspecified``: as the render declared it on the
  ``say`` beat (``unspecified`` when nothing was declared, as for the player's input).

``event_id`` is the ``beat_say`` (or ``player_input``) event, a soft reference: events are
never deleted while their story lives. ``proposition_ids`` are the utterance's normalized
propositions (``propositions``). The derived-record columns of ``core.membership`` make a row
follow its turn's operation (#130). Ids and labels only, no text: the subject index exempts
the table (``core.erasure.index.EXEMPT_TABLES``); the beat events it points at are indexed and
erased. Append-only by trigger (``belief_relation`` and the membership columns may change).
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY

from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, ulid_type

OBJECTIVE_RELATIONS: tuple[str, ...] = ("true", "false", "unresolved")
BELIEF_RELATIONS: tuple[str, ...] = ("consistent", "inconsistent", "unknown")
INTENTS: tuple[str, ...] = ("sincere", "deceptive", "joking", "unspecified")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


utterances = Table(
    "utterances",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    *derived_columns(),
    Column("event_id", ulid_type(), nullable=False),
    Column("speaker_character_id", ulid_type(), nullable=False),
    Column("proposition_ids", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("objective_relation", Text, nullable=False),
    Column("belief_relation", Text, nullable=False, server_default=text("'unknown'")),
    Column("intent", Text, nullable=False, server_default=text("'unspecified'")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(_in("objective_relation", OBJECTIVE_RELATIONS), name="objective_relation"),
    CheckConstraint(_in("belief_relation", BELIEF_RELATIONS), name="belief_relation"),
    CheckConstraint(_in("intent", INTENTS), name="intent"),
    *derived_indexes("utterances"),
)
Index("ix_utterances_story_event", utterances.c.story_id, utterances.c.event_id)
Index("ix_utterances_story_speaker", utterances.c.story_id, utterances.c.speaker_character_id)

UTTERANCES = register_derived(DerivedTable(utterances))
"""``utterances`` in ``core.membership.DERIVED_TABLES``: an utterance follows its turn."""

__all__ = ["BELIEF_RELATIONS", "INTENTS", "OBJECTIVE_RELATIONS", "UTTERANCES", "utterances"]
