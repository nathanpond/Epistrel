"""#682, #608: the OpenAPI document declares what the Engine does: ``?viewpoint=`` on the State and
scheduled-event reads (which take it, as their descriptions say), and ``POST /stories``' 404
``not_found`` for a provider binding naming an unregistered provider. #786: every
``OPERATION_ERRORS`` code is declared on its operation, the bundle routes' among them. #885: every
code a route's static walk can raise is declared on it, and every error class agrees with
``ERROR_STATUS``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from mypy.nodes import TypeInfo

from epistrel.api.routes.ownership import ownership_operation_ids
from epistrel.core.errors import EpistrelError, ProviderNotFound
from epistrel.core.ownership import REGISTRY
from tests import raisable_support

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


# -- #885: every code a route can raise is declared on it -----------------------------------------

E = "epistrel.engine.Engine"

NOT_RAISED: dict[tuple[str, str], str] = {
    ("epistrel.api.auth.require_principal", "epistrel.core.errors.PrincipalRequired"): (
        "raised on a mutating method only; `_rule_errors` declares it on each"
    ),
    (f"{E}._resume_staged", "epistrel.core.errors.TimelineChanged"): (
        "only a respond retry resumes a staged turn (`StagedOriginal`); respond declares it"
    ),
    (f"{E}.config_changes", "epistrel.core.errors.RevisionConflict"): (
        "checks the story's existence only: the expected revision passed is `None`"
    ),
    ("epistrel.api.routes.admin.settings.config_changes", "epistrel.core.errors.StoryNotFound"): (
        "the deployment's log names no story"
    ),
    (f"{E}._hold_render", "epistrel.core.errors.RewriteApplyInProgress"): (
        "cancels the session this call opened and has not answered: no apply can be running"
    ),
    (f"{E}._reconcile_run", "epistrel.core.errors.ErasureKeyMissing"): (
        "`_reapply_one` is handed to `erasure_ledger.reapply`, which keeps an entry whose "
        "reapply fails pending and raises nothing"
    ),
    (f"{E}.fork", "epistrel.core.errors.StoryArchived"): (
        "`reactivation.guard` refuses an archived story for every edit but `Edit.FORK`"
    ),
    (f"{E}.request_catalog_images", "epistrel.core.errors.NotPublished"): (
        "resolves the entry's draft (`draft=True`), never a published version"
    ),
    (f"{E}._build_story_image_prompt", "epistrel.core.errors.SubjectsRequired"): (
        "`place_at` refuses undeclared subjects only for an upload (`subjects_declared=False`)"
    ),
}
"""Raises the walk finds that the function (by full name) never raises or passes on, each with
why: a branch the walk cannot rule out. An entry the walk no longer meets fails the test."""

DECLARED_AS = {"epistrel.core.errors.ConfigConflict": "config_conflict"}
"""Error classes whose ``code`` another class shares under another status: the name
``ERROR_STATUS`` documents them by."""

UNWALKED = frozenset(
    {
        # The ownership routes reach the Engine through ``**values: Any`` (``_bind``).
        *(
            operation_id
            for entity_type in REGISTRY
            for operation_id in ownership_operation_ids(entity_type)
        ),
    }
)
"""Protected operations whose walk reaches no Engine method: the guard cannot see what they
raise."""


@pytest.fixture(scope="module")
def walked() -> raisable_support.Raisable:
    """The static walk (``tests.raisable_support``; about 30 s)."""
    return raisable_support.raisable(NOT_RAISED)


def _declared_as(error: type[EpistrelError]) -> str:
    return DECLARED_AS.get(f"{error.__module__}.{error.__qualname__}", error.code)


def _route_raises(
    walked: raisable_support.Raisable,
) -> Iterator[tuple[str, APIRoute, dict[TypeInfo, raisable_support.Path]]]:
    """Each protected route's operation id, route and what its walk finds (endpoint and every
    dependency, the router's included)."""
    from epistrel.api import create_app
    from epistrel.api.openapi import protected_operations
    from epistrel.config import Settings
    from epistrel.tools.openapi import OFFLINE_SETTINGS

    app = create_app(Settings.model_validate(OFFLINE_SETTINGS))
    for _method, _path, route in protected_operations(app):
        found: dict[TypeInfo, raisable_support.Path] = {}
        stack = [route.dependant]
        while stack:
            dependant = stack.pop()
            if dependant.call is not None:
                for info, path in walked.of(dependant.call).items():
                    found.setdefault(info, path)
            stack += dependant.dependencies
        yield str(route.operation_id or route.unique_id), route, found


def test_every_raisable_code_is_declared(
    generated: dict[str, Any], walked: raisable_support.Raisable
) -> None:
    """#885: every error a route's static walk can raise (``tests.raisable_support``, whose
    docstring states what the walk does not see) is declared on the route in the generated
    document, under its class's status. ``NOT_RAISED`` lists the raises the walk cannot rule
    out, each with its reason; every entry must still be met. ``UNWALKED`` lists the routes the
    walk cannot follow into the Engine, both ways."""
    operations = _operations(generated)
    missing = []
    unwalked = set()
    for operation_id, route, found in _route_raises(walked):
        if not route.include_in_schema:
            continue
        responses = operations[operation_id]["responses"]
        for info, path in found.items():
            error = raisable_support.error_class(info)
            code = _declared_as(error)
            declared = responses.get(str(error.http_status), {}).get("description", "")
            if f"`{code}`" not in declared:
                missing.append((operation_id, error.http_status, code, " -> ".join(path)))
        if not any(name.startswith(f"{E}.") for name in walked.reaches(route.endpoint)):
            unwalked.add(operation_id)
    assert not missing, "undeclared:\n" + "\n".join(map(str, sorted(set(missing))))
    assert walked.exempted == set(NOT_RAISED), set(NOT_RAISED) - walked.exempted
    assert unwalked == UNWALKED


def test_error_classes_agree_with_error_status(walked: raisable_support.Raisable) -> None:
    """#885: every ``EpistrelError`` subclass whose code ``ERROR_STATUS`` documents answers the
    status documented for it, so a declaration under that status describes the class."""
    from epistrel.api.openapi import ERROR_STATUS

    disagree = []
    for info in walked.errors:
        error = raisable_support.error_class(info)
        code = _declared_as(error)
        if code in ERROR_STATUS and ERROR_STATUS[code] != error.http_status:
            disagree.append((info.fullname, code, error.http_status, ERROR_STATUS[code]))
    assert disagree == []
