"""The structured read set: query keys, retrieved rows and the change index (FR-CONC-3,11,12;
docs/02 §4.4; #136).

A structured turn's read set is #47's Lite members plus what its deterministic lookups relied on:

- **query keys** (``QueryKey``): every lookup a verdict, a precondition or a blocker depends on
  is made through ``KeyedLookup``, which records ``(subjects | *, predicate, object | literal |
  *, holder, viewpoint, valid window, result_hash)`` beside the result, **an empty result
  included** (it hashes to ``EMPTY_RESULT``). ``KEYED_READERS`` names the modules whose reads of
  ``facts``/``propositions`` are verdict-bearing (#140's ``verify``, #144's permission check,
  #146's ledger lookups register on landing); ``python -m tools.gates check-keyed-readers``
  fails the build when one of them reads those tables other than through ``KeyedLookup``.
- **retrieved rows** (``Retrieved``): semantic retrieval used only as generation context,
  ``{table, id, version}`` (``version``: a fact's ``tx_from``, a memory's ``membership_tx``).
  They are never keyed: a change to one does not regenerate the turn (it rebases).

``StructuredReadSet`` is the ``ReadSetSource`` of such a turn (``run_with_revalidation(read_set=
...)``), beside ``LiteReadSet``: its ``hashes`` are the Lite members' (so guidance, instruction
and definition edits still regenerate), and every attempt starts a fresh set of keys
(``start_attempt``, called by the operation's read phase).

**The change index** (``core.tables.readset.proposition_changes``). Every canon write records
the propositions it added and closed in the same transaction (``record_fact_change``, a
``FACT_HOOKS`` hook of #133; ``record_change`` for #144's beliefs and #150's utterances), stamped
with the story's revision after the writing operation commits (``COMMIT_REVISION_SETTING``,
which ``Operation`` sets in its lock statement). At commit, under the story lock,
``touched_keys`` intersects the turn's keys with the changes committed since its
``base_revision`` in **one query**: same predicate and holder; a subject in the key's
(``same_as``-expanded) subject set, or any subject for ``*``; the key's object, a set object
containing it, the key's literal, or anything for ``*``; valid windows that may overlap (the
outer bounds, i.e. ``propositions.model.overlaps`` is not ``False``); and the key's viewpoint
able to see the change (a ``latent`` fact a speaker cannot see does not touch the speaker's
keys). A ``same_as`` change touches every key whose subject set holds either side, and a key of
a symmetric predicate matches either direction. A hit is "read set touched": the turn
regenerates within its budget, then 409 ``revision_conflict``. A turn whose base is below
``stories.changes_pruned_through_revision`` (the retention sweep pruned what it would need)
counts as touched too. With no hit the turn rebases: ``REBASE_CHECKS`` (#140's verify, #144's
permission checks) re-run against current state, and a failing one regenerates.

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.concurrency import LiteReadSet, ReadSet
from epistrel.core.errors import RevisionConflict
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import NUMERIC_KINDS, Proposition, TypedLiteral
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import stories
from epistrel.core.tables.readset import proposition_changes

if TYPE_CHECKING:
    from epistrel.core.canon.facts import Fact, FactChange, Reading
    from epistrel.core.events.operation import Operation

logger = logging.getLogger(__name__)

KEYED_READERS: tuple[str, ...] = ()
"""Modules whose ``facts``/``propositions`` reads decide a verdict, a precondition or a blocker:
each reads them only through ``KeyedLookup`` (``tools.gates check-keyed-readers``). #140's
``epistrel.core.verify.rules``, #144's permission check and #146's ledger lookups add
themselves when they land."""

READSET_MAX_KEYS = 2_000
"""``readset_max_keys``: a turn recording more keys than this conflicts (409
``revision_conflict``, reason ``read_set_overflow``) rather than dropping keys."""

EMPTY_RESULT = hashlib.sha256(b"").hexdigest()
"""The ``result_hash`` of a lookup that found nothing."""

COMMIT_REVISION_SETTING = "epistrel.commit_revision"
"""Transaction-local: the story's timeline revision once the operation committing in this
transaction has (set by ``Operation`` in its lock statement); change rows are stamped with it."""

CANON = "canon"
UTTERANCE_PREFIX = "utterance:"

ChangeKind = Literal["add", "supersede", "close"]

INTERSECTION_ENABLED = True
"""Off only under ``epistrel.testing.faults.readset_intersection_disabled`` (the negative run
of S-ABSENT-FACT, which must then fail)."""


class ReadSetInconsistent(RuntimeError):
    """The same key looked up twice within one attempt answered differently: the turn read two
    states of the story, so its read set cannot say what it depended on."""


def result_hash(found: Iterable[tuple[str, datetime]]) -> str:
    """SHA-256 over the sorted ``(fact_id, tx_from)`` pairs; ``EMPTY_RESULT`` when empty."""
    pairs = sorted((str(i), t.isoformat()) for i, t in found)
    if not pairs:
        return EMPTY_RESULT
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def literal_hash(literal: TypedLiteral) -> str:
    """A literal's canonical hash: numeric kinds by value (``40`` is ``40.0``), every other kind
    by kind, value and unit (``Proposition.object_key``)."""
    if literal.kind in NUMERIC_KINDS:
        key: list[Any] = ["number", float(literal.value)]
    else:
        key = [literal.kind.value, literal.value, literal.unit]
    return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()


def holder_of(speaker_id: str | None) -> str:
    """``canon``, or ``utterance:<speaker_id>`` for an utterance holder (no wildcard holder)."""
    return CANON if speaker_id is None else f"{UTTERANCE_PREFIX}{speaker_id}"


# -- the read set ----------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryKey(_Model):
    """One keyed lookup: what it asked and the hash of what it found.

    ``subjects`` is the ``same_as``-expanded subject set, sorted (``None``: any subject, ``*``);
    exactly one of ``object_id`` and ``literal`` or neither (``*``); ``valid_window`` the outer
    bounds ``[lo, hi)`` of the fetch window (``None`` open); ``holder`` ``canon`` or
    ``utterance:<speaker_id>``; ``viewpoint`` the read's visibility filter."""

    subjects: tuple[str, ...] | None
    predicate_id: str
    object_id: str | None = None
    literal: dict[str, Any] | None = None
    holder: str = CANON
    viewpoint: str = OMNISCIENT
    valid_window: tuple[int | None, int | None] = (None, None)
    reading: str = "overlap"
    """How the window was read: ``every``/``any`` (true at one story-time, #133's readings) or
    ``overlap`` (any fact whose window may meet it); part of what the key asked."""
    symmetric: bool = False
    result_hash: str = EMPTY_RESULT

    @model_validator(mode="after")
    def _shape(self) -> QueryKey:
        if self.object_id is not None and self.literal is not None:
            raise ValueError("a query key has one object: an entity, a literal or *")
        if self.subjects is not None:
            object.__setattr__(self, "subjects", tuple(sorted(set(self.subjects))))
        if self.holder != CANON and not self.holder.startswith(UTTERANCE_PREFIX):
            raise ValueError("a query key's holder is canon or utterance:<speaker_id>")
        lo, hi = self.valid_window
        if lo is not None and hi is not None and lo >= hi:
            raise ValueError("a query key's window is [lo, hi) with lo < hi")
        return self

    def identity(self) -> tuple[Any, ...]:
        """What the key asked (everything but its result)."""
        return (
            self.subjects,
            self.predicate_id,
            self.object_id,
            json.dumps(self.literal, sort_keys=True) if self.literal is not None else None,
            self.holder,
            self.viewpoint,
            self.valid_window,
            self.reading,
            self.symmetric,
        )

    def view(self) -> dict[str, Any]:
        """The key as ``GET /operations/{id}`` shows it to an omniscient read (ids only)."""
        return {
            "subject": "*" if self.subjects is None else list(self.subjects),
            "predicate": self.predicate_id,
            "object": self.object_id
            if self.object_id is not None
            else (self.literal if self.literal is not None else "*"),
            "holder": self.holder,
            "window": list(self.valid_window),
            "result_hash": self.result_hash,
        }


