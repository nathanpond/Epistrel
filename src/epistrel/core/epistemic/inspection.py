"""The omniscient inspection reads of a story's character knowledge (FR-ADMIN-5, FR-RESP-12,
FR-AUTH-2; docs/02 §9's ``/stories/{id}/inspect/…``; C-28; #178).

Four reads sit beside #146's ledger: a character's belief projection (``partitions.inspect``,
#159), the story's divergences (``inspect_divergences``: #166's lie records, by
``divergences.divergences_of``) and its exposures (``inspect_exposures``: #164's records, by
``exposure.exposures_of``) and #207's snapshotted external facts
(``core.states.external.inspect``, which canon facts join, so it lives beside the pins: FR-EPI-1
keeps this module off canon). Each is a **raw read**: it never resolves an exposure, never
writes, never touches an access time.

**The gate** (``omniscient_read``). An inspection names ``omniscient`` or is refused: no named
viewpoint is 422 ``viewpoint_unresolved`` (inspection never infers one: there is no actor and
no story default may be omniscient), a character viewpoint 403 ``omniscient_required``. Service
authentication is the only other check: which callers may inspect is the consumer's decision
(S-TRUST-BOUNDARY; the same answer under any principal, over REST and the library).

**Module off** (``require_partitions``). A story whose effective configuration keeps no
character partitions (a Lite story, or ``character_knowledge.partitions`` off or undelivered)
holds no beliefs, exposures or divergences: 409 ``module_off``. The external-facts read has no
such gate: a provider's value is no character's knowledge.

Pages are by id (``limit``, ``after_id``), oldest first; ``as_of=<turn>`` reads the corrected
view through that turn (FR-BELIEF-14: a resolution or a record a later turn made is not yet
one). Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import ExposureState
from epistrel.core.epistemic import divergences as divergence_records
from epistrel.core.epistemic import exposure as exposure_records
from epistrel.core.epistemic.transitions import PARTITIONS_MODULE
from epistrel.core.errors import InvalidRequest, ModuleOff, ViewpointUnresolved
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.propositions.codec import proposition_from_row
from epistrel.core.propositions.model import Proposition
from epistrel.core.tables import characters, propositions
from epistrel.core.viewpoint import require_omniscient

INSPECT_LIMIT_DEFAULT = 100
INSPECT_LIMIT_MAX = 500


def omniscient_read(viewpoint: str | None, *, field: str = "query.viewpoint") -> str:
    """The inspection gate: ``viewpoint`` names ``omniscient`` (answered), or the read is
    refused (422 ``viewpoint_unresolved`` when none is named, 403 ``omniscient_required`` for
    any other)."""
    if viewpoint is None or not viewpoint.strip():
        raise ViewpointUnresolved(
            "an inspection names its viewpoint: it is an omniscient read",
            {"field": field, "reason": "unresolved"},
        )
    named = viewpoint.strip()
    require_omniscient(named, field=field)
    return named


async def require_partitions(conn: AsyncConnection, story_id: str) -> None:
    """409 ``module_off`` unless the story's effective configuration keeps partitions."""
    found = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(found, PARTITIONS_MODULE):
        raise ModuleOff(
            "the story's configuration keeps no character partitions, so it has nothing to inspect",
            {"story_id": story_id, "module": PARTITIONS_MODULE},
        )


async def check_character(
    conn: AsyncConnection, story_id: str, character: str, *, field: str
) -> None:
    """422 ``validation_error`` (``unknown_character``) unless ``character`` is in the cast."""
    known = await conn.scalar(
        select(characters.c.id).where(
            characters.c.story_id == story_id, characters.c.id == character
        )
    )
    if known is None:
        raise InvalidRequest(
            "character is one of the story's characters",
            {"field": field, "reason": "unknown_character"},
        )


async def _propositions(
    conn: AsyncConnection, story_id: str, ids: Sequence[str | None]
) -> dict[str, Proposition]:
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    p = propositions.c
    found = await conn.execute(select(propositions).where(p.story_id == story_id, p.id.in_(wanted)))
    return {
        str(r.id): proposition_from_row(
            {k: v for k, v in r._mapping.items() if k not in ("id", "story_id")}
        )
        for r in found
    }


