"""A per-turn memo for story-constant reads (#615, #619): within ``scope()``, a read keyed by
what it depends on is computed once and shared by every stage of the turn attempt (each
render's check, the composed message's detection, the commit). Outside a scope nothing is
remembered. ``clear()`` drops everything (a rebase restart reads the story as it now stands).

Only data that cannot change within the attempt belongs here: the story's private records as
the turn's snapshot holds them, the erasure ledger, the cast. A write that would change a
memoized read clears it (``forget``)."""

from __future__ import annotations

from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_MEMO: ContextVar[dict[Hashable, Any] | None] = ContextVar("epistrel_turn_memo", default=None)

MISSING = object()
"""``get``'s answer for a key the memo does not hold (or no scope is active)."""


@contextmanager
def scope() -> Iterator[None]:
    """Remember reads until the scope ends (nested: the outer scope's memo stands)."""
    if _MEMO.get() is not None:
        yield
        return
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def active() -> bool:
    return _MEMO.get() is not None


def get(key: Hashable) -> Any:
    found = _MEMO.get()
    return MISSING if found is None else found.get(key, MISSING)


def put(key: Hashable, value: Any) -> None:
    found = _MEMO.get()
    if found is not None:
        found[key] = value


def forget(prefix: Hashable) -> None:
    """Drop every key whose first element is ``prefix`` (a write that changes that read)."""
    found = _MEMO.get()
    if found is not None:
        for key in [k for k in found if isinstance(k, tuple) and k and k[0] == prefix]:
            del found[key]


def in_transaction(conn: Any, key: Hashable) -> Any:
    """``get`` for a read that is constant only within one database transaction (#620): the
    value remembered under ``key`` by ``put_in_transaction`` on ``conn``'s current transaction,
    or ``MISSING`` (none, or another transaction's)."""
    found = get(key)
    if found is MISSING or found[0] is not _transaction(conn):
        return MISSING
    return found[1]


def put_in_transaction(conn: Any, key: Hashable, value: Any) -> None:
    """Remember ``value`` under ``key`` for ``conn``'s current transaction only."""
    put(key, (_transaction(conn), value))


def _transaction(conn: Any) -> Any:
    sync = getattr(conn, "sync_connection", None) or conn
    return sync.get_transaction()


def clear() -> None:
    found = _MEMO.get()
    if found is not None:
        found.clear()


__all__ = [
    "MISSING",
    "active",
    "clear",
    "forget",
    "get",
    "in_transaction",
    "put",
    "put_in_transaction",
    "scope",
]
