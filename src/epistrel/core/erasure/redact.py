"""Span redaction and summary regeneration (FR-ADMIN-6; #124): every span of a text that names
the erased subject becomes ``[erased]``, in place, and nothing else of the text changes; the Lite
rolling-summary segments that lost a span are then summarised again from the redacted window.

A ``Redactor`` matches the subject's terms (its names and aliases, as ``core.erasure.index``
collects them) the way the subject-reference index finds them: as whole words or phrases
(Unicode word boundaries, so a possessive ``'s`` stays behind: ``Linda's`` becomes
``[erased]'s``), ignoring case, with any run of whitespace matching the term's spaces, on the
NFC form of the text; terms shorter than ``index.MIN_TERM_CHARS`` are never matched. Longer
terms win over their prefixes (``Lin`` never splits ``Linda``). No attempt is made to rewrite the
grammar around a span (M4's reflections are regenerated instead).

``document`` redacts a JSON document the way the index walks one (``records.text_fields``):
every string leaf except the values of id-like keys (``id``, ``*_id``, ``*_ids``), which name
records rather than people, and never a key. It answers the redacted copy and how many strings
changed.

**Regeneration** (``regenerate_summaries``, erasure's step 2). The windows are the level-0
segments still in the summary (``active`` or ``superseded``) whose event was redacted and whose
text still holds ``[erased]`` (``redacted_windows``): this erasure's, and any an earlier
erasure could not regenerate. They go to the ``SummaryCompressor`` port (#72's compression over
each window's transcript rows as they are now, redacted, under one run's budget of
``core.summary.MAX_CALLS_PER_RUN`` calls), and each new text that still names the subject is
redacted too. The new texts replace the redacted ones **in place**, in each segment's own event
(``epistrel.erasure``), by one ``consolidation`` operation: a segment keeps its position,
coverage and sources, so the summary's coverage and the projection's fold are unchanged, and
the operation's ``stage_log`` records the run. Without a model (``available`` false), or when a
call fails, the redacted text stands (``summary_regeneration_failed``); windows past the
budget stay redacted (``summary_regeneration_capped``) and the next erasure of the story tries
them again.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership, OperationKind
from epistrel.core.erasure.index import MIN_TERM_CHARS, index_records
from epistrel.core.erasure.records import event_record
from epistrel.core.events.registry import validate
from epistrel.core.ports import SummaryCompressor, SummaryWindow
from epistrel.core.summary import included_segments
from epistrel.core.tables import events, lite_summary_segments
from epistrel.core.tables.catalog import ERASURE_GUC

ERASED = "[erased]"
"""What a redacted span (or a redacted beat, whole) reads as."""


def _id_like(key: str) -> bool:
    return key == "id" or key.endswith(("_id", "_ids"))


class Redactor:
    """Finds and redacts one subject's terms (see the module docstring)."""

    def __init__(self, terms: Iterable[str]) -> None:
        cleaned = sorted(
            {
                " ".join(unicodedata.normalize("NFC", t).split())
                for t in terms
                if isinstance(t, str)
            },
            key=lambda t: (-len(t), t),
        )
        self.terms: tuple[str, ...] = tuple(t for t in cleaned if len(t) >= MIN_TERM_CHARS)
        if self.terms:
            alternatives = "|".join(
                r"\s+".join(re.escape(part) for part in term.split()) for term in self.terms
            )
            self._pattern: re.Pattern[str] | None = re.compile(
                rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE
            )
        else:
            self._pattern = None

    def hits(self, value: str | None) -> bool:
        """Whether ``value`` names the subject."""
        if not value or self._pattern is None:
            return False
        return self._pattern.search(unicodedata.normalize("NFC", value)) is not None

    def text(self, value: str) -> str:
        """``value`` with every span naming the subject replaced by ``[erased]`` (``value``
        itself when nothing matched)."""
        if not value or self._pattern is None:
            return value
        normal = unicodedata.normalize("NFC", value)
        redacted = self._pattern.sub(ERASED, normal)
        return value if redacted == normal else redacted

    def document(self, value: Any) -> tuple[Any, int]:
        """A redacted copy of the JSON document ``value`` and how many strings changed."""
        changed = 0

        def walk(node: Any) -> Any:
            nonlocal changed
            if isinstance(node, str):
                new = self.text(node)
                if new != node:
                    changed += 1
                return new
            if isinstance(node, Mapping):
                return {
                    key: child if isinstance(key, str) and _id_like(key) else walk(child)
                    for key, child in node.items()
                }
            if isinstance(node, list | tuple):
                return [walk(child) for child in node]
            return node

        return walk(value), changed

    def count(self, value: Any) -> int:
        """How many strings of ``value`` (a text or a JSON document) name the subject."""
        if isinstance(value, str):
            return 1 if self.hits(value) else 0
        return self.document(value)[1]


