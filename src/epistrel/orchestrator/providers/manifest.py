"""Provider registrations and their manifests (FR-EXT-1,7,9,10,15,18,20; #204).

A registration (``ProviderRegistration``: ``POST /admin/external-providers``; ``ProviderSpec``
without the name: ``PUT /admin/external-providers/{name}``) names the provider, its trust tier,
its transport and its manifest, whether it is write-capable and, if so, its confirmation mode,
whether it deduplicates dispatch and returns resulting values; its unavailability policy with
its freshness window, and the synchronous wait budget.

The manifest is validated strictly by transport:

- ``http``: ``{values: {<value>: {type, read: {method, url, headers, query?, body?, value_path,
  timeout_seconds?}, write?: {method, url, headers, body, idempotency_header, outcome_path,
  outcome_map: {confirmed, failed}, resulting_values_path?, timeout_seconds?}}}}``.
- ``in_process``: ``{values: {<value>: {type, writable}}}`` (no templates; the callables are the
  process's, ``in_process``).

``validate_registration`` applies the rules ``http_manifest`` describes (placeholders, the path
subset, https or loopback, the deployment's allowed hosts) and refuses a write template (or a
``writable`` value) on a provider that is not write-capable. Every manifest refusal is 422
``invalid_manifest`` at the offending member (``details.field``, ``details.reason``); the other
fields refuse with 422 ``validation_error``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from epistrel.core.errors import InvalidManifest, InvalidRequest
from epistrel.core.providers import PROVIDER_NAME_PATTERN, placeholder, placeholders_in
from epistrel.core.tables.providers import DEFAULT_CONFIRM_WAIT_MAX_SECONDS
from epistrel.orchestrator.providers.http_manifest import (
    DEFAULT_TIMEOUT_SECONDS,
    PathError,
    UrlRefused,
    check_url,
    parse_path,
    template_authority_problem,
)

TrustTier = Literal["authoritative", "advisory"]
Transport = Literal["http", "in_process"]
Confirmation = Literal["synchronous", "asynchronous"]
Unavailability = Literal["fail_turn", "last_known", "unknown"]
ValueType = Literal["number", "boolean", "enum", "text"]
ReadMethod = Literal["GET", "POST"]
WriteMethod = Literal["POST", "PUT", "PATCH", "DELETE"]

ProviderName = Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN)]
ValueName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")]
HeaderName = Annotated[str, Field(pattern=r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")]
Template = Annotated[str, Field(min_length=1, max_length=2_000)]
Timeout = Annotated[StrictInt, Field(ge=1, le=60)]
JsonScalar = StrictStr | StrictInt | StrictBool
VALUES_MAX = 64
FRESHNESS_MAX_MINUTES = 525_600
CONFIRM_WAIT_MAX = 300


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadTemplate(_Strict):
    method: ReadMethod = "GET"
    url: Template
    headers: dict[HeaderName, Template] = Field(default_factory=dict, max_length=32)
    query: dict[Annotated[str, Field(min_length=1, max_length=64)], Template] | None = None
    body: Any = None
    value_path: Annotated[str, Field(min_length=1, max_length=200)]
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS


class OutcomeMap(_Strict):
    confirmed: list[JsonScalar] = Field(min_length=1, max_length=16)
    failed: list[JsonScalar] = Field(default_factory=list, max_length=16)


class WriteTemplate(_Strict):
    method: WriteMethod = "POST"
    url: Template
    headers: dict[HeaderName, Template] = Field(default_factory=dict, max_length=32)
    body: Any = None
    idempotency_header: HeaderName
    outcome_path: Annotated[str, Field(min_length=1, max_length=200)]
    outcome_map: OutcomeMap
    resulting_values_path: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS


class HttpValue(_Strict):
    type: ValueType
    read: ReadTemplate
    write: WriteTemplate | None = None


class InProcessValue(_Strict):
    type: ValueType
    writable: StrictBool = False


class HttpManifest(_Strict):
    values: dict[ValueName, HttpValue] = Field(min_length=1, max_length=VALUES_MAX)


class InProcessManifest(_Strict):
    values: dict[ValueName, InProcessValue] = Field(min_length=1, max_length=VALUES_MAX)


MANIFESTS: dict[str, type[_Strict]] = {"http": HttpManifest, "in_process": InProcessManifest}


EXAMPLE: dict[str, Any] = {
    "trust_tier": "authoritative",
    "transport": "http",
    "manifest": {
        "values": {
            "gold": {
                "type": "number",
                "read": {
                    "url": "https://api.game.example/players/{binding.player_id}/gold",
                    "headers": {"Authorization": "Bearer {secret.GAME_TOKEN}"},
                    "value_path": "$.balance",
                },
            }
        }
    },
    "unavailability": "last_known",
    "freshness_minutes": 10,
}
"""A provider owning one value, read over HTTP with the story's player id and a secret."""


