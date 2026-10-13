"""Media assets and the subject-reference index (FR-MEDIA-1,2, FR-ADMIN-6; docs/02 §4.10; #105).

``media_assets`` holds one row per asset: its owner principal (FR-AUTH-5), ``kind`` (``image``
or ``video``), the MIME type sniffed from its bytes, its size, its dimensions or duration (null
when no probe could read them), the SHA-256 ``checksum`` that addresses its bytes in the blob
store, the content-rating version it was uploaded under, alt text, caption, generation metadata,
``source`` (``uploaded`` here; ``generated`` from M8), ``state`` and the ``depicted_subjects`` the
upload declared (``[{kind, id}]``). Two assets with identical bytes share one blob: the blob is
keyed by checksum, never by asset. ``state`` is ``live``; ``retired`` once deleted (the row and
its bytes stay while anything references them, and the media sweep purges it once nothing does);
or ``candidate`` (M8's unattached catalog images, which the general sweep never touches).

``subject_references`` is the subject-reference index #123 completes: one row per (subject,
record, field) for every record that references a subject, written in the record's own
transaction so erasure (#124) can find it. #105 writes only ``scope = catalog`` rows (an
uploaded asset's declared subjects and the subjects its caption, alt text and prompts name),
whose ``story_id`` is null; ``scope = story`` rows name their story, are deleted with it
(``core.stories.STORY_TABLES``) and are not copied by a fork (#123 re-indexes the fork).
``record_table`` names the table (``media_assets``), ``record_id`` the row, ``field`` the
dotted path of the field that named the subject (null for a record that defines the subject
itself), and ``ref_kind`` how: ``declared`` (a structured id), ``mentioned`` (a name or alias
found in prose) or ``authored``. Both tables use plain ids, without foreign keys.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    CHAR,
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

CHECKSUM_LENGTH = 64
"""A SHA-256 digest in lowercase hex."""


class MediaKind(StrEnum):
    """What a media asset is: an ``image`` or a ``video`` (``image/*`` or ``video/*``)."""

    IMAGE = "image"
    VIDEO = "video"


class MediaSource(StrEnum):
    """Where an asset came from: ``uploaded`` by the consumer, or ``generated`` (M8)."""

    UPLOADED = "uploaded"
    GENERATED = "generated"


class MediaState(StrEnum):
    """An asset's state: ``live``; ``retired`` once deleted (still served to what references
    it); or ``candidate`` (an unattached catalog image, M8; private)."""

    LIVE = "live"
    RETIRED = "retired"
    CANDIDATE = "candidate"


class SubjectKind(StrEnum):
    """Who a subject is (#123): a story character, a catalog character, or a persona."""

    CHARACTER = "character"
    CATALOG_CHARACTER = "catalog_character"
    PERSONA = "persona"


class ReferenceScope(StrEnum):
    """A subject reference's scope: the ``catalog`` (catalog, persona and media records) or
    one ``story``."""

    CATALOG = "catalog"
    STORY = "story"


class ReferenceKind(StrEnum):
    """How a record references a subject: it was ``authored`` by it, ``mentioned`` it in prose,
    or ``declared`` it by id."""

    AUTHORED = "authored"
    MENTIONED = "mentioned"
    DECLARED = "declared"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v.value}'" for v in values) + ")"


KIND_CHECK = _in("kind", MediaKind)
SOURCE_CHECK = _in("source", MediaSource)
STATE_CHECK = _in("state", MediaState)
CHECKSUM_CHECK = "checksum ~ '^[0-9a-f]{64}$'"
SUBJECT_KIND_CHECK = _in("subject_kind", SubjectKind)
SCOPE_CHECK = _in("scope", ReferenceScope)
REF_KIND_CHECK = _in("ref_kind", ReferenceKind)
STORY_SCOPE_CHECK = "(story_id IS NOT NULL) = (scope = 'story')"

media_assets = Table(
    "media_assets",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("owner_principal", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("mime_type", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("width", Integer, nullable=True),
    Column("height", Integer, nullable=True),
    Column("duration_ms", BigInteger, nullable=True),
    Column("checksum", CHAR(CHECKSUM_LENGTH), nullable=False),
    Column("content_rating_version_id", ulid_type(), nullable=False),
    Column("alt_text", Text, nullable=True),
    Column("caption", Text, nullable=True),
    Column("generation_metadata", JSONB, nullable=True),
    Column("source", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("depicted_subjects", JSONB, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("created_by", Text, nullable=False),
    CheckConstraint(KIND_CHECK, name="kind"),
    CheckConstraint(SOURCE_CHECK, name="source"),
    CheckConstraint(STATE_CHECK, name="state"),
    CheckConstraint(CHECKSUM_CHECK, name="checksum"),
    CheckConstraint("size >= 0", name="size"),
)

# The blob's other users (the sweep's "no row of any state has this checksum"), listing by owner
# and principal lookup (#104, #117), and the newest-first listing by state.
Index("ix_media_assets_checksum", media_assets.c.checksum)
Index("ix_media_assets_owner_principal", media_assets.c.owner_principal)
Index(
    "ix_media_assets_state_created_at",
    media_assets.c.state,
    media_assets.c.created_at,
    media_assets.c.id,
)

subject_references = Table(
    "subject_references",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("subject_kind", Text, nullable=False),
    Column("subject_id", ulid_type(), nullable=False),
    Column("scope", Text, nullable=False),
    Column("story_id", ulid_type(), nullable=True),
    Column("record_table", Text, nullable=False),
    Column("record_id", Text, nullable=False),
    Column("field", Text, nullable=True),
    Column("ref_kind", Text, nullable=False),
    CheckConstraint(SUBJECT_KIND_CHECK, name="subject_kind"),
    CheckConstraint(SCOPE_CHECK, name="scope"),
    CheckConstraint(REF_KIND_CHECK, name="ref_kind"),
    CheckConstraint(STORY_SCOPE_CHECK, name="story_scope"),
    UniqueConstraint(
        "subject_kind",
        "subject_id",
        "record_table",
        "record_id",
        "field",
        name="uq_subject_references_subject_record_field",
        postgresql_nulls_not_distinct=True,
    ),
)

# Erasure's lookup (a subject's references in a scope) and a record's own rows (rewrite, drop).
Index(
    "ix_subject_references_subject",
    subject_references.c.subject_id,
    subject_references.c.scope,
    subject_references.c.story_id,
)
Index(
    "ix_subject_references_record",
    subject_references.c.record_table,
    subject_references.c.record_id,
)

__all__ = [
    "CHECKSUM_CHECK",
    "CHECKSUM_LENGTH",
    "KIND_CHECK",
    "REF_KIND_CHECK",
    "SCOPE_CHECK",
    "SOURCE_CHECK",
    "STATE_CHECK",
    "STORY_SCOPE_CHECK",
    "SUBJECT_KIND_CHECK",
    "MediaKind",
    "MediaSource",
    "MediaState",
    "ReferenceKind",
    "ReferenceScope",
    "SubjectKind",
    "media_assets",
    "subject_references",
]
