"""Message feedback (FR-FB-1,2; #112): rate a generated message, rate it again, retract the
rating, and read it.

**A rating.** One principal's rating of one generated message: 1 to 5, up to ten reason tags of
the deployment's ``feedback_tags`` (FR-FB-1's nine seeded ones, plus any an operator adds;
trimmed, lower-cased, kept distinct and sorted, so tags compare as a set), and optional free text
(trimmed; empty is none; at most ``feedback_text_max_chars`` characters, 2000 by default). A
principal has at most one rating per message (FR-AUTH-4: the rating is keyed by the requesting
principal, the one thing in Epistrel that is per principal by definition).

**The message.** ``message_id`` is the rated ``message_rendered`` event's id (#57's
``message_id``); a message's ``turn:variant`` form belongs to ``history?variant=`` only. Every
message the engine produced can be rated: a reply, a safe fallback (#68), a regenerated
variant, in any membership (a discarded or dormant variant still reads in the audit view). The
consumer's own narration (#58) cannot: no configuration produced it. Any other id, a message of
another story included, is 404 ``not_found``. A rating belongs to the variant it rated: a
regenerated message is a new message and starts unrated, and the earlier variant keeps its
rating (FR-FB-2).

**Its context.** The first write copies, from the producing operation's recorded
``config_revisions`` (#64, #109's stamp), the configuration the message was produced under: the
operation, the fingerprint, the profile version (``<profile id>@<version>``), each model
purpose's model and reported version, the story's content-rating version and the quality status
(FR-FB-1, FR-PROF-16). Nothing is recomputed, so a later profile or rating change never alters
it. An operation that recorded no configuration stamp leaves those fields null.

**Writes.** A rating is a plain table write, in one transaction, and nothing else: no operation,
no event and no ``Idempotency-Key`` row (a repeated request converges on the same row; REST flags
the routes ``mutating=False``), and no expected revision. The write holds the story row
``FOR KEY SHARE``, so a story deletion waits for it (or it finds the story gone: 404) while turns,
archives and settings writes go on: a rating is allowed on an archived story and during a turn.
It reads the deployment row ``FOR SHARE``, so a ``feedback_tags`` write that removes a tag waits
for it and then counts it. The principal's row is held ``FOR UPDATE``. A rating that finds none
writes by an upsert on the unique key whose update never applies (``ON CONFLICT ... DO UPDATE
... WHERE false``): it inserts the row, or it locks the one a concurrent first rating committed
meanwhile and replaces that. When that rating is retracted or changed before the lock is
granted, PostgreSQL runs the upsert again from the start, so the write always either creates
the rating or holds the one it replaces (a rating that lost to a rating since retracted creates
its own). Concurrent writes of one rating serialise:

- the first rating creates the row (``created``; REST 201);
- a rating that changes the value appends the value it replaces to ``feedback_history`` (kind
  ``replaced``) and updates the row (REST 200);
- the same value again (tags as a set) writes nothing and leaves ``updated_at`` as it was;
- a retraction appends the final value to the history (kind ``retracted``) and deletes the row;
  with no rating there is nothing to do, and REST answers 204 either way. A rating after a
  retraction is a new row.

**The subject index.** ``index_hook`` is called in the write's transaction after a write that
changes the rating (the first and every changing re-rating) with the rating's ``record_id``
(``<message_id>:<principal>``, the same across re-ratings), its story and ``{"text": ...}``;
``drop_hook`` after a retraction. They write and drop the rating's rows in the subject-reference
index (``core.erasure.index``; #123), and each history entry is indexed as it is appended, so an
erasure finds and redacts the subjects a rating's text names (FR-ADMIN-6, FR-FB-2).

**Never read by the story.** Feedback is an audit-category record (FR-STORE-25, FR-FB-2): no
story view, retrieval index or prompt assembler reads it, and nothing in the engine changes
because of it. Only this module (and the modules ``tests/integration/test_feedback.py`` lists:
the tables, story deletion, the retention sweep, the determinism comparator) names its tables.
It is deleted with its story, kept by its own ``feedback_retention``, and marked, never deleted,
when M7's compaction removes its message (``mark_compacted``). Log lines carry ids and the
outcome only, never a rating, a tag or text (invariant 3).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints
from sqlalchemy import delete, false, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.config_changes import section
from epistrel.core.enums import Provenance
from epistrel.core.erasure.index import Record, drop_references, index_records, index_references
from epistrel.core.erasure.records import rating_record_id
from epistrel.core.errors import (
    FeedbackTagInUse,
    InvalidRequest,
    MessageNotFound,
    StoryNotFound,
    UnknownFeedbackTag,
    refuse_nul,
)
from epistrel.core.ids import new_ulid
from epistrel.core.profiles.fingerprint import ModelIdentity, QualityStatusValue
from epistrel.core.settings_schema import FEEDBACK_TEXT_MAX_CHARS_LIMIT, FeedbackSettings
from epistrel.core.tables import deployment_state, events, operations, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.feedback import (
    RATING_MAX,
    RATING_MIN,
    HistoryKind,
    feedback_history,
    message_feedback,
)
from epistrel.observability.logging import get_logger
from epistrel.testing import pauses

log = get_logger(__name__)

RATED_EVENT = "message_rendered"
"""The event a rating names: a message the engine produced (``provenance = engine``)."""
TAGS_PER_RATING = 10
TAG_INPUT_MAX_CHARS = 64
"""The longest tag a request may send; a deployment's own tags are at most 40 characters."""
TEXT_FIELD = "body.text"
TAGS_FIELD = "body.tags"
TAGS_SETTING_FIELD = "body.feedback_tags"


