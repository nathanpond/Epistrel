"""Idempotent mutating requests: keys, request hashes, the result window and tombstones
(FR-CONC-6, NFR-REL-4, FR-RESP-6; #61).

Every mutating Engine call runs as one ``IdempotentCall`` under an ``Idempotency-Key`` (the
caller's, or a server-generated ULID: the key is optional everywhere). The call:

1. **claims** the key after the call's cheap prechecks: an ``in_flight`` row in
   ``idempotency_keys`` written in its own short transaction, with the request hash, an
   ``owner_token`` and a lease the running call renews. When the key already exists:

   - a different request hash is 409 ``idempotency_conflict`` (nothing written);
   - ``done`` within the result window replays the stored result (``Replayed``), never
     re-executing; after it (or once #71's sweep purged the result) the retry gets a reference
     to the original operation (``ReferenceReplay``), never a re-execution;
   - ``in_flight`` waits for the original (polling the row, 50 ms backing off to 250 ms) up to
     the turn deadline + 5 s, then 503 ``original_in_flight_timeout``. A dead original (its
     lease expired) is taken over under a new ``owner_token`` and re-executed — unless its
     operation already committed, in which case the retry gets the reference.

2. **binds** its operation inside the operation's commit transaction (``Operation`` calls
   ``bind``; configuration writes call ``bind_configuration``, which records a ``control``
   operation for them). A late original whose token was taken over is refused there, so a key
   never maps to two committed operations. A call may store its result in the same
   transaction (``result_in_commit``: the Lite turn does).

3. **finishes**: ``done`` with the result ``{status, body}`` and ``expires_at`` (the result
   window in force when the call started, ``idempotency_result_window`` hours). A failure
   (any error) deletes the row, so the key is free for a retry: failures are never stored.

The request hash covers the operation name, the validated request (the model dump, defaults
filled, keys sorted), the path ids, the acting character, the *resolved* output viewpoint and
the expected revision; never the principal. A replay therefore answers before the
expected-revision check (#44's order), and a key reused for another viewpoint conflicts
(FR-RESP-12).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.enums import OperationKind, OperationStatus
from epistrel.core.errors import IdempotencyConflict, InvalidRequest, OriginalInFlightTimeout
from epistrel.core.ids import new_ulid
from epistrel.core.settings_schema import IdempotencySettings
from epistrel.core.tables import deployment_state, idempotency_keys, operations
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENT_REPLAY_HEADER = "Idempotent-Replay"

KEY_MAX = 255
KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,255}")
"""A key: 1 to 255 of ``A-Z a-z 0-9 . _ : -`` (anything else is 422)."""

DEPLOYMENT_SCOPE_ID = "deployment"
"""``scope_id`` of every deployment-scoped key."""

QUEUE_OWNER_PREFIX = "queue:"
"""An ``owner_token`` naming a queued turn (``queue:<operation id>``, #63)."""

LEASE_S = 60.0
"""How long a claim lives without renewal; the running call renews it every third of it."""
POLL_FIRST_S = 0.05
POLL_MAX_S = 0.25
WAIT_SLACK_S = 5.0
"""A duplicate waits for the original up to the turn deadline plus this."""

OPERATIONS_PATH = "/operations/{}"


class Scope(StrEnum):
    STORY = "story"
    DEPLOYMENT = "deployment"


class KeyStatus(StrEnum):
    IN_FLIGHT = "in_flight"
    DONE = "done"


def validate_key(key: str, *, field: str = f"headers.{IDEMPOTENCY_HEADER}") -> str:
    """``key`` when it follows the key rule; else 422 ``validation_error`` at ``field``."""
    if not KEY_PATTERN.fullmatch(key):
        raise InvalidRequest(
            f"{IDEMPOTENCY_HEADER} must be 1 to {KEY_MAX} characters of A-Z, a-z, 0-9, '.', "
            "'_', ':' or '-'",
            {"field": field},
        )
    return key


def canonical(value: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, UTF-8 kept."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def request_hash(operation: str, material: Mapping[str, Any]) -> str:
    """SHA-256 of the operation name and the request material, as canonical JSON."""
    return hashlib.sha256(
        canonical({"operation": operation, "request": material}).encode()
    ).hexdigest()


# -- settings ------------------------------------------------------------------------------------


def idempotency_settings(document: Mapping[str, Any] | None) -> IdempotencySettings:
    """The result window in a ``deployment_state.settings`` document; an invalid stored value
    falls back to the default (a warning names only the key)."""
    values = {k: v for k, v in (document or {}).items() if k in IdempotencySettings.model_fields}
    try:
        return IdempotencySettings.model_validate(values)
    except ValidationError:
        logger.warning("invalid idempotency settings %s; using the defaults", sorted(values))
        return IdempotencySettings()


async def read_idempotency_settings(db: AsyncEngine | AsyncConnection) -> IdempotencySettings:
    query = select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if isinstance(db, AsyncConnection):
        return idempotency_settings(await db.scalar(query))
    async with db.connect() as conn:
        return idempotency_settings(await conn.scalar(query))


# -- results ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredResult:
    """What a key keeps for the result window: the response status and body."""

    status: int
    body: Any

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "body": self.body}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> StoredResult:
        return cls(status=int(value["status"]), body=value["body"])


class Replayed(Exception):
    """Internal: the key's stored result answers this call (``IdempotentCall.claim``)."""

    def __init__(self, stored: StoredResult) -> None:
        super().__init__("replayed")
        self.stored = stored


class QueuedOriginal(Exception):
    """Internal: the key's original request waits in its story's turn queue (#63); the retry
    waits for that queued turn too (and is answered 202 like it when the wait runs out)."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"queued as {operation_id}")
        self.operation_id = operation_id


class ReferenceReplay(Exception):
    """A retry after the result window (or after the original's result was lost): the original
    request completed and is **not** re-executed (FR-CONC-6, NFR-REL-4). REST answers 200
    ``{operation_id, reference, <revision field>, idempotency_key}`` with
    ``Idempotent-Replay: true``; the library raises this, because the call's own result type
    cannot hold a reference. ``reference`` is ``/operations/{id}`` (``None`` when the original
    changed nothing and so recorded no operation)."""

    def __init__(
        self,
        *,
        key: str,
        operation_id: str | None,
        revision: int | None,
        revision_field: str,
    ) -> None:
        super().__init__(f"the request under this key already completed ({operation_id})")
        self.key = key
        self.operation_id = operation_id
        self.reference = OPERATIONS_PATH.format(operation_id) if operation_id else None
        self.revision = revision
        self.revision_field = revision_field

    def body(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "reference": self.reference,
            self.revision_field: self.revision,
            "idempotency_key": self.key,
        }


def mark_replayed[T](value: T) -> T:
    """``value`` with ``replayed=True`` (every mutating result dataclass has the field)."""
    if (
        dataclasses.is_dataclass(value)
        and not isinstance(value, type)
        and any(f.name == "replayed" for f in dataclasses.fields(value))
    ):
        return dataclasses.replace(value, replayed=True)
    return value


def revision_of(value: Any) -> int | None:
    for name in ("timeline_revision", "config_revision"):
        found = getattr(value, name, None)
        if isinstance(found, int):
            return found
    return None


@dataclass(frozen=True, slots=True)
class ResultCodec[T]:
    """How one Engine method's result is stored and replayed: its type (``TypeAdapter``), the
    REST status it answers with, and how a replay is marked (``replayed=True``)."""

    adapter: TypeAdapter[T]
    status: Callable[[T], int] = lambda _: 200
    mark: Callable[[T], T] = mark_replayed
    revision_field: str = "timeline_revision"

    @classmethod
    def of(
        cls,
        type_: Any,
        *,
        status: Callable[[T], int] | int = 200,
        mark: Callable[[T], T] = mark_replayed,
        revision_field: str = "timeline_revision",
    ) -> ResultCodec[T]:
        fixed = status
        status_of: Callable[[T], int] = (lambda _: fixed) if isinstance(fixed, int) else fixed
        return cls(TypeAdapter(type_), status_of, mark, revision_field)

    def store(self, value: T) -> StoredResult:
        return StoredResult(self.status(value), self.adapter.dump_python(value, mode="json"))

    def load(self, stored: StoredResult) -> T:
        return self.mark(self.adapter.validate_python(stored.body))


ResultInCommit = Callable[[AsyncConnection, Any], Awaitable[Any]]
"""``build(conn, operation) -> result``: the call's result, built inside the commit
transaction of its operation so ``done`` and the result commit with it."""


# -- the call ----------------------------------------------------------------------------------

_CURRENT: ContextVar[IdempotentCall | None] = ContextVar("epistrel_idempotent_call", default=None)


def current_call() -> IdempotentCall | None:
    """The idempotent call this task runs under, if any."""
    return _CURRENT.get()


@contextlib.contextmanager
def calling(call: IdempotentCall) -> Iterator[IdempotentCall]:
    token = _CURRENT.set(call)
    try:
        yield call
    finally:
        _CURRENT.reset(token)


class IdempotentCall:
    """One mutating request under its key; see the module docstring."""

    def __init__(
        self,
        db: AsyncEngine,
        *,
        scope: Scope,
        scope_id: str,
        key: str,
        operation: str,
        principal: str,
        codec: ResultCodec[Any],
        wait_s: float,
    ) -> None:
        self.db = db
        self.scope = scope
        self.scope_id = scope_id
        self.key = key
        self.operation = operation
        self.principal = principal
        self.codec = codec
        self.wait_s = wait_s
        self.request_hash: str | None = None
        self.token = new_ulid()
        self.claimed = False
        self.window = IdempotencySettings()
        self.result_in_commit: ResultInCommit | None = None
        self.committed_value: Any = None
        self.stored_in_commit = False
        self._renewal: asyncio.Task[None] | None = None

    # -- claim ----------------------------------------------------------------------------

    def _where(self) -> Any:
        return (
            (idempotency_keys.c.scope == self.scope.value)
            & (idempotency_keys.c.scope_id == self.scope_id)
            & (idempotency_keys.c.key == self.key)
        )

    async def claim(self, material: Mapping[str, Any]) -> None:
        """Take the key for this request, or answer from it (``Replayed``/``ReferenceReplay``),
        or refuse (409), or wait for the original (then 503); see the module docstring."""
        if self.claimed:
            raise RuntimeError(f"key {self.key} is already claimed by this call")
        self.request_hash = request_hash(self.operation, material)
        deadline = time.monotonic() + self.wait_s
        delay = POLL_FIRST_S
        while True:
            inserted, row = await self._insert_or_read()
            if inserted:
                self.claimed = True
                self._renewal = asyncio.create_task(self._renew())
                return
            if row is None:  # released between the insert and the read: try again
                continue
            if row.request_hash != self.request_hash:
                raise IdempotencyConflict(
                    "this Idempotency-Key was used for a different request",
                    {"reason": "request_hash"},
                )
            if row.status == KeyStatus.DONE.value:
                self._answer(row)
            if row.owner_token.startswith(QUEUE_OWNER_PREFIX):
                # Owned by the queue, not by a process: no lease applies.
                raise QueuedOriginal(row.owner_token.removeprefix(QUEUE_OWNER_PREFIX))
            if row.lease_until <= clock.now():
                if row.operation_id is not None:
                    await self._settle_dead(row)
                    self._answer_reference(row)
                if await self._take_over(row.owner_token):
                    self.claimed = True
                    self._renewal = asyncio.create_task(self._renew())
                    return
                continue
            if time.monotonic() >= deadline:
                raise OriginalInFlightTimeout(
                    "the original request under this Idempotency-Key is still running",
                    {"operation_id": row.operation_id or await self._running_operation()},
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, POLL_MAX_S)

    async def _insert_or_read(self) -> tuple[bool, Any]:
        """``(True, None)`` when this call inserted the ``in_flight`` row; else ``(False, the
        existing row)`` (``None`` if it vanished meanwhile)."""
        now = clock.now()
        async with self.db.begin() as conn:
            self.window = await read_idempotency_settings(conn)
            inserted = await conn.scalar(
                pg_insert(idempotency_keys)
                .values(
                    scope=self.scope.value,
                    scope_id=self.scope_id,
                    key=self.key,
                    request_hash=self.request_hash,
                    status=KeyStatus.IN_FLIGHT.value,
                    owner_token=self.token,
                    lease_until=now + timedelta(seconds=LEASE_S),
                )
                .on_conflict_do_nothing()
                .returning(idempotency_keys.c.key)
            )
            if inserted is not None:
                return True, None
            found = await conn.execute(select(idempotency_keys).where(self._where()))
            return False, found.one_or_none()

    def _answer(self, row: Any) -> None:
        if row.result is not None and row.expires_at is not None and row.expires_at > clock.now():
            raise Replayed(StoredResult.from_json(row.result))
        self._answer_reference(row)

    def _answer_reference(self, row: Any) -> None:
        raise ReferenceReplay(
            key=self.key,
            operation_id=row.operation_id,
            revision=row.revision,
            revision_field=self.codec.revision_field,
        )

    async def _take_over(self, dead_token: str) -> bool:
        """Replace a dead original's claim (lease expired, nothing committed) with ours."""
        now = clock.now()
        async with self.db.begin() as conn:
            taken = await conn.scalar(
                update(idempotency_keys)
                .where(
                    self._where(),
                    idempotency_keys.c.owner_token == dead_token,
                    idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
                    idempotency_keys.c.operation_id.is_(None),
                    idempotency_keys.c.lease_until <= now,
                )
                .values(owner_token=self.token, lease_until=now + timedelta(seconds=LEASE_S))
                .returning(idempotency_keys.c.key)
            )
        if taken is not None:
            logger.info("idempotency key taken over from a dead original (%s)", self.operation)
        return taken is not None

    async def _settle_dead(self, row: Any) -> None:
        """A dead original committed but never stored its result: the key is ``done`` with no
        result (the tombstone), so every retry gets the reference."""
        async with self.db.begin() as conn:
            await conn.execute(
                update(idempotency_keys)
                .where(
                    self._where(),
                    idempotency_keys.c.owner_token == row.owner_token,
                    idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
                )
                .values(status=KeyStatus.DONE.value, expires_at=clock.now())
            )

    async def _running_operation(self) -> str | None:
        """The original's in-flight operation (by its stored key), when it has begun one."""
        query = select(operations.c.operation_id).where(
            operations.c.idempotency_key == self.key,
            operations.c.status == OperationStatus.IN_FLIGHT.value,
        )
        if self.scope is Scope.STORY:
            query = query.where(operations.c.story_id == self.scope_id)
        async with self.db.connect() as conn:
            found: str | None = await conn.scalar(query.limit(1))
        return found

    async def _renew(self) -> None:
        """Keep the claim alive while the call runs (every third of the lease)."""
        while True:
            await asyncio.sleep(LEASE_S / 3)
            try:
                async with self.db.begin() as conn:
                    await conn.execute(
                        update(idempotency_keys)
                        .where(
                            self._where(),
                            idempotency_keys.c.owner_token == self.token,
                            idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
                        )
                        .values(lease_until=clock.now() + timedelta(seconds=LEASE_S))
                    )
            except Exception as exc:  # pragma: no cover - the database went away; retry later
                logger.warning("could not renew an idempotency lease: %s", type(exc).__name__)

    async def _stop_renewal(self) -> None:
        if self._renewal is not None:
            self._renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renewal
            self._renewal = None

    # -- the turn queue (#63) ---------------------------------------------------------------

    async def hand_over(self, conn: AsyncConnection, owner: str) -> None:
        """Hand the claimed key to ``owner`` (a queued turn, ``queue:<operation id>``) in the
        caller's transaction: the turn's drainer binds and finishes it, and this call no
        longer renews, binds, stores or releases it."""
        if not self.claimed:
            return
        handed = await conn.scalar(
            update(idempotency_keys)
            .where(
                self._where(),
                idempotency_keys.c.owner_token == self.token,
                idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
            )
            .values(owner_token=owner)
            .returning(idempotency_keys.c.key)
        )
        if handed is None:
            raise IdempotencyConflict(
                "a retry under the same Idempotency-Key took this request over; it was not queued",
                {"reason": "superseded"},
            )
        self.claimed = False
        if self._renewal is not None:
            self._renewal.cancel()
            self._renewal = None

    @classmethod
    def owned_by(
        cls,
        db: AsyncEngine,
        owner: str,
        *,
        scope: Scope,
        scope_id: str,
        key: str,
        operation: str,
        principal: str,
        codec: ResultCodec[Any],
    ) -> IdempotentCall:
        """The call a queued turn's drainer runs under: the key it was handed, already
        claimed (no lease: a queued key is owned by the queue, not a process)."""
        call = cls(
            db,
            scope=scope,
            scope_id=scope_id,
            key=key,
            operation=operation,
            principal=principal,
            codec=codec,
            wait_s=0,
        )
        call.token = owner
        call.claimed = True
        return call

    async def load_window(self) -> None:
        self.window = await read_idempotency_settings(self.db)

    # -- bind (inside the commit transaction) --------------------------------------------------

    async def bind(self, conn: AsyncConnection, operation: Any) -> None:
        """Bind this call's committing ``operation`` to the key, in the commit transaction.

        Refuses the commit (409 ``idempotency_conflict``, ``reason: superseded``) when the
        claim was taken over: a late original never commits a second operation for a key. The
        first operation a call commits is the key's operation; later ones keep it."""
        if not self.claimed:
            return
        bound = await conn.scalar(
            update(idempotency_keys)
            .where(
                self._where(),
                idempotency_keys.c.owner_token == self.token,
                idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
            )
            .values(
                operation_id=func.coalesce(idempotency_keys.c.operation_id, operation.operation_id)
            )
            .returning(idempotency_keys.c.operation_id)
        )
        if bound is None:
            raise IdempotencyConflict(
                "a retry under the same Idempotency-Key took this request over; it was not "
                "committed",
                {"reason": "superseded"},
            )
        if self.result_in_commit is not None and bound == operation.operation_id:
            value = await self.result_in_commit(conn, operation)
            await self._store(conn, self.codec.store(value), revision_of(value))
            self.committed_value = value
            self.stored_in_commit = True

    async def bind_configuration(self, conn: AsyncConnection, *, story_id: str | None) -> None:
        """Bind a configuration write (which runs no ``Operation``) in its own transaction:
        the first change of the call records a ``control`` operation (committed, no events;
        story-less for deployment configuration) and binds it to the key."""
        if not self.claimed:
            return
        operation_id = new_ulid()
        bound = await conn.scalar(
            update(idempotency_keys)
            .where(
                self._where(),
                idempotency_keys.c.owner_token == self.token,
                idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
            )
            .values(operation_id=func.coalesce(idempotency_keys.c.operation_id, operation_id))
            .returning(idempotency_keys.c.operation_id)
        )
        if bound is None:
            raise IdempotencyConflict(
                "a retry under the same Idempotency-Key took this request over; it was not "
                "committed",
                {"reason": "superseded"},
            )
        if bound != operation_id:
            return
        now = clock.now()
        await conn.execute(
            insert(operations).values(
                operation_id=operation_id,
                story_id=story_id,
                kind=OperationKind.CONTROL.value,
                principal=self.principal,
                status=OperationStatus.COMMITTED.value,
                membership_tx=now,
                committed_at=now,
                idempotency_key=self.key,
            )
        )

    # -- finish ---------------------------------------------------------------------------------

    async def _store(
        self, conn: AsyncConnection, stored: StoredResult, revision: int | None
    ) -> None:
        await conn.execute(
            update(idempotency_keys)
            .where(self._where(), idempotency_keys.c.owner_token == self.token)
            .values(
                status=KeyStatus.DONE.value,
                result=stored.to_json(),
                revision=revision,
                expires_at=clock.now() + timedelta(hours=self.window.idempotency_result_window),
            )
        )

    async def finish(self, value: Any) -> Any:
        """Store ``value`` as the key's result (unless the commit already did) and return the
        value the caller gets (the one built in the commit, when there is one)."""
        await self._stop_renewal()
        if self.stored_in_commit:
            return self.committed_value
        if not self.claimed:
            return value
        async with self.db.begin() as conn:
            await self._store(conn, self.codec.store(value), revision_of(value))
        return value

    async def release(self) -> None:
        """The call failed: free the key (failures are never stored). If the call's operation
        committed before the failure, the key becomes a tombstone instead (``done``, no
        result), so a retry gets the reference rather than a second commit."""
        await self._stop_renewal()
        if not self.claimed or self.stored_in_commit:
            return
        try:
            async with self.db.begin() as conn:
                removed = await conn.scalar(
                    delete(idempotency_keys)
                    .where(
                        self._where(),
                        idempotency_keys.c.owner_token == self.token,
                        idempotency_keys.c.operation_id.is_(None),
                    )
                    .returning(idempotency_keys.c.key)
                )
                if removed is None:
                    await conn.execute(
                        update(idempotency_keys)
                        .where(
                            self._where(),
                            idempotency_keys.c.owner_token == self.token,
                            idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
                        )
                        .values(status=KeyStatus.DONE.value, expires_at=clock.now())
                    )
        except Exception as exc:  # pragma: no cover - the database went away; the lease expires
            logger.warning("could not release an idempotency key: %s", type(exc).__name__)


# -- the sweep's helper (#71) and reads -------------------------------------------------------


async def purge_expired(db: AsyncEngine, now: Any = None) -> int:
    """Drop every stored result whose window has passed; the rows stay as tombstones (key,
    request hash, operation id). Returns the number purged. #71's sweep calls it."""
    moment = now or clock.now()
    async with db.begin() as conn:
        result = await conn.execute(
            update(idempotency_keys)
            .where(idempotency_keys.c.result.is_not(None), idempotency_keys.c.expires_at <= moment)
            .values(result=None)
        )
    return int(result.rowcount or 0)


async def stored_result(conn: AsyncConnection, operation_id: str) -> StoredResult | None:
    """The result stored for ``operation_id``'s key while its window is open, else ``None``."""
    row = (
        await conn.execute(
            select(idempotency_keys.c.result, idempotency_keys.c.expires_at)
            .where(
                idempotency_keys.c.operation_id == operation_id,
                idempotency_keys.c.status == KeyStatus.DONE.value,
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None or row.result is None or row.expires_at is None:
        return None
    if row.expires_at <= clock.now():
        return None
    return StoredResult.from_json(row.result)


__all__ = [
    "DEPLOYMENT_SCOPE_ID",
    "IDEMPOTENCY_HEADER",
    "IDEMPOTENT_REPLAY_HEADER",
    "KEY_MAX",
    "LEASE_S",
    "QUEUE_OWNER_PREFIX",
    "IdempotentCall",
    "KeyStatus",
    "QueuedOriginal",
    "ReferenceReplay",
    "Replayed",
    "ResultCodec",
    "Scope",
    "StoredResult",
    "calling",
    "current_call",
    "idempotency_settings",
    "mark_replayed",
    "purge_expired",
    "read_idempotency_settings",
    "request_hash",
    "stored_result",
    "validate_key",
]
