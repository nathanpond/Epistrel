"""``beliefs`` and ``belief_state``: per-character belief partitions and their projection
(FR-EPI-1,3,5,6, FR-STORE-3; docs/02 §4.4, §4.5; #159, migration 0068).

**``beliefs``** — one row is one belief record of one holder (a modeled character): a
proposition (``propositions``, #131: viewpoint-free, FR-PROP-2) wrapped with

- **who holds it and how**: ``holder_character_id``; ``kind`` (``belief_kind``:
  ``first_order`` here, the other kinds #167's); ``polarity``, a denormalised copy of the
  proposition's for indexing (a disbelief is the negated proposition); ``confidence_level``
  (``belief_confidence_level``) and ``confidence_scalar`` (``0..1`` or ``NULL``; FR-BELIEF-4's
  dual confidence, #160's projection);
- **the times**: ``valid_from``/``valid_to``, the inner proposition's story-time in
  story-minutes (``NULL`` open), defaulting from the proposition's declared bounds as facts do,
  with ``valid_span`` their ``int8range`` (written by the application, checked equal,
  GiST-indexed);
  ``held_from_*``/``held_to_*``, the narrative positions (``core.membership.Position``'s five
  columns) between which the holder holds it (``held_to`` all ``NULL`` while held); and
  ``tx_from``/``tx_to``, the transaction-time of the record (#160's corrections close it);
- **where it came from**: ``provenance`` (the ``belief_transitions`` row that produced it: NOT NULL,
  a deferred foreign key and a constraint trigger, ADR-168), ``source_event_ids`` (the causing beat
  event, or the record's own ``belief_committed`` event), the derived-record columns (#130) with
  ``beat`` and ``step``;
- **who may read it**: ``visibility``, always ``{"scope": "characters", "characters":
  [holder]}`` (CHECK ``holder_only``): a belief is private by construction.

**How it is held now** (#160): ``accessibility`` (``1.0``; forgetting lowers it), ``state``
(``held``, ``decayed``, ``corrected``), ``corrected_by`` (the belief that corrected it) and
``lineage_id`` (the first belief of its correction chain).

**Lazy resolution** (#164): a belief resolved from an exposure holds from the exposure position
(``held_from_*``) but is written by the narrative turn that needed it (``operation_id``, its
``narrative_turn``), which ``resolution_operation_id`` repeats: #130's
``DerivedTable.inclusion_operation_column``, so a view through a turn before the resolving one
excludes it (FR-BELIEF-13,14).

**``belief_transitions``** (#160) — one row per belief-set change, the typed cause of a belief:
see ``core.epistemic.transitions``. A derived record of its operation, append-only (the
membership pair may change, and an erasure clears ``source_character_id``).

One open row per ``(story_id, holder, proposition)`` among the active first-order rows
(``ux_beliefs_open``). Append-only (``epistrel_beliefs_append_only``): an UPDATE may set
``tx_to`` and the ``held_to_*`` group once (``NULL`` to a value; #160's follower may reopen a
corrected belief), change ``membership``/``membership_tx`` and the in-place state; a DELETE
passes only under an erasure or the story's deletion. ``core.epistemic.partitions`` writes the
rows, called only by ``core.epistemic.transitions``.

**``belief_state``** — the queryable belief projection (FR-EPI-6): one row per holder and
proposition the holder currently holds at ``suspected`` or above (``unknown`` and closed beliefs
have none), with the current level and scalar, the polarity, the belief row it rests on and the
position since which it holds (``since_*``, the belief's ``held_from``). A rebuildable cache
(``core.projections.beliefs``): re-derived per write in the write's transaction, after every
membership flip, and folded by a rebuild, an erasure and a fork. First-order beliefs only (#167
adds ``nested_belief_state``).
"""

from __future__ import annotations