# -- the request and the answer --------------------------------------------------------------------

RatingTag = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, min_length=1, max_length=TAG_INPUT_MAX_CHARS
    ),
]
"""A reason tag as sent: trimmed and lower-cased before it is compared with the deployment's."""


class FeedbackRequest(BaseModel):
    """``PUT /stories/{id}/messages/{message_id}/feedback``: a ``rating`` from 1 to 5, up to ten
    reason ``tags`` of the deployment's ``feedback_tags`` (trimmed and lower-cased, compared as a
    set; none by default; an unknown one is 422 ``unknown_feedback_tag``), and optional free
    ``text``, trimmed (an empty text is none) and at most the deployment's
    ``feedback_text_max_chars`` characters (2000 by default; 422 ``validation_error`` beyond)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "rating": 2,
                    "tags": ["repetitive", "too_long"],
                    "text": "She said the same line about the tide twice.",
                }
            ]
        },
    )

    rating: StrictInt = Field(ge=RATING_MIN, le=RATING_MAX)
    tags: list[RatingTag] = Field(default_factory=list, max_length=TAGS_PER_RATING)
    text: str | None = Field(default=None, max_length=FEEDBACK_TEXT_MAX_CHARS_LIMIT)

    def value(self) -> tuple[int, list[str], str | None]:
        """The rating as stored: tags distinct and sorted, the text trimmed (empty: none)."""
        text = self.text.strip() if self.text is not None else None
        return self.rating, sorted(set(self.tags)), text or None


class FeedbackContext(BaseModel):
    """The configuration a rated message was produced under, copied from its operation's
    record when the rating was first written (FR-FB-1, FR-PROF-16): the operation, its
    configuration fingerprint, the engine profile version (``<profile id>@<version>``), each
    model purpose's model and reported version, the story's content-rating version and the
    quality status. A field the operation did not record is null."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    config_fingerprint: str | None = None
    profile_version: str | None = None
    models: dict[str, ModelIdentity] | None = None
    rating_version_id: str | None = None
    quality_status: QualityStatusValue | None = None


class FeedbackHistoryEntry(BaseModel):
    """An earlier value of a principal's rating of a message: the ``rating``, ``tags`` and
    ``text`` it held, when it was given (``rated_at``) and when it stopped being current
    (``replaced_at``), because a new rating ``replaced`` it or it was ``retracted``."""

    model_config = ConfigDict(frozen=True)

    rating: int
    tags: list[str]
    text: str | None
    kind: HistoryKind
    rated_at: datetime
    replaced_at: datetime