# -- summary regeneration ------------------------------------------------------------------------

REGENERATION_FAILED = "summary_regeneration_failed"
REGENERATION_CAPPED = "summary_regeneration_capped"
SUMMARY_LEVEL = 0


@dataclass(frozen=True, slots=True)
class RegenerationOutcome:
    """What step 2 did: ``status`` (``done``, ``partial``, ``failed`` or ``skipped``: nothing
    to regenerate), the windows it rewrote and found, and the ``degradation`` it reports."""

    status: str
    regenerated: int = 0
    windows: int = 0
    degradation: tuple[str, ...] = ()
    operation_id: str | None = None


async def redacted_windows(conn: AsyncConnection, story_id: str) -> list[SummaryWindow]:
    """The level-0 segments in the summary whose text was redacted and still holds
    ``[erased]``, oldest first, each with the included segment just before it as context."""
    t = lite_summary_segments
    rows = (
        await conn.execute(
            select(t.c.segment_id, t.c.covers_from, t.c.covers_to)
            .where(
                t.c.story_id == story_id,
                t.c.level == SUMMARY_LEVEL,
                t.c.membership.in_((Membership.ACTIVE.value, Membership.SUPERSEDED.value)),
                t.c.redacted_at.is_not(None),
                func.strpos(t.c.text, ERASED) > 0,
            )
            .order_by(t.c.covers_from, t.c.seq)
        )
    ).all()
    if not rows:
        return []
    by_end = {s.covers_to: s.label for s in await included_segments(conn, story_id)}
    return [
        SummaryWindow(
            segment_id=str(r.segment_id),
            covers_from=int(r.covers_from),
            covers_to=int(r.covers_to),
            prior=by_end.get(int(r.covers_from) - 1),
        )
        for r in rows
    ]


async def regenerate_summaries(
    db: AsyncEngine,
    story_id: str,
    redactor: Redactor,
    compressor: SummaryCompressor,
    *,
    principal: str,
) -> RegenerationOutcome:
    """Erasure's step 2 (see the module docstring)."""
    from epistrel.core.events.operation import Operation  # the commit path imports this module

    async with db.connect() as conn:
        windows = await redacted_windows(conn, story_id)
    if not windows:
        return RegenerationOutcome("skipped")
    if not compressor.available:
        return RegenerationOutcome("failed", 0, len(windows), (REGENERATION_FAILED,))
    made = await compressor.regenerate(db, story_id, windows)
    texts = {
        segment_id: (redactor.text(found.text), found.model, found.prompt_hash)
        for segment_id, found in made.texts.items()
        if found.text.strip()
    }
    degradation: list[str] = []
    if made.failed:
        degradation.append(REGENERATION_FAILED)
    if made.capped:
        degradation.append(REGENERATION_CAPPED)
    status = "done" if len(texts) == len(windows) else "partial" if texts else "failed"
    if not texts:
        if not degradation:
            degradation.append(REGENERATION_FAILED)
        return RegenerationOutcome(status, 0, len(windows), tuple(degradation))

    async def step(conn: AsyncConnection) -> None:
        await conn.execute(select(func.set_config(ERASURE_GUC, "on", True)))
        rewritten = []
        for segment_id, (new_text, model, prompt) in texts.items():
            row = (
                await conn.execute(
                    select(
                        events.c.id,
                        events.c.story_id,
                        events.c.character_id,
                        events.c.type,
                        events.c.payload,
                    ).where(events.c.id == segment_id)
                )
            ).one_or_none()
            if row is None:
                continue
            payload = {**dict(row.payload), "text": new_text, "model": model, "prompt_hash": prompt}
            validate(row.type, payload)
            await conn.execute(
                update(events)
                .where(events.c.id == segment_id)
                .values(payload=payload, redacted_at=func.transaction_timestamp())
            )
            rewritten.append(event_record(_Row(row.id, row.story_id, row.character_id, payload)))
        await index_records(conn, rewritten)

    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONSOLIDATION,
        principal=principal,
        erasure=True,
    ) as op:
        op.stage("regenerated", status)
        op.add_commit_step(step)
        await op.commit()
    return RegenerationOutcome(
        status, len(texts), len(windows), tuple(degradation), op.operation_id
    )


@dataclass(frozen=True, slots=True)
class _Row:
    id: str
    story_id: str
    character_id: str | None
    payload: Any


__all__ = [
    "ERASED",
    "REGENERATION_CAPPED",
    "REGENERATION_FAILED",
    "Redactor",
    "RegenerationOutcome",
    "redacted_windows",
    "regenerate_summaries",
]