# -- divergences (#166) -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DivergenceItem:
    """One divergence with the propositions it names (the believed one ``None`` for a lie no
    record settles)."""

    row: divergence_records.DivergenceRow
    asserted: Proposition
    believed: Proposition | None


@dataclass(frozen=True, slots=True)
class DivergencePage:
    story_id: str
    revision: int
    items: tuple[DivergenceItem, ...]
    next_after_id: str | None


async def inspect_divergences(
    db: AsyncEngine,
    story_id: str,
    *,
    speaker: str | None = None,
    subject: str | None = None,
    as_of: int | None = None,
    limit: int = INSPECT_LIMIT_DEFAULT,
    after_id: str | None = None,
) -> DivergencePage:
    """The story's divergences on the active timeline, by id after ``after_id``, ``limit`` a
    page: only ``speaker``'s, only those whose asserted proposition is about ``subject`` (an
    entity id), only those positioned at or before turn ``as_of``. 404 for an unknown story,
    409 ``module_off`` without partitions, 422 for a ``speaker`` outside the cast."""
    from epistrel.core.concurrency import check_expected_revision

    limit = max(1, min(INSPECT_LIMIT_MAX, limit))
    async with db.connect() as conn:
        revision = await check_expected_revision(conn, story_id, None)
        await require_partitions(conn, story_id)
        if speaker is not None:
            await check_character(conn, story_id, speaker, field="query.speaker")
        rows = [
            r
            for r in await divergence_records.divergences_of(conn, story_id, speaker=speaker)
            if (after_id is None or r.id > after_id) and (as_of is None or r.position.turn <= as_of)
        ]
        held = await _propositions(
            conn,
            story_id,
            [x for r in rows for x in (r.asserted_proposition_id, r.believed_proposition_id)],
        )
    items = [
        DivergenceItem(
            r,
            held[r.asserted_proposition_id],
            None if r.believed_proposition_id is None else held.get(r.believed_proposition_id),
        )
        for r in rows
        if r.asserted_proposition_id in held
        and (subject is None or held[r.asserted_proposition_id].subject_id == subject)
    ]
    page = items[:limit]
    more = len(items) > limit
    return DivergencePage(story_id, revision, tuple(page), page[-1].row.id if more else None)


# -- exposures (#164) ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExposurePage:
    story_id: str
    revision: int
    items: tuple[exposure_records.Exposure, ...]
    next_after_id: str | None


async def inspect_exposures(
    db: AsyncEngine,
    story_id: str,
    *,
    character: str | None = None,
    state: ExposureState | None = None,
    as_of: int | None = None,
    limit: int = INSPECT_LIMIT_DEFAULT,
    after_id: str | None = None,
) -> ExposurePage:
    """The story's exposures on the active timeline (``character``'s as the witness; in
    effective ``state`` as the record stood through turn ``as_of``), by id after ``after_id``,
    ``limit`` a page. Never resolves anything. 404 for an unknown story, 409 ``module_off``
    without partitions, 422 for a ``character`` outside the cast."""
    from epistrel.core.concurrency import check_expected_revision

    limit = max(1, min(INSPECT_LIMIT_MAX, limit))
    async with db.connect() as conn:
        revision = await check_expected_revision(conn, story_id, None)
        await require_partitions(conn, story_id)
        if character is not None:
            await check_character(conn, story_id, character, field="query.character")
        found = await exposure_records.exposures_of(
            conn,
            story_id,
            character,
            state=None if state is None else ExposureState(state),
            as_of_turn=as_of,
        )
    rows = sorted(
        (
            x
            for x in found
            if (after_id is None or x.id > after_id) and (as_of is None or x.position.turn <= as_of)
        ),
        key=lambda x: x.id,
    )
    page = rows[:limit]
    return ExposurePage(story_id, revision, tuple(page), page[-1].id if len(rows) > limit else None)


__all__ = [
    "INSPECT_LIMIT_DEFAULT",
    "INSPECT_LIMIT_MAX",
    "DivergenceItem",
    "DivergencePage",
    "ExposurePage",
    "check_character",
    "inspect_divergences",
    "inspect_exposures",
    "omniscient_read",
    "require_partitions",
]