class Feedback(BaseModel):
    """One principal's rating of one generated message (FR-FB-1): the message (``message_id``,
    its ``turn`` and ``variant``), the ``principal``, the ``rating`` (1 to 5; null when the
    principal has none), its ``tags`` and ``text``, when it was first given and last changed,
    ``compacted_at`` (set when an opt-in compaction removed the message; the rating is kept), the
    ``context`` the message was produced under (null without a rating), and the ``history`` of
    earlier values, newest first."""

    model_config = ConfigDict(frozen=True)

    story_id: str
    message_id: str
    turn: int
    variant: int
    principal: str
    rating: int | None
    tags: list[str]
    text: str | None
    compacted_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    context: FeedbackContext | None
    history: list[FeedbackHistoryEntry]


@dataclasses.dataclass(frozen=True, slots=True)
class FeedbackWrite:
    """What a rating did: the rating as it now stands, with its history; whether it created
    the principal's rating of the message (REST 201, else 200); and whether it changed anything
    (the same value again changes nothing)."""

    feedback: Feedback
    created: bool
    changed: bool


@dataclasses.dataclass(frozen=True, slots=True)
class FeedbackRetraction:
    """What a retraction did: whether the principal had a rating to retract (REST answers 204
    either way)."""

    story_id: str
    message_id: str
    retracted: bool


# -- the subject-indexing seams (FR-ADMIN-6; #123 rebinds them) ------------------------------------

IndexHook = Callable[[AsyncConnection, str, str, Mapping[str, str | None]], Awaitable[None]]
DropHook = Callable[[AsyncConnection, str], Awaitable[None]]


async def _index_rating(
    conn: AsyncConnection, record_id: str, story_id: str, fields: Mapping[str, str | None]
) -> None:
    """The default ``index_hook``: the rating's rows in the subject-reference index (#123)."""
    await index_references(conn, message_feedback.name, record_id, story_id=story_id, fields=fields)


async def _drop_rating(conn: AsyncConnection, record_id: str) -> None:
    """The default ``drop_hook``: the retracted rating's index rows go (#123)."""
    await drop_references(conn, message_feedback.name, record_id)


index_hook: IndexHook = _index_rating
"""Called as ``index_hook(conn, record_id, story_id, {"text": text})`` in the transaction of
every write that changes a rating (see the module docstring): the subject-reference index."""

drop_hook: DropHook = _drop_rating
"""Called as ``drop_hook(conn, record_id)`` in a retraction's transaction (see the module
docstring)."""


def _history_record(row: Any) -> Record:
    return Record(feedback_history.name, str(row.id), str(row.story_id), {"text": row.text})


async def index_records_of(
    conn: AsyncConnection, story_id: str | None, *, history: bool
) -> list[Record]:
    """The subject-index records of the story's ratings (or of its history entries, with
    ``history``) for the index rebuild (``core.erasure.records``); none in the catalog
    scope."""
    if story_id is None:
        return []
    if history:
        found = await conn.execute(
            select(
                feedback_history.c.id, feedback_history.c.story_id, feedback_history.c.text
            ).where(feedback_history.c.story_id == story_id)
        )
        return [_history_record(r) for r in found]
    found = await conn.execute(
        select(
            message_feedback.c.message_id, message_feedback.c.principal, message_feedback.c.text
        ).where(message_feedback.c.story_id == story_id)
    )
    return [
        Record(
            message_feedback.name,
            record_id(str(r.message_id), str(r.principal)),
            story_id,
            {"text": r.text},
        )
        for r in found
    ]


def record_id(message_id: str, principal: str) -> str:
    """A rating's id for the subject index: ``<message_id>:<principal>``, the same across
    re-ratings and after a retraction (the row's own id is not)."""
    return rating_record_id(message_id, principal)


