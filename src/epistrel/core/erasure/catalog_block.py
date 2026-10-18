"""The catalog's restore window: what a pending catalog-scope erasure reaches is not served until
it has been reapplied (FR-ADMIN-7, ADR-157; #346).

After a restore, ``ledger.mark`` makes every erasure the database lacks pending. A story-scope
one blocks its story (``blocked_by_erasure``). A catalog-scope one blocks no story, but until the
reconciler reapplies it the restored catalog still holds the erased subject. While it is
pending, everything its reapply would touch (``erase.catalog_reach``: the subject's own entry,
its companion scenarios, every catalog record the subject-reference index names for it or its
terms match, the media that depict or name it, the deployment-scoped stored results and events
that name it, and the creation reports that carry its public card) is **reached**:

- a call naming a reached entry (a catalog character, scenario, persona, media asset or upload
  session, by its id) is refused, reads and writes alike, with 503 ``erasure_pending``
  (``Retry-After: 30``; ``details``: ``story_id`` null, the first pending erasure that reaches
  it and how many do). Deletes are refused too: deleting the subject would take the terms its
  reapply redacts the other records by;
- a list or search leaves reached entries out, and the principal lookup lists them with a
  ``null`` name, as it lists a blocked story (#359);
- an operation read (``GET /operations/{id}``) or an idempotent replay whose stored result, or
  whose creation report, is reached is refused.

``read`` answers ``None`` when nothing catalog-scope is pending (one query); otherwise it
computes each pending entry's reach afresh, so a catalog write made during the window is seen.
Nothing is cached, and nothing is written. The block lifts entry by entry as the reapply settles
them (catalog entries go first, ``ledger.reapply``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.erasure import erase
from epistrel.core.erasure import ledger as ledger_store
from epistrel.core.erasure.records import idempotency_id
from epistrel.core.errors import ErasurePending
from epistrel.core.tables import (
    character_catalog,
    character_versions,
    idempotency_keys,
    media_assets,
    operations,
    personas,
    scenario_catalog,
    scenario_versions,
)
from epistrel.core.tables.media import media_upload_sessions
from epistrel.core.tables.ownership import EntityType

CHARACTER, SCENARIO, PERSONA, MEDIA = (
    EntityType.CHARACTER.value,
    EntityType.SCENARIO.value,
    EntityType.PERSONA.value,
    EntityType.MEDIA.value,
)
UPLOAD = "upload"
ENTRY_TABLES: Mapping[str, tuple[str, ...]] = {
    CHARACTER: (character_catalog.name, character_versions.name),
    SCENARIO: (scenario_catalog.name, scenario_versions.name),
    PERSONA: (personas.name,),
    MEDIA: (media_assets.name,),
    UPLOAD: (media_upload_sessions.name,),
}
"""The record tables of each kind of catalog entry: a version's record id is ``<id>@<n>``."""
STORY_KEY_SCOPE = "story"


@dataclass(frozen=True, slots=True)
class Reach:
    """One pending catalog-scope erasure and the records its reapply would touch."""

    erasure_id: str
    records: Mapping[str, frozenset[str]]

    def touches(self, table: str, record_id: str) -> bool:
        return record_id in self.records.get(table, frozenset())

    def touches_entry(self, kind: str, entry_id: str) -> bool:
        """Whether the entry ``entry_id`` of ``kind`` (``ENTRY_TABLES``), its draft or any of
        its versions, is reached."""
        version = f"{entry_id}@"
        for table in ENTRY_TABLES[kind]:
            found = self.records.get(table, frozenset())
            if entry_id in found or any(r.startswith(version) for r in found):
                return True
        return False


@dataclass(frozen=True, slots=True)
class CatalogBlock:
    """Every pending catalog-scope erasure's reach, in ``at`` order."""

    reaches: tuple[Reach, ...]

    def hides(self, kind: str, entry_id: str) -> bool:
        """Whether a list leaves the entry out (a lookup lists it without its name)."""
        return any(reach.touches_entry(kind, entry_id) for reach in self.reaches)

    def check_entry(self, kind: str, entry_id: str) -> None:
        """503 ``erasure_pending`` when a pending erasure reaches the entry."""
        self._refuse([r for r in self.reaches if r.touches_entry(kind, entry_id)])

    def check_records(self, records: Iterable[tuple[str, str]]) -> None:
        """503 ``erasure_pending`` when a pending erasure reaches any of ``records``
        (``(record_table, record_id)``)."""
        wanted = list(records)
        self._refuse([r for r in self.reaches if any(r.touches(t, i) for t, i in wanted)])

    @staticmethod
    def _refuse(reaching: list[Reach]) -> None:
        if reaching:
            raise ErasurePending(
                "this is not served until the erasure ledger's catalog erasures that reach it "
                "have been reapplied",
                {
                    "story_id": None,
                    "erasure_id": reaching[0].erasure_id,
                    "pending_count": len(reaching),
                },
            )


async def read(conn: AsyncConnection) -> CatalogBlock | None:
    """The block, or ``None`` while no catalog-scope erasure is pending."""
    pending = await ledger_store.pending_catalog(conn)
    if not pending:
        return None
    reaches: list[Reach] = []
    for item in pending:
        found = await erase.catalog_reach(conn, item.subject_kind, item.subject_id, item.confirmed)
        reaches.append(Reach(item.erasure_id, {t: frozenset(ids) for t, ids in found.items()}))
    return CatalogBlock(tuple(reaches))


async def operation_records(conn: AsyncConnection, operation_id: str) -> list[tuple[str, str]]:
    """The records an operation read serves: the operation (its creation report) and its
    deployment-scoped stored results (a story's are story records, which a catalog-scope
    erasure leaves alone)."""
    k = idempotency_keys.c
    keys = await conn.execute(
        select(k.scope, k.scope_id, k.key).where(
            k.operation_id == operation_id, k.scope != STORY_KEY_SCOPE
        )
    )
    return [(operations.name, operation_id)] + [
        (idempotency_keys.name, idempotency_id(str(r.scope), str(r.scope_id), str(r.key)))
        for r in keys
    ]


def key_record(scope: str, scope_id: str, key: str) -> tuple[str, str]:
    """A stored result's record, as the index names it."""
    return idempotency_keys.name, idempotency_id(scope, scope_id, key)


__all__ = [
    "CHARACTER",
    "ENTRY_TABLES",
    "MEDIA",
    "PERSONA",
    "SCENARIO",
    "UPLOAD",
    "CatalogBlock",
    "Reach",
    "key_record",
    "operation_records",
    "read",
]
