"""The resurrection guard: no write brings an erased subject back (FR-ADMIN-9; #127).

An erasure (#124) bumps the story's revision as a control action, so a turn generating
meanwhile abandons at commit (#47). What it cannot stop by itself is text built from the old
context: a worker's result, a model's output, a client's write prepared before the erasure.
``check_content`` stops those. It runs on every text and every structured reference an
operation writes, inside the commit transaction (``Operation._write``, so ``commit``,
``write_in``, ingest batches and job results share it), after #47's revalidation and the commit
steps and before the subject index is written.

**What it compares.** The erasure ledger (``erasure_ledger``) holds, per erasure, the keyed
hashes of the certain terms it scanned (``scan_term_hashes``: HMAC-SHA256 under
``EPISTREL_CONFIG_KEY`` of each term the erasure matcher guards, ``erase.term_hashes``; ADR-154),
never the terms. The guard hashes every span of one to ``MAX_NGRAM`` words of each text
(normalised: the matcher's fold, ``matcher.normalized``, whitespace runs as one space;
ADR-155, ADR-156; spans start and end at word boundaries, so a possessive ``'s`` still
matches), and every word capitalised in the middle of a sentence in its marked form
(``matcher.guard_spans``: a name word that is a common word is guarded only so, never "it
will rain"), with the same key and looks for a hash the ledger holds. Structured references
are compared by id: an event's speaking character, its ``character_id`` and ``*character_ids``
keys, against the erasure's subject and the story characters it deleted (its
``erasure_marker`` events on ``characters``).

**Whose text.** Engine, system and model text is always checked: it comes from context the
Engine assembled, which may predate the erasure. Text a consumer wrote (``player`` and
``consumer`` provenance) is checked only when the operation's base revision predates the
erasure; a consumer who types the name in a new turn afterwards is not refused (the owner's
decision on #127). The erasure's own operations and #125's reapply (``Operation(erasure=True)``)
are exempt.

**Which erasures.** The story-scope ledger rows of the story (``story_ref``), its own and those
a fork copied (``copied_from``; a copied row has no erasure revision in this story, so consumer
text is never refused by it). Catalog-scope erasures leave stories alone (FR-STORE-14): they
are consulted for structured references to the erased catalog id only, by ``assert_not_erased``
in the catalog and media writers (prose in an untouched story is never refused).

**The key.** ``bind_key`` registers the configuration key of every Engine built in the process
(the hashes are keyed with the deployment's key). Without one, a check that needs a prose
comparison answers 503 ``config_key_missing``; rotating the key disables the guard for the
earlier erasures (``docs/OPERATIONS.md``), because the names are gone by design.

A refusal is 409 ``erased_subject_referenced`` (``ErasedSubjectReferenced``: ``erasure_id``,
``match_count``). The hashes are read on every commit, under the story lock; nothing is cached.
Log lines hold identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Provenance
from epistrel.core.erasure.matcher import MIN_TERM_CHARS, WORD, guard_spans, normalized
from epistrel.core.erasure.records import character_ids, text_fields
from epistrel.core.errors import ErasedSubjectReferenced, ErasureKeyMissing
from epistrel.core.tables import events, operations
from epistrel.core.tables.erasure import erasure_ledger
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

MAX_NGRAM = 6
"""The longest span, in words, a text is hashed in (a name with its titles and particles)."""
STORY_SCOPE = "story"
CATALOG_SCOPE = "catalog"
CONSUMER_PROVENANCE = frozenset({Provenance.PLAYER, Provenance.CONSUMER})
"""Text a consumer wrote: checked only when its operation's base predates the erasure."""
MESSAGE = (
    "This operation references an erased subject; rebuild your context from the current "
    "history and retry."
)
_WORD = WORD

_KEYS: list[str] = []


def bind_key(key: str | None) -> None:
    """Register ``key`` (an Engine's ``EPISTREL_CONFIG_KEY``) as one the ledger's hashes may be
    keyed with. Every Engine built in the process binds its own; ``None`` is ignored."""
    if key and key not in _KEYS:
        _KEYS.append(key)