class Retrieved(_Model):
    """A row read as generation context only (semantic retrieval): never keyed."""

    table: str
    id: str
    version: str


RebaseCheck = Callable[[AsyncConnection, "Operation"], Awaitable[bool]]
"""A deterministic re-check a structured turn's rebase runs under the story lock (#140's
verify, #144's permission checks): ``False`` regenerates. It also runs after an interpretive
configuration change (FR-CONC-16), under the new configuration."""

REBASE_CHECKS: list[RebaseCheck] = []
"""Run in registration order (``register_rebase_check``)."""


def register_rebase_check(check: RebaseCheck) -> RebaseCheck:
    if check not in REBASE_CHECKS:
        REBASE_CHECKS.append(check)
    return check


def remove_rebase_check(check: RebaseCheck) -> None:
    if check in REBASE_CHECKS:
        REBASE_CHECKS.remove(check)


async def run_rebase_checks(conn: AsyncConnection, op: Operation) -> bool:
    """Every ``REBASE_CHECKS`` entry on ``conn``; ``False`` at the first that fails."""
    for check in list(REBASE_CHECKS):
        if not await check(conn, op):
            return False
    return True


@dataclass
class StructuredReadSet:
    """A structured turn's read set (see the module docstring): the Lite members, plus the
    keys and retrieved rows of the current attempt."""

    lite: LiteReadSet = field(default_factory=LiteReadSet)
    max_keys: int = READSET_MAX_KEYS
    keys: dict[tuple[Any, ...], QueryKey] = field(default_factory=dict)
    retrieved: dict[tuple[str, str], Retrieved] = field(default_factory=dict)
    attempt: int = 0
    rebased_to: int | None = None

    keyed = True
    """Marks a ``KeyedReadSetSource`` for ``Operation``."""

    async def hashes(self, conn: AsyncConnection, story_id: str) -> ReadSet:
        return await self.lite.hashes(conn, story_id)

    def start_attempt(self) -> None:
        """A new attempt reads afresh: no key or retrieved row of the last one is kept."""
        self.keys.clear()
        self.retrieved.clear()
        self.attempt += 1
        self.rebased_to = None

    @property
    def query_keys(self) -> list[QueryKey]:
        return list(self.keys.values())

    def record(self, key: QueryKey) -> QueryKey:
        """Add ``key`` (a repeat of an identical lookup dedupes to one entry)."""
        identity = key.identity()
        found = self.keys.get(identity)
        if found is not None:
            if found.result_hash != key.result_hash:
                raise ReadSetInconsistent(
                    "a lookup answered differently within one attempt of the turn"
                )
            return found
        if len(self.keys) >= self.max_keys:
            raise RevisionConflict(
                "the turn's read set holds more query keys than readset_max_keys",
                {"reason": "read_set_overflow", "keys": len(self.keys), "attempts": self.attempt},
            )
        self.keys[identity] = key
        return key

    def record_retrieved(self, table: str, row_id: str, version: str | datetime) -> None:
        stamp = version.isoformat() if isinstance(version, datetime) else str(version)
        self.retrieved[(table, row_id)] = Retrieved(table=table, id=row_id, version=stamp)

    def counts(self) -> dict[str, int]:
        return {"keys_count": len(self.keys), "retrieved_count": len(self.retrieved)}

    def document(self, lite: ReadSet | None) -> dict[str, Any]:
        """``operations.read_set`` of the turn: ``{lite, keys, retrieved}`` (and the revision
        the last rebase re-stamped it at)."""
        found: dict[str, Any] = {
            "lite": lite,
            "keys": [k.model_dump(mode="json") for k in self.keys.values()],
            "retrieved": [r.model_dump(mode="json") for r in self.retrieved.values()],
        }
        if self.rebased_to is not None:
            found["rebased_to"] = self.rebased_to
        return found

    async def touched(self, conn: AsyncConnection, story_id: str, base_revision: int) -> list[str]:
        return await touched_keys(conn, story_id, base_revision, self.query_keys)


