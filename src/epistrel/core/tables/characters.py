"""``characters``: the cast a story was created with (FR-ADMIN-1, FR-CAT-12).

A primary table, not a projection: the ``story_created`` event names each character (id, name,
``is_player``) but never its age, so the true age lives only here and no corrected view can
leak it. ``story_id`` is plain and NOT NULL (no foreign key; story deletion enumerates it), and
``ordinal`` fixes the cast order. Names are unique per story, ignoring case.

``definition`` is free text the Lite pipeline renders verbatim; structured catalog characters
arrive in M3. ``safe_fallback_text`` is the character's line when every regenerated reply fails
its guardrails (#68). ``source_catalog_id``/``source_catalog_version`` name the catalog entry and
version a character was created from (FR-CAT-3,9; #103; written by #119's genesis): provenance by
identifiers only, with no foreign key, so archiving, editing or deleting the entry never touches
the story.

#114 adds four columns:

- ``persona_id`` and ``persona_snapshot`` (FR-CAT-6): a player character's persona, as a plain
  identifier (``null`` for an ad-hoc persona) and a copy of it as it was when the story was
  created (``{persona_id, name, gender, age, description, snapshotted_at}``, written by #119's
  genesis, ``core.catalog.personas.snapshot``). Editing or deleting the saved persona never
  touches it. Only a player character holds one (``ck_characters_persona_player``).
- ``images``: the media asset ids of a character added in the story (FR-CAT-7), in order, by
  reference. ``core.media.assets.MEDIA_REFERENCE_SOURCES`` counts them, so an asset a story
  character shows survives its retirement until the story goes.
- ``promoted_to``: the catalog entries promoted from the character (FR-CAT-8), oldest first, as
  plain ids: the one link a promotion leaves on the character.

#115 adds ``deflection_style`` (FR-GUARD-7): how the character refuses or changes the subject,
in its own voice (at most ``DEFLECTION_STYLE_MAX`` characters; ``null`` for none). The Lite
prompt renders it with the character's author notes, so a reply regenerated after a guardrail
violation (#68) stays in character. It is set when the character is created: from the request
(a story's cast, a character added later), or by #119's genesis from the catalog definition's
``definition.deflection_style``.

#150 adds ``true_age_source`` (``TRUE_AGE_SOURCES``): where the one true age came from. A
character a structured render introduces is ``declared`` when the render gave its age and
``repaired`` when the turn's age-repair call supplied it (FR-CAT-12).

#163 adds ``weight`` (``character_weight``: ``minor`` for a character modeled lazily by
interaction or salience, ``major`` for the cast, the player and an author-created or promoted
character; FR-EPI-10) and the position it was modeled at (``modeled_turn`` … ``modeled_step``,
``core.membership.Position``'s columns; 0 for the genesis cast). The player is always ``major``
(``ck_characters_player_major``).

#176 adds ``participation_role`` (``primary``, ``reactive`` or ``silent``; default ``reactive``,
FR-MULTI-7) and ``participation_fixed`` (a bound the engine never crosses on its own); the
player's character has no role (ignored for it).

#119 adds ``role_id`` (the scenario role a character of a story created from a scenario fills;
``NULL`` otherwise) and ``character_snapshots``: one row per catalog character a story was
created with (FR-SCN-9, FR-CAT-3,9), keyed ``(story_id, character_id)``, holding the catalog id
and version, the version's whole ``content``, when it was published and the rating version it
was published under. Like ``characters`` it is a story table with no foreign key: archiving,
editing or deleting the catalog entry never touches it, story deletion enumerates it and a fork
copies it. Ad-hoc characters and the player's persona get no row (the persona's copy is
``persona_snapshot``). A catalog-sourced ``characters.definition`` holds the snapshot's private
definition rendered as prose author notes (``core.genesis.render.definition_notes``, #122) so
Lite turns read one text, as they read an ad-hoc character's; the snapshot stays the record.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.enums import CharacterWeight
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

TRUE_AGE_SOURCES: tuple[str, ...] = ("catalog", "genesis", "declared", "repaired")
"""Where a character's one true age came from (#150, FR-CAT-12): its catalog entry, the
scenario's genesis, the request that created it, or the structured turn's age repair."""
TRUE_AGE_MIN = 0
TRUE_AGE_MAX = 150
DEFLECTION_STYLE_MAX = 2_000
"""The longest deflection style, for a story character and a catalog definition alike."""

characters = Table(
    "characters",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("ordinal", SmallInteger, nullable=False),
    Column("name", Text, nullable=False),
    Column("definition", Text, nullable=False),
    Column("true_age", Integer, nullable=False),
    Column("age_recorded_at", TIMESTAMP(timezone=True), nullable=True),
    Column("is_player", Boolean, nullable=False, server_default=text("false")),
    Column("safe_fallback_text", Text, nullable=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("source_catalog_id", ulid_type(), nullable=True),
    Column("source_catalog_version", Integer, nullable=True),
    Column("persona_id", ulid_type(), nullable=True),
    Column("persona_snapshot", JSONB, nullable=True),
    Column("images", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("promoted_to", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("deflection_style", Text, nullable=True),
    Column("role_id", Text, nullable=True),
    Column("true_age_source", Text, nullable=False, server_default=text("'declared'")),
    # The language codes the character understands (#161); ``NULL``: every language.
    Column("languages", ARRAY(Text), nullable=True),
    # #163 (FR-EPI-10): narrative weight and the position the character was modeled at.
    Column(
        "weight",
        pg_enum(CharacterWeight, "character_weight"),
        nullable=False,
        server_default=text("'major'"),
    ),
    Column("modeled_turn", Integer, nullable=False, server_default=text("0")),
    Column("modeled_variant", SmallInteger, nullable=False, server_default=text("0")),
    Column("modeled_slot_k", SmallInteger, nullable=False, server_default=text("0")),
    Column("modeled_beat", SmallInteger, nullable=False, server_default=text("0")),
    Column("modeled_step", SmallInteger, nullable=False, server_default=text("0")),
    # #176 (FR-MULTI-7): the character's participation role, a default or a fixed bound.
    Column("participation_role", Text, nullable=False, server_default=text("'reactive'")),
    Column("participation_fixed", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint(f"true_age BETWEEN {TRUE_AGE_MIN} AND {TRUE_AGE_MAX}", name="true_age"),
    CheckConstraint(
        "true_age_source IN ('catalog', 'genesis', 'declared', 'repaired')",
        name="true_age_source",
    ),
    CheckConstraint(
        "(persona_id IS NULL AND persona_snapshot IS NULL) OR is_player", name="persona_player"
    ),
    CheckConstraint("NOT is_player OR weight = 'major'", name="player_major"),
    CheckConstraint(
        "participation_role IN ('primary', 'reactive', 'silent')", name="participation_role"
    ),
)

Index("uq_characters_story_name", characters.c.story_id, func.lower(characters.c.name), unique=True)
Index("uq_characters_story_ordinal", characters.c.story_id, characters.c.ordinal, unique=True)

character_snapshots = Table(
    "character_snapshots",
    metadata,
    Column("story_id", ulid_type(), primary_key=True),
    Column("character_id", ulid_type(), primary_key=True),
    Column("source_catalog_id", ulid_type(), nullable=False),
    Column("version", Integer, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("published_at", TIMESTAMP(timezone=True), nullable=False),
    Column("content_rating_version_id", ulid_type(), nullable=True),
)
