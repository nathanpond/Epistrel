"""Holding one live answer to the committed OpenAPI document (``docs/openapi.json``).

C-01 generates requests over the whole document; the helpers here check one answer a test
produced on purpose (a replayed ingest, a refused read) against the operation it came from: the
status is declared, an error's code is listed under it, and the body validates against the
declared schema (schemathesis' response conformance, as C-01 uses it).
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import schemathesis

from tests.conftest import REPO_ROOT

COMMITTED: dict[str, Any] = json.loads(
    (REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8")
)
SCHEMA = schemathesis.openapi.from_dict(COMMITTED)


def declared(method: str, path: str, status: int) -> dict[str, Any]:
    """The committed response object of ``method path`` for ``status`` (a test failure when the
    operation does not declare it)."""
    responses: dict[str, Any] = COMMITTED["paths"][path][method.lower()]["responses"]
    assert str(status) in responses, (method, path, status, sorted(responses))
    found: dict[str, Any] = responses[str(status)]
    return found


def lists_code(method: str, path: str, status: int, code: str) -> bool:
    """Whether the committed ``status`` response of ``method path`` names the error ``code``."""
    return f"`{code}`" in declared(method, path, status)["description"]


def assert_documented(response: httpx.Response, method: str, path: str) -> None:
    """``response`` is a declared answer of ``method path``: its status is declared, an error's
    code is listed under that status, and its body validates against the declared schema."""
    declared(method, path, response.status_code)
    if response.status_code >= 400:
        code = response.json()["error"]["code"]
        assert lists_code(method, path, response.status_code, code), (code, response.text)
    SCHEMA[path][method.upper()].validate_response(response)
