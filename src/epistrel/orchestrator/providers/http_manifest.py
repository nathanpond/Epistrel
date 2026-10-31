"""The declarative HTTP adapter (FR-EXT-1,5; #204): a manifest executed by template
substitution, with no code and no model.

**Path expressions.** ``value_path``, ``outcome_path`` and ``resulting_values_path`` use an
in-house subset of JSONPath: ``$`` (the whole answer) followed by any number of ``.key`` (a
letter or ``_``, then letters, digits, ``_`` and ``-``) and ``[n]`` (a list index, ``0`` or
more). Filters, wildcards, slices, recursive descent, quoted keys and negative indexes are not
supported: ``parse_path`` refuses them (registration answers 422 ``invalid_manifest``). No
dependency is added for it (owner decision, 2026-10-30).

**Substitution.** A template string's placeholders (``core.providers``' grammar) are replaced
at call time. In a URL every substituted value is percent-encoded with nothing kept safe, so a
``/``, ``?``, ``#`` or ``@`` in an id cannot change the URL's structure; a placeholder in the
scheme, host or port is refused at registration. In a header or query value the value is
substituted as text (the HTTP client encodes the query; a CR or LF in a header value makes the
call fail). In a JSON body a string that is exactly one placeholder becomes the typed value (a
number stays a number), and a placeholder inside a longer string interpolates as text.
``{secret.<NAME>}`` resolves from the environment variable ``EPISTREL_PROVIDER_SECRET_<NAME>``
at call time; a missing one is an adapter error (``secret_missing``). Secrets are never stored,
returned or logged.

**Hosts.** A URL is ``https``, or ``http`` on a loopback host (``127.0.0.1``, ``localhost``,
``::1``). Its host (and port, when the entry names one) must match an entry of the deployment's
``provider_allowed_hosts`` exactly (empty: every HTTP provider is refused). Both rules are
checked at registration on the template and again at call time on the substituted URL, before
any request is sent.

**Calls.** Each call uses the template's ``timeout_seconds`` (default 5), never follows a
redirect (a 3xx is an adapter error, ``redirect``), and a read is never retried. A read answers
the value at ``value_path``, typed as the manifest declares it. A write sends the effect's
idempotency key in the template's ``idempotency_header`` and maps the value at
``outcome_path`` through ``outcome_map`` (``confirmed``/``failed``; anything else, a timeout or
a transport error after the request may have left is ``unknown``).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx2

from epistrel.core.providers import ProviderRow, placeholder
from epistrel.orchestrator.providers.port import (
    Binding,
    Effect,
    ProviderUnavailable,
    ReadResult,
    WriteOutcome,
    coerce,
    raw_hash,
)

log = logging.getLogger(__name__)

SECRET_ENV_PREFIX: Final = "EPISTREL_PROVIDER_SECRET_"  # noqa: S105 - a variable-name prefix
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_TIMEOUT_SECONDS: Final = 5
PLACEHOLDER: Final = re.compile(r"\{([^{}]*)\}")

# -- the path subset -------------------------------------------------------------------------------

_STEP: Final = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(0|[1-9][0-9]{0,8})\]")

PathStep = str | int


class PathError(ValueError):
    """A path expression outside the subset (see the module docstring)."""


class PathMissing(LookupError):
    """A path that does not lead anywhere in a document."""


def parse_path(expression: str) -> tuple[PathStep, ...]:
    """``expression`` as its steps (a key or a list index); ``PathError`` outside the subset."""
    if not expression.startswith("$"):
        raise PathError("a path starts with $")
    steps: list[PathStep] = []
    at = 1
    while at < len(expression):
        match = _STEP.match(expression, at)
        if match is None:
            raise PathError(
                "a path is $ followed by .key and [n] steps only (no filters, wildcards, "
                "slices or recursive descent)"
            )
        key, index = match.groups()
        steps.append(key if key is not None else int(index))
        at = match.end()
    return tuple(steps)


def extract(document: Any, steps: Sequence[PathStep]) -> Any:
    """The value at ``steps`` in ``document``; ``PathMissing`` where a step finds nothing."""
    found = document
    for step in steps:
        if isinstance(step, int):
            if not isinstance(found, list) or step >= len(found):
                raise PathMissing(step)
            found = found[step]
        else:
            if not isinstance(found, dict) or step not in found:
                raise PathMissing(step)
            found = found[step]
    return found


# -- hosts and schemes -----------------------------------------------------------------------------


def _bare(host: str) -> str:
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def _same_host(a: str, b: str) -> bool:
    a, b = _bare(a).lower(), _bare(b).lower()
    if a == b:
        return True
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return False


def allowed_entry(entry: str) -> tuple[str, int | None]:
    """An allowed-hosts entry as ``(host, port or None)``."""
    entry = entry.strip().lower()
    if entry.startswith("["):
        host, _, rest = entry[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        return host, int(port) if port.isdigit() else None
    return entry, None


def host_allowed(host: str, port: int, allowed: Sequence[str]) -> bool:
    """Whether ``host``/``port`` matches an entry of ``allowed`` (a bare entry: any port)."""
    for entry in allowed:
        name, wanted = allowed_entry(entry)
        if _same_host(host, name) and (wanted is None or wanted == port):
            return True
    return False


def is_loopback(host: str) -> bool:
    return _bare(host).lower() in LOOPBACK_HOSTS


class UrlRefused(ValueError):
    """A URL a provider may not call; ``reason`` names the rule."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def check_url(url: str, allowed: Sequence[str]) -> None:
    """``UrlRefused`` unless ``url`` is ``https`` (or ``http`` on a loopback host), has no
    user info, and its host and port match ``allowed`` (see the module docstring)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlRefused("url", "the URL does not parse") from None
    host = parts.hostname
    if not host:
        raise UrlRefused("url", "the URL names no host")
    if parts.username is not None or parts.password is not None:
        raise UrlRefused("url", "a provider URL carries no user info")
    scheme = parts.scheme.lower()
    if scheme != "https" and not (scheme == "http" and is_loopback(host)):
        raise UrlRefused(
            "scheme", "a provider URL is https (http only on 127.0.0.1, localhost or ::1)"
        )
    if port is None:
        port = 443 if scheme == "https" else 80
    if not host_allowed(host, port, allowed):
        raise UrlRefused("host_not_allowed", "the URL's host is not in provider_allowed_hosts")


def template_authority_problem(template: str) -> str | None:
    """Why a URL template's scheme, host or port would be substituted (``None``: they are
    literal). The scheme and authority are what precedes the first ``/``, ``?`` or ``#`` after
    ``//``."""
    scheme, sep, rest = template.partition("://")
    if not sep:
        return "the URL has no scheme"
    if "{" in scheme or "}" in scheme:
        return "a placeholder in the URL's scheme"
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "{" in authority or "}" in authority:
        return "a placeholder in the URL's host or port"
    return None


