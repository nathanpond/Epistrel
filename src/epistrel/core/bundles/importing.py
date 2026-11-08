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

#705 adds:

- **Validation.** Every line holds to its member's schema (``core.bundles.format``: the
  manifest, ``StoryBundleEvent``, ``StoryBundleMedia``, ``StoryBundleRecord_<table>``) and every
  event's payload to #45's registry (``corrupt_bundle``, ``schema_violation``, with ``entry``,
  ``line`` and ``field``); the members come in the export's canonical order (``member_order``).
  A bundle of a newer MINOR of the same MAJOR imports: its unknown members and fields are
  ignored and listed as ``warnings``.
- **The import operation.** One committed ``import`` operation records ``imported_from
  {source_story_id, exported_revision, format_version, bundle_sha256}``; the story's
  ``timeline_revision`` is ``exported_revision + 1`` and its ``embedding_model_version`` null,
  and ``after`` (the Engine: the re-embed job) runs in the transaction.
- **Media.** Each ``media.jsonl`` line becomes a new ``media_assets`` row owned by the import's
  owner (its id remapped like the story's), its bytes written through ``blobs`` under their
  verified checksum (content-addressed: an existing blob is kept).

Two imports of one bundle give two independent stories. The safety gates that must pass before
an imported story is served (#251: the erasure ledger, the guardrail check, parking unsettled
effects), the background job, the ``importing`` status and older-format migrations are not
built yet (#705).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, TIMESTAMP, Date, LargeBinary, Table, insert, null, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.bundles import FORMAT_VERSION, NEVER, REBUILD
from epistrel.core.errors import BundleTooLarge, CorruptBundle, UnsupportedFormatVersion
from epistrel.core.ids import new_ulid
from epistrel.core.schema import metadata
from epistrel.core.tables import stories
from epistrel.observability.logging import get_logger
from epistrel.testing import crash

log = get_logger(__name__)

ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
CHUNK = 64 * 1024
BATCH = 200
MANIFEST_MAX = 64 * 1024 * 1024
"""The most bytes ``manifest.json`` may decompress to (#250's planner: a constant)."""


@dataclass(frozen=True, slots=True)
class Limits:
    """An import's limits (``RetentionSettings``' ``bundle_max_*``), frozen into its request:
    the decompressed total, the members and one member's decompressed size."""

    total: int
    entries: int
    entry: int

    @classmethod
    def of(cls, settings: Any) -> Limits:
        return cls(
            int(settings.bundle_max_uncompressed_bytes),
            int(settings.bundle_max_entries),
            min(int(settings.bundle_max_entry_bytes), int(settings.bundle_max_uncompressed_bytes)),
        )

    def as_json(self) -> dict[str, int]:
        return {"total": self.total, "entries": self.entries, "entry": self.entry}

    @classmethod
    def from_json(cls, found: Mapping[str, Any]) -> Limits:
        return cls(int(found["total"]), int(found["entries"]), int(found["entry"]))


def _defaults() -> Limits:
    from epistrel.core.settings_schema import RetentionSettings

    return Limits.of(RetentionSettings())


DEFAULT_LIMITS = _defaults()
MAX_TOTAL_BYTES = DEFAULT_LIMITS.total
MAX_ENTRIES = DEFAULT_LIMITS.entries
MAX_ENTRY_BYTES = DEFAULT_LIMITS.entry


# -- reading the archive -------------------------------------------------------------------------


def _members(data: bytes, limits: Limits = DEFAULT_LIMITS) -> Iterator[tuple[str, bytes]]:
    """The archive's members as a stream, under the limits (see the module docstring)."""
    total = entries = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tar:
            for member in tar:
                entries += 1
                if entries > limits.entries:
                    raise BundleTooLarge(
                        "the bundle has too many entries",
                        {"limit": "bundle_max_entries", "value": limits.entries},
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
                    if len(body) > limits.entry:
                        raise BundleTooLarge(
                            "a bundle member is too large",
                            {
                                "limit": "bundle_max_entry_bytes",
                                "value": limits.entry,
                                "entry": name,
                            },
                        )
                    if total > limits.total:
                        raise BundleTooLarge(
                            "the bundle is too large uncompressed",
                            {"limit": "bundle_max_uncompressed_bytes", "value": limits.total},
                        )
                yield name, bytes(body)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise CorruptBundle(
            "the bundle is not a readable gzip-compressed tar",
            {"reason": "unreadable_archive", "error": type(exc).__name__},
        ) from exc


def _rank(path: str) -> tuple[int, int, str]:
    """A member's place in the export's canonical order (``core.bundles``' members)."""
    from epistrel.core.bundles import catalog_tables, exported_tables

    if path == "records/story.json":
        return (0, 0, "")
    if path == "media.jsonl":
        return (1, 0, "")
    if path.startswith("catalog/"):
        names = [t.name for t in catalog_tables()]
        name = path.removeprefix("catalog/").removesuffix(".jsonl")
        return (2, names.index(name) if name in names else len(names), name)
    if path == "records/operations.jsonl":
        return (3, 0, "")
    if path == "events.jsonl":
        return (4, 0, "")
    if path == "records/erasure_ledger.jsonl":
        return (6, 0, "")
    if path.startswith("records/"):
        names = [t.name for t in exported_tables()]
        name = path.removeprefix("records/").removesuffix(".jsonl")
        return (5, names.index(name) if name in names else len(names), name)
    if path.startswith("media/"):
        return (7, 0, path)
    return (8, 0, path)


def _minor(version: str) -> int:
    parts = version.split(".")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def _manifest(name: str, body: bytes) -> dict[str, Any]:
    """The first member as the bundle's manifest: ``manifest.json``, JSON, a supported format
    and its schema (``corrupt_bundle``, ``unsupported_format_version``)."""
    if name != "manifest.json":
        raise CorruptBundle("a bundle starts with manifest.json", {"reason": "member_order"})
    if len(body) > MANIFEST_MAX:
        raise BundleTooLarge(
            "the bundle's manifest is too large",
            {"limit": "manifest_max_bytes", "value": MANIFEST_MAX, "entry": name},
        )
    try:
        manifest = json.loads(body)
    except ValueError as exc:
        raise CorruptBundle("the manifest is not JSON", {"reason": "manifest_invalid"}) from exc
    if not isinstance(manifest, dict):
        raise CorruptBundle("the manifest is not a JSON object", {"reason": "manifest_invalid"})
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
    from epistrel.core.bundles.format import LineInvalid, validate_line

    newer = _minor(version) > _minor(FORMAT_VERSION)
    try:
        validate_line("manifest.json", _known(manifest, "manifest.json") if newer else manifest)
    except LineInvalid as exc:
        raise CorruptBundle(
            "the manifest does not hold to its schema",
            {"reason": "manifest_invalid", "field": _field(str(exc))},
        ) from exc
    return manifest


def read_head(
    data: bytes, limits: Limits = DEFAULT_LIMITS
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """#250's up-front read: the manifest (the first member, checked) and the story's own record
    (``records/story.json``, the second member in the canonical order; ``None`` when the bundle
    does not start with it), reading no further."""
    members = _members(data, limits)
    try:
        first = next(members, None)
        if first is None:
            raise CorruptBundle("a bundle starts with manifest.json", {"reason": "member_order"})
        manifest = _manifest(*first)
        second = next(members, None)
    finally:
        members.close()  # type: ignore[attr-defined]
    story = None
    if second is not None and second[0] == "records/story.json":
        try:
            found = json.loads(second[1])
        except ValueError:
            found = None
        story = found if isinstance(found, dict) else None
    return manifest, story


def read_bundle(
    data: bytes, limits: Limits = DEFAULT_LIMITS
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The manifest and the members by path, checked (see the module docstring)."""
    members = list(_members(data, limits))
    if not members:
        raise CorruptBundle("a bundle starts with manifest.json", {"reason": "member_order"})
    manifest = _manifest(*members[0])
    listed = {str(f["path"]): f for f in manifest.get("files", [])}
    by_path: dict[str, bytes] = {}
    last: tuple[int, int, str] | None = None
    for name, body in members[1:]:
        rank = _rank(name)
        if last is not None and rank < last:
            raise CorruptBundle(
                "the bundle's members are out of the canonical order",
                {"reason": "member_order", "entry": name},
            )
        last = rank
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


def _field(message: str) -> str | None:
    """The JSON path a ``LineInvalid`` names (``<member>.<field>...: why``), less the member."""
    where = message.split(":", 1)[0]
    parts = where.split(".jsonl", 1) if ".jsonl" in where else where.split(".json", 1)
    rest = parts[1] if len(parts) > 1 else ""
    return rest.lstrip(".") or None


def _known(row: dict[str, Any], name: str) -> dict[str, Any]:
    """``row`` without the fields its member's schema does not know (a newer MINOR's)."""
    from epistrel.core.bundles.format import bundle_schemas, schema_for

    schema = bundle_schemas().get(schema_for(name) or "", {})
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return row
    return {k: v for k, v in row.items() if k in properties}


def _rows(
    name: str,
    body: bytes,
    *,
    newer: bool = False,
    warnings: list[dict[str, Any]] | None = None,
    steps: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """#705: the member's lines, each a JSON object holding to its schema (an event's payload to
    #45's registry); under a newer MINOR, unknown fields are dropped and listed in ``warnings``.
    #250: a bundle of an older format has each line migrated first (``steps``, from
    ``format.migrations_for``; a step answering ``None`` drops the line)."""
    from epistrel.core.bundles.format import LineInvalid, validate_line
    from epistrel.core.events.registry import validate

    found: list[dict[str, Any]] = []
    dropped: set[str] = set()
    for number, raw in enumerate(_json_rows(name, body), start=1):
        row: dict[str, Any] | None = raw
        for step in steps:
            row = step(name, row) if row is not None else None
        if row is None:
            continue
        if newer:
            kept = _known(row, name)
            dropped |= set(row) - set(kept)
            row = kept
        try:
            validate_line(name, row)
        except LineInvalid as exc:
            raise CorruptBundle(
                "a bundle line does not hold to its schema",
                {
                    "reason": "schema_violation",
                    "entry": name,
                    "line": number,
                    "field": _field(str(exc)),
                },
            ) from exc
        if name == "events.jsonl":
            try:
                validate(str(row.get("type")), dict(row.get("payload") or {}))
            except Exception as exc:  # any registry refusal is the line's schema violation
                raise CorruptBundle(
                    "an event's payload does not hold to its type",
                    {
                        "reason": "schema_violation",
                        "entry": name,
                        "line": number,
                        "field": "payload",
                    },
                ) from exc
        found.append(row)
    if dropped and warnings is not None:
        warnings.append({"code": "unknown_fields", "entry": name, "fields": sorted(dropped)})
    return found


def _json_rows(name: str, body: bytes) -> list[dict[str, Any]]:
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


BlobWriter = Callable[[str, bytes], None]
"""``blobs(sha256, data)``: write a media blob under its checksum (the Engine's media store;
content-addressed, so an existing blob is kept)."""
After = Callable[[AsyncConnection, str], Awaitable[None]]


def format_steps(version: str) -> list[Any]:
    """#250: the migration steps from the bundle's ``version`` to this deployment's format
    (``format.FORMAT_MIGRATIONS``); a registered version whose chain has a gap is
    ``unsupported_format_version``."""
    from epistrel.core.bundles import format as bundle_format

    steps = bundle_format.migrations_for(version, FORMAT_VERSION)
    if steps is None:
        raise UnsupportedFormatVersion(
            "this deployment cannot migrate the bundle's format",
            {"bundle_version": version, "supported": "1.x"},
        )
    return steps


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def fresh_id(job_id: str, old: str) -> str:
    """#250: a bundle id's new ULID, with no map table. It keeps the old id's 48-bit time
    prefix (so ids keep their order), and takes its 80 random bits from HMAC-SHA256(job id, old
    id), so a crash-resumed import recomputes the same ids."""
    import hmac

    digest = hmac.new(job_id.encode(), old.encode(), hashlib.sha256).digest()
    value = int.from_bytes(digest[:10], "big")
    tail = "".join(_CROCKFORD[(value >> (5 * (15 - n))) & 31] for n in range(16))
    return old[:10] + tail


@dataclass(slots=True)
class Inserted:
    """What ``insert_bundle`` wrote: the id map, the counts, the warnings, the ``import``
    operation, the media assets it created and the source's ids (for #251's gates)."""

    ids: dict[str, str]
    counts: dict[str, int]
    warnings: list[dict[str, Any]]
    operation_id: str
    media_ids: list[str]
    source_story_id: str
    exported_revision: int


async def stage_media(
    manifest: Mapping[str, Any], by_path: Mapping[str, bytes], blobs: BlobWriter | None
) -> list[dict[str, Any]]:
    """Each ``media.jsonl`` line's bytes checked against its checksum and written through
    ``blobs`` (outside any transaction: content-addressed, so a refusal leaves only an
    unreferenced blob for #105's sweep); the lines."""
    body = by_path.get("media.jsonl")
    if body is None:
        return []
    version = str(manifest.get("format_version", ""))
    newer = _minor(version) > _minor(FORMAT_VERSION)
    lines = _rows("media.jsonl", body, newer=newer, steps=format_steps(version))
    for line in lines:
        blob = by_path.get(f"media/{line['sha256']}")
        if blob is None or hashlib.sha256(blob).hexdigest() != line["sha256"]:
            raise CorruptBundle(
                "a media line's bytes are missing or do not match its checksum",
                {"reason": "checksum_mismatch", "entry": f"media/{line['sha256']}"},
            )
        if blobs is not None:
            await asyncio.to_thread(blobs, str(line["sha256"]), blob)
    return lines


async def insert_bundle(
    conn: AsyncConnection,
    story_id: str,
    manifest: Mapping[str, Any],
    by_path: Mapping[str, bytes],
    *,
    job_id: str,
    principal: str,
    owner: str,
    media: Sequence[Mapping[str, Any]],
    bundle_sha256: str,
) -> Inserted:
    """#250: the bundle's rows into the story row ``story_id`` (status ``importing``, created by
    the request with its name, owner, rating and profile), in ``conn``'s transaction (see the
    module docstring): every id the bundle owns remapped (``fresh_id``), the rows inserted in
    dependency order with their original recording times, the ``import`` operation recorded,
    the projections folded and the subject index rewritten."""
    from epistrel.core.erasure.index import reindex_story
    from epistrel.core.projections.fold import replace_with_fold

    version = str(manifest.get("format_version", ""))
    newer = _minor(version) > _minor(FORMAT_VERSION)
    steps = format_steps(version)
    warnings: list[dict[str, Any]] = []
    story_row = json.loads(by_path["records/story.json"])
    for step in steps:
        story_row = step("records/story.json", story_row) or story_row
    if newer:
        story_row = _known(story_row, "records/story.json")
    _check("records/story.json", story_row)
    tables: dict[str, list[dict[str, Any]]] = {}
    for path, body in by_path.items():
        if path in ("records/story.json", "media.jsonl") or path.startswith("media/"):
            continue
        name = (
            "events"
            if path == "events.jsonl"
            else path.removeprefix("records/").removeprefix("catalog/").removesuffix(".jsonl")
        )
        table = metadata.tables.get(name)
        if table is None or name in NEVER or name in REBUILD:
            if table is None and newer:
                warnings.append({"code": "unknown_member", "entry": path})
            continue  # an unknown or rebuilt table: ignored
        tables[name] = _rows(path, body, newer=newer, warnings=warnings, steps=steps)
    source = str(story_row["id"])
    ids: dict[str, str] = {source: story_id}
    for name, rows in tables.items():
        for old in _primary_ids(metadata.tables[name], rows):
            ids.setdefault(old, fresh_id(job_id, old))
    for line in media:
        ids.setdefault(str(line["id"]), fresh_id(job_id, str(line["id"])))
    exported = int(manifest.get("exported_revision") or 0)
    counts: dict[str, int] = {}
    held = (
        await conn.execute(
            select(
                stories.c.name,
                stories.c.content_rating_id,
                stories.c.engine_profile_id,
                stories.c.engine_profile_version_pin,
                stories.c.owner_principal,
            ).where(stories.c.id == story_id)
        )
    ).one()
    story = remap(story_row, ids)
    story.update(
        {
            "id": story_id,
            "name": held.name,
            "owner_principal": held.owner_principal,
            "content_rating_id": held.content_rating_id,
            "engine_profile_id": held.engine_profile_id,
            "engine_profile_version_pin": held.engine_profile_version_pin,
            "status": "importing",
            "archived_at": None,
            "paused_by": None,
            "status_before_block": None,
            "in_flight_turn": None,
            "in_flight_lease_until": None,
            "timeline_revision": exported + 1,
            "embedding_model_version": None,
        }
    )
    values = _typed(stories, story)
    values.pop("id", None)
    await conn.execute(stories.update().where(stories.c.id == story_id).values(**values))
    media_ids: list[str] = []
    if media:
        await _insert_media(conn, media, ids, owner=owner, principal=principal)
        media_ids = [ids[str(line["id"])] for line in media]
        counts["media_assets"] = len(media)
    ordered = [t for t in metadata.sorted_tables if t.name in tables]
    for table in ordered:
        rows = [_safe(table.name, _typed(table, remap(r, ids))) for r in tables[table.name]]
        if not rows:
            continue
        for start in range(0, len(rows), BATCH):
            await conn.execute(insert(table).values(rows[start : start + BATCH]))
        counts[table.name] = len(rows)
        crash.hit("bundles.import.mid_records")
    operation_id = await _import_operation(
        conn,
        story_id,
        principal=principal,
        revision=exported + 1,
        imported_from={
            "source_story_id": str(manifest.get("source_story_id", "")),
            "exported_revision": exported,
            "format_version": version,
            "bundle_sha256": bundle_sha256,
        },
    )
    await replace_with_fold(conn, story_id)
    await reindex_story(conn, story_id)
    log.info("bundle inserted", story_id=story_id, tables=len(counts), warnings=len(warnings))
    return Inserted(
        ids,
        counts,
        warnings,
        operation_id,
        media_ids,
        str(manifest.get("source_story_id", "")),
        exported,
    )


def _check(name: str, row: Mapping[str, Any]) -> None:
    from epistrel.core.bundles.format import LineInvalid, validate_line

    try:
        validate_line(name, row)
    except LineInvalid as exc:
        raise CorruptBundle(
            "a bundle line does not hold to its schema",
            {"reason": "schema_violation", "entry": name, "line": 1, "field": _field(str(exc))},
        ) from exc


async def _insert_media(
    conn: AsyncConnection,
    lines: Sequence[Mapping[str, Any]],
    ids: Mapping[str, str],
    *,
    owner: str,
    principal: str,
) -> None:
    """#705: each media line as a new asset of the import's owner (its id and its poster's
    remapped), ``live``, its bytes already in the store under its checksum."""
    from epistrel.core import clock
    from epistrel.core.tables import media_assets
    from epistrel.core.tables.media import MediaState

    rows = []
    for line in lines:
        poster = line.get("poster_media_id")
        rows.append(
            {
                "id": ids[str(line["id"])],
                "owner_principal": owner,
                "kind": line["kind"],
                "mime_type": line["mime_type"],
                "size": line["size"],
                "width": line.get("width"),
                "height": line.get("height"),
                "duration_ms": line.get("duration_ms"),
                "checksum": line["sha256"],
                "content_rating_version_id": line["content_rating_version_id"],
                "alt_text": line.get("alt_text"),
                "caption": line.get("caption"),
                "generation_metadata": line.get("generation_metadata") or null(),
                "source": line["source"],
                "state": MediaState.LIVE.value,
                "depicted_subjects": remap(line.get("depicted_subjects") or [], ids),
                "created_at": clock.now(),
                "created_by": principal,
                "poster_media_id": None if poster is None else ids.get(str(poster), str(poster)),
                # #261: a story image's asset says what its inputs were built from.
                "viewpoint_basis": line.get("viewpoint_basis"),
                # #272: the check the source recorded, as it recorded it (this deployment's
                # own verdicts live only in ``guardrail_check_results``).
                "check_status": line.get("check_status") or "unchecked",
                "check_model": line.get("check_model"),
                "check_endpoint": line.get("check_endpoint"),
                "policy_results": line.get("policy_results") or [],
            }
        )
    # Posters first: an asset names its poster.
    rows.sort(key=lambda r: r["poster_media_id"] is not None)
    for row in rows:
        await conn.execute(insert(media_assets).values(**row))


async def _import_operation(
    conn: AsyncConnection,
    story_id: str,
    *,
    principal: str,
    revision: int,
    imported_from: Mapping[str, Any],
) -> str:
    """#705: the committed ``import`` operation that records where the story came from."""
    from sqlalchemy import func

    from epistrel.core.enums import Membership, OperationKind, OperationStatus
    from epistrel.core.tables import operations

    operation_id = new_ulid()
    now = func.transaction_timestamp()
    await conn.execute(
        insert(operations).values(
            operation_id=operation_id,
            story_id=story_id,
            kind=OperationKind.IMPORT.value,
            principal=principal,
            status=OperationStatus.COMMITTED.value,
            membership=Membership.ACTIVE.value,
            membership_tx=now,
            committed_at=now,
            timeline_revision=revision,
            turn_report={"imported_from": dict(imported_from)},
        )
    )
    return operation_id


async def _profile_exists(conn: AsyncConnection, profile_id: str) -> bool:
    from epistrel.core.tables import engine_profiles

    found = await conn.scalar(
        select(engine_profiles.c.id).where(
            engine_profiles.c.id == profile_id, engine_profiles.c.deleted_at.is_(None)
        )
    )
    return found is not None


__all__ = [
    "DEFAULT_LIMITS",
    "MAX_ENTRIES",
    "MAX_ENTRY_BYTES",
    "MAX_TOTAL_BYTES",
    "Inserted",
    "Limits",
    "fresh_id",
    "insert_bundle",
    "read_bundle",
    "read_head",
    "remap",
    "stage_media",
]
