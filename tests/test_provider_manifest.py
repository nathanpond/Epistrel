"""The declarative provider manifest's pure rules (#204): the ``$``/``.key``/``[n]`` path
subset, template substitution (percent-encoding in URLs, typed values in JSON bodies), the
https-or-loopback and allowed-hosts rules, and the manifest validation that refuses with 422
``invalid_manifest`` at the offending member."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from epistrel.core import providers as core_providers
from epistrel.core.errors import InvalidManifest, InvalidRequest
from epistrel.orchestrator.providers import http_manifest as hm
from epistrel.orchestrator.providers.manifest import ProviderSpec, validate_spec
from epistrel.orchestrator.providers.port import Binding, Effect, ProviderUnavailable
from tests import provider_support as ps

BASE = "https://api.bank.test"
ALLOWED = ["api.bank.test"]


# -- the path subset ------------------------------------------------------------------------------


def test_the_path_subset_parses_dollar_keys_and_indexes() -> None:
    assert hm.parse_path("$") == ()
    assert hm.parse_path("$.balance") == ("balance",)
    assert hm.parse_path("$.data[0].amount_due") == ("data", 0, "amount_due")
    assert hm.parse_path("$[2][10].a-b") == (2, 10, "a-b")
    document = {"data": [{"amount_due": 7}], "x": None}
    assert hm.extract(document, hm.parse_path("$.data[0].amount_due")) == 7
    assert hm.extract(document, hm.parse_path("$")) == document
    assert hm.extract(document, hm.parse_path("$.x")) is None
    for missing in ("$.data[1]", "$.nope", "$.data.amount_due", "$.x.y"):
        with pytest.raises(hm.PathMissing):
            hm.extract(document, hm.parse_path(missing))


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "balance",
        "$balance",
        "$.data[*]",
        "$.*",
        "$..balance",
        "$.data[0:2]",
        "$.data[-1]",
        "$.data[?(@.x > 1)]",
        "$['balance']",
        "$.data[01]",
        "$.",
        "$.1abc",
    ],
)
def test_the_path_subset_refuses_everything_else(expression: str) -> None:
    with pytest.raises(hm.PathError):
        hm.parse_path(expression)


# -- substitution ---------------------------------------------------------------------------------


def _context(**external_ids: str) -> hm.Context:
    effect = Effect("gold", "01KEY", delta=Decimal(-50), new_value=Decimal("12.5"))
    binding = Binding("01STORY", "bank", external_ids)
    return hm.Context(
        "bank", "gold", binding, "char-1", effect, environ={"EPISTREL_PROVIDER_SECRET_TOKEN": "s3"}
    )


def test_a_json_body_keeps_a_lone_placeholder_typed_and_interpolates_the_rest() -> None:
    """A string that is exactly one placeholder becomes the typed value (a number stays a
    number); a placeholder inside a longer string is text."""
    body = {
        "amount": "{effect.delta}",
        "set": "{effect.value}",
        "note": "spend {effect.delta} for {binding.player}",
        "key": "{effect.idempotency_key}",
        "nested": [{"story": "{story.id}", "who": "{value.scope_key}"}, 3, True, None],
    }
    found = hm.substitute_body(body, _context(player="p/1"))
    assert found == {
        "amount": -50,
        "set": 12.5,
        "note": "spend -50 for p/1",
        "key": "01KEY",
        "nested": [{"story": "01STORY", "who": "char-1"}, 3, True, None],
    }
    assert isinstance(found["amount"], int)


def test_a_url_encodes_every_substituted_value() -> None:
    """``/``, ``?``, ``#``, ``@`` and spaces in an id are percent-encoded, so the URL keeps its
    host and path shape; secrets resolve from the environment, and a missing one or a missing
    binding key is an adapter error."""
    template = f"{BASE}/players/{{binding.player}}/gold?ref={{binding.ref}}"
    url = hm.substitute_url(template, _context(player="../x?y=1#@evil.test", ref="a b&c"))
    assert url == (f"{BASE}/players/..%2Fx%3Fy%3D1%23%40evil.test/gold?ref=a%20b%26c")
    hm.check_url(url, ALLOWED)
    assert hm.substitute("Bearer {secret.TOKEN}", _context()) == "Bearer s3"
    with pytest.raises(ProviderUnavailable) as missing:
        hm.substitute("{secret.OTHER}", _context())
    assert missing.value.reason == "secret_missing"
    with pytest.raises(ProviderUnavailable) as unbound:
        hm.substitute("{binding.player}", _context())
    assert unbound.value.reason == "binding_missing"
    with pytest.raises(ProviderUnavailable) as split:
        hm.substitute_headers({"X-Id": "{binding.player}"}, _context(player="a\r\nX-Evil: 1"))
    assert split.value.reason == "header_value"


def test_the_https_and_allowed_hosts_rules() -> None:
    hm.check_url("https://api.bank.test/x", ALLOWED)
    hm.check_url("https://API.bank.test:8443/x", ALLOWED)  # a bare entry allows any port
    hm.check_url("http://127.0.0.1:9000/x", ["127.0.0.1"])
    hm.check_url("http://localhost:9000/x", ["localhost:9000"])
    hm.check_url("http://[::1]:9000/x", ["[::1]:9000"])
    refused = {
        "http://api.bank.test/x": "scheme",
        "ftp://api.bank.test/x": "scheme",
        "https://evil.test/x": "host_not_allowed",
        "https://api.bank.test.evil.test/x": "host_not_allowed",
        "https://user@api.bank.test/x": "url",
        "https:///x": "url",
    }
    for url, reason in refused.items():
        with pytest.raises(hm.UrlRefused) as caught:
            hm.check_url(url, ALLOWED)
        assert caught.value.reason == reason, url
    with pytest.raises(hm.UrlRefused) as port:
        hm.check_url("http://127.0.0.1:9001/x", ["127.0.0.1:9000"])
    assert port.value.reason == "host_not_allowed"
    with pytest.raises(hm.UrlRefused) as empty:
        hm.check_url("https://api.bank.test/x", [])
    assert empty.value.reason == "host_not_allowed"


# -- the manifest -------------------------------------------------------------------------------


def _refusal(body: dict[str, Any], allowed: list[str] | None = None) -> tuple[str, str]:
    with pytest.raises(InvalidRequest) as refused:
        validate_spec(ProviderSpec.model_validate(body), ALLOWED if allowed is None else allowed)
    error = refused.value
    return error.code, f"{error.details['field']}|{error.details.get('reason', '')}"


def _read(**fields: Any) -> dict[str, Any]:
    return ps.read_template(BASE, **fields)


def test_a_valid_manifest_passes_and_binding_keys_are_found() -> None:
    body = ps.http_spec(
        BASE,
        read=_read(headers={"Authorization": "Bearer {secret.BANK_TOKEN}"}),
        write=ps.write_template(BASE),
    )
    validate_spec(ProviderSpec.model_validate(body), ALLOWED)
    assert core_providers.binding_keys(body["manifest"]) == {"player"}
    validate_spec(ProviderSpec.model_validate(ps.in_process_spec(writable=["gold"])), [])


def test_the_manifest_refusals() -> None:
    """Each rule refuses with ``invalid_manifest`` at its member."""
    values = "body.manifest.values.gold"
    cases: list[tuple[dict[str, Any], str]] = [
        (
            ps.http_spec(BASE, read=_read(path="/p/{binding.player}/{oops}")),
            f"{values}.read.url|unknown_placeholder",
        ),
        (
            ps.http_spec(BASE, read=_read(path="/p/{effect.delta}")),
            f"{values}.read.url|unknown_placeholder",
        ),
        (
            ps.http_spec(BASE, read=_read(headers={"X": "{secret.lower}"})),
            f"{values}.read.headers.X|unknown_placeholder",
        ),
        (
            ps.http_spec("https://{binding.host}.bank.test"),
            f"{values}.read.url|placeholder_in_authority",
        ),
        (
            ps.http_spec("https://api.bank.test:{binding.port}"),
            f"{values}.read.url|placeholder_in_authority",
        ),
        (
            ps.http_spec("{binding.scheme}://api.bank.test"),
            f"{values}.read.url|placeholder_in_authority",
        ),
        (ps.http_spec("https://evil.test"), f"{values}.read.url|host_not_allowed"),
        (ps.http_spec("http://api.bank.test"), f"{values}.read.url|scheme"),
        (
            ps.http_spec(BASE, read=_read(value_path="$.items[?(@.id==1)].balance")),
            f"{values}.read.value_path|path_expression",
        ),
        (
            ps.http_spec(BASE, read=_read(value_path="$..balance")),
            f"{values}.read.value_path|path_expression",
        ),
        (
            {
                **ps.http_spec(BASE),
                "manifest": {
                    "values": {
                        "gold": {
                            "type": "number",
                            "read": _read(),
                            "write": ps.write_template(BASE),
                        }
                    }
                },
            },
            f"{values}.write|not_write_capable",
        ),
        (
            ps.in_process_spec(
                writable=["gold"],
                write_capable=False,
                confirmation=None,
                dedupes_dispatch=None,
                returns_resulting_values=None,
            ),
            f"{values}.writable|not_write_capable",
        ),
    ]
    for body, expected in cases:
        assert _refusal(body) == ("invalid_manifest", expected), expected
    # An empty allowed-hosts list refuses every HTTP provider.
    assert _refusal(ps.http_spec(BASE), allowed=[]) == (
        "invalid_manifest",
        f"{values}.read.url|host_not_allowed",
    )
    # A shape error is invalid_manifest at its member too (strict models).
    code, where = _refusal(
        {**ps.in_process_spec(), "manifest": {"values": {"gold": {"type": "money"}}}}
    )
    assert (code, where.split("|")[0]) == ("invalid_manifest", f"{values}.type")
    code, where = _refusal(
        {
            **ps.http_spec(BASE),
            "manifest": {"values": {"gold": {"type": "number", "read": {**_read(), "retries": 3}}}},
        }
    )
    assert (code, where.split("|")[0]) == ("invalid_manifest", f"{values}.read.retries")


def test_write_capability_declarations() -> None:
    """A write-capable provider declares confirmation, dedupe and resulting values; a read-only
    one declares none (422 ``validation_error``)."""
    assert _refusal({**ps.in_process_spec(writable=["gold"]), "confirmation": None}) == (
        "validation_error",
        "body.confirmation|required",
    )
    assert _refusal({**ps.in_process_spec(), "dedupes_dispatch": True}) == (
        "validation_error",
        "body.dedupes_dispatch|not_write_capable",
    )


def test_placeholder_grammar() -> None:
    assert core_providers.placeholder("binding.player_id") is not None
    assert core_providers.placeholder("secret.API_KEY") is not None
    assert core_providers.placeholder("story.id") is not None
    assert core_providers.placeholder("effect.delta") is None
    assert core_providers.placeholder("effect.delta", write=True) is not None
    for unknown in ("binding", "binding.", "binding.a-b", "secret.lower", "story.name", "x.y", ""):
        assert core_providers.placeholder(unknown, write=True) is None, unknown


def test_invalid_manifest_is_a_validation_error_subtype() -> None:
    assert issubclass(InvalidManifest, InvalidRequest)
    assert (InvalidManifest.code, InvalidManifest.http_status) == ("invalid_manifest", 422)