# -- substitution ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Context:
    """What a call's placeholders resolve to: the provider, the value, the binding, the scope
    key and, for a write, the effect. Secrets are read from the environment by ``lookup``."""

    provider: str
    value: str
    binding: Binding
    scope_key: str | None = None
    effect: Effect | None = None
    environ: Mapping[str, str] | None = None

    def lookup(self, text: str) -> Any:
        """The value of placeholder ``text``; ``ProviderUnavailable`` when the call cannot give
        it (a missing binding key or secret)."""
        found = placeholder(text, write=self.effect is not None)
        if found is None:
            raise ProviderUnavailable(self.provider, self.value, "placeholder")
        if found.kind == "binding":
            if found.name not in self.binding.external_ids:
                raise ProviderUnavailable(self.provider, self.value, "binding_missing")
            return self.binding.external_ids[found.name]
        if found.kind == "secret":
            environ = os.environ if self.environ is None else self.environ
            secret = environ.get(f"{SECRET_ENV_PREFIX}{found.name}")
            if not secret:
                raise ProviderUnavailable(self.provider, self.value, "secret_missing")
            return secret
        if text == "story.id":
            return self.binding.story_id
        if text == "value.scope_key":
            return self.scope_key if self.scope_key is not None else ""
        effect = self.effect
        assert effect is not None  # noqa: S101 - placeholder() admits effect.* for writes only
        if text == "effect.delta":
            return effect.delta
        if text == "effect.value":
            return effect.new_value
        return effect.idempotency_key


def as_text(value: Any) -> str:
    """A substituted value as text: a number in plain notation, a boolean as JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral() else str(value)
    return str(value)


def as_json(value: Any) -> Any:
    """A substituted value as a JSON value (a ``Decimal`` as an int or a float)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    return value


def substitute(template: str, context: Context, encode: Callable[[str], str] = str) -> str:
    """``template`` with every placeholder replaced by its value as text, through ``encode``."""
    return PLACEHOLDER.sub(lambda m: encode(as_text(context.lookup(m.group(1)))), template)


def _encoded(text: str) -> str:
    return quote(text, safe="")


def substitute_url(template: str, context: Context) -> str:
    """A URL template with each value percent-encoded (nothing kept safe)."""
    return substitute(template, context, _encoded)