def read_set_counts(document: Any) -> dict[str, int]:
    """``keys_count`` and ``retrieved_count`` of a stored read set (a Lite one has neither)."""
    if not isinstance(document, dict) or "keys" not in document:
        return {"keys_count": 0, "retrieved_count": 0}
    return {
        "keys_count": len(document.get("keys") or ()),
        "retrieved_count": len(document.get("retrieved") or ()),
    }


def stored_keys(document: Any) -> list[QueryKey]:
    if not isinstance(document, dict):
        return []
    return [QueryKey.model_validate(k) for k in document.get("keys") or ()]


# -- keyed lookups ---------------------------------------------------------------------------


def _bounds(
    story_time: int | None, window: tuple[int | None, int | None] | None
) -> tuple[int | None, int | None]:
    if window is not None:
        return window
    if story_time is not None:
        return (story_time, story_time + 1)
    return (None, None)


def _meets(a: tuple[int | None, int | None], b: tuple[int | None, int | None]) -> bool:
    """``[lo, hi)`` ranges that may share a minute (``None`` open)."""
    lo = [x for x in (a[0], b[0]) if x is not None]
    hi = [x for x in (a[1], b[1]) if x is not None]
    return not lo or not hi or max(lo) < min(hi)


def _object_matches(
    proposition: Proposition, object_id: str | None, literal: TypedLiteral | None
) -> bool:
    if object_id is None and literal is None:
        return True
    if object_id is not None:
        return proposition.object_id == object_id or object_id in (proposition.object_set or ())
    return (
        proposition.literal is not None
        and literal is not None
        and literal.equals(proposition.literal)
    )


