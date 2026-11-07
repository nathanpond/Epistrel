"""Catalog image candidates: private until an author attaches one (FR-IMG-4,7, FR-MEDIA-2;
docs/02 §4.13; #260).

A catalog image job (``orchestrator.images.targets.catalog``) promotes each output that passed
its checks to a ``media_assets`` row in state ``candidate``, owned by the entry's owner, and a
``catalog.image_candidates`` row (``insert``): the entry, the job, the requested slot, the draft
revision and image-input fingerprint the job was requested from, and ``expires_at``
(``candidate_ttl`` after promotion, fixed then). A candidate is private: only the entry's
candidate endpoints read it (``list_for_entry``, ``live_candidate``), and the general media reads,
the card and full projections and stories never show a ``candidate`` asset.

**The fingerprint** (``image_input_fingerprint``) is ``"v1:"`` and the hex SHA-256 of the RFC 8785
(JCS) canonical JSON of the entry's image inputs, every string NFC-normalised, an absent field
written as ``null`` and list order kept: for a character ``definition.appearance``,
``definition.true_age``, the whole ``visual_identity``, ``metadata.content_rating_id`` (the
rating, not its version) and the visual style in use (its companion scenario's,
``perceived.companion_style``, the one #259's prompt builder reads); for a scenario its
``visual_style`` and ``metadata.content_rating_id``. No other field moves it, so a tagline or
label edit leaves a candidate attachable.

**Attach** is a draft save (#103) naming ``{candidate_id}`` in an image slot. ``referenced_ids``
finds the references in a merge patch, ``resolve`` maps them to their media ids before the
merged draft is validated, and ``attach`` runs in the save's transaction after its revision
compare: it locks the candidates (``FOR UPDATE``; the entry row is already locked by the save's
update), refuses an unknown, expired or other entry's candidate (422 ``candidate_not_found``),
then a fingerprint other than the saved draft's (409 ``candidate_stale``), and otherwise makes
each asset ``live`` (FR-IMG-7's "attached" is #105's ``live``), owned by the entry's current
owner, and deletes the candidate rows. A new rating version never makes a candidate stale: the
fingerprint holds the rating's id.

**Removal** deletes the candidate row and its asset row (the bytes after commit, through
``core.media.sweep.collect_blob``, which keeps bytes another asset row still names):
``discard`` (idempotent), ``delete_for_entry`` (the entry's delete, in its transaction), and
``expire_batch`` (the ``image_candidate_expiry`` task: past ``expires_at``, ``FOR UPDATE SKIP
LOCKED``, so an attach holding the lock wins). Nothing else does: the general media sweep skips
``candidate`` assets, and ``catalog.image_candidates`` is a media reference source.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy import insert as sql_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.erasure.index import drop_references
from epistrel.core.errors import (
    CandidateNotFound,
    CandidateStale,
    ImageCandidateNotFound,
    InvalidRequest,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.images.perceived import companion_style
from epistrel.core.media.assets import (
    RECORD_TABLE,
    ReferenceSource,
    register_reference_source,
)
from epistrel.core.media.checks import CHECK_STATEMENTS, ImageCheck, image_check_of
from epistrel.core.ownership import record_event
from epistrel.core.tables import media_assets
from epistrel.core.tables.catalog import character_catalog, scenario_catalog
from epistrel.core.tables.images import image_candidates
from epistrel.core.tables.media import CheckStatus, MediaSource, MediaState
from epistrel.core.tables.ownership import EntityType, OwnershipEventKind

type EntryKind = Literal["character", "scenario"]
type Slot = Literal["primary", "gallery", "cover"]

FINGERPRINT_VERSION: Final = "v1"
SLOTS: Final[Mapping[str, frozenset[str]]] = {
    "character": frozenset({"primary", "gallery"}),
    "scenario": frozenset({"cover"}),
}
"""The slots a request may name per kind (``cover`` is scenario-only, ``primary`` and
``gallery`` character-only)."""
IMAGE_SLOTS: Final[Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "character": (("public_card", "images", "primary"), ("public_card", "images", "gallery")),
    "scenario": (("public_card", "media", "cover"), ("public_card", "media", "gallery")),
}
"""Each kind's image slots in a draft: ``(single slot, list slot)``. Any of them may hold a
``{candidate_id}`` in a save; a candidate's own ``slot`` is a hint only."""
EXPIRY_BATCH: Final = 500
LIST_DEFAULT: Final = 20
LIST_MAX: Final = 50
CANDIDATES_TABLE: Final = "catalog.image_candidates"