def substitute_body(value: Any, context: Context) -> Any:
    """A JSON body template substituted (see the module docstring)."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value)
        if whole is not None:
            return as_json(context.lookup(whole.group(1)))
        return substitute(value, context)
    if isinstance(value, dict):
        return {key: substitute_body(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_body(item, context) for item in value]
    return value


def substitute_headers(headers: Mapping[str, str], context: Context) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, template in headers.items():
        text = substitute(template, context)
        if "\r" in text or "\n" in text:
            raise ProviderUnavailable(context.provider, context.value, "header_value")
        found[name] = text
    return found


# -- the adapter -----------------------------------------------------------------------------------


class HttpManifestProvider:
    """``ExternalProvider`` over a registered ``http`` manifest (see the module docstring).

    ``allowed_hosts`` is the deployment's ``provider_allowed_hosts`` as read when the provider
    was resolved (the start of the operation). ``transport`` replaces the HTTP transport (tests);
    ``environ`` the environment secrets are read from."""

    def __init__(
        self,
        row: ProviderRow,
        allowed_hosts: Sequence[str],
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.row = row
        self.allowed_hosts = tuple(allowed_hosts)
        self.transport = transport
        self.environ = environ

    @property
    def name(self) -> str:
        return self.row.name

    def _value(self, value: str) -> Mapping[str, Any]:
        found = self.row.values().get(value)
        if found is None:
            raise ProviderUnavailable(self.name, value, "unknown_value")
        return found

    async def _send(
        self, value: str, template: Mapping[str, Any], context: Context, extra: Mapping[str, str]
    ) -> httpx2.Response:
        url = substitute_url(str(template["url"]), context)
        try:
            check_url(url, self.allowed_hosts)
        except UrlRefused as refused:
            raise ProviderUnavailable(self.name, value, refused.reason) from None
        headers = {**substitute_headers(template.get("headers") or {}, context), **extra}
        query = template.get("query")
        params = {k: substitute(str(v), context) for k, v in query.items()} if query else None
        body = template.get("body")
        timeout = float(template.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        async with httpx2.AsyncClient(
            follow_redirects=False, timeout=timeout, transport=self.transport
        ) as client:
            return await client.request(
                str(template["method"]),
                url,
                params=params,
                headers=headers,
                json=None if body is None else substitute_body(body, context),
            )

    @staticmethod
    def _document(provider: str, value: str, response: httpx2.Response) -> Any:
        if response.is_redirect:
            raise ProviderUnavailable(provider, value, "redirect")
        if not response.is_success:
            raise ProviderUnavailable(provider, value, "status")
        try:
            return json.loads(response.content)
        except ValueError:
            raise ProviderUnavailable(provider, value, "not_json") from None

    async def read(self, value: str, binding: Binding, scope_key: str | None) -> ReadResult:
        declared = self._value(value)
        template = declared["read"]
        context = Context(self.name, value, binding, scope_key, environ=self.environ)
        try:
            response = await self._send(value, template, context, {})
        except httpx2.TimeoutException:
            log.warning("provider read timed out", extra={"provider": self.name, "value": value})
            raise ProviderUnavailable(self.name, value, "timeout") from None
        except httpx2.HTTPError:
            log.warning("provider read failed", extra={"provider": self.name, "value": value})
            raise ProviderUnavailable(self.name, value, "unreachable") from None
        document = self._document(self.name, value, response)
        try:
            found = extract(document, parse_path(str(template["value_path"])))
        except PathError, PathMissing:
            raise ProviderUnavailable(self.name, value, "path") from None
        return ReadResult(
            value=coerce(self.name, value, str(declared["type"]), found),
            at=datetime.now(UTC),
            raw_hash=raw_hash(response.content),
        )

    async def write(self, effect: Effect, binding: Binding) -> WriteOutcome:
        declared = self._value(effect.value)
        template = declared.get("write")
        if not self.row.write_capable or template is None:
            raise ProviderUnavailable(self.name, effect.value, "not_writable")
        context = Context(
            self.name, effect.value, binding, effect.scope_key, effect, environ=self.environ
        )
        idempotency = {str(template["idempotency_header"]): effect.idempotency_key}
        try:
            response = await self._send(effect.value, template, context, idempotency)
        except httpx2.HTTPError:
            # A timeout or a transport failure once the request may have left: ambiguous
            # (FR-EXT-16), never retried here.
            log.warning("provider write unanswered", extra={"provider": self.name})
            return WriteOutcome(status="unknown")
        digest = raw_hash(response.content)
        try:
            document = json.loads(response.content)
            outcome = extract(document, parse_path(str(template["outcome_path"])))
        except ValueError, PathError, PathMissing:
            return WriteOutcome(status="unknown", raw_hash=digest)
        mapping = template.get("outcome_map") or {}
        if outcome in (mapping.get("confirmed") or ()):
            status: Any = "confirmed"
        elif outcome in (mapping.get("failed") or ()):
            status = "failed"
        else:
            return WriteOutcome(status="unknown", raw_hash=digest)
        resulting: Mapping[str, Any] | None = None
        path = template.get("resulting_values_path")
        if status == "confirmed" and path:
            try:
                found = extract(document, parse_path(str(path)))
            except PathError, PathMissing:
                found = None
            resulting = found if isinstance(found, dict) else None
        return WriteOutcome(status=status, raw_hash=digest, resulting_values=resulting)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "LOOPBACK_HOSTS",
    "SECRET_ENV_PREFIX",
    "Context",
    "HttpManifestProvider",
    "PathError",
    "PathMissing",
    "UrlRefused",
    "allowed_entry",
    "as_json",
    "as_text",
    "check_url",
    "extract",
    "host_allowed",
    "is_loopback",
    "parse_path",
    "substitute",
    "substitute_body",
    "substitute_headers",
    "substitute_url",
    "template_authority_problem",
]
