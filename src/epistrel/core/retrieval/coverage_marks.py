"""Where a story's data is missing, marked on every read it could affect (FR-PROF-8; #229).

A ``forward_only`` upgrade (or a backfill not yet run) leaves the story's turns before the switch
without the data a data-producing capability writes: ``core.backfill.coverage.gaps``, the same
function a backfill plans from, names them. The reads mark them instead of hiding them:

- **retrieval** (``GET /stories/{id}/retrieve`` and each structured turn's
  ``turn_report.retrieval``) lists ``coverage_gaps: [{capability, from_turn, to_turn}]``, every gap
  of a capability retrieval reads (``RETRIEVAL_READERS``) that overlaps the query's window;
- **verification**: each ``turn_report.verify`` verdict carries ``gap_affected: {capability,
  through_turn}`` when a capability its evidence search reads (``VERIFICATION_READERS``: canon
  is the structured pipeline's) has a gap at or before the claim's turn. The mark does not
  block: a ``novel`` verdict under a gap is accepted as before;
- the respond response lists the turn's ``coverage_gaps`` at its top level when any applies.

A story with no gap has ``coverage_gaps: []`` and no verdict carries ``gap_affected``. Only a
capability the story has on can have a gap (one that is off writes nothing, by design).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.backfill import coverage
from epistrel.core.profiles.schema import ProfileSettings

RETRIEVAL_READERS = ("pipeline", "consolidation", "partitions", "nested_beliefs")
"""The data-producing capabilities whose records retrieval reads: the structured pipeline's
memories and canon, consolidation's episodes and assessments, the partitions' private records
and nested beliefs."""
VERIFICATION_READERS = ("pipeline", "verification")
"""The capabilities a verdict's evidence search reads: canon and the verified claims."""


class CoverageGap(BaseModel):
    """A run of turns a capability has no data for."""

    model_config = ConfigDict(frozen=True)

    capability: str
    from_turn: int
    to_turn: int


async def gaps(
    conn: AsyncConnection,
    story_id: str,
    settings: ProfileSettings,
    *,
    readers: Sequence[str] = RETRIEVAL_READERS,
    window: tuple[int | None, int | None] | None = None,
) -> list[CoverageGap]:
    """The story's gaps of ``readers`` it has on that overlap ``window`` (both ends included;
    ``None``: open)."""
    levels = coverage.capabilities_of(settings)
    low, high = window or (None, None)
    found: list[CoverageGap] = []
    for capability in readers:
        if capability not in levels:
            continue
        for first, last in await coverage.gaps(
            conn, story_id, capability, level=levels[capability]
        ):
            if (high is None or first <= high) and (low is None or last >= low):
                found.append(CoverageGap(capability=capability, from_turn=first, to_turn=last))
    return found


def gap_affected(found: Sequence[CoverageGap], turn: int) -> dict[str, Any] | None:
    """A verdict's ``gap_affected`` for a claim at ``turn``: the first gap at or before it."""
    for gap in found:
        if gap.from_turn <= turn:
            return {"capability": gap.capability, "through_turn": gap.to_turn}
    return None


def mark_verdicts(report: dict[str, Any], found: Sequence[CoverageGap], turn: int) -> None:
    """Add ``gap_affected`` to each verdict of a ``turn_report.verify`` section."""
    mark = gap_affected(found, turn)
    if mark is None:
        return
    for verdict in report.get("verdicts", []):
        if isinstance(verdict, dict):
            verdict["gap_affected"] = dict(mark)


__all__ = [
    "RETRIEVAL_READERS",
    "VERIFICATION_READERS",
    "CoverageGap",
    "gap_affected",
    "gaps",
    "mark_verdicts",
]
