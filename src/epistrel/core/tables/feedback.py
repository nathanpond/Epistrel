"""Message feedback (FR-FB-1,2; #112): ``message_feedback`` and ``feedback_history``.

``message_feedback`` holds each principal's current rating of one generated message: at most one
row per ``(story_id, message_id, principal)``. ``message_id`` is the rated ``message_rendered``
event's id, with its ``turn`` and ``variant`` beside it (denormalised, so a rating names the
message variant it rated). ``rating`` is 1 to 5, ``tags`` the reason tags (sorted, distinct;
the deployment's ``feedback_tags``) and ``text`` the optional free text. The context the message
was produced under is copied from its operation's recorded ``config_revisions`` when the rating
is first written, never recomputed: ``operation_id``, ``config_fingerprint``,
``profile_version`` (``<profile id>@<version>``), ``models`` (``{purpose: {id,
reported_version}}``), ``rating_version_id`` (the story's content-rating version) and
``quality_status`` (the status string); an operation that recorded no configuration stamp
leaves them null. ``compacted_at`` marks a rating whose message an opt-in compaction removed
(M7: ``core.feedback.mark_compacted``); ``redacted_at`` marks one an erasure redacted (#124).

``feedback_history`` keeps every earlier value: a re-rating that changes something appends the
value it replaces (``kind = replaced``), and a retraction appends the final value before the row
is deleted (``kind = retracted``). ``rated_at`` is when that value was given and
``replaced_at`` when it stopped being current.

Both are audit-category records (FR-STORE-25, FR-FB-2): no story view, retrieval index or prompt
assembler reads them, and no event or operation records a rating. They are story-scoped (deleted
with the story, ``core.stories.STORY_TABLES``; a fork does not copy them) and expire only by
their own ``feedback_retention``. ``operation_id`` and ``message_id`` are plain ids, without
foreign keys, so a compaction can remove the message and its operation while the rating stays.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type

RATING_MIN = 1
RATING_MAX = 5
RATING_CHECK = f"rating BETWEEN {RATING_MIN} AND {RATING_MAX}"


class HistoryKind(StrEnum):
    """Why an earlier value of a rating stopped being current: a new rating ``replaced`` it, or
    it was ``retracted``."""

    REPLACED = "replaced"
    RETRACTED = "retracted"


HISTORY_KIND_CHECK = "kind IN ('replaced', 'retracted')"

message_feedback = Table(
    "message_feedback",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), ForeignKey("stories.id"), nullable=False),
    Column("message_id", ulid_type(), nullable=False),
    Column("turn", Integer, nullable=False),
    Column("variant", SmallInteger, nullable=False),
    Column("principal", Text, nullable=False),
    Column("rating", SmallInteger, nullable=False),
    Column("tags", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("text", Text, nullable=True),
    Column("operation_id", ulid_type(), nullable=False),
    Column("config_fingerprint", Text, nullable=True),
    Column("profile_version", Text, nullable=True),
    Column("models", JSONB, nullable=True),
    Column("rating_version_id", ulid_type(), nullable=True),
    Column("quality_status", Text, nullable=True),
    Column("compacted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("redacted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(RATING_CHECK, name="rating"),
    UniqueConstraint("story_id", "message_id", "principal"),
    # The retention sweep's cut-off (``feedback_retention`` in days).
    Index("ix_message_feedback_created_at", "created_at"),
)

feedback_history = Table(
    "feedback_history",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), ForeignKey("stories.id"), nullable=False),
    Column("message_id", ulid_type(), nullable=False),
    Column("turn", Integer, nullable=False),
    Column("variant", SmallInteger, nullable=False),
    Column("principal", Text, nullable=False),
    Column("rating", SmallInteger, nullable=False),
    Column("tags", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("text", Text, nullable=True),
    Column("kind", Text, nullable=False),
    Column("rated_at", TIMESTAMP(timezone=True), nullable=False),
    Column("replaced_at", TIMESTAMP(timezone=True), nullable=False),
    Column("redacted_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint(RATING_CHECK, name="rating"),
    CheckConstraint(HISTORY_KIND_CHECK, name="kind"),
    # One principal's history of one message (the read), and the retention sweep's cut-off.
    Index("ix_feedback_history_story_id_message_id", "story_id", "message_id", "principal"),
    Index("ix_feedback_history_replaced_at", "replaced_at"),
)

__all__ = [
    "HISTORY_KIND_CHECK",
    "RATING_CHECK",
    "RATING_MAX",
    "RATING_MIN",
    "HistoryKind",
    "feedback_history",
    "message_feedback",
]
