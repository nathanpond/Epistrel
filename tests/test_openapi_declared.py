"""#682, #608: the OpenAPI document declares what the Engine does: ``?viewpoint=`` on the State and
scheduled-event reads (which take it, as their descriptions say), and ``POST /stories``' 404
``not_found`` for a provider binding naming an unregistered provider."""

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
