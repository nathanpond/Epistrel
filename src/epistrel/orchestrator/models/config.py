"""Per-purpose model configuration: effective-config resolution, the revisioned cache, the
writes behind ``/admin/model-config``, and encrypted keys (FR-MODEL-1,3,4,8; NFR-PORT-1;
NFR-SEC-3).

``model_config`` holds one row per configured purpose and a ``default`` row. Resolution is
per field: a purpose's ``base_url``, ``model`` and API key are its own when set, else the
``default`` row's; ``params`` inherit per key. A purpose with no row is the ``default`` row.

**Writes** (FR-MODEL-4) follow every deployment-scoped configuration write (#48): take
``lock_deployment``, move ``deployment_state.config_revision`` by exactly one, and record one
``config_changes`` row per changed field (class ``prospective``, setting
``model_config:<purpose>.<field>``, the acting principal) in the same transaction. A write that
changes nothing writes nothing. API-key rows record the key's fingerprint, never the key.

**Reads** go through ``ConfigCache``: each call reads ``config_revision`` once on a short
connection of its own and re-reads ``model_config`` only when the revision moved, so the call
after a write sees it, with no restart and no stale cache.

API keys are stored as Fernet ciphertext under ``EPISTREL_CONFIG_KEY`` and decrypted only to
build a client or compute a fingerprint. They never reach story data, ``config_changes``,
responses or logs (NFR-SEC-3).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from sqlalchemy import RowMapping, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import (
    ChangeClass,
    bump_config_revision,
    lock_deployment,
    record_config_change,
)
from epistrel.core.errors import ConfigConflict, InvalidRequest
from epistrel.core.tables import deployment_state, model_config
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.orchestrator.models.adapter import ModelParams, ModelUnavailable
from epistrel.orchestrator.models.registry import DEFAULT_MODEL_KEY, DELIVERED

FINGERPRINT_HEX = 8
MAX_API_KEY_CHARS = 512
MAX_MODEL_CHARS = 200
MAX_BASE_URL_CHARS = 2048
DEFAULT_STRUCTURED_RETRY_BUDGET = 2
DEFAULT_UNCERTAIN_BELOW = 0.5
UNREADABLE_KEY = "unreadable"
"""What a change row records for a stored key that cannot be decrypted (no fingerprint)."""

type Source = Literal["own", "default"]

log = logging.getLogger(__name__)


def key_fingerprint(api_key: str) -> str:
    """First 8 hex of the key's SHA-256: safe to log and show, never reversible."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:FINGERPRINT_HEX]


class KeyBox:
    """Encrypts and decrypts provider API keys with ``EPISTREL_CONFIG_KEY``."""

    def __init__(self, config_key: SecretStr | None) -> None:
        self._fernet = Fernet(config_key.get_secret_value()) if config_key is not None else None

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, api_key: str) -> str:
        if self._fernet is None:
            raise ConfigKeyMissing
        return self._fernet.encrypt(api_key.encode()).decode("ascii")

    def decrypt(self, ciphertext: str, *, purpose: str) -> SecretStr:
        if self._fernet is None:
            raise ModelUnavailable(purpose, "auth", "the stored API key cannot be decrypted")
        try:
            return SecretStr(self._fernet.decrypt(ciphertext.encode("ascii")).decode())
        except InvalidToken, ValueError:
            raise ModelUnavailable(
                purpose, "auth", "the stored API key cannot be decrypted"
            ) from None

    def fingerprint(self, ciphertext: str | None) -> str | None:
        """The stored key's fingerprint; ``None`` without a key or when it cannot be decrypted."""
        if ciphertext is None:
            return None
        try:
            return key_fingerprint(self.decrypt(ciphertext, purpose="").get_secret_value())
        except ModelUnavailable:
            return None

    def marker(self, ciphertext: str | None) -> str | None:
        """What a ``config_changes`` row records for a stored key: its fingerprint."""
        if ciphertext is None:
            return None
        return self.fingerprint(ciphertext) or UNREADABLE_KEY


class ConfigKeyMissing(InvalidRequest):
    """An API key was written while ``EPISTREL_CONFIG_KEY`` is unset."""

    code = "config_key_missing"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("EPISTREL_CONFIG_KEY is not set; API keys cannot be stored")


