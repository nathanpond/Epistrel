"""Per-purpose model configuration: effective-config resolution and encrypted keys
(FR-MODEL-1,3,8; NFR-PORT-1).

``model_config`` holds one row per configured purpose and a ``default`` row. Resolution is
per field: a purpose's ``base_url``, ``model`` and API key are its own when set, else the
``default`` row's; ``params`` inherit per key. A purpose with no row is the ``default`` row.

API keys are stored as Fernet ciphertext under ``EPISTREL_CONFIG_KEY`` and decrypted only to
build a client. ``config_revision`` is the newest ``updated_at``; adapters key their client
caches on it, so a write is seen by the next call (#52 adds the REST writes and revisioning).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr, ValidationError
from sqlalchemy import RowMapping, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.errors import InvalidRequest
from epistrel.core.tables import model_config
from epistrel.orchestrator.models.adapter import ModelParams, ModelUnavailable
from epistrel.orchestrator.models.registry import DEFAULT_MODEL_KEY, DELIVERED

FINGERPRINT_HEX = 8

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
    """What a call for ``purpose`` uses after inheritance from ``default``."""

    purpose: str
    base_url: str | None
    model: str | None
    api_key_ciphertext: str | None
    params: ModelParams
    revision: str

    @property
    def has_api_key(self) -> bool:
        return self.api_key_ciphertext is not None


def _params(raw: Mapping[str, Any], purpose: str) -> ModelParams:
    try:
        return ModelParams.model_validate(dict(raw))
    except ValidationError:
        # A stored row is written through the same schema; a bad one is an operator problem.
        raise ModelUnavailable(purpose, "not_configured", "stored params are invalid") from None


def resolve(
    purpose: str, own: ConfigRow | None, default: ConfigRow, revision: str
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


def _row(mapping: RowMapping) -> ConfigRow:
    return ConfigRow(
        purpose=mapping["purpose"],
        base_url=mapping["base_url"],
        model=mapping["model"],
        api_key_ciphertext=mapping["api_key_ciphertext"],
        params=mapping["params"] or {},
        updated_at=mapping["updated_at"],
    )


async def read_rows(conn: AsyncConnection) -> dict[str, ConfigRow]:
    result = await conn.execute(select(model_config))
    return {row["purpose"]: _row(row) for row in result.mappings()}


async def config_revision(conn: AsyncConnection) -> str:
    newest = await conn.scalar(select(func.max(model_config.c.updated_at)))
    return newest.isoformat() if newest is not None else ""


async def effective_config(db: AsyncEngine, purpose: str) -> EffectiveConfig:
    """The effective configuration of ``purpose`` (two rows read in one statement)."""
    async with db.connect() as conn:
        result = await conn.execute(
            select(model_config).where(model_config.c.purpose.in_([purpose, DEFAULT_MODEL_KEY]))
        )
        rows = {row["purpose"]: _row(row) for row in result.mappings()}
    default = rows.get(DEFAULT_MODEL_KEY)
    if default is None:  # the migration seeds it; a missing row is a broken deployment
        raise ModelUnavailable(purpose, "not_configured", "the default model row is missing")
    own = rows.get(purpose) if purpose != DEFAULT_MODEL_KEY else None
    revision = max(row.updated_at for row in rows.values()).isoformat()
    return resolve(purpose, own, default, revision)


async def all_effective(db: AsyncEngine) -> tuple[EffectiveConfig, dict[str, EffectiveConfig]]:
    """The ``default`` row and every delivered purpose's effective config."""
    async with db.connect() as conn:
        rows = await read_rows(conn)
        revision = await config_revision(conn)
    default = rows.get(DEFAULT_MODEL_KEY)
    if default is None:
        raise ModelUnavailable(DEFAULT_MODEL_KEY, "not_configured", "the default row is missing")
    return (
        resolve(DEFAULT_MODEL_KEY, None, default, revision),
        {name: resolve(name, rows.get(name), default, revision) for name in DELIVERED},
    )


class _Unset:
    pass


UNSET: Any = _Unset()


def _check_purpose(purpose: str) -> None:
    if purpose != DEFAULT_MODEL_KEY and purpose not in DELIVERED:
        raise InvalidRequest(
            f"unknown model purpose {purpose!r}",
            details={"field": "path.purpose", "purpose": purpose},
        )


def _check_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    if not base_url.startswith(("http://", "https://")):
        raise InvalidRequest("base_url must be an http(s) URL", details={"field": "body.base_url"})
    return base_url.rstrip("/")


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
) -> None:
    """Upsert ``purpose``'s row, changing only the fields given (``None`` clears a field).

    The library-level write #52's ``/admin/model-config`` routes build on; the key is
    encrypted before it reaches SQL.
    """
    _check_purpose(purpose)
    values: dict[str, Any] = {"updated_by": principal, "updated_at": func.clock_timestamp()}
    if base_url is not UNSET:
        values["base_url"] = _check_base_url(base_url)
    if model is not UNSET:
        values["model"] = model
    if params is not UNSET:
        values["params"] = params.wire() if params is not None else {}
    if api_key is not UNSET:
        values["api_key_ciphertext"] = keybox.encrypt(api_key) if api_key is not None else None
    statement = insert(model_config).values(purpose=purpose, **values)
    statement = statement.on_conflict_do_update(index_elements=["purpose"], set_=values)
    async with db.begin() as conn:
        await conn.execute(statement)


async def remove_override(db: AsyncEngine, purpose: str) -> None:
    """Drop ``purpose``'s own row so it inherits ``default`` entirely (tests; #52's DELETE)."""
    _check_purpose(purpose)
    if purpose == DEFAULT_MODEL_KEY:
        raise InvalidRequest("the default row cannot be removed", details={"field": "purpose"})
    async with db.begin() as conn:
        await conn.execute(model_config.delete().where(model_config.c.purpose == purpose))


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