class KeyedLookup:
    """The read path for verdict-bearing proposition queries: each lookup records its query key
    and the hash of its result in ``read_set`` (async, on the turn's connection)."""

    def __init__(self, conn: AsyncConnection, read_set: StructuredReadSet) -> None:
        self.conn = conn
        self.read_set = read_set

    async def canon_at(
        self,
        story_id: str,
        *,
        predicate: str,
        subjects: str | Sequence[str] | None,
        object: str | TypedLiteral | None = None,
        story_time: int | None = None,
        window: tuple[int | None, int | None] | None = None,
        viewpoint: str = OMNISCIENT,
        reading: Reading = "every",
        through_position: tuple[int, int] | None = None,
    ) -> list[Fact]:
        """The current canon facts of ``predicate`` for ``subjects`` (one id, a ``same_as``-
        expanded set, or ``None`` for any), with ``object`` (an entity id, a literal, or
        ``None`` for any), true at ``story_time`` (or overlapping ``window``, #140's widened
        fetch window; neither: at any time) as ``viewpoint`` may read them; the key and its
        result hash are recorded. Keys are canonicalized with the predicate's symmetric and
        inverse rules (#131): facts are stored in the canonical direction."""
        from epistrel.core.canon import facts as canon
        from epistrel.core.propositions.registry import load

        if story_time is not None and window is not None:
            raise ValueError("a keyed lookup takes a story time or a window, not both")
        subject_set = (
            None
            if subjects is None
            else (subjects,)
            if isinstance(subjects, str)
            else tuple(sorted(set(subjects)))
        )
        object_id = object if isinstance(object, str) else None
        literal = object if isinstance(object, TypedLiteral) else None
        found = (await load(self.conn, story_id)).get(predicate)
        symmetric = bool(found is not None and found.symmetric)
        inverse_objects: tuple[str, ...] | None = None
        if found is not None and found.inverse_of is not None and literal is None:
            # (S, p⁻¹, o) is (o, p, S): read the canonical direction, the subjects becoming the
            # object filter (a key over several subjects, or any, keeps ``*`` as its object).
            predicate = found.inverse_of
            inverse_objects = subject_set
            subject_set = None if object_id is None else (object_id,)
            object_id = (
                inverse_objects[0] if inverse_objects and len(inverse_objects) == 1 else None
            )
        bounds = _bounds(story_time, window)
        rows = await canon.canon(
            self.conn,
            story_id,
            story_time=story_time,
            through_position=through_position,
            viewpoint=viewpoint,
            predicate=predicate,
            subject=subject_set[0] if subject_set and len(subject_set) == 1 else None,
            reading=reading,
        )
        if symmetric and subject_set is not None:
            rows += [
                r
                for r in await canon.canon(
                    self.conn,
                    story_id,
                    story_time=story_time,
                    through_position=through_position,
                    viewpoint=viewpoint,
                    predicate=predicate,
                    reading=reading,
                )
                if r.proposition.object_id in subject_set and r.subject_id not in subject_set
            ]

        def kept(fact: Fact) -> bool:
            p = fact.proposition
            if inverse_objects is not None and p.object_id not in inverse_objects:
                return False
            direct = (subject_set is None or fact.subject_id in subject_set) and _object_matches(
                p, object_id, literal
            )
            reverse = (
                symmetric
                and subject_set is not None
                and p.object_id in subject_set
                and (object_id is None or p.subject_id == object_id)
            )
            if not (direct or reverse):
                return False
            return story_time is not None or _meets((fact.valid_from, fact.valid_to), bounds)

        result = sorted({f.id: f for f in rows if kept(f)}.values(), key=lambda f: f.id)
        self.read_set.record(
            QueryKey(
                subjects=subject_set,
                predicate_id=predicate,
                object_id=object_id,
                literal=None if literal is None else literal.model_dump(mode="json"),
                holder=CANON,
                viewpoint=viewpoint,
                valid_window=bounds,
                reading=reading if story_time is not None else "overlap",
                symmetric=symmetric,
                result_hash=result_hash((f.id, f.tx_from) for f in result),
            )
        )
        return result

    def retrieved(self, table: str, row_id: str, version: str | datetime) -> None:
        """Record a row read as generation context only (semantic retrieval): not keyed."""
        self.read_set.record_retrieved(table, row_id, version)


