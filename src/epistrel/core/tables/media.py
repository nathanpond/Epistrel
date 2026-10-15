"""Media assets and the subject-reference index (FR-MEDIA-1,2, FR-ADMIN-6; docs/02 §4.10; #105).

``media_assets`` holds one row per asset: its owner principal (FR-AUTH-5), ``kind`` (``image``
or ``video``), the MIME type sniffed from its bytes, its size, its dimensions or duration (null
when no probe could read them), the SHA-256 ``checksum`` that addresses its bytes in the blob
store, the content-rating version it was uploaded under, alt text, caption, generation metadata,
``source`` (``uploaded``; ``extracted`` for a video's poster ffmpeg took from its first frame,
#107; ``generated`` from M8), ``state``, the ``depicted_subjects`` the upload declared (``[{kind,
id}]``) and, for a video, ``poster_media_id``: the image asset that is its poster (FR-MEDIA-3;
#107), a plain reference the media sweep counts. Two assets with identical bytes share one blob:
the blob is keyed by checksum, never by asset. ``state`` is ``live``; ``retired`` once deleted
(the row and its bytes stay while anything references them, and the media sweep purges it once
nothing does); or ``candidate`` (M8's unattached catalog images, which the general sweep never
touches).

``media_upload_sessions`` holds the resumable uploads (FR-MEDIA-3; #107): the asset's metadata,
declared size, MIME type and SHA-256, the part size and the parts received so far, its ``state``
(``open``, ``completed`` or ``aborted``), when it expires (``media_upload_ttl`` after its last
activity), the asset a completion created, and, on an S3-compatible store, the store's own
multipart ``UploadId``.

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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type

CHECKSUM_LENGTH = 64
"""A SHA-256 digest in lowercase hex."""


class MediaKind(StrEnum):
    """What a media asset is: an ``image`` or a ``video`` (``image/*`` or ``video/*``)."""

    IMAGE = "image"
    VIDEO = "video"


class MediaSource(StrEnum):
    """Where an asset came from: ``uploaded`` by the consumer, ``generated`` (M8), or
    ``extracted``: a video's poster taken from its first frame with ffmpeg (#107)."""

    UPLOADED = "uploaded"
    GENERATED = "generated"
    EXTRACTED = "extracted"


class MediaState(StrEnum):
    """An asset's state: ``live``; ``retired`` once deleted (still served to what references
    it); or ``candidate`` (an unattached catalog image, M8; private)."""

    LIVE = "live"
    RETIRED = "retired"
    CANDIDATE = "candidate"


class UploadState(StrEnum):
    """A resumable upload session's state (#107): ``open`` (parts may arrive), ``completed``
    (its asset exists) or ``aborted`` (by ``DELETE`` or by the sweep once it expired)."""

    OPEN = "open"
    COMPLETED = "completed"
    ABORTED = "aborted"


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
MENTION_RULES = ("name", "part", "candidate")
"""A mention's ``rule`` (``core.erasure.matcher.Rule``; ADR-154)."""
RULE_CHECK = "rule IS NULL OR rule IN (" + ", ".join(f"'{r}'" for r in MENTION_RULES) + ")"
SPAN_CHECK = (
    "(span_start IS NULL) = (span_end IS NULL) AND (span_start IS NULL) = (rule IS NULL) "
    "AND (span_start IS NULL OR (ref_kind = 'mentioned' AND 0 <= span_start "
    "AND span_start < span_end))"
)
"""Only a mention has a position; one indexed before positions were recorded (migration
0044) has none until the one-time re-index."""
UPLOAD_STATE_CHECK = _in("state", UploadState)
DECLARED_SHA256_CHECK = "declared_sha256 ~ '^[0-9a-f]{64}$'"

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
    Column("poster_media_id", ulid_type(), nullable=True),
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
# The media sweep's "does a video name this poster" (#107).
Index("ix_media_assets_poster_media_id", media_assets.c.poster_media_id)

media_upload_sessions = Table(
    "media_upload_sessions",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("owner_principal", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("mime_type", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("declared_sha256", CHAR(CHECKSUM_LENGTH), nullable=False),
    Column("part_size", Integer, nullable=False),
    Column("received_parts", ARRAY(Integer), nullable=False),
    Column("state", Text, nullable=False),
    Column("s3_upload_id", Text, nullable=True),
    Column("media_id", ulid_type(), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    Column("closed_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint(UPLOAD_STATE_CHECK, name="state"),
    CheckConstraint(DECLARED_SHA256_CHECK, name="declared_sha256"),
    CheckConstraint("size > 0", name="size"),
    CheckConstraint("part_size > 0", name="part_size"),
)

# The sweep's expired open sessions and its closed rows past the result window.
Index(
    "ix_media_upload_sessions_state_expires_at",
    media_upload_sessions.c.state,
    media_upload_sessions.c.expires_at,
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
    # A mention's position in the field's NFC text (code points) and its matcher rule, never the
    # text (ADR-154).
    Column("span_start", Integer, nullable=True),
    Column("span_end", Integer, nullable=True),
    Column("rule", Text, nullable=True),
    CheckConstraint(SUBJECT_KIND_CHECK, name="subject_kind"),
    CheckConstraint(SCOPE_CHECK, name="scope"),
    CheckConstraint(REF_KIND_CHECK, name="ref_kind"),
    CheckConstraint(STORY_SCOPE_CHECK, name="story_scope"),
    CheckConstraint(RULE_CHECK, name="rule"),
    CheckConstraint(SPAN_CHECK, name="span"),
    UniqueConstraint(
        "subject_kind",
        "subject_id",
        "record_table",
        "record_id",
        "field",
        "span_start",
        name="uq_subject_references_subject_record_field_span",
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
    "DECLARED_SHA256_CHECK",
    "KIND_CHECK",
    "MENTION_RULES",
    "REF_KIND_CHECK",
    "RULE_CHECK",
    "SCOPE_CHECK",
    "SOURCE_CHECK",
    "SPAN_CHECK",
    "STATE_CHECK",
    "STORY_SCOPE_CHECK",
    "SUBJECT_KIND_CHECK",
    "UPLOAD_STATE_CHECK",
    "MediaKind",
    "MediaSource",
    "MediaState",
    "ReferenceKind",
    "ReferenceScope",
    "SubjectKind",
    "UploadState",
    "media_assets",
    "media_upload_sessions",
    "subject_references",
]