# -- the message and its context -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _Message:
    story_id: str
    message_id: str
    turn: int
    variant: int
    operation_id: str
    revisions: Mapping[str, Any]


async def _message(
    conn: AsyncConnection, story_id: str, message_id: str, *, lock: bool
) -> _Message:
    """The rated message, its position and its operation's recorded configuration: 404 for an
    unknown story, then for anything that is not a message the engine produced in it. ``lock``
    holds the story row ``FOR KEY SHARE`` (a write; see the module docstring)."""
    story = select(stories.c.id).where(stories.c.id == story_id)
    if lock:
        story = story.with_for_update(read=True, key_share=True)
    if await conn.scalar(story) is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    found = (
        await conn.execute(
            select(
                events.c.narrative_turn,
                events.c.turn_variant,
                events.c.operation_id,
                operations.c.config_revisions,
            )
            .select_from(
                events.join(operations, operations.c.operation_id == events.c.operation_id)
            )
            .where(
                events.c.id == message_id,
                events.c.story_id == story_id,
                events.c.type == RATED_EVENT,
                events.c.provenance == Provenance.ENGINE.value,
            )
        )
    ).one_or_none()
    if found is None or found.narrative_turn is None or found.turn_variant is None:
        raise MessageNotFound(
            f"message {message_id} is not a generated message of story {story_id}",
            {"story_id": story_id, "message_id": message_id},
        )
    revisions = found.config_revisions if isinstance(found.config_revisions, Mapping) else {}
    return _Message(
        story_id=story_id,
        message_id=message_id,
        turn=int(found.narrative_turn),
        variant=int(found.turn_variant),
        operation_id=str(found.operation_id),
        revisions=revisions,
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def context_of(operation_id: str, revisions: Mapping[str, Any]) -> dict[str, Any]:
    """The context columns a rating copies from its message's operation (``config_revisions``:
    #64's ``rating_version``, #109's ``fingerprint``, ``profile``, ``profile_version``,
    ``models`` and ``quality_status``); null for each it did not record."""
    profile, version = revisions.get("profile"), revisions.get("profile_version")
    models = revisions.get("models")
    quality = revisions.get("quality_status")
    status = quality.get("status") if isinstance(quality, Mapping) else None
    return {
        "operation_id": operation_id,
        "config_fingerprint": _text(revisions.get("fingerprint")),
        "profile_version": (
            f"{profile}@{version}" if isinstance(profile, str) and _is_int(version) else None
        ),
        "models": dict(models) if isinstance(models, Mapping) else None,
        "rating_version_id": _text(revisions.get("rating_version")),
        "quality_status": _text(status),
    }


# -- reads -----------------------------------------------------------------------------------------


def _key(message: _Message, principal: str) -> Any:
    return (
        (message_feedback.c.story_id == message.story_id)
        & (message_feedback.c.message_id == message.message_id)
        & (message_feedback.c.principal == principal)
    )


async def _current(conn: AsyncConnection, message: _Message, principal: str, *, lock: bool) -> Any:
    query = select(message_feedback).where(_key(message, principal))
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).one_or_none()


async def _history(conn: AsyncConnection, message: _Message, principal: str) -> list[Any]:
    found = await conn.execute(
        select(feedback_history)
        .where(
            feedback_history.c.story_id == message.story_id,
            feedback_history.c.message_id == message.message_id,
            feedback_history.c.principal == principal,
        )
        .order_by(feedback_history.c.replaced_at.desc(), feedback_history.c.id.desc())
    )
    return list(found)


def _entry(row: Any) -> FeedbackHistoryEntry:
    return FeedbackHistoryEntry(
        rating=int(row.rating),
        tags=list(row.tags),
        text=row.text,
        kind=HistoryKind(row.kind),
        rated_at=row.rated_at,
        replaced_at=row.replaced_at,
    )


def _context(row: Any) -> FeedbackContext:
    return FeedbackContext(
        operation_id=row.operation_id,
        config_fingerprint=row.config_fingerprint,
        profile_version=row.profile_version,
        models=row.models,
        rating_version_id=row.rating_version_id,
        quality_status=row.quality_status,
    )


