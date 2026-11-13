"""The story bundle's JSON Schema (FR-STORE-7; #704): one schema per line kind, published in the
OpenAPI document's ``components.schemas``.

- ``StoryBundleManifest``: ``manifest.json`` (``FORMAT_VERSION``, the source story and revision,
  the content rating and engine profile, the files with their checksums, the counts);
- ``StoryBundleEvent``: one line of ``events.jsonl`` (an ``events`` row; its ``payload`` is
  validated by #45's registry for its ``type``, not by a duplicate schema);
- ``StoryBundleMedia``: one line of ``media.jsonl``;
- ``StoryBundleRecord_<table>``: one line of ``records/<table>.jsonl`` or
  ``catalog/<table>.jsonl`` (``records/story.json`` is ``StoryBundleRecord_stories``), built
  from the table's columns: a ULID ``char(26)`` is a string of that pattern, a timestamp a
  ``date-time`` string, ``jsonb`` unconstrained, a number or a boolean as such, an
  ``int8range`` an object of ``lower``, ``upper``, ``bounds`` and ``empty`` (#877), every
  nullable column ``[T, null]``, and no other property (``additionalProperties: false``); the
  columns a bundle never carries (``COLUMNS_NOT_EXPORTED``) are left out. A column is required
  when the row could not be written without it (``NOT NULL`` with no default); an export
  writes them all.

````validate_line`` checks one decoded line against its schema (tests and #250's import), with a
small checker for the subset of JSON Schema these schemas use (no new dependency).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Table,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, INT8RANGE, JSONB
from sqlalchemy.types import TIMESTAMP, Enum

from epistrel.core import bundles
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.schema import metadata

MANIFEST = "StoryBundleManifest"
EVENT = "StoryBundleEvent"
MEDIA = "StoryBundleMedia"
RECORD = "StoryBundleRecord_{table}"


def _range_schema() -> dict[str, Any]:
    """#877: an ``int8range`` column's value (``bundles.RANGE_FIELDS``)."""
    bound: dict[str, Any] = {"type": ["integer", "null"]}
    return {
        "type": "object",
        "properties": {
            "lower": bound,
            "upper": bound,
            "bounds": {"type": "string", "enum": ["[)", "[]", "(]", "()"]},
            "empty": {"type": "boolean"},
        },
        "required": list(bundles.RANGE_FIELDS),
        "additionalProperties": False,
    }


def _column_schema(column: Any) -> dict[str, Any]:
    kind = column.type
    found: dict[str, Any]
    if isinstance(kind, JSONB) or kind.__class__.__name__ in ("JSON", "JSONB"):
        found = {}
    elif isinstance(kind, TIMESTAMP):
        found = {"type": "string", "format": "date-time"}
    elif isinstance(kind, Date):
        found = {"type": "string", "format": "date"}
    elif isinstance(kind, Boolean):
        found = {"type": "boolean"}
    elif isinstance(kind, SmallInteger | Integer | BigInteger):
        found = {"type": "integer"}
    elif isinstance(kind, Numeric):
        found = {"type": "string"}  # a Decimal travels as a string
    elif isinstance(kind, Float):
        found = {"type": "number"}
    elif isinstance(kind, LargeBinary):
        found = {"type": "string", "contentEncoding": "base64"}
    elif isinstance(kind, ARRAY):
        found = {"type": "array"}
    elif isinstance(kind, ENUM | Enum):
        found = {"type": "string", "enum": sorted(str(e) for e in kind.enums)}
    elif isinstance(kind, INT8RANGE):
        found = _range_schema()
    else:
        found = {"type": "string"}
        length = getattr(kind, "length", None)
        if length == 26 and column.name.endswith(("id", "_ref")):
            found["pattern"] = ULID_PATTERN
    if column.nullable and "type" in found:
        found = {**found, "type": [found["type"], "null"]}
        if "enum" in found:
            found["enum"] = [*found["enum"], None]
    return found