class ProviderSpec(_Strict):
    """A provider's registration without its name (``PUT``: replaces every field)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [EXAMPLE]},
    )

    trust_tier: TrustTier
    transport: Transport
    manifest: dict[str, Any] = Field(
        description="The tracked values the provider owns: for `http`, each value's `type` and "
        "its `read` (and, write-capable, `write`) request templates; for `in_process`, each "
        "value's `type` and `writable`. Templates may use `{binding.<key>}`, `{story.id}`, "
        "`{value.scope_key}`, `{effect.delta}`, `{effect.value}`, `{effect.idempotency_key}` "
        "(writes) and `{secret.<NAME>}` (the environment's `EPISTREL_PROVIDER_SECRET_<NAME>`; "
        "never stored or returned). Paths are `$`, `.key` and `[n]` only."
    )
    write_capable: StrictBool = False
    confirmation: Confirmation | None = None
    dedupes_dispatch: StrictBool | None = None
    returns_resulting_values: StrictBool | None = None
    unavailability: Unavailability = "last_known"
    freshness_minutes: Annotated[StrictInt, Field(ge=1, le=FRESHNESS_MAX_MINUTES)] | None = None
    confirm_wait_max_seconds: Annotated[StrictInt, Field(ge=1, le=CONFIRM_WAIT_MAX)] = (
        DEFAULT_CONFIRM_WAIT_MAX_SECONDS
    )

    def columns(self) -> dict[str, Any]:
        """The ``external_providers`` columns this spec sets (the name aside)."""
        return {
            "trust_tier": self.trust_tier,
            "transport": self.transport,
            "manifest": self.manifest,
            "write_capable": self.write_capable,
            "confirmation": self.confirmation,
            "dedupes_dispatch": self.dedupes_dispatch,
            "returns_resulting_values": self.returns_resulting_values,
            "unavailability": self.unavailability,
            "freshness_minutes": self.freshness_minutes,
            "confirm_wait_max_seconds": self.confirm_wait_max_seconds,
        }


SPEC_FIELDS: tuple[str, ...] = tuple(ProviderSpec.model_fields)
"""The fields a ``PUT`` replaces, each recorded as ``external_provider:<name>.<field>``."""

WRITE_FIELDS: tuple[str, ...] = ("confirmation", "dedupes_dispatch", "returns_resulting_values")
"""What a write-capable provider must declare (FR-EXT-15,18,20), and a read-only one must not."""


class ProviderRegistration(ProviderSpec):
    """``POST /admin/external-providers``: a new provider, by name."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"name": "game_ledger", **EXAMPLE}]},
    )

    name: ProviderName


# -- validation ---------------------------------------------------------------------------------


def _refuse(field: Sequence[str | int], reason: str, message: str) -> InvalidManifest:
    path = ".".join(str(part) for part in ("body", "manifest", *field))
    return InvalidManifest(message, {"field": path, "reason": reason})


def _strings(value: Any, at: tuple[str | int, ...]) -> Iterator[tuple[tuple[str | int, ...], str]]:
    if isinstance(value, str):
        yield at, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, (*at, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*at, index))