def bound_keys() -> tuple[str, ...]:
    return tuple(_KEYS)


# -- the comparison ------------------------------------------------------------------------------


def spans(value: str) -> Iterator[str]:
    """Every span of one to ``MAX_NGRAM`` words of ``value`` once normalised (see the module
    docstring), as a whole-word match of a term would cover it, then its marked capitalised
    mid-sentence words."""
    text = normalized(value)
    words = [(m.start(), m.end()) for m in _WORD.finditer(text)]
    for i, (start, _) in enumerate(words):
        for _, end in words[i : i + MAX_NGRAM]:
            if end - start >= MIN_TERM_CHARS:
                yield text[start:end]
    yield from guard_spans(value)


def span_hashes(values: Iterable[str], key: str) -> set[str]:
    """The keyed hashes of every span of ``values`` (``erase.term_hashes``'s construction)."""
    secret = key.encode()
    return {
        hmac.new(secret, span.encode(), hashlib.sha256).hexdigest()
        for value in values
        for span in spans(value)
    }


@dataclass(frozen=True, slots=True)
class Content:
    """What one record of an operation writes: its texts, the subject ids it references and
    whether a consumer wrote it."""

    texts: tuple[str, ...]
    references: frozenset[str] = frozenset()
    consumer: bool = False


@dataclass(frozen=True, slots=True)
class Erased:
    """A story-scope erasure the guard enforces: its id, the ids it erased (the subject and the
    story characters it deleted), its term hashes and the revision it committed at (``None``:
    not committed in this story, a fork's copy or a restored database's reapply)."""

    erasure_id: str
    ids: frozenset[str]
    hashes: frozenset[str]
    revision: int | None


async def story_erasures(conn: AsyncConnection, story_id: str) -> list[Erased]:
    """The story's story-scope ledger rows, with the characters each deleted (its markers)."""
    el = erasure_ledger.c
    found = (
        await conn.execute(
            select(el.id, el.subject_id, el.scan_term_hashes, operations.c.timeline_revision)
            .select_from(
                erasure_ledger.outerjoin(
                    operations,
                    and_(
                        operations.c.operation_id == el.operation_id,
                        operations.c.story_id == story_id,
                    ),
                )
            )
            .where(el.scope == STORY_SCOPE, el.story_ref == story_id)
            .order_by(el.at, el.id)
        )
    ).all()
    if not found:
        return []
    deleted: dict[str, set[str]] = {}
    markers = await conn.execute(
        select(events.c.payload).where(
            events.c.story_id == story_id, events.c.type == "erasure_marker"
        )
    )
    for (payload,) in markers:
        if isinstance(payload, Mapping) and payload.get("record_table") == "characters":
            deleted.setdefault(str(payload.get("erasure_id")), set()).add(
                str(payload.get("record_id"))
            )
    return [
        Erased(
            erasure_id=str(row.id),
            ids=frozenset({str(row.subject_id), *deleted.get(str(row.id), set())}),
            hashes=frozenset(row.scan_term_hashes or ()),
            revision=None if row.timeline_revision is None else int(row.timeline_revision),
        )
        for row in found
    ]


_SPY: ContextVar[list[str] | None] = ContextVar("erasure_guard_spy", default=None)


@contextmanager
def recording() -> Iterator[list[str]]:
    """Record the write path of every ``check_content``/``assert_not_erased`` call made inside
    the block (the coverage test's runtime half)."""
    calls: list[str] = []
    token = _SPY.set(calls)
    try:
        yield calls
    finally:
        _SPY.reset(token)


def _spied(path: str) -> None:
    calls = _SPY.get()
    if calls is not None:
        calls.append(path)


DISABLED: ContextVar[bool] = ContextVar("erasure_guard_disabled", default=False)
"""Test-only: true turns the commit-time check off (the scenario's run without the hook shows a
late worker result would otherwise be written). Never set by the Engine."""


