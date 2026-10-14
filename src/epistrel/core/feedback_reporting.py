"""Feedback reporting (FR-FB-3; #126): aggregates of the current ratings, the implicit signals
already in the log, and the guardrail and memory review lists.

**Aggregates** (``aggregates``) group the current ratings (``message_feedback``: one per principal
and message; a retracted or replaced value is history, not counted) by one dimension
(``FeedbackGroupBy``):

- ``profile_version``: the engine profile version the message was produced under
  (``<profile id>@<version>``);
- ``fingerprint``: its configuration fingerprint;
- ``model``: the ``generation`` purpose's model id;
- ``content_rating``: the content rating (its id, not the version) the story had;
- ``scenario``: the scenario the story was created from;
- ``tag``: each reason tag (a rating counts once under each of its tags; a rating with none
  under ``null``);
- ``day``: the day (UTC, ``YYYY-MM-DD``) the rating was first given.

All but ``day`` come from the context the rating copied when it was first written (#112), so a
later profile or rating change never moves a rating between groups. A rating whose message
recorded no such value is grouped under ``null``. ``from`` (inclusive) and ``to`` (exclusive)
bound when the rating was first given (``created_at``); none means all time. ``story_id``
narrows to one story. Every rating counts, whichever variant it rated: a dormant or discarded
variant's too.

Each group answers ``count`` (ratings), ``messages`` (distinct rated messages: the signals'
denominator), ``mean_rating`` (two decimals), ``histogram`` (always the five keys ``1`` to ``5``)
and ``tags`` (the tags present, with their counts), and the **signals** of its rated messages:

- ``regenerations``: regenerations of a rated message: a regenerate's ``turn`` operation names
  the variant it replaced (``turn_report.regenerated_variant``), and that variant's message is
  the one regenerated. A turn written over a rewound tail is not one, though its variant is
  above 1 too;
- ``regen_after_low_rating``: ratings of 1 or 2 followed, within the deployment's
  ``feedback_regen_window_s`` (default 300 seconds), by a regeneration of the rated message
  (from the rating's latest value, ``updated_at``, to the regenerate's commit);
- ``swaps_back``: ``swap`` events at the message's turn that made it dormant in favour of an
  older variant (a lower variant number; an untruncate that swaps back is one too);
- ``rewinds``: ``truncate`` events whose first discarded turn is the message's own, with the
  message among the operations they discarded.

A message's signals count once in each group it falls in, however many principals rated it.
``totals`` is the same over every rating in the window (not per tag). Groups are ordered by
``count`` (highest first), then ``key`` (``null`` last), and at most ``MAX_GROUPS`` are answered
(``truncated`` says when there were more).

**Review lists** (``review``): the current ratings tagged ``should_have_been_blocked`` (the
**guardrail** review list: candidate guardrail misses) or ``forgot_something`` or
``factual_error`` (the **memory** review list: candidate memory or verification misses). A
rating carrying tags of both lists is on both. One item per rating, newest rating first (by its
latest value), in keyset pages, with the rated message's text and the player input it answered
(as stored: an erased span reads ``[erased]``), its position and the context it was produced
under. Ratings of a story the erasure reconciler holds blocked (``blocked_by_erasure``) are left
out until it is reconciled: its text may still name an erased subject.

Feedback is flag-only: nothing in the engine reads these reports to change a turn, a profile or
a guardrail (the import-linter contract ``feedback-is-flag-only``). Log lines carry counts only
(invariant 3).
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, String, and_, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.catalog.search import Position, decode_cursor, encode_cursor
from epistrel.core.config_changes import section
from epistrel.core.enums import OperationKind, OperationStatus, StoryStatus
from epistrel.core.errors import InvalidRequest, StoryNotFound
from epistrel.core.feedback import FeedbackContext
from epistrel.core.settings_schema import FeedbackSettings
from epistrel.core.tables import deployment_state, events, operations, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.feedback import RATING_MAX, RATING_MIN, message_feedback
from epistrel.core.tables.operations import REGENERATED_VARIANT
from epistrel.core.tables.ratings import content_rating_versions
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

MAX_GROUPS = 500
"""The most groups an aggregate answers (``truncated`` when there are more)."""
LOW_RATING = 2
"""A rating of at most this is low: a regeneration soon after it is ``regen_after_low_rating``."""
GENERATION_PURPOSE = "generation"
"""The model purpose the ``model`` dimension groups by."""
DEFAULT_PAGE = 50
MAX_PAGE = 200
CURSOR_MAX = 512
PLAYER_INPUT = "player_input"
RATED_EVENT = "message_rendered"
SWAP = "swap"
TRUNCATE = "truncate"


class FeedbackGroupBy(StrEnum):
    """The one dimension an aggregate groups by (see the module docstring)."""

    PROFILE_VERSION = "profile_version"
    FINGERPRINT = "fingerprint"
    MODEL = "model"
    CONTENT_RATING = "content_rating"
    SCENARIO = "scenario"
    TAG = "tag"
    DAY = "day"


class FeedbackReviewList(StrEnum):
    """A review list: ``guardrail`` (``should_have_been_blocked``) or ``memory``
    (``forgot_something``, ``factual_error``)."""

    GUARDRAIL = "guardrail"
    MEMORY = "memory"


REVIEW_TAGS: dict[FeedbackReviewList, tuple[str, ...]] = {
    FeedbackReviewList.GUARDRAIL: ("should_have_been_blocked",),
    FeedbackReviewList.MEMORY: ("forgot_something", "factual_error"),
}
"""The reason tags that put a rating on each review list (FR-FB-3)."""


# -- the answers ----------------------------------------------------------------------------------


class FeedbackSignals(BaseModel):
    """The implicit signals of a group's rated messages (see the module docstring)."""

    model_config = ConfigDict(frozen=True)

    regenerations: int = 0
    regen_after_low_rating: int = 0
    swaps_back: int = 0
    rewinds: int = 0