def record_schema(table: Table) -> dict[str, Any]:
    """One table's line schema (see the module docstring)."""
    dropped = bundles.COLUMNS_NOT_EXPORTED.get(table.name, frozenset())
    columns = [c for c in table.c if c.name not in dropped and c.computed is None]
    return {
        "title": RECORD.format(table=table.name),
        "description": f"One line of a story bundle's `records/{table.name}.jsonl` (or "
        f"`catalog/{table.name}.jsonl`): a `{table.name}` row as exported (FR-STORE-7).",
        "type": "object",
        "properties": {c.name: _column_schema(c) for c in columns},
        "required": sorted(
            c.name
            for c in columns
            if not c.nullable and c.server_default is None and c.default is None
        ),
        "additionalProperties": False,
    }


def _manifest_schema() -> dict[str, Any]:
    nullable_object: dict[str, Any] = {"type": ["object", "null"]}
    return {
        "title": MANIFEST,
        "description": "A story bundle's `manifest.json`: its format version, the source story "
        "and revision, the content rating and engine profile, every member with its SHA-256, and "
        "the counts (FR-STORE-7).",
        "type": "object",
        "properties": {
            "format_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
            "schema_version": {"type": "string"},
            "source_story_id": {"type": "string", "pattern": ULID_PATTERN},
            "exported_revision": {"type": "integer"},
            "config_revision": {"type": "integer"},
            "exported_at": {"type": "string", "format": "date-time"},
            "exported_by": {"type": "string"},
            "content_rating": nullable_object,
            "engine_profile": nullable_object,
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "size": {"type": "integer"},
                        "records": {"type": ["integer", "null"]},
                    },
                    "required": ["path", "sha256", "size", "records"],
                    "additionalProperties": False,
                },
            },
            "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
        "required": [
            "format_version",
            "schema_version",
            "source_story_id",
            "exported_revision",
            "exported_at",
            "exported_by",
            "files",
            "counts",
        ],
        "additionalProperties": False,
    }


