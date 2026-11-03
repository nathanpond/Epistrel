"""Story import (FR-STORE-7, FR-ADMIN-4's restore; #250): a bundle becomes a new story.

The archive is **untrusted input**. It is read as a stream under the deployment's limits,
counted while it decompresses (``MAX_TOTAL_BYTES``, ``MAX_ENTRIES``, ``MAX_ENTRY_BYTES``:
``bundle_too_large``), never extracted to a path taken from it (a member that is not a regular
file, or whose path is absolute or climbs, is ``corrupt_bundle``), and checked before anything
is written: ``manifest.json`` first, a supported format (major ``1``; a newer major is
``unsupported_format_version``), every member listed once with its SHA-256 matching, and every
line a JSON object.

Then, in **one transaction** (so any refusal leaves nothing: no story row, no record):

- every id the bundle's rows carry as a primary key is given a fresh ULID, the story's own
  included, and every value of every row (JSON included, and an id inside a longer string) is
  rewritten through that map, as a fork remaps (``core.fork``); ids the bundle does not own
  (catalog entries, predicates, policies of the deployment) are kept;
- the rows are inserted under the new story in dependency order with their original recording
  times (``tx_time``, ``created_at``), the story owned by the requesting principal (or
  ``owner``) and attached to ``engine_profile_id``, else its bundle profile when this deployment
  has it, else the deployment's default;
- no imported operation counts as guardrail-checked, and no imported outbox row can be
  dispatched;
- the projections are refolded from the imported log and the subject index is rewritten.

Two imports of one bundle give two independent stories. The safety gates that must pass before
an imported story is served (#251: the erasure ledger, the guardrail check, parking unsettled
effects) are not built yet; nor are the background job, the ``importing`` status or format
migrations (#705).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import JSON, TIMESTAMP, Date, LargeBinary, Table, insert, null, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.bundles import FORMAT_VERSION, NEVER, REBUILD
from epistrel.core.errors import BundleTooLarge, CorruptBundle, UnsupportedFormatVersion
from epistrel.core.ids import new_ulid
from epistrel.core.schema import metadata
from epistrel.core.tables import stories
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 1_000
MAX_ENTRY_BYTES = 256 * 1024 * 1024
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
CHUNK = 64 * 1024
BATCH = 200


class ImportRequest(BaseModel):
    """``POST /admin/stories/import``'s choices (query parameters; the archive is the body)."""

    model_config = ConfigDict(extra="forbid")

    owner: StrictStr | None = Field(default=None, max_length=200)
    engine_profile_id: StrictStr | None = Field(default=None, max_length=26)


@dataclass(frozen=True, slots=True)
class Imported:
    """An import's answer (REST 201): the new story, the bundle's source and its counts."""

    story_id: str
    source_story_id: str
    exported_revision: int
    counts: dict[str, int] = field(default_factory=dict)
    timeline_revision: int = 0
    replayed: bool = False


# -- reading the archive -------------------------------------------------------------------------


def _members(data: bytes) -> Iterator[tuple[str, bytes]]:
    """The archive's members as a stream, under the limits (see the module docstring)."""
    total = entries = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tar:
            for member in tar:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise BundleTooLarge(
                        "the bundle has too many entries",
                        {"limit": "bundle_max_entries", "value": MAX_ENTRIES},
                    )
                name = member.name
                if not member.isreg():
                    raise CorruptBundle(
                        "a bundle member is not a regular file",
                        {"reason": "entry_outside_archive", "entry": name},
                    )
                if name.startswith("/") or ".." in name.split("/"):
                    raise CorruptBundle(
                        "a bundle member's path leaves the archive",
                        {"reason": "entry_outside_archive", "entry": name},
                    )
                handle = tar.extractfile(member)
                body = bytearray()
                while handle is not None:
                    piece = handle.read(CHUNK)
                    if not piece:
                        break
                    body += piece
                    total += len(piece)
                    if len(body) > MAX_ENTRY_BYTES:
                        raise BundleTooLarge(
                            "a bundle member is too large",
                            {
                                "limit": "bundle_max_entry_bytes",
                                "value": MAX_ENTRY_BYTES,
                                "entry": name,
                            },
                        )
                    if total > MAX_TOTAL_BYTES:
                        raise BundleTooLarge(
                            "the bundle is too large uncompressed",
                            {"limit": "bundle_max_uncompressed_bytes", "value": MAX_TOTAL_BYTES},
                        )
                yield name, bytes(body)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise CorruptBundle(
            "the bundle is not a readable gzip-compressed tar",
            {"reason": "unreadable_archive", "error": type(exc).__name__},
        ) from exc