async def check_content(
    conn: AsyncConnection,
    story_id: str,
    contents: Sequence[Content],
    *,
    base_revision: int | None,
    path: str = "epistrel.core.events.operation",
) -> None:
    """Refuse (409 ``erased_subject_referenced``) when one of ``contents`` names, or references,
    a subject erased from ``story_id`` (see the module docstring)."""
    _spied(path)
    if DISABLED.get() or not contents:
        return
    erased = await story_erasures(conn, story_id)
    if not erased:
        return
    matches: dict[str, int] = {}
    hashed: dict[int, set[str]] = {}
    for entry in erased:
        for index, content in enumerate(contents):
            if content.references & entry.ids:
                matches[entry.erasure_id] = matches.get(entry.erasure_id, 0) + 1
                continue
            if not content.texts or not entry.hashes:
                continue
            if content.consumer and (
                entry.revision is None
                or (base_revision is not None and base_revision >= entry.revision)
            ):
                continue
            if index not in hashed:
                hashed[index] = _hashes(content.texts)
            if hashed[index] & entry.hashes:
                matches[entry.erasure_id] = matches.get(entry.erasure_id, 0) + 1
    if matches:
        erasure_id = next(iter(matches))
        log.info(
            "erased subject referenced",
            story_id=story_id,
            erasure_id=erasure_id,
            match_count=sum(matches.values()),
            write_path=path,
        )
        raise ErasedSubjectReferenced(
            MESSAGE, {"erasure_id": erasure_id, "match_count": sum(matches.values())}
        )


def _hashes(texts: Sequence[str]) -> set[str]:
    keys = bound_keys()
    if not keys:
        raise ErasureKeyMissing(
            "the erasure guard needs EPISTREL_CONFIG_KEY: the ledger's term hashes are keyed "
            "with it",
            {"setting": "config_key"},
        )
    found: set[str] = set()
    for key in keys:
        found |= span_hashes(texts, key)
    return found


async def assert_not_erased(
    conn: AsyncConnection, ids: Iterable[str], *, field: str, path: str
) -> None:
    """Refuse (409 ``erased_subject_referenced``) a new structured reference to a subject a
    catalog-scope erasure removed (a scenario role filled with her, a medium depicting her):
    the catalog writers' and the media writer's hook. Prose is never refused here."""
    _spied(path)
    wanted = sorted({i for i in ids if i})
    if DISABLED.get() or not wanted:
        return
    el = erasure_ledger.c
    found = list(
        await conn.scalars(
            select(el.id)
            .where(el.scope == CATALOG_SCOPE, el.subject_id.in_(wanted))
            .order_by(el.at, el.id)
        )
    )
    if found:
        raise ErasedSubjectReferenced(
            MESSAGE, {"erasure_id": str(found[0]), "match_count": len(found), "field": field}
        )


def staged_content(
    payload: Mapping[str, Any], character_id: str | None, provenance: Provenance
) -> Content:
    """One staged event as the guard reads it: every string leaf of its payload (id-like keys
    aside), the characters it names by id and its speaker, and whether a consumer wrote it."""
    texts = tuple(v for v in text_fields(payload, "payload").values() if v)
    references = {subject_id for _, subject_id, _ in character_ids(payload, "payload")}
    if character_id is not None:
        references.add(character_id)
    return Content(texts, frozenset(references), provenance in CONSUMER_PROVENANCE)


# -- the write paths -----------------------------------------------------------------------------

OPERATION = "operation"
"""Guarded by ``Operation._write``'s ``check_content`` (every event an operation commits)."""
CATALOG_REFERENCES = "assert_not_erased"
"""Guarded by ``assert_not_erased`` on its new structured references."""
FORK_FENCE = "fork_fence"
"""The fork's ledger fence: an erasure committed after its snapshot retries it, and the copy
carries the source's ledger rows."""
DIRECT = "check_content"
"""Guarded by its own ``check_content`` call on what it writes, in its write transaction (the
structured stores, #131, which write outside an ``Operation``)."""
EXEMPT = "exempt"


@dataclass(frozen=True, slots=True)
class WritePath:
    """A module that writes story or catalog content, and how the guard covers it (``how``:
    ``OPERATION``, ``CATALOG_REFERENCES``, ``FORK_FENCE``, ``DIRECT`` or ``EXEMPT`` with
    ``why``)."""

    module: str
    how: str
    why: str = ""