@dataclass(frozen=True)
class ConfigRow:
    """One ``model_config`` row as stored (the key still encrypted)."""

    purpose: str
    base_url: str | None
    model: str | None
    api_key_ciphertext: str | None
    params: Mapping[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class EffectiveConfig:
    """What a call for ``purpose`` uses after inheritance from ``default``, tagged with the
    deployment ``config_revision`` it was read at."""

    purpose: str
    base_url: str | None
    model: str | None
    api_key_ciphertext: str | None
    params: ModelParams
    revision: int

    @property
    def has_api_key(self) -> bool:
        return self.api_key_ciphertext is not None


@dataclass(frozen=True)
class ConfigEntry:
    """One purpose as ``GET /admin/model-config`` shows it: effective values, where each came
    from, and the key only as a fingerprint."""

    purpose: str
    base_url: str | None
    model: str | None
    params: dict[str, Any]
    has_api_key: bool
    api_key_fingerprint: str | None
    sources: dict[str, Any]


def _params(raw: Mapping[str, Any], purpose: str) -> ModelParams:
    try:
        return ModelParams.model_validate(dict(raw))
    except ValidationError:
        # A stored row is written through the same schema; a bad one is an operator problem.
        raise ModelUnavailable(purpose, "not_configured", "stored params are invalid") from None


def resolve(
    purpose: str, own: ConfigRow | None, default: ConfigRow, revision: int
) -> EffectiveConfig:
    """Per-field inheritance: the purpose's own value when set, else ``default``'s."""

    def pick(name: str) -> Any:
        value = getattr(own, name) if own is not None else None
        return value if value is not None else getattr(default, name)

    params = _params(default.params, purpose)
    if own is not None:
        params = params.merged(_params(own.params, purpose))
    return EffectiveConfig(
        purpose=purpose,
        base_url=pick("base_url"),
        model=pick("model"),
        api_key_ciphertext=pick("api_key_ciphertext"),
        params=params,
        revision=revision,
    )


def describe(purpose: str, rows: Mapping[str, ConfigRow], keybox: KeyBox) -> ConfigEntry:
    """``purpose``'s effective configuration with a ``source`` (``own``/``default``) per field;
    ``params`` carry a source per key. Every field of ``default`` is its own."""
    default = _default_row(rows, purpose)
    own = rows.get(purpose) if purpose != DEFAULT_MODEL_KEY else None
    effective = resolve(purpose, own, default, 0)

    def source(name: str) -> Source:
        if purpose == DEFAULT_MODEL_KEY:
            return "own"
        return "own" if own is not None and getattr(own, name) is not None else "default"

    own_params = _params(own.params, purpose).wire() if own is not None else {}
    params = effective.params.wire()
    return ConfigEntry(
        purpose=purpose,
        base_url=effective.base_url,
        model=effective.model,
        params=params,
        has_api_key=effective.has_api_key,
        api_key_fingerprint=keybox.fingerprint(effective.api_key_ciphertext),
        sources={
            "base_url": source("base_url"),
            "model": source("model"),
            "api_key": source("api_key_ciphertext"),
            "params": {
                key: "own" if purpose == DEFAULT_MODEL_KEY or key in own_params else "default"
                for key in params
            },
        },
    )


def _row(mapping: RowMapping) -> ConfigRow:
    return ConfigRow(
        purpose=mapping["purpose"],
        base_url=mapping["base_url"],
        model=mapping["model"],
        api_key_ciphertext=mapping["api_key_ciphertext"],
        params=mapping["params"] or {},
        updated_at=mapping["updated_at"],
    )


def _default_row(rows: Mapping[str, ConfigRow], purpose: str) -> ConfigRow:
    default = rows.get(DEFAULT_MODEL_KEY)
    if default is None:  # the migration seeds it; a missing row is a broken deployment
        raise ModelUnavailable(purpose, "not_configured", "the default model row is missing")
    return default


async def read_rows(conn: AsyncConnection) -> dict[str, ConfigRow]:
    result = await conn.execute(select(model_config))
    return {row["purpose"]: _row(row) for row in result.mappings()}


async def current_revision(conn: AsyncConnection) -> int:
    """The deployment's ``config_revision`` (one primary-key read)."""
    revision = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(revision or 0)


async def snapshot(db: AsyncEngine) -> tuple[int, dict[str, ConfigRow]]:
    """``config_revision`` and every row, read in one REPEATABLE READ snapshot."""
    async with db.begin() as conn:
        await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        revision = await current_revision(conn)
        rows = await read_rows(conn)
    return revision, rows


class StructuredSettings(BaseModel):
    """Deployment settings for structured model results (FR-MODEL-6,7), stored under
    ``deployment_state.settings``: retries after the first attempt, and the ``confidence``
    below which a result is ``Uncertain``. Other keys in that document belong to other stories
    and are ignored here."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    structured_retry_budget: int = Field(default=DEFAULT_STRUCTURED_RETRY_BUDGET, ge=0, le=10)
    uncertain_below: float = Field(default=DEFAULT_UNCERTAIN_BELOW, ge=0.0, le=1.0)


def structured_settings(document: Mapping[str, Any] | None) -> StructuredSettings:
    """Parse ``deployment_state.settings``; an invalid stored value falls back to the defaults
    (the conservative ones) with a warning naming the keys, never the values."""
    try:
        return StructuredSettings.model_validate(dict(document or {}))
    except ValidationError as exc:
        keys = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        log.warning("invalid structured-output settings %s; using the defaults", keys)
        return StructuredSettings()


async def read_structured_settings(conn: AsyncConnection) -> StructuredSettings:
    document = await conn.scalar(
        select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return structured_settings(document)


class ConfigCache:
    """The rows (and the structured-output settings) as of one ``config_revision``. Every
    lookup reads the revision first, on its own short connection, and re-reads the rows only
    when it moved."""

    def __init__(self) -> None:
        self._revision: int | None = None
        self._rows: dict[str, ConfigRow] = {}
        self._structured = StructuredSettings()

    def clear(self) -> None:
        self._revision, self._rows = None, {}
        self._structured = StructuredSettings()

    async def _refresh(self, db: AsyncEngine) -> int:
        async with db.connect() as conn:
            revision = await current_revision(conn)
        if revision != self._revision:
            async with db.begin() as conn:
                await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                revision = await current_revision(conn)
                rows = await read_rows(conn)
                structured = await read_structured_settings(conn)
            self._revision, self._rows, self._structured = revision, rows, structured
        return revision

    async def rows(self, db: AsyncEngine) -> tuple[int, dict[str, ConfigRow]]:
        revision = await self._refresh(db)
        return revision, self._rows

    async def structured(self, db: AsyncEngine) -> tuple[int, StructuredSettings]:
        revision = await self._refresh(db)
        return revision, self._structured

    async def effective(self, db: AsyncEngine, purpose: str) -> EffectiveConfig:
        revision, rows = await self.rows(db)
        default = _default_row(rows, purpose)
        own = rows.get(purpose) if purpose != DEFAULT_MODEL_KEY else None
        return resolve(purpose, own, default, revision)


def configurable() -> tuple[str, ...]:
    """The purposes ``/admin/model-config`` lists and writes: ``default`` plus every delivered
    purpose (reserved ones are not configurable yet, FR-MODEL-2)."""
    return (DEFAULT_MODEL_KEY, *DELIVERED)


async def listing(db: AsyncEngine, keybox: KeyBox) -> tuple[int, dict[str, ConfigEntry]]:
    """``config_revision`` and every configurable purpose, ``default`` first."""
    revision, rows = await snapshot(db)
    return revision, {name: describe(name, rows, keybox) for name in configurable()}


async def entry(db: AsyncEngine, keybox: KeyBox, purpose: str) -> tuple[int, ConfigEntry]:
    check_purpose(purpose)
    revision, rows = await snapshot(db)
    return revision, describe(purpose, rows, keybox)


# -- validation ------------------------------------------------------------------------------


def check_purpose(purpose: str) -> None:
    if purpose not in configurable():
        raise InvalidRequest(
            f"unknown model purpose {purpose!r}",
            details={"field": "path.purpose", "purpose": purpose, "reason": "unknown"},
        )


def normalise_base_url(base_url: str) -> str:
    """An ``http(s)://`` URL with a host, trailing slash stripped; ``ValueError`` otherwise."""
    value = base_url.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or " " in value:
        raise ValueError("base_url must be an http(s) URL")
    return value.rstrip("/")


class ConfigReplacement(BaseModel):
    """``PUT /admin/model-config/{purpose}``: the purpose's own ``base_url``, ``model`` and
    ``params``, replaced in full. An omitted or ``null`` field inherits from ``default``
    (``params`` per key). The API key is never part of this body (``extra=forbid``)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str | None = Field(default=None, max_length=MAX_BASE_URL_CHARS)
    model: str | None = Field(default=None, min_length=1, max_length=MAX_MODEL_CHARS)
    params: ModelParams | None = None

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str | None) -> str | None:
        return None if value is None else normalise_base_url(value)


class ApiKeyBody(BaseModel):
    """``PUT /admin/model-config/{purpose}/api-key``: the key, trimmed, 1..512 characters."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=1, max_length=MAX_API_KEY_CHARS)


# -- writes ----------------------------------------------------------------------------------

_COLUMNS = ("base_url", "model", "params", "api_key_ciphertext")


def config_setting(purpose: str, field: str) -> str:
    """The ``setting`` of a model-configuration change row."""
    return f"model_config:{purpose}.{field}"


@dataclass(frozen=True)
class ConfigWrite:
    """The outcome of a write: whether anything changed, and the revision after it."""

    changed: bool
    config_revision: int


def _columns(row: ConfigRow | None) -> dict[str, Any]:
    if row is None:
        return {"base_url": None, "model": None, "params": {}, "api_key_ciphertext": None}
    return {
        "base_url": row.base_url,
        "model": row.model,
        "params": dict(row.params),
        "api_key_ciphertext": row.api_key_ciphertext,
    }


type Change = Callable[[dict[str, Any]], dict[str, Any] | None]
"""Maps the purpose's stored columns to the columns to store, or ``None`` to drop its row."""


async def _store(
    db: AsyncEngine, purpose: str, change: Change, *, keybox: KeyBox, principal: str
) -> ConfigWrite:
    check_purpose(purpose)
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        result = await conn.execute(select(model_config).where(model_config.c.purpose == purpose))
        found = result.mappings().one_or_none()
        own = _row(found) if found is not None else None
        before = _columns(own)
        target = change(dict(before))
        after = target if target is not None else _columns(None)
        changed = [name for name in _COLUMNS if before[name] != after[name]]
        if not changed:
            if target is None and own is not None:  # an empty row: dropping it changes nothing
                await conn.execute(model_config.delete().where(model_config.c.purpose == purpose))
            return ConfigWrite(changed=False, config_revision=current.config_revision)
        revision = await bump_config_revision(conn, current)
        if target is None:
            await conn.execute(model_config.delete().where(model_config.c.purpose == purpose))
        else:
            values = {**after, "updated_by": principal, "updated_at": func.clock_timestamp()}
            statement = insert(model_config).values(purpose=purpose, **values)
            await conn.execute(
                statement.on_conflict_do_update(index_elements=["purpose"], set_=values)
            )
        for name in changed:
            old, new = before[name], after[name]
            field = name
            if name == "api_key_ciphertext":
                field, old, new = "api_key", keybox.marker(old), keybox.marker(new)
            await record_config_change(
                conn,
                config_revision=revision,
                setting=config_setting(purpose, field),
                old=old,
                new=new,
                actor=principal,
                change_class=ChangeClass.PROSPECTIVE,
            )
        return ConfigWrite(changed=True, config_revision=revision)


class _Unset:
    pass


UNSET: Any = _Unset()


def _check_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    try:
        return normalise_base_url(base_url)
    except ValueError as exc:
        raise InvalidRequest(str(exc), details={"field": "body.base_url"}) from None


def _encrypted(keybox: KeyBox, stored: str | None, api_key: str) -> str:
    """The ciphertext to store for ``api_key``: the stored one when it is the same key."""
    if stored is not None and keybox.fingerprint(stored) == key_fingerprint(api_key):
        return stored
    return keybox.encrypt(api_key)


async def write_config(
    db: AsyncEngine,
    purpose: str,
    *,
    keybox: KeyBox,
    principal: str,
    base_url: str | None = UNSET,
    model: str | None = UNSET,
    params: ModelParams | None = UNSET,
    api_key: str | None = UNSET,
) -> ConfigWrite:
    """Change only the given fields of ``purpose``'s row (``None`` clears a field).

    The library-level write the REST routes are built on; the key is encrypted before it
    reaches SQL.
    """
    if base_url is not UNSET:
        base_url = _check_base_url(base_url)
    if api_key is not UNSET and api_key is not None and not keybox.available:
        raise ConfigKeyMissing

    def change(columns: dict[str, Any]) -> dict[str, Any]:
        if base_url is not UNSET:
            columns["base_url"] = base_url
        if model is not UNSET:
            columns["model"] = model
        if params is not UNSET:
            columns["params"] = params.wire() if params is not None else {}
        if api_key is not UNSET:
            stored = columns["api_key_ciphertext"]
            columns["api_key_ciphertext"] = (
                _encrypted(keybox, stored, api_key) if api_key is not None else None
            )
        return columns

    return await _store(db, purpose, change, keybox=keybox, principal=principal)


async def replace_config(
    db: AsyncEngine,
    purpose: str,
    body: ConfigReplacement,
    *,
    keybox: KeyBox,
    principal: str,
) -> ConfigWrite:
    """``PUT``: replace the purpose's own ``base_url``, ``model`` and ``params``; its key stays."""
    return await write_config(
        db,
        purpose,
        keybox=keybox,
        principal=principal,
        base_url=body.base_url,
        model=body.model,
        params=body.params,
    )


async def remove_override(
    db: AsyncEngine, purpose: str, *, keybox: KeyBox, principal: str
) -> ConfigWrite:
    """Drop ``purpose``'s own row (its key included) so it inherits ``default`` entirely; the
    ``default`` row cannot be removed (409)."""
    check_purpose(purpose)
    if purpose == DEFAULT_MODEL_KEY:
        raise ConfigConflict(
            "the default model configuration cannot be removed",
            details={"field": "path.purpose", "purpose": purpose, "reason": "is_default"},
        )
    return await _store(db, purpose, lambda _: None, keybox=keybox, principal=principal)


async def set_api_key(
    db: AsyncEngine, purpose: str, api_key: SecretStr, *, keybox: KeyBox, principal: str
) -> ConfigWrite:
    """Store ``purpose``'s own API key (encrypted); setting the same key again is a no-op.
    503 ``config_key_missing`` without ``EPISTREL_CONFIG_KEY``."""
    return await write_config(
        db, purpose, keybox=keybox, principal=principal, api_key=api_key.get_secret_value()
    )


async def remove_api_key(
    db: AsyncEngine, purpose: str, *, keybox: KeyBox, principal: str
) -> ConfigWrite:
    """Remove ``purpose``'s own API key; it then inherits ``default``'s, if any."""
    return await write_config(db, purpose, keybox=keybox, principal=principal, api_key=None)


# -- startup ---------------------------------------------------------------------------------


async def stored_key_needs_config_key(settings: Settings, db: AsyncEngine) -> bool:
    """True when the ``openai`` adapter would need ``EPISTREL_CONFIG_KEY`` and it is unset:
    some purpose has a stored API key. Skipped under the stub, which reads no keys."""
    if settings.model_adapter == "stub" or settings.config_key is not None:
        return False
    async with db.connect() as conn:
        stored = await conn.scalar(
            select(func.count())
            .select_from(model_config)
            .where(model_config.c.api_key_ciphertext.is_not(None))
        )
    return bool(stored)


CONFIG_KEY_REQUIRED = (
    "error: EPISTREL_CONFIG_KEY: is required while a model API key is stored (model_config)"
)


async def config_key_problem(settings: Settings, db: AsyncEngine) -> str | None:
    """The startup refusal line, or ``None``. An unreachable database is not this check's
    problem (``/health`` reports it), so it passes and the first model call fails instead."""
    try:
        missing = await stored_key_needs_config_key(settings, db)
    except (DBAPIError, OSError) as exc:
        log.warning("model configuration not checked at startup: %s", type(exc).__name__)
        return None
    return CONFIG_KEY_REQUIRED if missing else None