register_reference_source(ReferenceSource(CANDIDATES_TABLE, "asset_id", None))
"""A live candidate record references its asset (FR-MEDIA-2): every row counts until the expiry
task, a discard, an attach or the entry's delete removes it."""


# -- the fingerprint --------------------------------------------------------------------------


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {unicodedata.normalize("NFC", str(k)): _nfc(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_nfc(v) for v in value]
    return value


def _number(value: float) -> str:
    """An ECMAScript number's shortest form (RFC 8785 §3.2.2.3) for a finite float."""
    if not math.isfinite(value):
        raise ValueError("JCS has no form for a non-finite number")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    if "e" in text:
        mantissa, exponent = text.split("e")
        sign = "-" if exponent.startswith("-") else "+"
        text = f"{mantissa}e{sign}{exponent.lstrip('+-').lstrip('0') or '0'}"
    return text


def jcs(value: Any) -> str:
    """``value`` as RFC 8785 (JCS) canonical JSON: keys sorted by their UTF-16 code units, no
    whitespace, ECMAScript number forms, strings as JSON escapes them minimally."""
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        keys = sorted((str(k) for k in value), key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(f"{jcs(k)}:{jcs(value[k])}" for k in keys) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    raise TypeError(f"no JSON form for {type(value).__name__}")


def _at(document: Mapping[str, Any] | None, *path: str) -> Any:
    found: Any = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found


def image_inputs(
    entry_kind: str,
    document: Mapping[str, Any] | None,
    style: tuple[str | None, str | None],
) -> dict[str, Any]:
    """The entry's image inputs as the fingerprint hashes them (see the module docstring);
    ``style`` is the visual style in use, ``(prompt, negative_prompt)``."""
    prompt, negative = style
    inputs: dict[str, Any] = {
        "content_rating_id": _at(document, "metadata", "content_rating_id"),
        "visual_style": {"prompt": prompt, "negative_prompt": negative},
    }
    if entry_kind == "character":
        inputs["appearance"] = _at(document, "definition", "appearance")
        inputs["true_age"] = _at(document, "definition", "true_age")
        inputs["visual_identity"] = _at(document, "visual_identity")
    return inputs


def fingerprint_of(inputs: Mapping[str, Any]) -> str:
    """``"v1:"`` and the hex SHA-256 of ``inputs``' JCS form, strings NFC-normalised."""
    digest = hashlib.sha256(jcs(_nfc(dict(inputs))).encode()).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest}"


def _scenario_style(document: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    style = _at(document, "visual_style")
    style = style if isinstance(style, Mapping) else {}

    def text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    return text(style.get("prompt")), text(style.get("negative_prompt"))


async def image_input_fingerprint(
    conn: AsyncConnection, entry_kind: str, entry_id: str, document: Mapping[str, Any] | None
) -> str:
    """The fingerprint of ``document`` (the entry's draft, as stored or about to be saved): a
    character's visual style is its companion scenario's draft's, read in ``conn``, as the
    prompt builder reads it (#259)."""
    if entry_kind == "character":
        style = await companion_style(conn, entry_id, None)
    else:
        style = _scenario_style(document)
    return fingerprint_of(image_inputs(entry_kind, document, style))


# -- the reads --------------------------------------------------------------------------------


class CandidateMedia(BaseModel):
    """A candidate's image: its type, size, dimensions, source, check and the full generation
    metadata (FR-IMG-4)."""

    model_config = ConfigDict(json_schema_extra={"description": CHECK_STATEMENTS})

    mime_type: str
    size: int
    width: int | None
    height: int | None
    source: MediaSource
    check_status: CheckStatus = Field(
        description="`passed`: the image passed every applicable image-output policy under the "
        f"checker `check` names; `unchecked`: no policy applied. {CHECK_STATEMENTS}"
    )
    check: ImageCheck | None = Field(
        description="The checker and its per-policy results when `passed`; `null` when `unchecked`."
    )
    generation_metadata: dict[str, Any] | None


class ImageCandidate(BaseModel):
    """One catalog image candidate (FR-IMG-7): private, listed and previewed only through its
    entry's candidate endpoints until an author attaches it with a draft save naming
    ``{candidate_id}``. ``stale`` is true when its image-input fingerprint differs from the
    draft's now: an attach would be refused ``candidate_stale``. It is deleted at
    ``expires_at`` unless attached first."""

    model_config = ConfigDict(json_schema_extra={"description": CHECK_STATEMENTS})

    candidate_id: str
    slot: Literal["primary", "gallery", "cover"] = Field(
        description="The slot it was requested for: a hint, as it may fill any image slot."
    )
    job_id: str
    draft_revision: int
    expires_at: datetime
    stale: bool
    media: CandidateMedia


class CandidatePage(BaseModel):
    """A page of an entry's candidates, oldest first (generation order), and the cursor of the
    next page (``null`` at the end)."""

    items: list[ImageCandidate]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class LiveCandidate:
    """A live candidate's row: its asset and the asset's checksum."""

    id: str
    entry_kind: str
    entry_id: str
    job_id: str
    asset_id: str
    checksum: str
    slot: str
    draft_revision: int
    fingerprint: str
    expires_at: datetime


_CANDIDATE_COLUMNS = (
    image_candidates.c.id,
    image_candidates.c.entry_kind,
    image_candidates.c.entry_id,
    image_candidates.c.job_id,
    image_candidates.c.asset_id,
    image_candidates.c.slot,
    image_candidates.c.draft_revision,
    image_candidates.c.image_input_fingerprint,
    image_candidates.c.expires_at,
    media_assets.c.checksum,
)
_JOINED = image_candidates.join(media_assets, media_assets.c.id == image_candidates.c.asset_id)


def _live(row: Any) -> LiveCandidate:
    return LiveCandidate(
        id=str(row.id),
        entry_kind=str(row.entry_kind),
        entry_id=str(row.entry_id),
        job_id=str(row.job_id),
        asset_id=str(row.asset_id),
        checksum=str(row.checksum),
        slot=str(row.slot),
        draft_revision=int(row.draft_revision),
        fingerprint=str(row.image_input_fingerprint),
        expires_at=row.expires_at,
    )


def _is_ulid(value: str) -> bool:
    return re.fullmatch(ULID_PATTERN, value) is not None


async def live_candidate(
    conn: AsyncConnection, entry_kind: str, entry_id: str, candidate_id: str
) -> LiveCandidate:
    """The entry's candidate ``candidate_id`` while it lives (not past ``expires_at``); 404
    ``not_found`` for an unknown, another entry's, attached, discarded or expired one alike."""
    row = None
    if _is_ulid(candidate_id):
        row = (
            await conn.execute(
                select(*_CANDIDATE_COLUMNS)
                .select_from(_JOINED)
                .where(
                    image_candidates.c.id == candidate_id,
                    image_candidates.c.entry_kind == entry_kind,
                    image_candidates.c.entry_id == entry_id,
                    image_candidates.c.expires_at > clock.now(),
                    media_assets.c.state == MediaState.CANDIDATE.value,
                )
            )
        ).one_or_none()
    if row is None:
        key = "character_id" if entry_kind == "character" else "scenario_id"
        raise ImageCandidateNotFound(
            f"the {entry_kind} has no candidate {candidate_id.replace(chr(0), '')}",
            {key: entry_id, "candidate_id": candidate_id.replace("\x00", "")},
        )
    return _live(row)


def _encode_cursor(candidate_id: str) -> str:
    return base64.urlsafe_b64encode(candidate_id.encode()).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
    except ValueError, binascii.Error, UnicodeDecodeError:
        raw = ""
    if not _is_ulid(raw):
        raise InvalidRequest("not a candidate cursor", {"field": "query.cursor"})
    return raw


async def list_for_entry(
    conn: AsyncConnection,
    entry_kind: str,
    entry_id: str,
    *,
    current_fingerprint: str,
    job_id: str | None = None,
    limit: int = LIST_DEFAULT,
    cursor: str | None = None,
) -> CandidatePage:
    """The entry's live candidates (rows past ``expires_at`` are left out even before the expiry
    task runs), by job when ``job_id`` names one (an unknown job: an empty page), in keyset
    pages on the candidate id (generation order); ``stale`` against ``current_fingerprint``."""
    if not 1 <= limit <= LIST_MAX:
        raise InvalidRequest(f"limit must be 1 to {LIST_MAX}", {"field": "query.limit"})
    if job_id is not None and not _is_ulid(job_id):
        return CandidatePage(items=[], next_cursor=None)
    where = [
        image_candidates.c.entry_kind == entry_kind,
        image_candidates.c.entry_id == entry_id,
        image_candidates.c.expires_at > clock.now(),
        media_assets.c.state == MediaState.CANDIDATE.value,
    ]
    if job_id is not None:
        where.append(image_candidates.c.job_id == job_id)
    if cursor is not None:
        where.append(image_candidates.c.id > _decode_cursor(cursor))
    rows = (
        await conn.execute(
            select(
                *_CANDIDATE_COLUMNS,
                media_assets.c.mime_type,
                media_assets.c.size,
                media_assets.c.width,
                media_assets.c.height,
                media_assets.c.source,
                media_assets.c.check_status,
                media_assets.c.check_model,
                media_assets.c.check_endpoint,
                media_assets.c.policy_results,
                media_assets.c.generation_metadata,
            )
            .select_from(_JOINED)
            .where(*where)
            .order_by(image_candidates.c.id)
            .limit(limit + 1)
        )
    ).all()
    items = [
        ImageCandidate(
            candidate_id=str(row.id),
            slot=row.slot,
            job_id=str(row.job_id),
            draft_revision=int(row.draft_revision),
            expires_at=row.expires_at,
            stale=str(row.image_input_fingerprint) != current_fingerprint,
            media=CandidateMedia(
                mime_type=row.mime_type,
                size=int(row.size),
                width=row.width,
                height=row.height,
                source=MediaSource(row.source),
                check_status=CheckStatus(row.check_status),
                check=image_check_of(
                    row.check_status, row.check_model, row.check_endpoint, row.policy_results or ()
                ),
                generation_metadata=row.generation_metadata,
            ),
        )
        for row in rows[:limit]
    ]
    following = _encode_cursor(items[-1].candidate_id) if len(rows) > limit and items else None
    return CandidatePage(items=items, next_cursor=following)


# -- the writes -------------------------------------------------------------------------------


async def insert(
    conn: AsyncConnection,
    *,
    entry_kind: str,
    entry_id: str,
    job_id: str,
    slot: str,
    draft_revision: int,
    fingerprint: str,
    asset_ids: Sequence[str],
    ttl_s: int,
) -> list[str]:
    """One candidate row per promoted asset, in ``conn``'s transaction (the promotion's), each
    expiring ``ttl_s`` seconds from now; their ids, in the assets' order."""
    now = clock.now()
    expires = now + timedelta(seconds=ttl_s)
    ids = [new_ulid() for _ in asset_ids]
    if ids:
        await conn.execute(
            sql_insert(image_candidates),
            [
                {
                    "id": candidate_id,
                    "entry_kind": entry_kind,
                    "entry_id": entry_id,
                    "job_id": job_id,
                    "asset_id": asset_id,
                    "slot": slot,
                    "draft_revision": draft_revision,
                    "image_input_fingerprint": fingerprint,
                    "created_at": now,
                    "expires_at": expires,
                }
                for candidate_id, asset_id in zip(ids, asset_ids, strict=True)
            ],
        )
    return ids


def _candidate_ref(value: Any) -> str | None:
    if isinstance(value, Mapping) and set(value) == {"candidate_id"}:
        found = value["candidate_id"]
        return found if isinstance(found, str) else None
    return None


def referenced_ids(entry_kind: str, patch: Mapping[str, Any]) -> list[str]:
    """The candidate ids a draft merge patch names in the kind's image slots, in order and
    once each (one candidate may fill two slots)."""
    single, many = IMAGE_SLOTS[entry_kind]
    found: list[str] = []
    ref = _candidate_ref(_at(patch, *single))
    if ref is not None:
        found.append(ref)
    listed = _at(patch, *many)
    for item in listed if isinstance(listed, list) else []:
        ref = _candidate_ref(item)
        if ref is not None:
            found.append(ref)
    return list(dict.fromkeys(found))


def substituted(
    entry_kind: str, patch: Mapping[str, Any], media_ids: Mapping[str, str]
) -> dict[str, Any]:
    """``patch`` with each ``{candidate_id}`` in an image slot replaced by its media id."""
    single, many = IMAGE_SLOTS[entry_kind]
    copied: dict[str, Any] = json.loads(json.dumps(patch, default=str))

    def parent(path: tuple[str, ...]) -> dict[str, Any] | None:
        found: Any = copied
        for key in path[:-1]:
            found = found.get(key) if isinstance(found, dict) else None
        return found if isinstance(found, dict) else None

    holder = parent(single)
    if holder is not None:
        ref = _candidate_ref(holder.get(single[-1]))
        if ref is not None:
            holder[single[-1]] = media_ids[ref]
    holder = parent(many)
    if holder is not None and isinstance(holder.get(many[-1]), list):
        holder[many[-1]] = [
            media_ids[ref] if (ref := _candidate_ref(item)) is not None else item
            for item in holder[many[-1]]
        ]
    return copied


def _not_found(entry_kind: str, entry_id: str, ids: Sequence[str]) -> CandidateNotFound:
    key = "character_id" if entry_kind == "character" else "scenario_id"
    return CandidateNotFound(
        f"{len(ids)} candidate(s) cannot be attached to the {entry_kind}: unknown, expired, "
        "already attached or another entry's",
        {key: entry_id, "candidate_ids": list(ids)},
    )


async def resolve(
    conn: AsyncConnection, entry_kind: str, entry_id: str, candidate_ids: Sequence[str]
) -> dict[str, str]:
    """``{candidate_id: media_id}`` of the entry's live candidates ``candidate_ids``, read
    without a lock (``attach`` checks them again under one); 422 ``candidate_not_found``
    naming every one that is not."""
    found = await _rows(conn, entry_kind, entry_id, candidate_ids, lock=False)
    missing = [c for c in candidate_ids if c not in found]
    if missing:
        raise _not_found(entry_kind, entry_id, missing)
    return {c: found[c].asset_id for c in candidate_ids}


async def _rows(
    conn: AsyncConnection,
    entry_kind: str,
    entry_id: str,
    candidate_ids: Sequence[str],
    *,
    lock: bool,
) -> dict[str, LiveCandidate]:
    wanted = [c for c in dict.fromkeys(candidate_ids) if _is_ulid(c)]
    if not wanted:
        return {}
    query = (
        select(*_CANDIDATE_COLUMNS)
        .select_from(_JOINED)
        .where(
            image_candidates.c.id.in_(wanted),
            image_candidates.c.entry_kind == entry_kind,
            image_candidates.c.entry_id == entry_id,
            image_candidates.c.expires_at > clock.now(),
            media_assets.c.state == MediaState.CANDIDATE.value,
        )
        .order_by(image_candidates.c.id)
    )
    if lock:
        query = query.with_for_update(of=image_candidates)
    return {str(row.id): _live(row) for row in (await conn.execute(query)).all()}


async def attach(
    conn: AsyncConnection,
    entry_kind: str,
    entry_id: str,
    candidate_ids: Sequence[str],
    *,
    fingerprint: str,
    owner: str,
    principal: str,
) -> dict[str, str]:
    """Attach ``candidate_ids`` to the entry inside a draft save's transaction (see the module
    docstring): ``{candidate_id: media_id}``. 422 ``candidate_not_found`` (every offending
    candidate in ``details.candidate_ids``), then 409 ``candidate_stale`` (likewise, ``reason:
    stale``), in that precedence; nothing is changed by either."""
    found = await _rows(conn, entry_kind, entry_id, candidate_ids, lock=True)
    missing = [c for c in candidate_ids if c not in found]
    if missing:
        raise _not_found(entry_kind, entry_id, missing)
    stale = [c for c in candidate_ids if found[c].fingerprint != fingerprint]
    if stale:
        key = "character_id" if entry_kind == "character" else "scenario_id"
        raise CandidateStale(
            f"{len(stale)} candidate(s) were generated from other image inputs than the draft "
            "now has (its appearance, visual identity, true age, rating or style changed)",
            {key: entry_id, "candidate_ids": stale, "reason": "stale"},
        )
    assets = [found[c].asset_id for c in candidate_ids]
    owners = dict(
        (
            await conn.execute(
                select(media_assets.c.id, media_assets.c.owner_principal).where(
                    media_assets.c.id.in_(assets)
                )
            )
        ).all()
    )
    await conn.execute(
        update(media_assets)
        .where(media_assets.c.id.in_(assets))
        .values(state=MediaState.LIVE.value, owner_principal=owner)
    )
    for asset_id in assets:
        previous = owners.get(asset_id)
        if previous is not None and previous != owner:
            await record_event(
                conn,
                OwnershipEventKind.TRANSFER,
                EntityType.MEDIA,
                asset_id,
                old=previous,
                new=owner,
                principal=principal,
            )
    await conn.execute(delete(image_candidates).where(image_candidates.c.id.in_(list(found))))
    return {c: found[c].asset_id for c in candidate_ids}


async def _remove(conn: AsyncConnection, rows: Sequence[Any]) -> list[str]:
    """Delete candidate rows and their (still ``candidate``) asset rows; the checksums whose
    bytes may now be unnamed."""
    asset_ids = [str(r.asset_id) for r in rows]
    if not asset_ids:
        return []
    await conn.execute(
        delete(image_candidates).where(image_candidates.c.id.in_([str(r.id) for r in rows]))
    )
    checksums = list(
        await conn.scalars(
            delete(media_assets)
            .where(
                media_assets.c.id.in_(asset_ids),
                media_assets.c.state == MediaState.CANDIDATE.value,
            )
            .returning(media_assets.c.checksum)
        )
    )
    for asset_id in asset_ids:
        await drop_references(conn, RECORD_TABLE, asset_id)
    return sorted({str(c) for c in checksums})


async def discard(
    conn: AsyncConnection, entry_kind: str, entry_id: str, candidate_id: str
) -> list[str]:
    """Delete the entry's candidate ``candidate_id`` and its asset row (an author's discard):
    the checksums to collect after commit. Idempotent: an unknown, already discarded or
    already attached candidate changes nothing, and an attached candidate's ``live`` asset is
    never touched."""
    if not _is_ulid(candidate_id):
        return []
    rows = (
        await conn.execute(
            select(image_candidates.c.id, image_candidates.c.asset_id)
            .where(
                image_candidates.c.id == candidate_id,
                image_candidates.c.entry_kind == entry_kind,
                image_candidates.c.entry_id == entry_id,
            )
            .with_for_update()
        )
    ).all()
    return await _remove(conn, rows)


async def delete_for_entry(conn: AsyncConnection, entry_kind: str, entry_id: str) -> list[str]:
    """Delete every candidate of the entry with its asset row, inside the entry's delete
    transaction (#103): the checksums to collect after commit."""
    rows = (
        await conn.execute(
            select(image_candidates.c.id, image_candidates.c.asset_id)
            .where(
                image_candidates.c.entry_kind == entry_kind,
                image_candidates.c.entry_id == entry_id,
            )
            .with_for_update()
        )
    ).all()
    return await _remove(conn, rows)


async def entry_checksums(conn: AsyncConnection, entries: Sequence[tuple[str, str]]) -> list[str]:
    """The checksums of the candidates of ``entries`` (``(kind, id)``), read before a delete
    so their bytes can be collected once it commits."""
    found: set[str] = set()
    for entry_kind, entry_id in entries:
        found.update(
            str(c)
            for c in await conn.scalars(
                select(media_assets.c.checksum)
                .select_from(_JOINED)
                .where(
                    image_candidates.c.entry_kind == entry_kind,
                    image_candidates.c.entry_id == entry_id,
                )
            )
        )
    return sorted(found)


async def companions_of(conn: AsyncConnection, character_id: str) -> list[str]:
    """The character's companion scenarios (deleted with it, #116)."""
    return [
        str(i)
        for i in await conn.scalars(
            select(scenario_catalog.c.id).where(scenario_catalog.c.companion_of == character_id)
        )
    ]


async def expire_batch(
    conn: AsyncConnection, *, limit: int = EXPIRY_BATCH, now: datetime | None = None
) -> tuple[int, list[str]]:
    """Delete up to ``limit`` candidates past ``expires_at`` (oldest first; a row another
    transaction holds, such as an attach, is skipped): how many, and the checksums to collect
    after commit."""
    moment = now or clock.now()
    rows = (
        await conn.execute(
            select(image_candidates.c.id, image_candidates.c.asset_id)
            .where(image_candidates.c.expires_at <= moment)
            .order_by(image_candidates.c.expires_at, image_candidates.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    return len(rows), await _remove(conn, rows)


async def entry_owner(
    conn: AsyncConnection, entry_kind: str, entry_id: str, *, share: bool = False
) -> str | None:
    """The entry's owner (``share``: its row held ``FOR SHARE``), or ``None`` when it is gone."""
    table = character_catalog if entry_kind == "character" else scenario_catalog
    query = select(table.c.owner_principal).where(table.c.id == entry_id)
    if share:
        query = query.with_for_update(read=True)
    found = await conn.scalar(query)
    return None if found is None else str(found)


async def count_for_entry(conn: AsyncConnection, entry_kind: str, entry_id: str) -> int:
    """How many candidate rows the entry has, expired ones included (tests and reports)."""
    found = await conn.scalar(
        select(func.count())
        .select_from(image_candidates)
        .where(image_candidates.c.entry_kind == entry_kind, image_candidates.c.entry_id == entry_id)
    )
    return int(found or 0)


__all__ = [
    "CANDIDATES_TABLE",
    "EXPIRY_BATCH",
    "FINGERPRINT_VERSION",
    "IMAGE_SLOTS",
    "LIST_DEFAULT",
    "LIST_MAX",
    "SLOTS",
    "CandidateMedia",
    "CandidatePage",
    "ImageCandidate",
    "LiveCandidate",
    "attach",
    "companions_of",
    "count_for_entry",
    "delete_for_entry",
    "discard",
    "entry_checksums",
    "entry_owner",
    "expire_batch",
    "fingerprint_of",
    "image_input_fingerprint",
    "image_inputs",
    "insert",
    "jcs",
    "list_for_entry",
    "live_candidate",
    "referenced_ids",
    "resolve",
    "substituted",
]
