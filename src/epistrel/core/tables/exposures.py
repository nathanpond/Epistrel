"""``exposures``: what a non-salient witness heard, recorded instead of believed (FR-BELIEF-5,6,7,
12,13,14, FR-STORE-31; docs/02 §4.4; #164, migration 0072).

One row is one proposition one witness was exposed to at one beat: the witness
(``witness_character_id``, a modeled character with a partition), the proposition
(``proposition_id``, the claim as said), who said it (``source_character_id``) and the beat's
event (``source_event_id``), how much was caught (``kind`` ``content``; ``fidelity`` ``full`` or
``partial`` for a render-limited eavesdropper) and the **exposure position** (the five
``position_*`` columns of ``core.membership.Position``: ``(turn, beat, step)`` with ``slot_k =
0``, or an inter-turn slot ``(turn, slot_k)`` with ``beat = 0``; CHECK ``slot_shape``).

The witness's belief is resolved lazily, inside a later narrative turn that needs it
(``core.epistemic.exposure.resolve_exposure``), as of the exposure frame. ``state``,
``resolved_by_transition`` and ``resolution_operation_id`` record the latest resolution; the
**effective** state is read through the resolving operation's membership (#154's pattern, as
``thunks`` are): a resolution whose operation left the timeline (truncation, swap) leaves the
exposure unresolved again, and a fork that does not copy the resolving operation copies the
exposure unresolved.

A derived record of the writing operation (#130's registry: truncation, swap, regenerate and fork
move it). Append-only (``epistrel_exposures_append_only``): an UPDATE may change the resolution
columns and the membership pair; a DELETE passes only under an erasure or the story's deletion.
``core.epistemic.exposure`` writes the rows. ``visibility`` is implicit: an exposure is its
witness's alone (never read for anyone else).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Index, Table, text

from epistrel.core.enums import ExposureFidelity, ExposureKind, ExposureState
from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    position_group,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

MUTABLE_EXPOSURE_COLUMNS: tuple[str, ...] = (
    "state",
    "resolved_by_transition",
    "resolution_operation_id",
    "membership",
    "membership_tx",
)
"""The columns an UPDATE of ``exposures`` may change: the latest resolution and the membership
pair."""

SLOT_SHAPE_CHECK = "position_slot_k = 0 OR position_beat = 0"
"""A turn position ``(turn, beat, step)`` or an inter-turn slot ``(turn, slot_k)``, never both."""

exposures = Table(
    "exposures",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("witness_character_id", ulid_type(), nullable=False),
    Column("proposition_id", ulid_type(), nullable=False),
    Column("source_character_id", ulid_type(), nullable=True),
    Column("source_event_id", ulid_type(), nullable=False),
    Column("kind", pg_enum(ExposureKind, "exposure_kind"), nullable=False),
    Column("fidelity", pg_enum(ExposureFidelity, "exposure_fidelity"), nullable=False),
    *position_group("position", nullable=False),
    Column(
        "state",
        pg_enum(ExposureState, "exposure_state"),
        nullable=False,
        server_default=text("'unresolved'"),
    ),
    Column("resolved_by_transition", ulid_type(), nullable=True),
    Column("resolution_operation_id", ulid_type(), nullable=True),
    *derived_columns(),
    CheckConstraint(SLOT_SHAPE_CHECK, name="slot_shape"),
    CheckConstraint(
        "(state = 'resolved') = (resolution_operation_id IS NOT NULL)", name="resolution_pair"
    ),
    *derived_indexes("exposures"),
)
Index(
    "ix_exposures_story_witness",
    exposures.c.story_id,
    exposures.c.witness_character_id,
    exposures.c.state,
)
Index("ix_exposures_story_proposition", exposures.c.story_id, exposures.c.proposition_id)

EXPOSURES = register_derived(DerivedTable(exposures))
"""``exposures`` in ``core.membership.DERIVED_TABLES``: an exposure follows the operation that
wrote it."""

__all__ = [
    "EXPOSURES",
    "MUTABLE_EXPOSURE_COLUMNS",
    "SLOT_SHAPE_CHECK",
    "exposures",
]
