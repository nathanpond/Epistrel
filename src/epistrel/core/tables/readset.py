"""``proposition_changes``: the change index the structured read set is checked against
(FR-CONC-11,12; docs/02 §4.4; #136, migration 0051).

One row per proposition a committed write added or closed: a fact version written (``add``, or
``supersede`` when it closed another), a version closed (``close``, a supersede writes it with
its ``supersede``), a remainder kept (``add``). Canon writes (#133) record it in the same
transaction through ``core.readset.record_fact_change`` (a ``FACT_HOOKS`` hook); #144's belief
and #150's utterance writes call ``core.readset.record_change`` the same way.

- ``timeline_revision`` is the story's revision **after** the writing operation committed
  (``core.readset.COMMIT_REVISION_SETTING``): work that does not bump the revision is stamped
  with the current one, so it is never "since" a base it did not move.
- ``subject_id``, ``predicate_id``, ``object_id`` (an entity object), ``literal_hash`` (a
  literal object's canonical hash), ``object_set`` (a set object's sorted members), ``holder``
  (``canon`` | ``utterance`` | ``belief``, with ``speaker_id`` the speaker of an utterance or
  the holder of a belief, #159), ``visibility`` (the fact's
  ``{"scope", "characters"}``) and ``valid_window`` (the outer valid-time bounds, ``[)``) are
  what a query key matches on.
- ``operation_id`` and ``tx_time`` say who wrote it and when (the retention sweep prunes by
  ``tx_time`` and moves ``stories.changes_pruned_through_revision``).

Identifiers, hashes and ranges only: no story text (``core.erasure.index.EXEMPT_TABLES``). A
story's rows go with the story; a fork does not copy them (its turns start from its own base).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INT8RANGE, JSONB

from epistrel.core.tables.base import metadata, ulid_type

CHANGE_KINDS: tuple[str, ...] = ("add", "supersede", "close")
HOLDERS: tuple[str, ...] = ("canon", "utterance", "belief")
"""Whose proposition changed: canon, a speaker's utterance, or a holder's belief (#159)."""
SPOKEN_HOLDERS: tuple[str, ...] = ("utterance", "belief")
"""The holders a change names a character for (``speaker_id``: the speaker, or the belief's
holder)."""

proposition_changes = Table(
    "proposition_changes",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("timeline_revision", BigInteger, nullable=False),
    Column("proposition_id", ulid_type(), nullable=True),
    Column("subject_id", ulid_type(), nullable=False),
    Column("predicate_id", ulid_type(), nullable=False),
    Column("object_id", ulid_type(), nullable=True),
    Column("literal_hash", Text, nullable=True),
    Column("object_set", ARRAY(ulid_type()), nullable=True),
    Column("holder", Text, nullable=False, server_default=text("'canon'")),
    Column("speaker_id", ulid_type(), nullable=True),
    Column(
        "visibility",
        JSONB,
        nullable=False,
        server_default=text("""'{"scope": "objective"}'::jsonb"""),
    ),
    Column("valid_window", INT8RANGE, nullable=False),
    Column("change", Text, nullable=False),
    Column("operation_id", ulid_type(), ForeignKey("operations.operation_id"), nullable=False),
    Column(
        "tx_time",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    CheckConstraint(
        "change IN (" + ", ".join(f"'{c}'" for c in CHANGE_KINDS) + ")", name="change_kind"
    ),
    CheckConstraint("holder IN (" + ", ".join(f"'{h}'" for h in HOLDERS) + ")", name="holder"),
    CheckConstraint(
        "(holder IN ("
        + ", ".join(f"'{h}'" for h in SPOKEN_HOLDERS)
        + ")) = (speaker_id IS NOT NULL)",
        name="speaker",
    ),
)
Index(
    "ix_proposition_changes_story_revision",
    proposition_changes.c.story_id,
    proposition_changes.c.timeline_revision,
)
Index(
    "ix_proposition_changes_story_subject_predicate",
    proposition_changes.c.story_id,
    proposition_changes.c.subject_id,
    proposition_changes.c.predicate_id,
)
Index("ix_proposition_changes_tx_time", proposition_changes.c.tx_time)

__all__ = ["CHANGE_KINDS", "HOLDERS", "SPOKEN_HOLDERS", "proposition_changes"]
