"""Typed settings loaded from the environment.

Every setting is `EPISTREL_<NAME>`. Settings read the process environment only;
compose and `uv run --env-file` supply a file when one is wanted. Startup fails
fast on a missing or malformed required setting and names it.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "EPISTREL_"
# Names starting with this prefix belong to test tooling (e.g. EPISTREL_TEST_DATABASE_URL)
# and are never settings.
TEST_PREFIX = "EPISTREL_TEST_"
DATABASE_URL_SCHEME = "postgresql+psycopg://"
MIN_CREDENTIAL_LENGTH = 32
"""FR-AUTH-1: a service credential shorter than this is refused at startup."""
FERNET_KEY_BYTES = 32
FERNET_KEY_LENGTH = 44

log = logging.getLogger(__name__)

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
ProcessMode = Literal["all", "api", "worker"]
PROCESS_MODES: tuple[ProcessMode, ...] = ("all", "api", "worker")


class ConfigError(Exception):
    """Every configuration problem found in one load, one line each."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__("\n".join(lines))
        self.lines = lines


class Settings(BaseSettings):
    """The service's configuration boundary."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="forbid", frozen=True)

    database_url: SecretStr
    log_level: LogLevel = "INFO"
    host: str = "0.0.0.0"  # noqa: S104 - a server binds all interfaces by default; compose maps the port
    port: int = Field(default=8000, ge=1, le=65535)
    debug: bool = False
    # FR-AUTH-1: every REST call presents one of these as `Authorization: Bearer <credential>`.
    # Comma-separated in the environment (several allow rotation); all are equivalent.
    service_credentials: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    # Development only: accept REST calls without a credential. Refused unless `debug` is on.
    auth_disabled: bool = False
    # Key that encrypts stored provider API keys (Fernet: URL-safe base64 of 32 random bytes).
    # Required once any purpose has a stored key and the adapter is `openai` (checked at startup).
    config_key: SecretStr | None = None
    # Which model adapter serves every purpose (FR-MODEL-11, NFR-PORT-2): `openai` talks to the
    # configured OpenAI-compatible endpoints; `stub` is the deterministic test adapter.
    model_adapter: Literal["openai", "stub"] = "openai"
    # Seconds the stub adapter waits before answering (load and latency tests); 0 answers at once.
    stub_latency_s: float = Field(default=0.0, ge=0.0, le=600.0)
    # A projection rebuild of a story with more events than this runs as a background job
    # (``rebuild_projections``, #70); smaller stories rebuild synchronously in the request.
    rebuild_async_threshold: int = Field(default=10_000, ge=1)
    # Background workers (NFR-REL-5, #70): poll for due jobs every ``worker_poll_interval``
    # seconds whether or not a notification arrived; a claim holds a ``job_lease_seconds`` lease
    # renewed every third of it; a job fails for good after ``job_max_attempts`` failed
    # attempts; ``worker_listen_enabled`` turns the LISTEN wake-up on; ``worker_concurrency``
    # loops run per worker process.
    worker_poll_interval: float = Field(default=5.0, gt=0.0, le=300.0)
    job_lease_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    job_max_attempts: int = Field(default=5, ge=1, le=100)
    worker_listen_enabled: bool = True
    worker_concurrency: int = Field(default=2, ge=1, le=64)
    # Process mode (NFR-OPS-1, #71): ``all`` serves the API and runs ``worker_concurrency``
    # worker loops in one process (the image's default); ``api`` serves the API only; ``worker``
    # runs the worker loops and serves only ``/health``. ``epistrel serve --mode`` overrides it.
    mode: ProcessMode = "all"

    @field_validator("database_url")
    @classmethod
    def _must_be_psycopg_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(DATABASE_URL_SCHEME):
            raise ValueError(f"must be a {DATABASE_URL_SCHEME} URL")
        return value

    @field_validator("config_key")
    @classmethod
    def _config_key_is_a_fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not is_fernet_key(value.get_secret_value()):
            # Never echo the value.
            raise ValueError("must be URL-safe base64 of 32 bytes (44 characters)")
        return value

    @field_validator("service_credentials", mode="before")
    @classmethod
    def _split_credentials(cls, value: object) -> object:
        """Split the comma-separated environment form; empty items are dropped, items trimmed."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list | tuple):
            return [item.strip() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("service_credentials")
    @classmethod
    def _credentials_long_enough(cls, value: list[SecretStr]) -> list[SecretStr]:
        for index, credential in enumerate(value):
            if len(credential.get_secret_value()) < MIN_CREDENTIAL_LENGTH:
                # Name the position, never the value.
                raise ValueError(
                    f"credential #{index + 1} is shorter than {MIN_CREDENTIAL_LENGTH} characters"
                )
        return value

    @model_validator(mode="after")
    def _authentication_configured(self) -> Self:
        if self.auth_disabled:
            if not self.debug:
                raise ValueError(
                    f"{_env_name('auth_disabled')}: is refused unless {_env_name('debug')} is on"
                )
            log.warning("service authentication is disabled (EPISTREL_AUTH_DISABLED, debug only)")
        elif not self.service_credentials:
            raise ValueError(
                f"{_env_name('service_credentials')}: at least one credential of "
                f"{MIN_CREDENTIAL_LENGTH}+ characters is required"
            )
        return self


def is_fernet_key(value: str) -> bool:
    """True when ``value`` is URL-safe base64 of exactly 32 bytes (a Fernet key)."""
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except ValueError, binascii.Error:
        return False
    return len(decoded) == FERNET_KEY_BYTES and len(value) == FERNET_KEY_LENGTH


def _env_name(field: str) -> str:
    return f"{ENV_PREFIX}{field.upper()}"


def _unknown_env_names() -> list[str]:
    known = {_env_name(name) for name in Settings.model_fields}
    return sorted(
        name
        for name in os.environ
        if name.upper().startswith(ENV_PREFIX)
        and not name.upper().startswith(TEST_PREFIX)
        and name.upper() not in known
    )


def _describe(error: dict[str, object]) -> str:
    kind = str(error.get("type", ""))
    message = str(error.get("msg", ""))
    if kind == "missing":
        return "is required"
    if kind == "value_error":
        return message.removeprefix("Value error, ")
    return message


def load_settings() -> Settings:
    """Load settings from the environment or raise ConfigError naming every problem."""
    lines: list[str] = []
    settings: Settings | None = None
    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors():
            loc = error.get("loc")
            if loc:
                lines.append(f"error: {_env_name(str(loc[0]))}: {_describe(dict(error))}")
            else:  # a cross-field rule: its message names the variables itself
                lines.append(f"error: {_describe(dict(error))}")
    lines.extend(f"error: {name}: unknown setting" for name in _unknown_env_names())
    if lines or settings is None:
        raise ConfigError(lines or ["error: settings could not be loaded"])
    return settings