def _check_placeholders(
    template: Mapping[str, Any], at: tuple[str | int, ...], *, write: bool
) -> None:
    for name in ("url", "headers", "query", "body"):
        for where, text in _strings(template.get(name), (*at, name)):
            for inner in placeholders_in(text):
                if placeholder(inner, write=write) is None:
                    raise _refuse(where, "unknown_placeholder", f"unknown placeholder {{{inner}}}")


def _check_url(
    template: Mapping[str, Any], at: tuple[str | int, ...], allowed: Sequence[str]
) -> None:
    url = str(template["url"])
    problem = template_authority_problem(url)
    if problem is not None:
        raise _refuse((*at, "url"), "placeholder_in_authority", problem)
    # The literal URL with each placeholder as a harmless path value: its scheme and host are
    # what every call's substituted URL has.
    literal = re.sub(r"\{[^{}]*\}", "x", url)
    try:
        check_url(literal, allowed)
    except UrlRefused as refused:
        raise _refuse((*at, "url"), refused.reason, str(refused)) from None


def _check_path(expression: str | None, at: tuple[str | int, ...]) -> None:
    if expression is None:
        return
    try:
        parse_path(expression)
    except PathError as exc:
        raise _refuse(at, "path_expression", str(exc)) from None


def validate_manifest(
    transport: str, manifest: Mapping[str, Any], *, write_capable: bool, allowed: Sequence[str]
) -> None:
    """422 ``invalid_manifest`` unless ``manifest`` is a valid ``transport`` manifest (see the
    module docstring)."""
    model = MANIFESTS[transport]
    try:
        parsed = model.model_validate(dict(manifest))
    except ValidationError as exc:
        raise InvalidManifest.from_errors(exc.errors(), prefix=("body", "manifest")) from None
    if isinstance(parsed, InProcessManifest):
        for name, owned in parsed.values.items():
            if owned.writable and not write_capable:
                raise _refuse(
                    ("values", name, "writable"),
                    "not_write_capable",
                    "a value is writable only on a write-capable provider",
                )
        return
    assert isinstance(parsed, HttpManifest)  # noqa: S101 - MANIFESTS has two models
    for name, value in parsed.values.items():
        at: tuple[str | int, ...] = ("values", name)
        read = value.read.model_dump(mode="json")
        _check_placeholders(read, (*at, "read"), write=False)
        _check_url(read, (*at, "read"), allowed)
        _check_path(value.read.value_path, (*at, "read", "value_path"))
        if value.write is None:
            continue
        if not write_capable:
            raise _refuse(
                (*at, "write"),
                "not_write_capable",
                "a write template needs a write-capable provider",
            )
        write = value.write.model_dump(mode="json")
        _check_placeholders(write, (*at, "write"), write=True)
        _check_url(write, (*at, "write"), allowed)
        _check_path(value.write.outcome_path, (*at, "write", "outcome_path"))
        _check_path(value.write.resulting_values_path, (*at, "write", "resulting_values_path"))


def validate_spec(spec: ProviderSpec, allowed: Sequence[str]) -> None:
    """``spec``'s rules (see the module docstring): the manifest, and a write-capable
    provider's declarations (each required, 422 ``validation_error``; a read-only provider
    declares none)."""
    for name in WRITE_FIELDS:
        given = getattr(spec, name)
        if spec.write_capable and given is None:
            raise InvalidRequest(
                f"a write-capable provider declares {name}",
                {"field": f"body.{name}", "reason": "required"},
            )
        if not spec.write_capable and given is not None:
            raise InvalidRequest(
                f"only a write-capable provider declares {name}",
                {"field": f"body.{name}", "reason": "not_write_capable"},
            )
    validate_manifest(
        spec.transport, spec.manifest, write_capable=spec.write_capable, allowed=allowed
    )


__all__ = [
    "MANIFESTS",
    "SPEC_FIELDS",
    "WRITE_FIELDS",
    "HttpManifest",
    "HttpValue",
    "InProcessManifest",
    "InProcessValue",
    "OutcomeMap",
    "ProviderRegistration",
    "ProviderSpec",
    "ReadTemplate",
    "WriteTemplate",
    "validate_manifest",
    "validate_spec",
]
