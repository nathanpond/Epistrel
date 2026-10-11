"""Generate the committed OpenAPI document (``docs/openapi.json``) from the app (FR-API-1,
NFR-MAINT-2; #73).

    uv run python -m epistrel.tools.openapi --check   # exit 1 (with a diff) when it is stale
    uv run python -m epistrel.tools.openapi --write   # regenerate it

The app is built offline: ``create_app`` with stub settings (a placeholder database URL that is
never connected to, a placeholder credential, ``api`` mode, the stub adapter), so the document
needs no database, no environment and no network. It is exactly what a running server serves at
``/openapi.json`` (``epistrel.api.openapi.build``), written with sorted keys and a two-space
indent. CI runs ``--check`` in the ``lint`` job, byte for byte.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from epistrel.tools.generated import REPO_ROOT
from epistrel.tools.generated import main as generated_main

SCHEMA_PATH = REPO_ROOT / "docs" / "openapi.json"
PROG = "epistrel.tools.openapi"

OFFLINE_SETTINGS: dict[str, Any] = {
    "database_url": "postgresql+psycopg://offline:offline@localhost:5432/offline",
    "service_credentials": ["offline-schema-build-placeholder-credential"],
    "mode": "api",
    "model_adapter": "stub",
}
"""Settings for the offline build: nothing here is connected to or authenticated against."""


def schema() -> dict[str, Any]:
    """The app's OpenAPI document, built offline."""
    from epistrel.api import create_app
    from epistrel.config import Settings

    app = create_app(Settings.model_validate(OFFLINE_SETTINGS))
    return app.openapi()


def render() -> str:
    from epistrel.api.openapi import dumps

    return dumps(schema())


def main(argv: Sequence[str] | None = None) -> int:
    return generated_main(
        argv,
        prog=PROG,
        description="Generate docs/openapi.json from the app.",
        default_path=SCHEMA_PATH,
        generate=render,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