def _media_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {name: {} for name in bundles.MEDIA_FIELDS if name != "checksum"}
    properties["id"] = {"type": "string", "pattern": ULID_PATTERN}
    properties["sha256"] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    return {
        "title": MEDIA,
        "description": "One line of a story bundle's `media.jsonl`: a media asset the story's "
        "rows name, its bytes the member `media/<sha256>` (FR-STORE-7).",
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


def bundle_schemas() -> dict[str, dict[str, Any]]:
    """Every bundle line schema by component name (see the module docstring)."""
    found: dict[str, dict[str, Any]] = {
        MANIFEST: _manifest_schema(),
        EVENT: {
            **record_schema(metadata.tables["events"]),
            "title": EVENT,
            "description": "One line of a story bundle's `events.jsonl`: an event as recorded, "
            "its payload valid for its type (FR-STORE-7).",
        },
        MEDIA: _media_schema(),
    }
    tables = [
        metadata.tables["stories"],
        metadata.tables["operations"],
        *bundles.catalog_tables(),
        *bundles.exported_tables(),
    ]
    ledger = metadata.tables.get("erasure_ledger")
    if ledger is not None:
        tables.append(ledger)
    for table in tables:
        found[RECORD.format(table=table.name)] = record_schema(table)
    return found


def schema_for(path: str) -> str | None:
    """The component name of a member's lines (``None``: a media blob)."""
    if path == "manifest.json":
        return MANIFEST
    if path == "events.jsonl":
        return EVENT
    if path == "media.jsonl":
        return MEDIA
    if path == "records/story.json":
        return RECORD.format(table="stories")
    for prefix in ("records/", "catalog/"):
        if path.startswith(prefix) and path.endswith(".jsonl"):
            return RECORD.format(table=path.removeprefix(prefix).removesuffix(".jsonl"))
    return None


class LineInvalid(ValueError):
    """A bundle line that does not hold to its schema: the member, the JSON path and why."""


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _check(schema: dict[str, Any], value: Any, where: str) -> None:
    """The subset of JSON Schema the bundle schemas use: ``type`` (one or a list), ``enum``,
    ``pattern``, ``properties``, ``required``, ``additionalProperties`` (``false`` or a
    schema) and ``items``. No dependency: the import validates every line with it."""
    import re

    wanted = schema.get("type")
    if wanted is not None:
        kinds = wanted if isinstance(wanted, list) else [wanted]
        if not any(
            isinstance(value, _TYPES[k])
            and not (k in ("integer", "number") and value is True)
            and not (k in ("integer", "number") and value is False)
            for k in kinds
        ):
            raise LineInvalid(f"{where}: expected {wanted}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise LineInvalid(f"{where}: not one of the allowed values")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        raise LineInvalid(f"{where}: does not match {schema['pattern']}")
    if isinstance(value, dict):
        properties: dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise LineInvalid(f"{where}: missing {name}")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                _check(properties[name], item, f"{where}.{name}")
            elif extra is False:
                raise LineInvalid(f"{where}: unexpected {name}")
            elif isinstance(extra, dict):
                _check(extra, item, f"{where}.{name}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for n, item in enumerate(value):
            _check(schema["items"], item, f"{where}[{n}]")


def validate_line(path: str, line: Any) -> None:
    """Validate one decoded line of member ``path`` against its schema (``LineInvalid`` when
    it does not hold)."""
    name = schema_for(path)
    if name is None:
        return
    schemas = bundle_schemas()
    if name not in schemas:
        raise LineInvalid(f"{path}: no schema for this member")
    _check(schemas[name], line, path)


# -- older formats (#250) ----------------------------------------------------------------------

Step = Callable[[str, dict[str, Any]], dict[str, Any] | None]
"""One format migration step: ``(member path, line) -> line`` (``None`` drops the line)."""


def _from_1_0(path: str, line: dict[str, Any]) -> dict[str, Any] | None:
    """#272: a ``1.0.0`` line as ``1.1.0`` reads it. A media line gets ``viewpoint_basis``
    (#261) and the check fields with their empty values (``unchecked``, no model, no endpoint,
    no results); a ``story_images`` line, which ``1.0.0`` never defined, is dropped, so the
    bundle imports with no story images (its assets, then unreferenced, fall to #105's
    sweep)."""
    if path == "records/story_images.jsonl":
        return None
    if path == "media.jsonl":
        return {
            "viewpoint_basis": None,
            "check_status": "unchecked",
            "check_model": None,
            "check_endpoint": None,
            "policy_results": [],
            **line,
        }
    return line


FORMAT_MIGRATIONS: dict[str, tuple[str, Step]] = {"1.0.0": ("1.1.0", _from_1_0)}
"""#250: the migrations from an older format, keyed by the version they read, each naming the
version it produces; ``migrations_for`` chains them to ``bundles.FORMAT_VERSION``. #272:
``1.0.0`` → ``1.1.0`` (``_from_1_0``)."""


def _minor_of(version: str) -> tuple[int, int]:
    parts = ([*version.split("."), "0", "0"])[:2]
    return int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 0


def migrations_for(version: str, current: str | None = None) -> list[Step] | None:
    """The steps that bring a bundle of ``version`` to the current format (``[]``: none is
    needed, its MAJOR.MINOR is the current one or newer, or an older one with no step imports
    as it is); ``None`` when the registry holds a step for ``version`` but its chain has a gap
    (the bundle is then ``unsupported_format_version``). PATCH is never significant."""
    target = _minor_of(current or bundles.FORMAT_VERSION)
    if _minor_of(version) >= target:
        return []
    key = ".".join(str(p) for p in _minor_of(version))
    found = next((k for k in FORMAT_MIGRATIONS if _minor_of(k) == _minor_of(key)), None)
    if found is None:
        return []
    steps: list[Step] = []
    seen: set[tuple[int, int]] = set()
    at = found
    while _minor_of(at) < target:
        if _minor_of(at) in seen:
            return None
        seen.add(_minor_of(at))
        entry = next(
            (v for k, v in FORMAT_MIGRATIONS.items() if _minor_of(k) == _minor_of(at)), None
        )
        if entry is None:
            return None
        produced, step = entry
        steps.append(step)
        at = produced
    return steps


__all__ = [
    "EVENT",
    "FORMAT_MIGRATIONS",
    "MANIFEST",
    "MEDIA",
    "RECORD",
    "LineInvalid",
    "Step",
    "bundle_schemas",
    "migrations_for",
    "record_schema",
    "schema_for",
    "validate_line",
]
