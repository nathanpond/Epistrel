"""``divergences``: what a lying speaker asserted against what it believes (FR-TURN-7,
FR-VERIFY-10; docs/02 §4.9; #166, migration 0073).

One row per claim of an utterance the verifier classified as a **lie**
(``core.verify.speech_acts``): the utterance (``utterance_id``, a soft reference to
``utterances``), its speaker, the proposition asserted (``asserted_proposition_id``, the
utterance's stored proposition) and the contrary belief it deviates from
(``believed_belief_id``, a deferred foreign key to ``beliefs``, and its proposition
``believed_proposition_id``; both ``NULL`` for a lie about something nobody's record settles),
at the say beat's position (the five ``position_*`` columns). ``revealed_by_event_id`` is set
when a later reveal settles the deception (#169; X-02's "divergences resolve").

A derived record of the turn's operation (#130's registry: truncation, swap, regenerate and fork
move it), written by ``core.epistemic.divergences``. Append-only
(``epistrel_divergences_append_only``): an UPDATE may change ``revealed_by_event_id`` and the
membership pair; a DELETE passes only under an erasure or the story's deletion. Ids only, no
text: the speaker and both propositions' subject and object characters are declared erasure
subjects.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, Column, ForeignKey, Index, Table, text

from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    position_group,
    register_derived,
)
from epistrel.core.tables.base import metadata, ulid_type

MUTABLE_DIVERGENCE_COLUMNS: tuple[str, ...] = (
    "revealed_by_event_id",
    "membership",
    "membership_tx",
)
"""The columns an UPDATE of ``divergences`` may change: the reveal and the membership pair."""

divergences = Table(
    "divergences",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("utterance_id", ulid_type(), nullable=False),
    Column("speaker_character_id", ulid_type(), nullable=False),
    Column("asserted_proposition_id", ulid_type(), nullable=False),
    Column("believed_proposition_id", ulid_type(), nullable=True),
    Column(
        "believed_belief_id",
        ulid_type(),
        ForeignKey("beliefs.id", deferrable=True, initially="DEFERRED"),
        nullable=True,
    ),
    *position_group("position", nullable=False),
    Column("revealed_by_event_id", ulid_type(), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    *derived_columns(),
    *derived_indexes("divergences"),
)
Index("ix_divergences_story_speaker", divergences.c.story_id, divergences.c.speaker_character_id)
Index("ix_divergences_story_utterance", divergences.c.story_id, divergences.c.utterance_id)

DIVERGENCES = register_derived(DerivedTable(divergences))
"""``divergences`` in ``core.membership.DERIVED_TABLES``: a divergence follows its turn."""

__all__ = ["DIVERGENCES", "MUTABLE_DIVERGENCE_COLUMNS", "divergences"]
