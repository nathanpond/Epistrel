"""The external-provider port (FR-EXT-1, NFR-PORT-2; #204): what every adapter offers.

``ExternalProvider.read(value, binding, scope_key)`` answers one tracked value for one story
(``binding`` carries the story's external ids) as a ``ReadResult``: the value, typed as the
manifest declares it (a number is a ``Decimal``), when it was read, and a hash of the raw
answer (the ``external_read`` event #205 records keeps the hash, not the raw body).
``write(effect, binding)`` dispatches one effect (#210) and answers a ``WriteOutcome``:
``confirmed``, ``failed`` or ``unknown`` (an ambiguous answer or a timeout, FR-EXT-16), with the
resulting values when the provider returns them (FR-EXT-20).

An adapter that cannot answer at all (the endpoint is down, a secret is missing, a host is not
allowed after substitution, no callable is registered in this process) raises
``ProviderUnavailable`` with a ``reason`` code: the provider's unavailability policy applies
(#206). Nothing an adapter raises or logs carries a URL with substituted ids, a secret or a
value (invariant 3): only the provider's name, the value's name and the reason.

Two adapters implement the port: ``HttpManifestProvider`` (``http_manifest``) and
``InProcessProvider`` (``in_process``). The rest of the Orchestrator sees only this protocol
(C-07's M6 part swaps them with no Engine Core change).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

OutcomeStatus = Literal["confirmed", "failed", "unknown"]
OUTCOMES: tuple[OutcomeStatus, ...] = ("confirmed", "failed", "unknown")


@dataclass(frozen=True, slots=True)
class Binding:
    """One story's binding to a provider: the story and its external ids there."""

    story_id: str
    provider: str
    external_ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Effect:
    """One effect to dispatch (#210): the provider's value it changes, its signed ``delta`` or
    new ``value``, the scope key of a character-scoped value, and the provider idempotency key
    (the outbox row id, stable across re-sends; FR-EXT-18)."""

    value: str
    idempotency_key: str
    delta: Decimal | None = None
    new_value: Decimal | bool | str | None = None
    scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class ReadResult:
    """A value read from a provider: typed, when, and the raw answer's SHA-256."""

    value: Decimal | bool | str
    at: datetime
    raw_hash: str


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """A write's outcome, the resulting values the provider returned (FR-EXT-20), and the raw
    answer's SHA-256 (empty when there was no answer)."""

    status: OutcomeStatus
    raw_hash: str = ""
    resulting_values: Mapping[str, Any] | None = None


class ProviderUnavailable(Exception):
    """An adapter could not answer (see the module docstring). ``reason`` is a code such as
    ``timeout``, ``unreachable``, ``status``, ``redirect``, ``not_json``, ``path``, ``type``,
    ``secret_missing``, ``binding_missing``, ``host_not_allowed`` or ``not_registered``."""

    def __init__(self, provider: str, value: str | None, reason: str) -> None:
        self.provider = provider
        self.value = value
        self.reason = reason
        named = f"{provider}.{value}" if value else provider
        super().__init__(f"provider {named} unavailable: {reason}")


@runtime_checkable
class ExternalProvider(Protocol):
    """The port (see the module docstring)."""

    @property
    def name(self) -> str: ...

    async def read(self, value: str, binding: Binding, scope_key: str | None) -> ReadResult: ...

    async def write(self, effect: Effect, binding: Binding) -> WriteOutcome: ...


def raw_hash(raw: bytes | Any) -> str:
    """The SHA-256 of a raw answer: its bytes, or a JSON value's canonical form."""
    if not isinstance(raw, bytes):
        raw = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def coerce(provider: str, value: str, declared: str, found: Any) -> Decimal | bool | str:
    """``found`` as the manifest's ``declared`` type (``number``: a JSON number, never a
    boolean, as a ``Decimal``; ``boolean``; ``enum`` and ``text``: a string); else
    ``ProviderUnavailable`` (``type``)."""
    if declared == "number":
        if isinstance(found, bool) or not isinstance(found, int | float | Decimal):
            raise ProviderUnavailable(provider, value, "type")
        return Decimal(str(found))
    if declared == "boolean":
        if not isinstance(found, bool):
            raise ProviderUnavailable(provider, value, "type")
        return found
    if not isinstance(found, str):
        raise ProviderUnavailable(provider, value, "type")
    return found


__all__ = [
    "OUTCOMES",
    "Binding",
    "Effect",
    "ExternalProvider",
    "OutcomeStatus",
    "ProviderUnavailable",
    "ReadResult",
    "WriteOutcome",
    "coerce",
    "raw_hash",
]
