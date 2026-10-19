"""Span redaction and summary regeneration (FR-ADMIN-6; #124): every span of a text that refers
to the erased subject becomes ``[erased]``, in place, and nothing else of the text changes; the
Lite rolling-summary segments that lost a span are then summarised again from the redacted
window.

A ``Redactor`` finds the subject's mentions with the erasure matcher (``core.erasure.matcher``,
ADR-154), the one the subject-reference index records them with: its names, aliases and parts as
whole words or phrases on the NFC form of the text (Unicode word boundaries, so a possessive
``'s`` stays behind: ``Linda's`` becomes ``[erased]'s``). It redacts exactly the **certain**
mentions, plus the **candidates** (a common word of the name, capitalised at the start of a
sentence) the operator confirmed by id. No attempt is made to rewrite the grammar around a span
(M4's reflections are regenerated instead).

**Candidates.** A text redacted at a location (``at``: its record table, record id and field
path) records each candidate it holds (``candidates``): a ``Candidate`` with its
``candidate_id`` (``candidate_id``: a hash of the table, record, field, start and rule, so the
dry run's ids are the real run's), and a short snippet of the text around it for the operator,
which is returned and never stored. ``remaining`` is how many it met that were not confirmed;
``unknown`` the confirmed ids it never met.

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

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership, OperationKind
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.matcher import Mention, matcher, nfc, snippet
from epistrel.core.erasure.records import event_record
from epistrel.core.events.registry import validate
from epistrel.core.ports import SummaryCompressor, SummaryWindow
from epistrel.core.summary import read_included_segments
from epistrel.core.tables import events, lite_summary_segments
from epistrel.core.tables.catalog import ERASURE_GUC

ERASED = "[erased]"
"""What a redacted span (or a redacted beat, whole) reads as."""
CANDIDATE_ID_CHARS = 32

Where = tuple[str, str, str]
"""Where a text is: its record table, record id and field path."""
Located = tuple[str, str]
"""Where a document is: its record table and record id."""


def _id_like(key: str) -> bool:
    return key == "id" or key.endswith(("_id", "_ids"))


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def candidate_id(table: str, record_id: str, field: str, start: int, rule: str) -> str:
    """A candidate's stable id (see the module docstring)."""
    material = "\x1f".join((table, record_id, field, str(start), rule))
    return hashlib.sha256(material.encode()).hexdigest()[:CANDIDATE_ID_CHARS]


@dataclass(frozen=True, slots=True)
class Candidate:
    """A mention an erasure redacts only when confirmed: its id, where it is, the rule and a
    snippet of the text around it (returned only, never stored)."""

    candidate_id: str
    record_table: str
    record_id: str
    field: str
    snippet: str
    rule: str


class Redactor:
    """Finds and redacts one subject's mentions (see the module docstring)."""

    def __init__(
        self,
        names: Iterable[str],
        parts: Iterable[str] = (),
        *,
        confirmed: Iterable[str] = (),
    ) -> None:
        self.matcher = matcher(names, parts)
        self.terms: tuple[str, ...] = self.matcher.terms
        self.confirmed: frozenset[str] = frozenset(confirmed)
        self.candidates: dict[str, Candidate] = {}

    def hits(self, value: str | None) -> bool:
        """Whether ``value`` certainly names the subject."""
        return self.matcher.hits(value)

    def finds(self, value: str | None) -> bool:
        """Whether ``value`` holds a mention of the subject, a candidate included."""
        return self.matcher.finds(value)

    def _spans(self, value: str, at: Where | None) -> list[Mention]:
        spans: list[Mention] = []
        for mention in self.matcher.mentions(value):
            if mention.certain:
                spans.append(mention)
                continue
            if at is None:
                continue
            found = candidate_id(*at, mention.start, mention.rule.value)
            self.candidates.setdefault(
                found,
                Candidate(
                    found,
                    *at,
                    snippet(value, mention.start, mention.end),
                    mention.rule.value,
                ),
            )
            if found in self.confirmed:
                spans.append(mention)
        return spans

    def text(self, value: str, at: Where | None = None) -> str:
        """``value`` with every certain span, and every confirmed candidate (``at`` given),
        replaced by ``[erased]`` (``value`` itself when nothing matched)."""
        if not value:
            return value
        spans = self._spans(value, at)
        if not spans:
            return value
        normal = nfc(value)
        pieces: list[str] = []
        cursor = 0
        for span in spans:
            pieces += [normal[cursor : span.start], ERASED]
            cursor = span.end
        pieces.append(normal[cursor:])
        redacted = "".join(pieces)
        return value if redacted == normal else redacted

    def document(self, value: Any, at: Located | None = None, prefix: str = "") -> tuple[Any, int]:
        """A redacted copy of the JSON document ``value`` and how many strings changed; ``at``
        (and the leaves' paths under ``prefix``) locates its candidates."""
        changed = 0

        def walk(node: Any, path: str) -> Any:
            nonlocal changed
            if isinstance(node, str):
                new = self.text(node, None if at is None else (*at, path))
                if new != node:
                    changed += 1
                return new
            if isinstance(node, Mapping):
                return {
                    key: child
                    if isinstance(key, str) and _id_like(key)
                    else walk(child, _join(path, str(key)))
                    for key, child in node.items()
                }
            if isinstance(node, list | tuple):
                return [walk(child, f"{path}[{index}]") for index, child in enumerate(node)]
            return node

        return walk(value, prefix), changed

    def count(self, value: Any, at: Located | None = None, prefix: str = "") -> int:
        """How many strings of ``value`` (a text or a JSON document) a redaction at ``at``
        would change: those certainly naming the subject, and with ``at``, those holding a
        confirmed candidate."""
        if isinstance(value, str):
            return int(self.text(value, None if at is None else (*at, prefix)) != value)
        return self.document(value, at, prefix)[1]

    @property
    def remaining(self) -> int:
        """The candidates met that were not confirmed."""
        return sum(1 for found in self.candidates if found not in self.confirmed)

    @property
    def unknown(self) -> frozenset[str]:
        """The confirmed ids no text met."""
        return self.confirmed - self.candidates.keys()


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
    by_end = {s.covers_to: s.label for s in await read_included_segments(conn, story_id)}
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
        segment_id: (
            redactor.text(found.text, (events.name, segment_id, "payload.text")),
            found.model,
            found.prompt_hash,
        )
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
    "Candidate",
    "Redactor",
    "RegenerationOutcome",
    "candidate_id",
    "redacted_windows",
    "regenerate_summaries",
]
