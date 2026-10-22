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
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
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
MediaStoreKind = Literal["local", "s3"]
DEFAULT_S3_REGION = "us-east-1"
S3_BUCKET_PATTERN = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
"""An S3 bucket name: 3 to 63 lowercase letters, digits, dots and hyphens."""
ProcessMode = Literal["all", "api", "worker"]
PROCESS_MODES: tuple[ProcessMode, ...] = ("all", "api", "worker")


class ConfigError(Exception):
    """Every configuration problem found in one load, one line each."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__("\n".join(lines))
        self.lines = lines


class StubRenderClaim(BaseModel):
    """A claim the stub's structured renders declare (P-04's structured variant, #153): on
    ``share`` of the renders, chosen by the request's hash, the first ``say`` beat declares this
    proposition sketch (``subject`` ``I`` is the rendering character)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    share: float = Field(gt=0.0, le=1.0)
    subject: str = Field(min_length=1, max_length=200)
    relation: str = Field(min_length=1, max_length=200)
    object: str | None = Field(default=None, max_length=200)
    negated: bool = False


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
    # Claims the stub's structured renders declare, each on its share of the renders (JSON in
    # the environment; #153's P-04 structured variant: 10% contradicted, 10% novel). The shares
    # are stacked over the request's hash and sum to at most 1; empty declares none.
    stub_render_claims: list[StubRenderClaim] = Field(default_factory=list)
    # Which adapter serves the embedding port (FR-EMBED-1, NFR-PORT-2; #138): `openai` calls the
    # `embedding` row's OpenAI-compatible `/embeddings` endpoint; `stub` derives a deterministic
    # vector from the text's SHA-256. Unset, it follows `model_adapter`.
    embedding_adapter: Literal["openai", "stub"] | None = None
    # The stub embedding's dimension (#138): 64 keeps fixtures small.
    stub_embedding_dimension: int = Field(default=64, ge=2, le=4096)
    # The lexical search backend (NFR-PORT-2; #138): `tsvector`, PostgreSQL full-text search.
    lexical_backend: Literal["tsvector"] = "tsvector"
    # A projection rebuild of a story with more events than this runs as a background job
    # (``rebuild_projections``, #70); smaller stories rebuild synchronously in the request.
    rebuild_async_threshold: int = Field(default=10_000, ge=1)
    # Keep each story's resolved engine configuration in this process (FR-PROF-5; #113): it is
    # used only while the story's profile version, its overrides' revision and the deployment's
    # configuration revision are unchanged, so a turn resolves its configuration in one query.
    # Tests turn it off.
    effective_config_cache: bool = True
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
    # Operator-only content debug logging (NFR-SEC-4, invariant 3; #74): log lines may carry
    # story content. Refused unless ``debug`` is on; switches itself off after 24 hours; keep
    # such logs at most 7 days.
    content_debug_logging: bool = False
    # OpenTelemetry traces over OTLP/HTTP (FR-OBS-1; #74), e.g. ``http://collector:4318``; unset
    # (the default) leaves tracing off. ``OTEL_EXPORTER_OTLP_*`` variables tune the exporter.
    otlp_endpoint: str | None = None
    # Where the local blob store keeps media bytes (FR-MEDIA-1; #105): ``<root>/<sha256[:2]>/
    # <sha256>``, with uploads staged under ``<root>/tmp``. Created at start-up; a root that
    # cannot be written refuses the start (exit 78). The image sets /var/lib/epistrel/media.
    media_root: Path = Path("var/media")
    # Where the blobs live (FR-MEDIA-1,3; #107): ``local`` (under ``media_root``) or ``s3``, any
    # S3-compatible endpoint. Uploads are staged under ``media_root`` either way. The store kind is
    # recorded in the database at the first start-up; switching it while media exist is refused
    # (exit 78). The bucket must exist (checked at start-up, exit 78).
    media_store: MediaStoreKind = "local"
    # The S3-compatible endpoint (unset: AWS's own), the endpoint presigned URLs name when it
    # differs from the one the server reaches (``media_s3_public_endpoint``), the bucket, the
    # region (signature v4), the access key pair (unset: boto3's credential chain), path-style
    # addressing (``http://host/bucket/key``, which MinIO needs) and TLS verification.
    media_s3_endpoint: str | None = None
    media_s3_public_endpoint: str | None = None
    media_s3_bucket: str | None = Field(default=None, pattern=S3_BUCKET_PATTERN)
    media_s3_region: str = Field(default=DEFAULT_S3_REGION, min_length=1, max_length=64)
    media_s3_access_key: str | None = None
    media_s3_secret_key: SecretStr | None = None
    media_s3_path_style: bool = False
    media_s3_verify_tls: bool = True
    # The ffmpeg executable that extracts a video's poster from its first frame (FR-MEDIA-3;
    # #107): a name looked up on ``PATH`` or a path. ``ffprobe`` is taken from beside it. ffmpeg is
    # a separate GPL program run as a process, never linked (invariant 2's exception); without it,
    # a video uploaded without a poster is refused (422 ``poster_required``).
    ffmpeg_path: str = Field(default="ffmpeg", min_length=1, max_length=1024)
    # The erasure ledger's authoritative copy (FR-ADMIN-7; #125): ``<dir>/ledger.jsonl``, one
    # hash-chained JSON line per erasure, kept outside the database and its backups (the
    # ``erasure_ledger`` table is the copy every ``pg_dump`` carries). Created at start-up; a
    # directory that cannot be written, or a file whose chain is broken, refuses the start
    # (exit 78). The image sets /var/lib/epistrel/erasure-ledger (compose: the
    # ``erasure-ledger`` volume, separate from ``pgdata`` and ``media``).
    erasure_ledger_dir: Path = Path("var/erasure-ledger")

    @property
    def embedding_adapter_kind(self) -> Literal["openai", "stub"]:
        """The embedding port's adapter: ``embedding_adapter``, else ``model_adapter``'s."""
        return self.embedding_adapter or self.model_adapter

    @field_validator("database_url")
    @classmethod
    def _must_be_psycopg_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(DATABASE_URL_SCHEME):
            raise ValueError(f"must be a {DATABASE_URL_SCHEME} URL")
        return value

    @field_validator("stub_render_claims")
    @classmethod
    def _shares_fit(cls, value: list[StubRenderClaim]) -> list[StubRenderClaim]:
        if sum(c.share for c in value) > 1.0 + 1e-9:
            raise ValueError("the claims' shares sum to at most 1")
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

    @field_validator("media_s3_endpoint", "media_s3_public_endpoint")
    @classmethod
    def _s3_endpoint_is_http(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("must be an http:// or https:// URL")
        return value

    @model_validator(mode="after")
    def _s3_store_needs_a_bucket(self) -> Self:
        if self.media_store == "s3" and self.media_s3_bucket is None:
            raise ValueError(
                f"{_env_name('media_s3_bucket')}: is required when {_env_name('media_store')} is s3"
            )
        if (self.media_s3_access_key is None) != (self.media_s3_secret_key is None):
            raise ValueError(
                f"{_env_name('media_s3_access_key')} and {_env_name('media_s3_secret_key')}: set "
                "both, or neither (boto3's credential chain)"
            )
        return self

    @field_validator("otlp_endpoint")
    @classmethod
    def _otlp_endpoint_is_http(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("must be an http:// or https:// URL")
        return value

    @model_validator(mode="after")
    def _content_logging_needs_debug(self) -> Self:
        if self.content_debug_logging and not self.debug:
            raise ValueError(
                f"{_env_name('content_debug_logging')}: is refused unless "
                f"{_env_name('debug')} is on (content debug logging is operator-only, NFR-SEC-4)"
            )
        return self

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