class FeedbackAggregateTotals(BaseModel):
    """Counts over a set of ratings: how many (``count``), of how many distinct messages
    (``messages``), their mean (``mean_rating``, two decimals; null with none), their
    ``histogram`` (keys ``1`` to ``5``), the ``tags`` they carry (present tags only) and the
    ``signals`` of their messages."""

    model_config = ConfigDict(frozen=True)

    count: int
    messages: int
    mean_rating: float | None
    histogram: dict[str, int]
    tags: dict[str, int]
    signals: FeedbackSignals


class FeedbackAggregateGroup(FeedbackAggregateTotals):
    """One group: its ``key`` (null for ratings without a value in that dimension) and its
    counts."""

    key: str | None


class FeedbackAggregateWindow(BaseModel):
    """What the aggregate covers: ratings first given from ``from`` (inclusive) to ``to``
    (exclusive; null: unbounded), and the regeneration window it used, in seconds."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    regen_window_s: int


class FeedbackAggregates(BaseModel):
    """``GET /feedback/aggregates`` (FR-FB-3): the groups by one dimension, with the totals."""

    model_config = ConfigDict(frozen=True)

    group_by: FeedbackGroupBy
    story_id: str | None
    window: FeedbackAggregateWindow
    groups: list[FeedbackAggregateGroup]
    truncated: bool
    totals: FeedbackAggregateTotals


class ReviewedPlayerInput(BaseModel):
    """The player input a message answered: what the acting character said and did."""

    model_config = ConfigDict(frozen=True)

    say: str | None
    do: str | None


class FeedbackReviewItem(BaseModel):
    """One rating on a review list: the message (``story_id``, ``turn``, ``variant``,
    ``message_id``), the rating with its tags and text and when it was given (its latest value),
    the principal who rated, the message's text and the player input it answered (null for an
    opening narration), and the context the message was produced under."""

    model_config = ConfigDict(frozen=True)

    story_id: str
    turn: int
    variant: int
    message_id: str
    principal: str
    rating: int
    tags: list[str]
    text: str | None
    rated_at: datetime
    message_text: str | None
    player_input: ReviewedPlayerInput | None
    context: FeedbackContext


class FeedbackReviewPage(BaseModel):
    """A page of a review list, newest rating first, and the cursor of the next page (null: the
    last)."""

    model_config = ConfigDict(frozen=True)

    review: FeedbackReviewList
    tags: list[str]
    items: list[FeedbackReviewItem]
    next_cursor: str | None


# -- the request ----------------------------------------------------------------------------------


def check_window(
    start: datetime | None, end: datetime | None, *, field: str = "query.from"
) -> None:
    """422 ``validation_error`` at ``field`` when the window ends before it starts."""
    if start is not None and end is not None and start > end:
        message = "from must not be after to"
        raise InvalidRequest(
            message,
            {
                "field": field,
                "errors": [{"field": field, "message": message, "type": "value_error"}],
            },
        )


async def _require_story(conn: AsyncConnection, story_id: str | None) -> None:
    if story_id is None:
        return
    if await conn.scalar(select(stories.c.id).where(stories.c.id == story_id)) is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})


async def feedback_settings(conn: AsyncConnection) -> FeedbackSettings:
    """The deployment's feedback settings (``feedback_regen_window_s``, ``feedback_export``)."""
    stored = await conn.scalar(
        select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return section(FeedbackSettings, stored)


def _windowed(start: datetime | None, end: datetime | None, story_id: str | None) -> list[Any]:
    where: list[Any] = []
    if start is not None:
        where.append(message_feedback.c.created_at >= start)
    if end is not None:
        where.append(message_feedback.c.created_at < end)
    if story_id is not None:
        where.append(message_feedback.c.story_id == story_id)
    return where


# -- the signals ----------------------------------------------------------------------------------


def _regenerations_of(turn: Any, variant: Any, story: Any) -> Any:
    """The committed regenerate operations that replaced the variant at (``story``, ``turn``,
    ``variant``)."""
    return and_(
        operations.c.story_id == story,
        operations.c.kind == OperationKind.TURN.value,
        operations.c.status == OperationStatus.COMMITTED.value,
        operations.c.narrative_turn == turn,
        cast(operations.c.turn_report[REGENERATED_VARIANT].astext, Integer) == variant,
    )


def _signal_columns(window_s: int) -> list[Any]:
    """Per rating: its message's regenerations, swaps back and rewinds, and whether it was low
    and followed by a regeneration within ``window_s`` (correlated with ``message_feedback``)."""
    f = message_feedback.c
    regenerations = (
        select(func.count())
        .select_from(operations)
        .where(_regenerations_of(f.turn, f.variant, f.story_id))
        .scalar_subquery()
    )
    soon = exists(
        select(operations.c.operation_id).where(
            _regenerations_of(f.turn, f.variant, f.story_id),
            operations.c.committed_at >= f.updated_at,
            operations.c.committed_at
            <= f.updated_at + func.make_interval(0, 0, 0, 0, 0, 0, window_s),
        )
    )
    activated = operations.alias("activated")
    swaps_back = (
        select(func.count())
        .select_from(events)
        .where(
            events.c.story_id == f.story_id,
            events.c.type == SWAP,
            cast(events.c.payload["turn"].astext, Integer) == f.turn,
            events.c.payload["dormant_operation_ids"].has_key(cast(f.operation_id, String)),
            exists(
                select(activated.c.operation_id).where(
                    activated.c.story_id == f.story_id,
                    activated.c.narrative_turn == f.turn,
                    activated.c.turn_variant < f.variant,
                    events.c.payload["activated_operation_ids"].has_key(
                        cast(activated.c.operation_id, String)
                    ),
                )
            ),
        )
        .scalar_subquery()
    )
    rewinds = (
        select(func.count())
        .select_from(events)
        .where(
            events.c.story_id == f.story_id,
            events.c.type == TRUNCATE,
            cast(events.c.payload["to_turn"].astext, Integer) + 1 == f.turn,
            events.c.payload["operation_ids"].has_key(cast(f.operation_id, String)),
        )
        .scalar_subquery()
    )
    return [
        regenerations.label("regenerations"),
        and_(f.rating <= LOW_RATING, soon).label("low_then_regenerated"),
        swaps_back.label("swaps_back"),
        rewinds.label("rewinds"),
    ]


# -- aggregates -----------------------------------------------------------------------------------


def _dimension(group_by: FeedbackGroupBy) -> Any:
    f = message_feedback.c
    match group_by:
        case FeedbackGroupBy.PROFILE_VERSION:
            return f.profile_version
        case FeedbackGroupBy.FINGERPRINT:
            return f.config_fingerprint
        case FeedbackGroupBy.MODEL:
            return f.models[GENERATION_PURPOSE]["id"].astext
        case FeedbackGroupBy.CONTENT_RATING:
            return (
                select(content_rating_versions.c.rating_id)
                .where(content_rating_versions.c.version_id == f.rating_version_id)
                .scalar_subquery()
            )
        case FeedbackGroupBy.SCENARIO:
            return select(stories.c.scenario_id).where(stories.c.id == f.story_id).scalar_subquery()
        case FeedbackGroupBy.DAY:
            return func.to_char(func.timezone("UTC", f.created_at), "YYYY-MM-DD")
        case FeedbackGroupBy.TAG:
            return literal(None, String)


@dataclasses.dataclass(slots=True)
class _Tally:
    ratings: list[int] = dataclasses.field(default_factory=list)
    tags: Counter[str] = dataclasses.field(default_factory=Counter)
    messages: dict[tuple[str, str], tuple[int, int, int]] = dataclasses.field(default_factory=dict)
    low_then_regenerated: int = 0

    def add(self, row: Any) -> None:
        self.ratings.append(int(row.rating))
        self.tags.update(row.tags)
        self.messages[(str(row.story_id), str(row.message_id))] = (
            int(row.regenerations),
            int(row.swaps_back),
            int(row.rewinds),
        )
        self.low_then_regenerated += 1 if row.low_then_regenerated else 0

    def totals(self) -> dict[str, Any]:
        count = len(self.ratings)
        histogram = Counter(self.ratings)
        return {
            "count": count,
            "messages": len(self.messages),
            "mean_rating": round(sum(self.ratings) / count, 2) if count else None,
            "histogram": {
                str(value): histogram.get(value, 0) for value in range(RATING_MIN, RATING_MAX + 1)
            },
            "tags": dict(sorted(self.tags.items())),
            "signals": FeedbackSignals(
                regenerations=sum(m[0] for m in self.messages.values()),
                regen_after_low_rating=self.low_then_regenerated,
                swaps_back=sum(m[1] for m in self.messages.values()),
                rewinds=sum(m[2] for m in self.messages.values()),
            ),
        }


def _order(item: tuple[str | None, _Tally]) -> tuple[int, int, str]:
    key, tally = item
    return (-len(tally.ratings), 1 if key is None else 0, key or "")


async def aggregates(
    db: AsyncEngine,
    group_by: FeedbackGroupBy,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    story_id: str | None = None,
) -> FeedbackAggregates:
    """The current ratings grouped by ``group_by`` with their signals (see the module
    docstring): 422 for a window that ends before it starts, 404 for an unknown story."""
    check_window(start, end)
    async with db.connect() as conn:
        await _require_story(conn, story_id)
        settings = await feedback_settings(conn)
        window_s = settings.feedback_regen_window_s
        f = message_feedback.c
        found = await conn.execute(
            select(
                f.story_id,
                f.message_id,
                f.rating,
                f.tags,
                _dimension(group_by).label("key"),
                *_signal_columns(window_s),
            ).where(*_windowed(start, end, story_id))
        )
        rows = list(found)
    groups: dict[str | None, _Tally] = {}
    totals = _Tally()
    for row in rows:
        totals.add(row)
        keys: Sequence[str | None]
        if group_by is FeedbackGroupBy.TAG:
            keys = list(row.tags) or [None]
        else:
            keys = [None if row.key is None else str(row.key)]
        for key in keys:
            groups.setdefault(key, _Tally()).add(row)
    ordered = sorted(groups.items(), key=_order)
    answer = FeedbackAggregates(
        group_by=group_by,
        story_id=story_id,
        window=FeedbackAggregateWindow(from_=start, to=end, regen_window_s=window_s),
        groups=[
            FeedbackAggregateGroup(key=key, **tally.totals()) for key, tally in ordered[:MAX_GROUPS]
        ],
        truncated=len(ordered) > MAX_GROUPS,
        totals=FeedbackAggregateTotals(**totals.totals()),
    )
    log.info(
        "feedback aggregates",
        group_by=group_by.value,
        groups=len(answer.groups),
        ratings=len(rows),
    )
    return answer


# -- the rated message's text ---------------------------------------------------------------------


def message_text_column() -> Any:
    """The rated message's text (its ``message_rendered`` payload, as stored: erased spans read
    ``[erased]``), correlated with ``message_feedback``."""
    rendered = events.alias("rendered")
    return (
        select(rendered.c.payload["text"].astext)
        .where(rendered.c.id == message_feedback.c.message_id)
        .scalar_subquery()
    )


def player_input_column() -> Any:
    """The player input the rated message answered (the ``player_input`` event of its
    operation, as ``{say, do}`` JSON), correlated with ``message_feedback``; null for a message
    no input preceded (a scenario's opening narration)."""
    said = events.alias("said")
    return (
        select(
            func.jsonb_build_object(
                "say", said.c.payload["say"], "do", said.c.payload["do"], type_=JSONB
            )
        )
        .where(
            said.c.operation_id == message_feedback.c.operation_id,
            said.c.type == PLAYER_INPUT,
        )
        .order_by(said.c.seq)
        .limit(1)
        .scalar_subquery()
    )


def player_input_of(value: Any) -> ReviewedPlayerInput | None:
    if not isinstance(value, Mapping):
        return None
    say, do = value.get("say"), value.get("do")
    return ReviewedPlayerInput(
        say=say if isinstance(say, str) else None, do=do if isinstance(do, str) else None
    )


def context_of_row(row: Any) -> FeedbackContext:
    return FeedbackContext(
        operation_id=row.operation_id,
        config_fingerprint=row.config_fingerprint,
        profile_version=row.profile_version,
        models=row.models,
        rating_version_id=row.rating_version_id,
        quality_status=row.quality_status,
    )


def unblocked() -> Any:
    """Ratings of stories the erasure reconciler does not hold blocked (#125)."""
    return exists(
        select(stories.c.id).where(
            stories.c.id == message_feedback.c.story_id,
            stories.c.status != StoryStatus.BLOCKED_BY_ERASURE.value,
        )
    )


# -- review lists ---------------------------------------------------------------------------------


def _fingerprint(
    which: FeedbackReviewList,
    story_id: str | None,
    start: datetime | None,
    end: datetime | None,
    tag: str | None,
) -> str:
    parts = [
        which.value,
        story_id or "",
        start.isoformat() if start else "",
        end.isoformat() if end else "",
        tag or "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


async def review(
    db: AsyncEngine,
    which: FeedbackReviewList,
    *,
    story_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    tag: str | None = None,
    limit: int = DEFAULT_PAGE,
    cursor: str | None = None,
) -> FeedbackReviewPage:
    """A page of a review list (see the module docstring): 422 for a window that ends before it
    starts, a ``tag`` that is not one of the list's, or a cursor another listing gave; 404 for
    an unknown story."""
    check_window(start, end)
    tags = REVIEW_TAGS[which]
    if tag is not None and tag not in tags:
        message = f"tag must be one of the {which.value} review list's tags: {', '.join(tags)}"
        raise InvalidRequest(
            message,
            {
                "field": "query.tag",
                "errors": [{"field": "query.tag", "message": message, "type": "enum"}],
            },
        )
    bound = _fingerprint(which, story_id, start, end, tag)
    after = decode_cursor(cursor, bound, ranked=False) if cursor is not None else None
    f = message_feedback.c
    where: list[Any] = [
        *_windowed(start, end, story_id),
        f.tags.overlap(list(tags)) if tag is None else f.tags.contains([tag]),
        unblocked(),
    ]
    if after is not None:
        where.append(
            or_(
                f.updated_at < after.updated_at,
                and_(f.updated_at == after.updated_at, f.id < after.id),
            )
        )
    async with db.connect() as conn:
        await _require_story(conn, story_id)
        found = await conn.execute(
            select(
                message_feedback,
                message_text_column().label("message_text"),
                player_input_column().label("player_input"),
            )
            .where(*where)
            .order_by(f.updated_at.desc(), f.id.desc())
            .limit(limit + 1)
        )
        rows = list(found)
    page = rows[:limit]
    items = [
        FeedbackReviewItem(
            story_id=row.story_id,
            turn=row.turn,
            variant=row.variant,
            message_id=row.message_id,
            principal=row.principal,
            rating=row.rating,
            tags=list(row.tags),
            text=row.text,
            rated_at=row.updated_at,
            message_text=row.message_text,
            player_input=player_input_of(row.player_input),
            context=context_of_row(row),
        )
        for row in page
    ]
    next_cursor = (
        encode_cursor(Position(None, page[-1].updated_at, str(page[-1].id)), bound)
        if len(rows) > limit and page
        else None
    )
    log.info("feedback review", list=which.value, items=len(items))
    return FeedbackReviewPage(review=which, tags=list(tags), items=items, next_cursor=next_cursor)


__all__ = [
    "MAX_GROUPS",
    "REVIEW_TAGS",
    "FeedbackAggregateGroup",
    "FeedbackAggregateTotals",
    "FeedbackAggregateWindow",
    "FeedbackAggregates",
    "FeedbackGroupBy",
    "FeedbackReviewItem",
    "FeedbackReviewList",
    "FeedbackReviewPage",
    "FeedbackSignals",
    "ReviewedPlayerInput",
    "aggregates",
    "check_window",
    "context_of_row",
    "feedback_settings",
    "message_text_column",
    "player_input_column",
    "player_input_of",
    "review",
    "unblocked",
]