# -- the change index ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    """One row of ``proposition_changes`` to write."""

    proposition: Proposition
    proposition_id: str | None
    change: ChangeKind
    valid_window: tuple[int | None, int | None]
    visibility: dict[str, Any]
    speaker_id: str | None = None


async def record_change(
    conn: AsyncConnection, story_id: str, operation_id: str, changes: Sequence[Change]
) -> None:
    """Write ``changes`` to the change index in ``conn``'s transaction (the write's own), stamped
    with the revision the committing operation leaves (``COMMIT_REVISION_SETTING``; outside an
    ``Operation`` commit, the story's next revision). Asserted propositions only: #131's
    implications are never indexed. #150's utterance writes call it with ``speaker_id``."""
    if not changes:
        return
    stamped = await conn.scalar(
        select(
            func.coalesce(
                func.nullif(func.current_setting(COMMIT_REVISION_SETTING, True), "").cast(
                    stories.c.timeline_revision.type
                ),
                stories.c.timeline_revision + 1,
            )
        ).where(stories.c.id == story_id)
    )
    if stamped is None:
        return
    await conn.execute(
        insert(proposition_changes),
        [
            {
                "id": new_ulid(),
                "story_id": story_id,
                "timeline_revision": int(stamped),
                "proposition_id": c.proposition_id,
                "subject_id": c.proposition.subject_id,
                "predicate_id": c.proposition.predicate_id,
                "object_id": c.proposition.object_id,
                "literal_hash": None
                if c.proposition.literal is None
                else literal_hash(c.proposition.literal),
                "object_set": None
                if c.proposition.object_set is None
                else list(c.proposition.object_set),
                "holder": CANON if c.speaker_id is None else "utterance",
                "speaker_id": c.speaker_id,
                "visibility": c.visibility,
                "valid_window": Range(c.valid_window[0], c.valid_window[1], bounds="[)"),
                "change": c.change,
                "operation_id": operation_id,
            }
            for c in changes
        ],
    )


async def record_fact_change(conn: AsyncConnection, change: FactChange) -> None:
    """The ``FACT_HOOKS`` writer (#133): a new version is ``supersede`` when it closed another,
    else ``add``; each closed version is a ``close``; each remainder an ``add``."""

    def row(fact: Fact, kind: ChangeKind) -> Change:
        return Change(
            proposition=fact.proposition,
            proposition_id=fact.proposition_id,
            change=kind,
            valid_window=(fact.valid_from, fact.valid_to),
            visibility=fact.visibility.document(),
        )

    fact = change.fact
    rows = [row(fact, "supersede" if change.superseded_ids else "add")]
    rows += [row(c, "close") for c in change.closed]
    rows += [row(r, "add") for r in change.remainders]
    await record_change(conn, change.story_id, change.operation_id, rows)


