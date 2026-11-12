"""#682, #608: the OpenAPI document declares what the Engine does: ``?viewpoint=`` on the State and
scheduled-event reads (which take it, as their descriptions say), and ``POST /stories``' 404
``not_found`` for a provider binding naming an unregistered provider. #786: every
``OPERATION_ERRORS`` code is declared on its operation, the bundle routes' among them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from epistrel.core.errors import ProviderNotFound

DOC = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


@pytest.fixture(scope="module")
def doc() -> dict[str, Any]:
    found: dict[str, Any] = json.loads(DOC.read_text())
    return found


@pytest.mark.parametrize(
    "path",
    [
        "/stories/{story_id}/states",
        "/stories/{story_id}/states/{name}",
        "/stories/{story_id}/scheduled-events",
    ],
)
def test_a_viewpoint_read_declares_the_query(doc: dict[str, Any], path: str) -> None:
    parameters = doc["paths"][path]["get"]["parameters"]
    (viewpoint,) = [p for p in parameters if p["name"] == "viewpoint"]
    assert viewpoint["in"] == "query"


def test_story_creation_declares_the_unregistered_provider(doc: dict[str, Any]) -> None:
    assert (ProviderNotFound.http_status, ProviderNotFound.code) == (404, "not_found")
    assert "not_found" in doc["paths"]["/stories"]["post"]["responses"]["404"]["description"]


def test_respond_declares_module_off(doc: dict[str, Any]) -> None:
    """#608: respond's 409 lists ``module_off`` (a multi-primary override on a ``combined`` or
    Lite story), and ``AdjudicationBody`` names its ``state`` rule."""
    conflict = doc["paths"]["/stories/{story_id}/respond"]["post"]["responses"]["409"]
    assert "`module_off`" in conflict["description"]
    adjudication = doc["components"]["schemas"]["AdjudicationBody"]
    assert "``state``" in adjudication["description"]
    assert adjudication["properties"]["rule"]["enum"] == ["request_order", "director", "state"]


@pytest.fixture(scope="module")
def generated() -> dict[str, Any]:
    """The document the app builds now (``epistrel.tools.openapi.schema``)."""
    from epistrel.tools.openapi import schema

    return schema()


def _operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each operation of ``document`` by its ``operationId``."""
    return {
        operation["operationId"]: operation
        for item in document["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


def test_every_operation_error_is_declared(generated: dict[str, Any]) -> None:
    """#786: every code ``OPERATION_ERRORS`` lists for an operation is declared on it in the
    generated document, under the status ``ERROR_STATUS`` maps it to."""
    from epistrel.api.openapi import ERROR_STATUS, OPERATION_ERRORS

    operations = _operations(generated)
    missing = []
    for operation_id, codes in OPERATION_ERRORS.items():
        assert operation_id in operations, operation_id
        responses = operations[operation_id]["responses"]
        for code in codes:
            status = str(ERROR_STATUS[code])
            declared = responses.get(status, {}).get("description", "")
            if f"`{code}`" not in declared:
                missing.append((operation_id, status, code))
    assert missing == []


@pytest.mark.parametrize(
    ("path", "method", "status", "code"),
    [
        ("/admin/stories/import", "post", "413", "bundle_too_large"),
        ("/admin/stories/import", "post", "422", "corrupt_bundle"),
        ("/admin/stories/import", "post", "422", "unsupported_format_version"),
        ("/admin/stories/import", "post", "422", "rating_required"),
        ("/admin/stories/exports/{job_id}/bundle", "get", "409", "export_not_ready"),
        ("/admin/stories/exports/{job_id}/bundle", "get", "410", "export_expired"),
        ("/admin/stories/exports/{job_id}/bundle", "get", "404", "job_not_found"),
    ],
)
def test_bundle_refusals_are_declared(
    doc: dict[str, Any], path: str, method: str, status: str, code: str
) -> None:
    """#786: the committed document declares the import's 413 and the export bundle's 409/410
    (Console#333), each with the error envelope and its code listed."""
    declared = doc["paths"][path][method]["responses"][status]
    assert f"`{code}`" in declared["description"]
    assert declared["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Error"
    }
