"""``idempotency_keys``: every mutating request's key, its request hash and its result (#61).

One row per ``(scope, scope_id, key)``: scope ``story`` (``scope_id`` = the story) for story
calls, ``deployment`` (``scope_id`` = ``deployment``) for story creation, deletion and admin
calls. The row is written ``in_flight`` before the call's operation, in its own short
transaction, with an ``owner_token`` and a ``lease_until`` the running call renews; it becomes
``done`` with the ``result`` (``{status, body}``) and ``expires_at`` (the result window, FR-CONC-6)
once the call succeeds. Failures delete it. #71's sweep purges ``result`` after ``expires_at``
(to SQL NULL) and keeps the row as the tombstone (key, request hash, operation id) for the
life of the story; story deletion removes the story's rows by ``scope_id`` (#50).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

SCOPE_CHECK = "scope IN ('story', 'deployment')"
STATUS_CHECK = "status IN ('in_flight', 'done')"

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("scope", Text, nullable=False),
    Column("scope_id", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("operation_id", ulid_type(), nullable=True),
    Column("status", Text, nullable=False, server_default=text("'in_flight'")),
    # ``none_as_null``: a purged or absent result is SQL NULL, never JSON ``null``, so the
    # sweep's ``result IS NOT NULL`` does not find a purged row again (#361).
    Column("result", JSONB(none_as_null=True), nullable=True),
    Column("revision", BigInteger, nullable=True),
    Column("owner_token", Text, nullable=False),
    Column("lease_until", TIMESTAMP(timezone=True), nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=True),
    PrimaryKeyConstraint("scope", "scope_id", "key"),
    CheckConstraint(SCOPE_CHECK, name="scope"),
    CheckConstraint(STATUS_CHECK, name="status"),
    Index("ix_idempotency_keys_operation_id", "operation_id"),
    Index("ix_idempotency_keys_expires_at", "expires_at"),
)
