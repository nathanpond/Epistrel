"""Per-purpose model configuration: effective-config resolution, the revisioned cache, the
writes behind ``/admin/model-config``, and encrypted keys (FR-MODEL-1,3,4,8; NFR-PORT-1;
NFR-SEC-3).

``model_config`` holds one row per configured purpose and a ``default`` row. Resolution is
per field: a purpose's ``base_url``, ``model`` and API key are its own when set, else those of
the purpose it defaults to when the registry names one (``rating_suggestion`` defaults to
``guardrail_check``, FR-MODEL-2; #121), else the ``default`` row's; ``params`` inherit per key
the same way. A purpose with no row is its parent's row, then the ``default`` row. A **port**
(``embedding``, #138; ``image_generation``, #254) is its own row only: nothing falls back to
``default`` (a chat model is never an embedding or an image model), so an unset field stays
unset and reads ``source: own``. Its ``params`` take only that port's keys (``PORT_PARAMS``):
``embedding``'s ``timeout``; ``image_generation``'s image fields, validated by
``images.params.ImageGenerationParams`` (adapter, default size, aspect ratio and quality,
capability TTL, request timeout), where a non-null ``adapter`` must be registered in this
process and needs a ``model`` (422 naming the field). ``image_check`` (#254) reads its own row
only too (``registry.OWN_ROW_ONLY``), but as a purpose: while a field is unset it reads
``source: unset``, and it has no default model.

Every entry also names its ``endpoint``: the host of its effective ``base_url`` (``None`` when
unset), which is how an operator sees where each purpose's data goes (FR-MODEL-1, FR-IMG-13).

**Writes** (FR-MODEL-4) follow every deployment-scoped configuration write (#48): take
``lock_deployment``, move ``deployment_state.config_revision`` by exactly one, and record one
``config_changes`` row per changed field (class ``prospective``, setting
``model_config:<purpose>.<field>``, the acting principal) in the same transaction. A write that
changes nothing writes nothing. API-key rows record the key's fingerprint, never the key.

**Reads** go through ``ConfigCache``: each call reads ``config_revision`` once on a short
connection of its own and re-reads ``model_config`` only when the revision moved, so the call
after a write sees it, with no restart and no stale cache.

**Routes** (#325): a story's effective configuration may route a story purpose
(``models.purposes.<purpose>``, FR-PROF-5). ``routed`` lays a ``Route`` over the deployment's
effective configuration: the route's ``model`` wins (#106), and its ``params`` sit between the
deployment's and the call's own (``model_config`` < route < call). Endpoints and keys are never
routed. ``routes_of`` reads a configuration's routes.

API keys are stored as Fernet ciphertext under ``EPISTREL_CONFIG_KEY`` and decrypted only to
build a client or compute a fingerprint. They never reach story data, ``config_changes``,
responses or logs (NFR-SEC-3).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    SecretStr,
    ValidationError,
    field_validator,
)
from sqlalchemy import RowMapping, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import (
    bump_config_revision,
    lock_deployment,
    record_config_change,
)
from epistrel.core.errors import ConfigConflict, InvalidRequest
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.profiles.tiers import STRONGEST
from epistrel.core.settings_schema import ConfigClass, StructuredSettings
from epistrel.core.tables import deployment_state, model_config
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.trimmed import trim_as_sent
from epistrel.orchestrator.images.params import IMAGE_GENERATION_PORT, ImageGenerationParams
from epistrel.orchestrator.models.adapter import ModelParams, ModelUnavailable
from epistrel.orchestrator.models.registry import DEFAULT_MODEL_KEY, DELIVERED, OWN_ROW_ONLY, PORTS

FINGERPRINT_HEX = 8
MAX_API_KEY_CHARS = 512
MAX_MODEL_CHARS = 200
MAX_BASE_URL_CHARS = 2048
UNREADABLE_KEY = "unreadable"
"""What a change row records for a stored key that cannot be decrypted (no fingerprint)."""

type Source = Literal["own", "inherited", "default", "unset"]
"""Where an effective value came from: the purpose's ``own`` row, the purpose it defaults to
(``inherited``), the ``default`` row, or nowhere (``unset``: ``image_check`` before it is
configured, #254)."""

type AnyParams = ModelParams | ImageGenerationParams
"""A row's ``params``: a chat purpose's (and ``embedding``'s) ``ModelParams``, or the
``image_generation`` port's ``ImageGenerationParams``."""

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
    image_params: ImageGenerationParams | None = None
    """The ``image_generation`` port's own ``params`` (``params`` is then empty); ``None`` for
    every other purpose."""

    @property
    def has_api_key(self) -> bool:
        return self.api_key_ciphertext is not None


@dataclass(frozen=True)
class Route:
    """A story's routing of one purpose (``models.purposes.<purpose>``, #325): the model and
    the params it sets; ``None`` inherits the deployment's."""

    model: str | None = None
    params: ModelParams | None = None


def routes_of(settings: ProfileSettings) -> dict[str, Route]:
    """The purposes ``settings`` route, each with what its route sets (a route that sets
    nothing is left out)."""
    found: dict[str, Route] = {}
    for purpose, route in settings.models.purposes.items():
        set_params = route.params.model_dump(exclude_none=True) if route.params else {}
        params = ModelParams.model_validate(set_params) if set_params else None
        if route.model is not None or params is not None:
            found[purpose] = Route(model=route.model, params=params)
    return found


def routed(config: EffectiveConfig, route: Route | None) -> EffectiveConfig:
    """``config`` under ``route``: its model when it names one, and its params over the
    deployment's (a call's own params still win over both)."""
    if route is None:
        return config
    return replace(
        config,
        model=route.model if route.model is not None else config.model,
        params=config.params.merged(route.params),
    )


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
    endpoint: str | None = None
    """The host of the effective ``base_url``; ``None`` when it is unset (FR-IMG-13)."""


def endpoint_of(base_url: str | None) -> str | None:
    """The host ``base_url`` points at (``api.openai.com``), or ``None``."""
    if base_url is None:
        return None
    return urlsplit(base_url).hostname


def _params(raw: Mapping[str, Any], purpose: str) -> ModelParams:
    try:
        return ModelParams.model_validate(dict(raw))
    except ValidationError:
        # A stored row is written through the same schema; a bad one is an operator problem.
        raise ModelUnavailable(purpose, "not_configured", "stored params are invalid") from None


def _image_params(raw: Mapping[str, Any], purpose: str) -> ImageGenerationParams:
    try:
        return ImageGenerationParams.model_validate(dict(raw))
    except ValidationError:
        raise ModelUnavailable(purpose, "not_configured", "stored params are invalid") from None


def _wire_params(raw: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    if purpose == IMAGE_GENERATION_PORT:
        return _image_params(raw, purpose).wire()
    return _params(raw, purpose).wire()


def parent_of(purpose: str) -> str | None:
    """The purpose whose row ``purpose`` inherits from before ``default`` (its registry
    ``default_model_key``: ``rating_suggestion`` → ``guardrail_check``, FR-MODEL-2), or
    ``None`` when it inherits from ``default`` directly."""
    declared = DELIVERED.get(purpose)
    if declared is None or declared.default_model_key in (DEFAULT_MODEL_KEY, purpose):
        return None
    return declared.default_model_key


def resolve(
    purpose: str,
    own: ConfigRow | None,
    default: ConfigRow,
    revision: int,
    parent: ConfigRow | None = None,
) -> EffectiveConfig:
    """Per-field inheritance: the purpose's own value when set, else its ``parent`` row's (the
    purpose it defaults to, when it names one and that purpose has a row), else ``default``'s.
    ``params`` inherit per key the same way."""
    chain = [row for row in (own, parent) if row is not None]

    def pick(name: str) -> Any:
        for row in chain:
            value = getattr(row, name)
            if value is not None:
                return value
        return getattr(default, name)

    if purpose == IMAGE_GENERATION_PORT:
        # The image port's params are its own image fields, never the chat parameters.
        return EffectiveConfig(
            purpose=purpose,
            base_url=pick("base_url"),
            model=pick("model"),
            api_key_ciphertext=pick("api_key_ciphertext"),
            params=ModelParams(),
            revision=revision,
            image_params=_image_params(own.params if own is not None else {}, purpose),
        )
    params = _params(default.params, purpose)
    for row in reversed(chain):
        params = params.merged(_params(row.params, purpose))
    return EffectiveConfig(
        purpose=purpose,
        base_url=pick("base_url"),
        model=pick("model"),
        api_key_ciphertext=pick("api_key_ciphertext"),
        params=params,
        revision=revision,
    )


def _parent_row(rows: Mapping[str, ConfigRow], purpose: str) -> ConfigRow | None:
    parent = parent_of(purpose)
    return None if parent is None else rows.get(parent)


def describe(purpose: str, rows: Mapping[str, ConfigRow], keybox: KeyBox) -> ConfigEntry:
    """``purpose``'s effective configuration with a ``source`` per field: ``own``,
    ``inherited`` (from the purpose it defaults to, ``parent_of``), ``default``, or ``unset``
    (an own-row-only purpose's field it has not set: ``image_check``, #254); ``params`` carry a
    source per key. Every field of ``default`` and of a port is its own."""
    default = _default_row(rows, purpose)
    own = rows.get(purpose) if purpose != DEFAULT_MODEL_KEY else None
    parent = _parent_row(rows, purpose)
    effective = resolve(purpose, own, default, 0, parent)

    def source(name: str) -> Source:
        if purpose == DEFAULT_MODEL_KEY or purpose in PORTS:
            return "own"
        if own is not None and getattr(own, name) is not None:
            return "own"
        if purpose in OWN_ROW_ONLY:
            return "unset"
        if parent is not None and getattr(parent, name) is not None:
            return "inherited"
        return "default"

    own_params = _wire_params(own.params, purpose) if own is not None else {}
    parent_params = _params(parent.params, purpose).wire() if parent is not None else {}

    def param_source(key: str) -> Source:
        if purpose == DEFAULT_MODEL_KEY or purpose in OWN_ROW_ONLY or key in own_params:
            return "own"
        return "inherited" if key in parent_params else "default"

    image = effective.image_params
    params = image.wire() if image is not None else effective.params.wire()
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
            "params": {key: param_source(key) for key in params},
        },
        endpoint=endpoint_of(effective.base_url),
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


PORT_PARAMS: Mapping[str, frozenset[str]] = {
    "embedding": frozenset({"timeout"}),
    IMAGE_GENERATION_PORT: frozenset(ImageGenerationParams.model_fields),
}
"""The params each port's row may set: ``embedding`` only ``timeout`` (#138: the chat
parameters mean nothing to ``/embeddings``); ``image_generation`` its image fields (#254)."""


def _empty_row(purpose: str) -> ConfigRow:
    return ConfigRow(
        purpose=purpose,
        base_url=None,
        model=None,
        api_key_ciphertext=None,
        params={},
        updated_at=datetime.min,
    )


def _default_row(rows: Mapping[str, ConfigRow], purpose: str) -> ConfigRow:
    if purpose in OWN_ROW_ONLY:  # a port (or image_check) never falls back to the default row
        return _empty_row(purpose)
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
        return resolve(purpose, own, default, revision, _parent_row(rows, purpose))


def configurable() -> tuple[str, ...]:
    """The purposes ``/admin/model-config`` lists and writes: ``default`` plus every delivered
    purpose and port (reserved ones are not configurable yet, FR-MODEL-2)."""
    return (DEFAULT_MODEL_KEY, *DELIVERED, *PORTS)


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


def check_image_adapter(adapter: str | None) -> None:
    """422 at ``body.params.adapter`` when ``adapter`` is not registered in this process."""
    if adapter is None:
        return
    # Imported here: the image registry builds adapters from this module's configuration.
    from epistrel.orchestrator.images.registry import registered_adapters

    known = registered_adapters()
    if adapter not in known:
        raise InvalidRequest(
            f"image adapter {adapter!r} is not registered",
            details={
                "field": "body.params.adapter",
                "adapter": adapter,
                "registered": sorted(known),
                "reason": "unregistered_adapter",
            },
        )


def check_port_params(purpose: str, params: AnyParams) -> None:
    """422 when ``params`` are not ``purpose``'s kind: image fields anywhere but the
    ``image_generation`` port, anything else there, or a port key outside ``PORT_PARAMS``
    (a chat parameter on ``embedding``). An image port's ``adapter`` must be registered."""
    if purpose == IMAGE_GENERATION_PORT:
        if not isinstance(params, ImageGenerationParams):
            keys = sorted(params.model_dump(exclude_none=True))
            if keys:
                raise InvalidRequest(
                    f"{purpose} takes no chat parameters: {', '.join(keys)}",
                    details={
                        "field": f"body.params.{keys[0]}",
                        "purpose": purpose,
                        "params": keys,
                        "reason": "not_a_port_param",
                    },
                )
            return
        check_image_adapter(params.adapter)
        return
    if isinstance(params, ImageGenerationParams):
        keys = sorted(params.wire()) or ["adapter"]
        raise InvalidRequest(
            f"{purpose} takes no image parameters: {', '.join(keys)}",
            details={
                "field": f"body.params.{keys[0]}",
                "purpose": purpose,
                "params": keys,
                "reason": "not_a_port_param",
            },
        )
    allowed = PORT_PARAMS.get(purpose)
    if allowed is None:
        return
    refused = sorted(set(params.model_dump(exclude_none=True)) - allowed)
    if refused:
        raise InvalidRequest(
            f"{purpose} takes no chat parameters: {', '.join(refused)}",
            details={
                "field": f"body.params.{refused[0]}",
                "purpose": purpose,
                "params": refused,
                "reason": "not_a_port_param",
            },
        )


def normalise_base_url(base_url: str) -> str:
    """An ``http(s)://`` URL with a host, trailing slash stripped; ``ValueError`` otherwise."""
    value = base_url.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or " " in value:
        raise ValueError("base_url must be an http(s) URL")
    return value.rstrip("/")


def _params_object(value: Any) -> dict[str, Any] | None:
    """``params`` as sent: a JSON object, checked against the purpose's own params schema once
    the purpose is known (``ConfigReplacement.params_for``)."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("params must be an object")
    return dict(value)


class ConfigReplacement(BaseModel):
    """``PUT /admin/model-config/{purpose}``: the purpose's own ``base_url``, ``model`` and
    ``params``, replaced in full. An omitted or ``null`` field inherits from ``default``
    (``params`` per key). The API key is never part of this body (``extra=forbid``).

    ``params`` are the chat parameters (``ModelParams``) for every purpose and ``embedding``,
    and the image fields (``ImageGenerationParams``) for the ``image_generation`` port (owner
    decision 11, #254). Which one applies depends on the path's purpose, so they are checked by
    ``params_for`` (422 at ``body.params.<field>``, as the body's own validation reports)."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "base_url": "http://localhost:11434/v1",
                    "model": "llama3.1:8b",
                    "params": {"temperature": 0.8, "max_tokens": 600},
                }
            ]
        },
    )

    # Checked as sent before the model strips it, as the schema's maxLength says (#366).
    base_url: Annotated[str | None, BeforeValidator(trim_as_sent(MAX_BASE_URL_CHARS))] = Field(
        default=None, max_length=MAX_BASE_URL_CHARS
    )
    model: Annotated[str | None, BeforeValidator(trim_as_sent(MAX_MODEL_CHARS))] = Field(
        default=None, min_length=1, max_length=MAX_MODEL_CHARS
    )
    params: Annotated[
        dict[str, Any] | None,
        PlainValidator(
            _params_object, json_schema_input_type=ModelParams | ImageGenerationParams | None
        ),
    ] = None

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str | None) -> str | None:
        return None if value is None else normalise_base_url(value)

    def params_for(self, purpose: str) -> AnyParams | None:
        """``params`` as ``purpose``'s own schema (``ImageGenerationParams`` for the
        ``image_generation`` port, ``ModelParams`` otherwise); 422 naming the field."""
        if self.params is None:
            return None
        schema: type[ModelParams] | type[ImageGenerationParams] = (
            ImageGenerationParams if purpose == IMAGE_GENERATION_PORT else ModelParams
        )
        try:
            return schema.model_validate(self.params)
        except ValidationError as exc:
            raise InvalidRequest.from_errors(exc.errors(), prefix=("body", "params")) from None