def _feedback(message: _Message, principal: str, row: Any, history: list[Any]) -> Feedback:
    return Feedback(
        story_id=message.story_id,
        message_id=message.message_id,
        turn=message.turn,
        variant=message.variant,
        principal=principal,
        rating=None if row is None else int(row.rating),
        tags=[] if row is None else list(row.tags),
        text=None if row is None else row.text,
        compacted_at=None if row is None else row.compacted_at,
        created_at=None if row is None else row.created_at,
        updated_at=None if row is None else row.updated_at,
        context=None if row is None else _context(row),
        history=[_entry(item) for item in history],
    )


async def _answer(conn: AsyncConnection, message: _Message, principal: str) -> Feedback:
    row = await _current(conn, message, principal, lock=False)
    return _feedback(message, principal, row, await _history(conn, message, principal))


async def read(db: AsyncEngine, story_id: str, message_id: str, *, principal: str) -> Feedback:
    """``principal``'s rating of the message, with its history (``rating`` null when it has
    none): 404 for an unknown story or a message that is not a generated message of it."""
    async with db.connect() as conn:
        message = await _message(conn, story_id, message_id, lock=False)
        return await _answer(conn, message, principal)


# -- writes ----------------------------------------------------------------------------------------


async def _deployment(conn: AsyncConnection) -> tuple[list[str], FeedbackSettings]:
    """The deployment's reason tags and feedback settings, its row held ``FOR SHARE``."""
    row = (
        await conn.execute(
            select(deployment_state.c.feedback_tags, deployment_state.c.settings)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .with_for_update(read=True)
        )
    ).one()
    return list(row.feedback_tags), section(FeedbackSettings, row.settings)


def _check(
    tags: Sequence[str], text: str | None, allowed: Sequence[str], settings: FeedbackSettings
) -> None:
    unknown = [tag for tag in tags if tag not in set(allowed)]
    if unknown:
        raise UnknownFeedbackTag(
            "a rating's tags must be the deployment's feedback tags",
            {"field": TAGS_FIELD, "unknown_tags": unknown, "allowed_tags": list(allowed)},
        )
    cap = settings.feedback_text_max_chars
    if text is not None and len(text) > cap:
        message = f"the text may hold at most {cap} characters once trimmed"
        raise InvalidRequest(
            message,
            {
                "field": TEXT_FIELD,
                "errors": [{"field": TEXT_FIELD, "message": message, "type": "string_too_long"}],
                "max_chars": cap,
            },
        )


async def _append_history(conn: AsyncConnection, row: Any, kind: HistoryKind, at: datetime) -> None:
    history_id = new_ulid()
    await conn.execute(
        insert(feedback_history).values(
            id=history_id,
            story_id=row.story_id,
            message_id=row.message_id,
            turn=row.turn,
            variant=row.variant,
            principal=row.principal,
            rating=row.rating,
            tags=list(row.tags),
            text=row.text,
            kind=kind.value,
            rated_at=row.updated_at,
            replaced_at=at,
        )
    )
    await index_records(
        conn,
        [_history_record(SimpleNamespace(id=history_id, story_id=row.story_id, text=row.text))],
    )