TOUCH_SQL = text(
    """
SELECT DISTINCT k.i
FROM jsonb_to_recordset(CAST(:keys AS jsonb)) AS k(
    i int, subjects jsonb, predicate_id text, object_id text, literal_hash text, holder text,
    speaker_id text, viewpoint text, lo bigint, hi bigint, sym boolean)
JOIN proposition_changes c
  ON c.story_id = :story_id
 AND c.timeline_revision > :base
 AND c.holder = k.holder
 AND c.speaker_id IS NOT DISTINCT FROM k.speaker_id
 AND c.valid_window && int8range(k.lo, k.hi, '[)')
 AND (k.viewpoint = 'omniscient'
      OR c.visibility->>'scope' IN ('objective', 'public')
      OR (c.visibility->>'scope' = 'characters'
          AND k.viewpoint LIKE 'character:%'
          AND (c.visibility->'characters') ? substr(k.viewpoint, 11)))
 AND (
      (c.predicate_id = k.predicate_id
       AND (
         ((k.subjects IS NULL OR k.subjects ? c.subject_id::text)
          AND ((k.object_id IS NULL AND k.literal_hash IS NULL)
               OR c.object_id = k.object_id
               OR k.object_id = ANY(CAST(c.object_set AS text[]))
               OR c.literal_hash = k.literal_hash))
         OR (k.sym AND k.subjects ? c.object_id::text
             AND (k.object_id IS NULL OR c.subject_id = k.object_id))))
      OR (c.predicate_id = '01JPRED0000000000000000001' AND k.subjects IS NOT NULL
          AND (k.subjects ? c.subject_id::text OR k.subjects ? c.object_id::text)))
ORDER BY k.i
"""
)
"""The intersection of a turn's keys with the changes since its base: one statement over the
``(story_id, timeline_revision)`` index (see the module docstring). The literals are
``OMNISCIENT``, ``CHARACTER_VIEWPOINT_PREFIX`` (10 characters, so a character id starts at 11) and
``SAME_AS`` (``tests/test_readset.py`` checks them)."""


def _key_rows(keys: Sequence[QueryKey]) -> list[dict[str, Any]]:
    rows = []
    for i, key in enumerate(keys):
        speaker = (
            key.holder.removeprefix(UTTERANCE_PREFIX)
            if key.holder.startswith(UTTERANCE_PREFIX)
            else None
        )
        rows.append(
            {
                "i": i,
                "subjects": None if key.subjects is None else list(key.subjects),
                "predicate_id": key.predicate_id,
                "object_id": key.object_id,
                "literal_hash": None
                if key.literal is None
                else literal_hash(TypedLiteral.model_validate(key.literal)),
                "holder": CANON if speaker is None else "utterance",
                "speaker_id": speaker,
                "viewpoint": key.viewpoint,
                "lo": key.valid_window[0],
                "hi": key.valid_window[1],
                "sym": key.symmetric,
            }
        )
    return rows


async def touched_keys(
    conn: AsyncConnection, story_id: str, base_revision: int, keys: Sequence[QueryKey]
) -> list[str]:
    """``query_key:<n>`` for each key a change committed since ``base_revision`` matches (one
    query); empty when nothing matches or there are no keys."""
    if not keys or not INTERSECTION_ENABLED:
        return []
    found = await conn.scalars(
        TOUCH_SQL,
        {"keys": json.dumps(_key_rows(keys)), "story_id": story_id, "base": base_revision},
    )
    touched = [f"query_key:{i}" for i in found]
    logger.debug("read set of story %s: %d keys, %d touched", story_id, len(keys), len(touched))
    return touched


__all__ = [
    "CANON",
    "COMMIT_REVISION_SETTING",
    "EMPTY_RESULT",
    "KEYED_READERS",
    "READSET_MAX_KEYS",
    "REBASE_CHECKS",
    "Change",
    "KeyedLookup",
    "QueryKey",
    "ReadSetInconsistent",
    "RebaseCheck",
    "Retrieved",
    "StructuredReadSet",
    "holder_of",
    "literal_hash",
    "read_set_counts",
    "record_change",
    "record_fact_change",
    "register_rebase_check",
    "remove_rebase_check",
    "result_hash",
    "run_rebase_checks",
    "stored_keys",
    "touched_keys",
]