def read_bundle(data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The manifest and the members by path, checked (see the module docstring)."""
    members = list(_members(data))
    if not members or members[0][0] != "manifest.json":
        raise CorruptBundle("a bundle starts with manifest.json", {"reason": "member_order"})
    try:
        manifest = json.loads(members[0][1])
    except ValueError as exc:
        raise CorruptBundle("the manifest is not JSON", {"reason": "manifest_invalid"}) from exc
    version = str(manifest.get("format_version", ""))
    major = version.split(".", 1)[0]
    if not major.isdigit():
        raise CorruptBundle(
            "the manifest names no format version",
            {"reason": "manifest_invalid", "field": "format_version"},
        )
    if int(major) != int(FORMAT_VERSION.split(".", 1)[0]):
        raise UnsupportedFormatVersion(
            "this deployment cannot read the bundle's format",
            {"bundle_version": version, "supported": "1.x"},
        )
    listed = {str(f["path"]): f for f in manifest.get("files", [])}
    by_path: dict[str, bytes] = {}
    for name, body in members[1:]:
        if name not in listed or name in by_path:
            raise CorruptBundle(
                "a bundle member is not listed in its manifest, or appears twice",
                {"reason": "unexpected_entry", "entry": name},
            )
        if hashlib.sha256(body).hexdigest() != listed[name].get("sha256"):
            raise CorruptBundle(
                "a bundle member's checksum does not match its manifest",
                {"reason": "checksum_mismatch", "entry": name},
            )
        by_path[name] = body
    missing = sorted(set(listed) - set(by_path))
    if missing:
        raise CorruptBundle(
            "the manifest lists members the bundle lacks",
            {"reason": "manifest_invalid", "entry": missing[0]},
        )
    if "records/story.json" not in by_path:
        raise CorruptBundle("the bundle has no story", {"reason": "manifest_invalid"})
    return manifest, by_path


def _rows(name: str, body: bytes) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for number, line in enumerate(body.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise CorruptBundle(
                "a bundle line is not JSON",
                {"reason": "schema_violation", "entry": name, "line": number},
            ) from exc
        if not isinstance(row, dict):
            raise CorruptBundle(
                "a bundle line is not a JSON object",
                {"reason": "schema_violation", "entry": name, "line": number},
            )
        found.append(row)
    return found


# -- the id map -----------------------------------------------------------------------------------


def _primary_ids(table: Table, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    keys = [c.name for c in table.primary_key.columns]
    found: list[str] = []
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and ULID.fullmatch(value):
                found.append(value)
    return found


def remap(value: Any, ids: Mapping[str, str]) -> Any:
    """``value`` with every id ``ids`` maps rewritten (in JSON, and inside longer strings)."""
    if isinstance(value, str):
        if value in ids:
            return ids[value]
        if len(value) > 26 and ULID.search(value):
            return ULID.sub(lambda m: ids.get(m.group(0), m.group(0)), value)
        return value
    if isinstance(value, list):
        return [remap(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: remap(v, ids) for k, v in value.items()}
    return value


def _typed(table: Table, row: Mapping[str, Any]) -> dict[str, Any]:
    """A bundle row as ``table`` takes it: its known columns, times and bytes decoded."""
    found: dict[str, Any] = {}
    for column in table.c:
        if column.name not in row:
            continue
        value = row[column.name]
        if value is None and isinstance(column.type, JSON):
            value = null()  # SQL NULL, not the JSON ``null`` a bare ``None`` binds as
        elif value is not None and isinstance(column.type, TIMESTAMP):
            value = datetime.fromisoformat(value)
        elif value is not None and isinstance(column.type, Date):
            value = date.fromisoformat(value)
        elif value is not None and isinstance(column.type, LargeBinary):
            value = base64.b64decode(value)
        found[column.name] = value
    return found


def _safe(name: str, row: dict[str, Any]) -> dict[str, Any]:
    """#250's two import-time safety rules: no imported operation counts as guardrail-checked
    (an inactive turn is checked before reactivation, FR-RATE-7), and no imported outbox row
    can be dispatched (#211)."""
    if name == "operations":
        row["checked_guardrails"] = None
    elif name == "external_effect_outbox":
        row["executable"] = False
    return row


# -- the import ----------------------------------------------------------------------------------


async def import_story(
    db: AsyncEngine, data: bytes, request: ImportRequest, *, principal: str
) -> Imported:
    """Import ``data`` as a new story (see the module docstring)."""
    from epistrel.core.erasure.index import reindex_story
    from epistrel.core.profiles.store import default_profile
    from epistrel.core.projections.fold import replace_with_fold

    manifest, by_path = read_bundle(data)
    story_row = json.loads(by_path["records/story.json"])
    tables: dict[str, list[dict[str, Any]]] = {}
    for path, body in by_path.items():
        if path == "records/story.json":
            continue
        name = (
            "events"
            if path == "events.jsonl"
            else path.removeprefix("records/").removesuffix(".jsonl")
        )
        table = metadata.tables.get(name)
        if table is None or name in NEVER or name in REBUILD:
            continue  # an unknown or rebuilt table: ignored
        tables[name] = _rows(path, body)
    ids: dict[str, str] = {str(story_row["id"]): new_ulid()}
    for name, rows in tables.items():
        for old in _primary_ids(metadata.tables[name], rows):
            ids.setdefault(old, new_ulid())
    new_story = ids[str(story_row["id"])]
    counts: dict[str, int] = {}
    async with db.begin() as conn:
        profile = request.engine_profile_id or story_row.get("engine_profile_id")
        if profile is None or not await _profile_exists(conn, str(profile)):
            profile = await default_profile(conn)
        story = remap(story_row, ids)
        story.update(
            {
                "id": new_story,
                "owner_principal": request.owner or principal,
                "engine_profile_id": profile,
                "engine_profile_version_pin": None,
                "status": "active",
                "paused_by": None,
                "status_before_block": None,
                "in_flight_turn": None,
                "in_flight_lease_until": None,
            }
        )
        await conn.execute(insert(stories).values(**_typed(stories, story)))
        ordered = [t for t in metadata.sorted_tables if t.name in tables]
        for table in ordered:
            rows = [_safe(table.name, _typed(table, remap(r, ids))) for r in tables[table.name]]
            if not rows:
                continue
            for start in range(0, len(rows), BATCH):
                await conn.execute(insert(table).values(rows[start : start + BATCH]))
            counts[table.name] = len(rows)
        await replace_with_fold(conn, new_story)
        await reindex_story(conn, new_story)
        revision = int(
            await conn.scalar(select(stories.c.timeline_revision).where(stories.c.id == new_story))
            or 0
        )
    log.info("story imported", story_id=new_story, tables=len(counts))
    return Imported(
        new_story,
        str(manifest.get("source_story_id", "")),
        int(manifest.get("exported_revision") or 0),
        counts,
        timeline_revision=revision,
    )


async def _profile_exists(conn: AsyncConnection, profile_id: str) -> bool:
    from epistrel.core.tables import engine_profiles

    found = await conn.scalar(
        select(engine_profiles.c.id).where(
            engine_profiles.c.id == profile_id, engine_profiles.c.deleted_at.is_(None)
        )
    )
    return found is not None


__all__ = [
    "MAX_ENTRIES",
    "MAX_ENTRY_BYTES",
    "MAX_TOTAL_BYTES",
    "ImportRequest",
    "Imported",
    "import_story",
    "read_bundle",
    "remap",
]