async def rate(
    db: AsyncEngine,
    story_id: str,
    message_id: str,
    request: FeedbackRequest,
    *,
    principal: str,
) -> FeedbackWrite:
    """Write ``principal``'s rating of the message (see the module docstring): 404 for an
    unknown story or message, 422 ``unknown_feedback_tag`` for a tag outside the deployment's
    set, 422 ``validation_error`` at ``body.text`` for a text over ``feedback_text_max_chars``.
    Nothing is written on a refusal."""
    refuse_nul({"body": request.model_dump(mode="json")})
    rating, tags, text = request.value()
    async with db.begin() as conn:
        message = await _message(conn, story_id, message_id, lock=True)
        allowed, settings = await _deployment(conn)
        _check(tags, text, allowed, settings)
        now = clock.now()
        current = await _current(conn, message, principal, lock=True)
        created = False
        if current is None:
            await pauses.pause(pauses.FEEDBACK_RATE_UNRATED)
            # Insert the rating, or lock the one a concurrent first rating committed: the
            # update never applies (WHERE false), but PostgreSQL locks the conflicting row
            # before it tests that, and reruns the statement when the row is deleted or changed
            # before the lock is granted (a retraction, a re-rating). It always does one.
            inserted = await conn.execute(
                pg_insert(message_feedback)
                .values(
                    id=new_ulid(),
                    story_id=story_id,
                    message_id=message_id,
                    turn=message.turn,
                    variant=message.variant,
                    principal=principal,
                    rating=rating,
                    tags=tags,
                    text=text,
                    created_at=now,
                    updated_at=now,
                    **context_of(message.operation_id, message.revisions),
                )
                .on_conflict_do_update(
                    index_elements=["story_id", "message_id", "principal"],
                    set_={"updated_at": message_feedback.c.updated_at},
                    where=false(),
                )
                .returning(message_feedback.c.id)
            )
            created = inserted.scalar() is not None
            if not created:  # a concurrent first rating committed first: this one replaces it
                locked = select(message_feedback).where(_key(message, principal))
                current = (await conn.execute(locked.with_for_update())).one()
        changed = created
        if current is not None and (current.rating, list(current.tags), current.text) != (
            rating,
            tags,
            text,
        ):
            await _append_history(conn, current, HistoryKind.REPLACED, now)
            await conn.execute(
                update(message_feedback)
                .where(message_feedback.c.id == current.id)
                .values(rating=rating, tags=tags, text=text, updated_at=now)
            )
            changed = True
        if changed:
            await index_hook(conn, record_id(message_id, principal), story_id, {"text": text})
        answer = await _answer(conn, message, principal)
    outcome = "created" if created else "replaced" if changed else "unchanged"
    log.info("feedback rated", story_id=story_id, message_id=message_id, outcome=outcome)
    return FeedbackWrite(feedback=answer, created=created, changed=changed)


async def retract(
    db: AsyncEngine, story_id: str, message_id: str, *, principal: str
) -> FeedbackRetraction:
    """Retract ``principal``'s rating of the message (see the module docstring): its final value
    joins the history and the rating goes. Nothing to retract is not an error. 404 for an
    unknown story or message."""
    async with db.begin() as conn:
        message = await _message(conn, story_id, message_id, lock=True)
        current = await _current(conn, message, principal, lock=True)
        if current is not None:
            await pauses.pause(pauses.FEEDBACK_RETRACT_LOCKED)
            await _append_history(conn, current, HistoryKind.RETRACTED, clock.now())
            await conn.execute(delete(message_feedback).where(message_feedback.c.id == current.id))
            await drop_hook(conn, record_id(message_id, principal))
    retracted = current is not None
    log.info(
        "feedback retracted",
        story_id=story_id,
        message_id=message_id,
        outcome="retracted" if retracted else "none",
    )
    return FeedbackRetraction(story_id=story_id, message_id=message_id, retracted=retracted)


# -- the deployment's tag set, and compaction ------------------------------------------------------


async def refuse_tag_removal(
    conn: AsyncConnection, before: Sequence[str], after: Sequence[str]
) -> None:
    """409 ``feedback_tag_in_use`` when a ``feedback_tags`` write removes tags that a rating or
    a history entry still carries (in the caller's transaction, which holds the deployment row
    ``FOR UPDATE``, so every rating that read the old set has committed). ``details.in_use``
    lists each such tag with its counts, in the old list's order."""
    kept = set(after)
    in_use: list[dict[str, Any]] = []
    for tag in (tag for tag in before if tag not in kept):
        current = await conn.scalar(
            select(func.count()).where(message_feedback.c.tags.contains([tag]))
        )
        history = await conn.scalar(
            select(func.count()).where(feedback_history.c.tags.contains([tag]))
        )
        if current or history:
            in_use.append({"tag": tag, "current": int(current or 0), "history": int(history or 0)})
    if in_use:
        raise FeedbackTagInUse(
            "a removed feedback tag is still carried by ratings; keep it in the list",
            {"field": TAGS_SETTING_FIELD, "in_use": in_use},
        )


