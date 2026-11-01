"""The in-process adapter (FR-EXT-1, NFR-PORT-2; #204): a library consumer's own callables.

A consumer embedding Epistrel registers its callables per process with
``Engine.register_in_process_provider(name, *, read, write=None)``; the provider's
``external_providers`` row (``transport = in_process``, its manifest naming the values and
their types) is still required, and is what a story's States name. The callables live in the
``InProcessRegistry`` of that Engine only: another process (a separate worker) has none.

- ``read(value, binding, scope_key)`` answers the raw value (or an awaitable of it, or a
  ``ReadResult``); the adapter types it as the manifest declares and hashes it.
- ``write(effect, binding)`` answers a ``WriteOutcome`` or its status (``confirmed``,
  ``failed`` or ``unknown``), or an awaitable of either.

A row with no callable registered in this process is an adapter error on read
(``ProviderUnavailable``, ``not_registered``: #206's unavailability policy applies) and on
write (#210 leaves the outbox row ``intended``). An exception a callable raises is an adapter
error too (``callable_failed``); a ``ProviderUnavailable`` it raises is passed on as raised.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from epistrel.core.providers import PROVIDER_NAME_PATTERN, ProviderRow
from epistrel.orchestrator.providers.port import (
    OUTCOMES,
    Binding,
    Effect,
    ProviderUnavailable,
    ReadResult,
    WriteOutcome,
    coerce,
    raw_hash,
)

log = logging.getLogger(__name__)

ReadCallable = Callable[[str, Binding, str | None], Any]
"""``read(value, binding, scope_key)``: the raw value, a ``ReadResult``, or an awaitable."""
WriteCallable = Callable[[Effect, Binding], Any]
"""``write(effect, binding)``: a ``WriteOutcome`` or a status, or an awaitable of one."""


@dataclass(frozen=True, slots=True)
class Callables:
    read: ReadCallable
    write: WriteCallable | None = None


class InProcessRegistry:
    """One process's in-process callables, by provider name (thread-safe)."""

    def __init__(self) -> None:
        self._callables: dict[str, Callables] = {}
        self._lock = threading.Lock()

    def register(
        self, name: str, *, read: ReadCallable, write: WriteCallable | None = None
    ) -> None:
        """Register (or replace) ``name``'s callables."""
        if not re.match(PROVIDER_NAME_PATTERN, name):
            raise ValueError(f"{name!r} is not a provider name")
        if not callable(read) or (write is not None and not callable(write)):
            raise TypeError("read and write are callables")
        with self._lock:
            self._callables[name] = Callables(read, write)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._callables.pop(name, None)

    def get(self, name: str) -> Callables | None:
        with self._lock:
            return self._callables.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._callables)


async def _settle(result: Any) -> Any:
    return await result if inspect.isawaitable(result) else result


class InProcessProvider:
    """``ExternalProvider`` over a registered ``in_process`` row and this process's callables
    (see the module docstring)."""

    def __init__(self, row: ProviderRow, registry: InProcessRegistry) -> None:
        self.row = row
        self.registry = registry

    @property
    def name(self) -> str:
        return self.row.name

    def _callables(self, value: str) -> Callables:
        found = self.registry.get(self.name)
        if found is None:
            raise ProviderUnavailable(self.name, value, "not_registered")
        return found

    async def read(self, value: str, binding: Binding, scope_key: str | None) -> ReadResult:
        declared = self.row.values().get(value)
        if declared is None:
            raise ProviderUnavailable(self.name, value, "unknown_value")
        call = self._callables(value).read
        try:
            found = await _settle(call(value, binding, scope_key))
        except ProviderUnavailable:
            raise
        except Exception:
            log.warning("in-process read failed", extra={"provider": self.name, "value": value})
            raise ProviderUnavailable(self.name, value, "callable_failed") from None
        if isinstance(found, ReadResult):
            typed = coerce(self.name, value, str(declared["type"]), found.value)
            return ReadResult(typed, found.at, found.raw_hash)
        typed = coerce(self.name, value, str(declared["type"]), found)
        return ReadResult(typed, datetime.now(UTC), raw_hash(found))

    async def write(self, effect: Effect, binding: Binding) -> WriteOutcome:
        declared = self.row.values().get(effect.value)
        if declared is None or not self.row.write_capable or not declared.get("writable"):
            raise ProviderUnavailable(self.name, effect.value, "not_writable")
        call = self._callables(effect.value).write
        if call is None:
            raise ProviderUnavailable(self.name, effect.value, "not_registered")
        try:
            found = await _settle(call(effect, binding))
        except ProviderUnavailable:
            raise
        except Exception:
            log.warning(
                "in-process write failed", extra={"provider": self.name, "value": effect.value}
            )
            return WriteOutcome(status="unknown")
        if isinstance(found, WriteOutcome):
            return found
        if found in OUTCOMES:
            return WriteOutcome(status=found, raw_hash=raw_hash(found))
        return WriteOutcome(status="unknown", raw_hash=raw_hash(found))


__all__ = [
    "Callables",
    "InProcessProvider",
    "InProcessRegistry",
    "ReadCallable",
    "WriteCallable",
]