CONTENT_WRITE_PATHS: tuple[WritePath, ...] = (
    WritePath("epistrel.core.events.operation", OPERATION),
    WritePath("epistrel.core.ingest", OPERATION),
    WritePath("epistrel.orchestrator.jobs.queue", OPERATION),
    WritePath("epistrel.orchestrator.lite.pipeline", OPERATION),
    WritePath("epistrel.orchestrator.structured.pipeline", OPERATION),
    WritePath("epistrel.core.stories", OPERATION),
    WritePath("epistrel.core.genesis.create", OPERATION),
    WritePath("epistrel.core.fork", FORK_FENCE),
    WritePath("epistrel.core.catalog.scenarios", CATALOG_REFERENCES),
    WritePath("epistrel.core.media.assets", CATALOG_REFERENCES),
    WritePath("epistrel.core.media.uploads", CATALOG_REFERENCES),
    WritePath(
        "epistrel.core.config_changes",
        EXEMPT,
        "a configuration change is written by the consumer against current state in one "
        "transaction, so no context it was built from predates an erasure",
    ),
    WritePath(
        "epistrel.core.ratings.change",
        EXEMPT,
        "a rating change's control event holds rating ids and counts only",
    ),
    WritePath(
        "epistrel.orchestrator.policies.admin",
        EXEMPT,
        "a policy is written by an operator against current state in one transaction",
    ),
    WritePath(
        "epistrel.core.feedback",
        EXEMPT,
        "a rating is typed by a consumer now and references messages, never a subject",
    ),
    WritePath(
        "epistrel.core.idempotency",
        EXEMPT,
        "a stored result is the result of an operation the guard already checked, and an "
        "erasure purges the results that name the subject",
    ),
    WritePath(
        "epistrel.core.catalog.characters",
        EXEMPT,
        "a catalog character's document defines its own subject and references no other by id",
    ),
    WritePath(
        "epistrel.core.catalog.personas",
        EXEMPT,
        "a persona defines its own subject and references no other",
    ),
    # #498: a promotion copies story memory texts into a catalog companion draft; it checks
    # them against the story's erasures in its own transaction.
    WritePath("epistrel.core.catalog.promote", DIRECT),
    WritePath("epistrel.core.propositions.registry", DIRECT),
    WritePath("epistrel.core.propositions.store", DIRECT),
    WritePath("epistrel.core.canon.facts", DIRECT),
    WritePath("epistrel.core.memory.records", DIRECT),
    WritePath("epistrel.core.mutation", DIRECT),
    WritePath("epistrel.core.commit.lifecycle", DIRECT),
    WritePath("epistrel.core.epistemic.partitions", DIRECT),
    WritePath("epistrel.core.epistemic.transitions", DIRECT),
    WritePath("epistrel.core.epistemic.modeling", DIRECT),
    WritePath("epistrel.core.epistemic.exposure", DIRECT),
    WritePath("epistrel.core.epistemic.divergences", DIRECT),
    WritePath("epistrel.core.turns.beats", DIRECT),
    WritePath("epistrel.core.states", DIRECT),
    WritePath(
        "epistrel.core.verify.shadow",
        EXEMPT,
        "a shadow sample adds counts, spans, ids and hashes to the sampled turn's report, "
        "never text",
    ),
)
"""Every content write path (``SUBJECT_WRITERS``' writer modules, plus ingest, the job commit
and the Lite pipeline); ``tests/integration/test_erasure_resurrection.py`` checks the set and
each claim. The M7 import path registers here when it lands."""


__all__ = [
    "CATALOG_REFERENCES",
    "CONTENT_WRITE_PATHS",
    "DIRECT",
    "DISABLED",
    "EXEMPT",
    "FORK_FENCE",
    "MAX_NGRAM",
    "MESSAGE",
    "OPERATION",
    "Content",
    "Erased",
    "WritePath",
    "assert_not_erased",
    "bind_key",
    "bound_keys",
    "check_content",
    "recording",
    "span_hashes",
    "spans",
    "staged_content",
    "story_erasures",
]
