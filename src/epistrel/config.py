"""Typed settings loaded from the environment.

Every setting is `EPISTREL_<NAME>`. Settings read the process environment only;
compose and `uv run --env-file` supply a file when one is wanted. Startup fails
fast on a missing or malformed required setting and names it.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "EPISTREL_"
# Names starting with this prefix belong to test tooling (e.g. EPISTREL_TEST_DATABASE_URL)
# and are never settings.
TEST_PREFIX = "EPISTREL_TEST_"
DATABASE_URL_SCHEME = "postgresql+psycopg://"

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


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

    @field_validator("database_url")
    @classmethod
    def _must_be_psycopg_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(DATABASE_URL_SCHEME):
            raise ValueError(f"must be a {DATABASE_URL_SCHEME} URL")
        return value


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
            loc = error.get("loc") or ("settings",)
            lines.append(f"error: {_env_name(str(loc[0]))}: {_describe(dict(error))}")
    lines.extend(f"error: {name}: unknown setting" for name in _unknown_env_names())
    if lines or settings is None:
        raise ConfigError(lines or ["error: settings could not be loaded"])
    return settings