from sqlalchemy import (
    NUMERIC,
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INT8RANGE, JSONB

from epistrel.core.enums import (
    AntecedentKind,
    BeliefConfidenceLevel,
    BeliefHoldState,
    BeliefKind,
    BeliefTransitionKind,
)
from epistrel.core.membership import (
    DerivedTable,
    active_only_predicate,
    derived_columns,
    derived_indexes,
    position_group,
    position_group_check,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type
from epistrel.core.visibility import VISIBILITY_CHARACTERS_CHECK, VISIBILITY_SCOPE_CHECK

POLARITIES: tuple[str, ...] = ("affirmed", "negated")

MUTABLE_BELIEF_COLUMNS: tuple[str, ...] = (
    "tx_to",
    "held_to_turn",
    "held_to_variant",
    "held_to_slot_k",
    "held_to_beat",
    "held_to_step",
    "membership",
    "membership_tx",
    "confidence_level",
    "confidence_scalar",
    "accessibility",
    "state",
    "corrected_by",
)
"""The columns an UPDATE of ``beliefs`` may change: ``tx_to`` and the ``held_to`` group once
(reopened only by #160's membership follower), the membership pair, and the in-place state a
revision or a forgetting changes (#160)."""

HOLDER_ONLY_CHECK = (
    "visibility = jsonb_build_object('scope', 'characters', 'characters', "
    "jsonb_build_array(holder_character_id::text))"
)
"""A belief's visibility names its holder and no one else."""

OPEN_WHERE = (
    f"tx_to IS NULL AND held_to_turn IS NULL AND kind = 'first_order' AND {active_only_predicate()}"
)
"""The rows ``ux_beliefs_open`` keeps unique: the active, open first-order beliefs."""

beliefs = Table(
    "beliefs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("holder_character_id", ulid_type(), nullable=False),
    Column(
        "kind",
        pg_enum(BeliefKind, "belief_kind"),
        nullable=False,
        server_default=text("'first_order'"),
    ),
    Column("proposition_id", ulid_type(), nullable=False),
    Column("polarity", Text, nullable=False),
    Column(
        "confidence_level",
        pg_enum(BeliefConfidenceLevel, "belief_confidence_level"),
        nullable=False,
    ),
    Column("confidence_scalar", NUMERIC(4, 3), nullable=True),
    Column("valid_from", BigInteger, nullable=True),
    Column("valid_to", BigInteger, nullable=True),
    Column("valid_span", INT8RANGE, nullable=False),
    *position_group("held_from", nullable=False),
    *position_group("held_to", nullable=True),
    Column("tx_from", TIMESTAMP(timezone=True), nullable=False),
    Column("tx_to", TIMESTAMP(timezone=True), nullable=True),
    Column(
        "provenance",
        ulid_type(),
        ForeignKey("belief_transitions.id", deferrable=True, initially="DEFERRED", use_alter=True),
        nullable=False,
    ),
    Column("visibility", JSONB, nullable=False),
    *derived_columns(),
    Column("beat", SmallInteger, nullable=True),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    Column("accessibility", NUMERIC(4, 3), nullable=False, server_default=text("1.0")),
    Column(
        "state",
        pg_enum(BeliefHoldState, "belief_hold_state"),
        nullable=False,
        server_default=text("'held'"),
    ),
    Column("corrected_by", ulid_type(), nullable=True),
    Column("lineage_id", ulid_type(), nullable=False),
    Column("resolution_operation_id", ulid_type(), nullable=True),
    CheckConstraint(
        "polarity IN (" + ", ".join(f"'{p}'" for p in POLARITIES) + ")", name="polarity"
    ),
    CheckConstraint(
        "confidence_scalar IS NULL OR (confidence_scalar >= 0 AND confidence_scalar <= 1)",
        name="confidence_scalar",
    ),
    CheckConstraint(
        "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to", name="valid_order"
    ),
    CheckConstraint("valid_span = int8range(valid_from, valid_to, '[)')", name="valid_span"),
    CheckConstraint("tx_to IS NULL OR tx_from < tx_to", name="tx_order"),
    CheckConstraint(position_group_check("held_to"), name="held_to_whole"),
    CheckConstraint(
        "held_to_turn IS NULL OR (held_from_turn, held_from_slot_k, held_from_beat, "
        "held_from_step) < (held_to_turn, held_to_slot_k, held_to_beat, held_to_step)",
        name="held_order",
    ),
    CheckConstraint(VISIBILITY_SCOPE_CHECK, name="visibility_scope"),
    CheckConstraint(VISIBILITY_CHARACTERS_CHECK, name="visibility_characters"),
    CheckConstraint(HOLDER_ONLY_CHECK, name="holder_only"),
    CheckConstraint("accessibility >= 0 AND accessibility <= 1", name="accessibility"),
    CheckConstraint("(state = 'corrected') = (corrected_by IS NOT NULL)", name="corrected_pair"),
    *derived_indexes("beliefs"),
)
Index(
    "ix_beliefs_story_holder_proposition",
    beliefs.c.story_id,
    beliefs.c.holder_character_id,
    beliefs.c.proposition_id,
)
Index("ix_beliefs_story_proposition", beliefs.c.story_id, beliefs.c.proposition_id)
Index("ix_beliefs_valid_span", beliefs.c.valid_span, postgresql_using="gist")
Index("ix_beliefs_visibility", beliefs.c.visibility, postgresql_using="gin")
Index("ux_beliefs_provenance", beliefs.c.provenance, unique=True)
Index("ix_beliefs_story_lineage", beliefs.c.story_id, beliefs.c.lineage_id)
Index(
    "ux_beliefs_open",
    beliefs.c.story_id,
    beliefs.c.holder_character_id,
    beliefs.c.proposition_id,
    unique=True,
    postgresql_where=text(OPEN_WHERE),
)

BELIEFS = register_derived(
    DerivedTable(
        beliefs,
        valid_time=("valid_from", "valid_to"),
        tx_time=("tx_from", "tx_to"),
        inclusion_operation_column="resolution_operation_id",
    )
)
"""``beliefs`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation, swap, regenerate
and fork move its rows with their operations (ADR-167: the derived table; ``belief_state`` is the
rebuildable projection over it)."""

TRANSITION_SOURCE_KINDS: tuple[str, ...] = ("testimony", "observation", "discovery")
"""The kinds whose transition may name a ``source_character_id`` (testimony needs one)."""

OUTCOMES: tuple[str, ...] = ("accept", "reject", "suspend", "speech_act_only")
"""A testimony transition's ``outcome`` (``NULL`` for every other kind)."""

MUTABLE_TRANSITION_COLUMNS: tuple[str, ...] = ("membership", "membership_tx")
"""The columns an UPDATE of ``belief_transitions`` may change (an erasure also clears
``source_character_id``)."""

belief_transitions = Table(
    "belief_transitions",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("holder_character_id", ulid_type(), nullable=False),
    Column(
        "belief_id",
        ulid_type(),
        ForeignKey("beliefs.id", deferrable=True, initially="DEFERRED"),
        nullable=False,
    ),
    Column("kind", pg_enum(BeliefTransitionKind, "belief_transition_kind"), nullable=False),
    Column("antecedent_kind", pg_enum(AntecedentKind, "antecedent_kind"), nullable=False),
    Column("antecedent_ref", JSONB, nullable=False),
    Column("source_character_id", ulid_type(), nullable=True),
    Column("valid_at", BigInteger, nullable=True),
    Column(
        "confidence_level",
        pg_enum(BeliefConfidenceLevel, "belief_confidence_level"),
        nullable=False,
    ),
    Column("confidence_scalar", NUMERIC(4, 3), nullable=True),
    Column("accessibility", NUMERIC(4, 3), nullable=False, server_default=text("1.0")),
    Column(
        "state",
        pg_enum(BeliefHoldState, "belief_hold_state"),
        nullable=False,
        server_default=text("'held'"),
    ),
    Column("outcome", Text, nullable=True),
    Column("prior_belief_ids", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("prior", JSONB(none_as_null=True), nullable=True),
    *position_group("position", nullable=False),
    Column("tx_from", TIMESTAMP(timezone=True), nullable=False),
    Column("tx_to", TIMESTAMP(timezone=True), nullable=True),
    *derived_columns(),
    CheckConstraint(
        "source_character_id IS NULL OR kind IN ("
        + ", ".join(f"'{k}'" for k in TRANSITION_SOURCE_KINDS)
        + ")",
        name="source_kind",
    ),
    CheckConstraint(
        "outcome IS NULL OR (kind = 'testimony' AND outcome IN ("
        + ", ".join(f"'{o}'" for o in OUTCOMES)
        + "))",
        name="outcome",
    ),
    CheckConstraint(
        "confidence_scalar IS NULL OR (confidence_scalar >= 0 AND confidence_scalar <= 1)",
        name="confidence_scalar",
    ),
    CheckConstraint("accessibility >= 0 AND accessibility <= 1", name="accessibility"),
    CheckConstraint("jsonb_typeof(antecedent_ref) = 'object'", name="antecedent_ref"),
    CheckConstraint("tx_to IS NULL", name="tx_open"),
    *derived_indexes("belief_transitions"),
)
Index(
    "ix_belief_transitions_story_holder_belief",
    belief_transitions.c.story_id,
    belief_transitions.c.holder_character_id,
    belief_transitions.c.belief_id,
)
Index("ix_belief_transitions_story_kind", belief_transitions.c.story_id, belief_transitions.c.kind)
Index(
    "ix_belief_transitions_prior",
    belief_transitions.c.prior_belief_ids,
    postgresql_using="gin",
)

BELIEF_TRANSITIONS = register_derived(
    DerivedTable(
        belief_transitions,
        valid_time=("valid_at", "valid_at"),
        tx_time=("tx_from", "tx_to"),
    )
)
"""``belief_transitions`` in ``core.membership.DERIVED_TABLES`` (#160): a transition follows its
operation, and ``core.epistemic.transitions``' follower re-settles the beliefs it touched."""

belief_state = Table(
    "belief_state",
    metadata,
    Column("story_id", ulid_type(), nullable=False),
    Column("holder_character_id", ulid_type(), nullable=False),
    Column("proposition_id", ulid_type(), nullable=False),
    Column("belief_id", ulid_type(), nullable=False),
    Column("polarity", Text, nullable=False),
    Column(
        "current_level",
        pg_enum(BeliefConfidenceLevel, "belief_confidence_level"),
        nullable=False,
    ),
    Column("current_scalar", NUMERIC(4, 3), nullable=True),
    *position_group("since", nullable=False),
    PrimaryKeyConstraint("story_id", "holder_character_id", "proposition_id"),
    CheckConstraint("current_level <> 'unknown'", name="held_level"),
)
Index("ix_belief_state_story_proposition", belief_state.c.story_id, belief_state.c.proposition_id)

__all__ = [
    "BELIEFS",
    "BELIEF_TRANSITIONS",
    "HOLDER_ONLY_CHECK",
    "MUTABLE_BELIEF_COLUMNS",
    "MUTABLE_TRANSITION_COLUMNS",
    "OPEN_WHERE",
    "OUTCOMES",
    "POLARITIES",
    "TRANSITION_SOURCE_KINDS",
    "belief_state",
    "belief_transitions",
    "beliefs",
]
