"""FR-API-6's route table (#128): the committed list of endpoints the requirement names, and the
check of an app against it (``test_c_15__api_surface``).

``tests/fixtures/api/fr_api_6_routes.json`` lists FR-API-6 (docs/03) item by item as
``(method, path template, present | not_yet, milestone)``; rows for later milestones are
``not_yet`` at docs/02 §9's paths. ``extras`` are served routes beside FR-API-6's items (a read
or the undo of one of them) and ``outside`` the served routes FR-API-6 does not name, each with
the requirement it serves. The check runs both ways: every ``present`` row is served with its
method, no ``not_yet`` row is, and every served route is a row, an extra or an outside route,
so a route added without a place in the table fails as surely as a row whose route was
removed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from epistrel.core.tables.ownership import EntityType
from tests.conftest import REPO_ROOT

TABLE_PATH = REPO_ROOT / "tests" / "fixtures" / "api" / "fr_api_6_routes.json"
CURRENT = "M3"
"""The milestone this table was checked at: every ``present`` row is delivered by it, every
``not_yet`` row by a later one."""

ITEMS = (
    "characters",
    "scenarios",
    "media",
    "personas",
    "stories",
    "engine profiles",
    "per story",
    "ownership",
    "principals",
    "content ratings",
    "per catalog entry",
    "images",
    "operations",
    "media delivery",
    "message feedback",
    "erasure",
)
"""FR-API-6's groups, in its order (plus the erasure routes #128's AC names): each has a row."""

OWNED_PATHS = {
    EntityType.STORY: "/stories/{story_id}",
    EntityType.CHARACTER: "/catalog/characters/{character_id}",
    EntityType.SCENARIO: "/catalog/scenarios/{scenario_id}",
    EntityType.PERSONA: "/personas/{persona_id}",
    EntityType.MEDIA: "/media/{media_id}",
    EntityType.ENGINE_PROFILE: "/engine-profiles/{profile_id}",
}
"""Where each owned type lives: "for every owned type, transfer the owner and add or remove
relations"."""

OWNERSHIP_ROUTES = (
    ("PUT", "{}/owner"),
    ("POST", "{}/relations"),
    ("DELETE", "{}/relations/{{relation}}/{{principal}}"),
)


@dataclass(frozen=True)
class Row:
    item: str
    method: str
    path: str
    status: str
    milestone: str

    @property
    def route(self) -> tuple[str, str]:
        return self.method, self.path


def load_table() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    return loaded


def rows(table: dict[str, Any]) -> list[Row]:
    return [
        Row(r["item"], r["method"], r["path"], r["status"], r["milestone"]) for r in table["routes"]
    ]


CONVERTER = re.compile(r"\{(\w+):[^}]+\}")
"""A path parameter's Starlette converter (``{principal:path}``), which OpenAPI omits."""


def served_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every ``(method, path template)`` the app serves (HEAD aside), as OpenAPI writes it."""
    found: set[tuple[str, str]] = set()
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        for method in context.methods or ():
            if method != "HEAD":
                found.add((method, CONVERTER.sub(r"{\1}", context.path or "")))
    return found


def _milestone(value: str) -> int:
    assert value.startswith("M"), value
    return int(value[1:])


def surface_problems(app: FastAPI, table: dict[str, Any]) -> list[str]:
    """Every way ``app`` and the table disagree (empty: they agree)."""
    served = served_routes(app)
    found = rows(table)
    problems: list[str] = []
    for row in found:
        if row.status == "present":
            if _milestone(row.milestone) > _milestone(CURRENT):
                problems.append(f"present row from a later milestone: {row}")
            if row.route not in served:
                problems.append(f"not served: {row.method} {row.path} ({row.item})")
        elif row.status == "not_yet":
            if _milestone(row.milestone) <= _milestone(CURRENT):
                problems.append(f"not_yet row without a later milestone: {row}")
            if row.route in served:
                problems.append(f"served, but listed not_yet: {row.method} {row.path}")
        else:
            problems.append(f"unknown status {row.status!r}: {row}")
    listed = {row.route for row in found if row.status == "present"}
    beside = _routes(table["extras"]) | _routes(table["outside"])
    problems += [f"extra or outside route not served: {m} {p}" for m, p in sorted(beside - served)]
    problems += [f"listed twice: {m} {p}" for m, p in sorted(listed & beside)]
    problems += [f"served, but in no list: {m} {p}" for m, p in sorted(served - listed - beside)]
    groups = {row.item.split(" — ")[0].split(" (")[0] for row in found}
    problems += [f"no row for FR-API-6's {item!r}" for item in ITEMS if item not in groups]
    for entity, base in OWNED_PATHS.items():
        for method, template in OWNERSHIP_ROUTES:
            if (method, template.format(base)) not in listed:
                problems.append(f"no ownership row: {method} {template.format(base)} ({entity})")
    return problems


def _routes(entries: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(e["method"], e["path"]) for e in entries}


__all__ = [
    "CURRENT",
    "ITEMS",
    "OWNED_PATHS",
    "TABLE_PATH",
    "Row",
    "load_table",
    "rows",
    "served_routes",
    "surface_problems",
]