class ApiKeyBody(BaseModel):
    """``PUT /admin/model-config/{purpose}/api-key``: the key, trimmed, 1..512 characters."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{"api_key": "sk-example-not-a-real-key"}]},
    )

    api_key: Annotated[SecretStr, BeforeValidator(trim_as_sent(MAX_API_KEY_CHARS))] = Field(
        min_length=1, max_length=MAX_API_KEY_CHARS
    )


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
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


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
                change_class=ConfigClass.prospective,
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
    params: AnyParams | None = UNSET,
    api_key: str | None = UNSET,
) -> ConfigWrite:
    """Change only the given fields of ``purpose``'s row (``None`` clears a field).

    The library-level write the REST routes are built on; the key is encrypted before it
    reaches SQL.
    """
    if base_url is not UNSET:
        base_url = _check_base_url(base_url)
    if model == STRONGEST:
        # #227: the sentinel is a profile route's, never a configured model.
        raise InvalidRequest(
            f"{STRONGEST!r} is valid only as a profile route's model",
            {
                "field": "body.model",
                "errors": [
                    {
                        "field": "body.model",
                        "message": "a sentinel, not a model",
                        "type": "value_error",
                    }
                ],
            },
        )
    if params is not UNSET and params is not None:
        check_port_params(purpose, params)
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
        if purpose == IMAGE_GENERATION_PORT:
            _check_image_row(columns)
        return columns

    return await _store(db, purpose, change, keybox=keybox, principal=principal)


def _check_image_row(columns: Mapping[str, Any]) -> None:
    """422 at ``body.model`` when the ``image_generation`` row would name an adapter without a
    model (nothing is written: the write's transaction rolls back)."""
    adapter = (columns.get("params") or {}).get("adapter")
    if adapter is not None and columns.get("model") is None:
        raise InvalidRequest(
            "an image adapter needs a model",
            details={"field": "body.model", "adapter": adapter, "reason": "model_required"},
        )


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
        params=body.params_for(purpose),
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