async def mark_compacted(conn: AsyncConnection, story_id: str, message_ids: Sequence[str]) -> int:
    """Mark the ratings of messages an opt-in compaction removed (FR-FB-2, FR-STORE-32): the
    hook M7's compaction calls in its transaction. The ratings are kept, with ``compacted_at``
    set once; returns how many were newly marked."""
    if not message_ids:
        return 0
    marked = await conn.execute(
        update(message_feedback)
        .where(
            message_feedback.c.story_id == story_id,
            message_feedback.c.message_id.in_(list(message_ids)),
            message_feedback.c.compacted_at.is_(None),
        )
        .values(compacted_at=clock.now())
    )
    return int(marked.rowcount or 0)


RATING_RECORDS = message_feedback.name
"""``subject_references.record_table`` of a rating (an erasure reads it from here, so only the
listed modules name the feedback tables)."""
HISTORY_RECORDS = feedback_history.name
"""``subject_references.record_table`` of a rating's history entry."""


@dataclasses.dataclass(frozen=True, slots=True)
class FeedbackRedaction:
    """What an erasure's redaction of a story's feedback changed (or, in a dry run, would):
    the ratings and history entries whose text named the subject, and their texts."""

    ratings: int = 0
    history: int = 0

    @property
    def records(self) -> int:
        return self.ratings + self.history


class TextRedactor(Protocol):
    def text(self, value: str) -> str: ...


async def redact_for_erasure(
    conn: AsyncConnection,
    story_id: str,
    *,
    rating_ids: Collection[str],
    history_ids: Collection[str],
    redactor: TextRedactor,
    write: bool,
) -> FeedbackRedaction:
    """Erasure (FR-ADMIN-6; #124): redact the text of the story's ratings (by their index
    ``record_id``) and history entries (by id) that name the subject, in place, marking each
    ``redacted_at``; the rating, its tags and its history stay. With ``write`` false nothing
    is written (a dry run's count)."""
    now = clock.now()
    ratings = 0
    if rating_ids:
        found = await conn.execute(
            select(
                message_feedback.c.id,
                message_feedback.c.message_id,
                message_feedback.c.principal,
                message_feedback.c.text,
            ).where(message_feedback.c.story_id == story_id)
        )
        for row in found:
            if record_id(str(row.message_id), str(row.principal)) not in rating_ids:
                continue
            new = redactor.text(row.text) if row.text else row.text
            if new == row.text:
                continue
            ratings += 1
            if write:
                await conn.execute(
                    update(message_feedback)
                    .where(message_feedback.c.id == row.id)
                    .values(text=new, redacted_at=now)
                )
    history = 0
    if history_ids:
        found = await conn.execute(
            select(feedback_history.c.id, feedback_history.c.text).where(
                feedback_history.c.story_id == story_id,
                feedback_history.c.id.in_(sorted(history_ids)),
            )
        )
        for row in found:
            new = redactor.text(row.text) if row.text else row.text
            if new == row.text:
                continue
            history += 1
            if write:
                await conn.execute(
                    update(feedback_history)
                    .where(feedback_history.c.id == row.id)
                    .values(text=new, redacted_at=now)
                )
    return FeedbackRedaction(ratings=ratings, history=history)


__all__ = [
    "HISTORY_RECORDS",
    "RATED_EVENT",
    "RATING_RECORDS",
    "TAGS_PER_RATING",
    "Feedback",
    "FeedbackContext",
    "FeedbackHistoryEntry",
    "FeedbackRedaction",
    "FeedbackRequest",
    "FeedbackRetraction",
    "FeedbackWrite",
    "context_of",
    "drop_hook",
    "index_hook",
    "mark_compacted",
    "rate",
    "read",
    "record_id",
    "redact_for_erasure",
    "refuse_tag_removal",
    "retract",
]
