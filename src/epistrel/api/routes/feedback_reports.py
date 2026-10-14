"""Feedback reporting (FR-FB-3; #126): the aggregates and the two review lists.

- ``GET /feedback/aggregates?group_by=&from=&to=&story_id=``: the current ratings grouped by one
  dimension, with their implicit signals, and the totals.
- ``GET /feedback/review/guardrail`` and ``GET /feedback/review/memory``: the ratings tagged
  ``should_have_been_blocked``, or ``forgot_something``/``factual_error``, newest first, in
  keyset pages. Both routes serve ``Engine.feedback_review`` (operation ids
  ``feedback_review_guardrail``, ``feedback_review_memory``).

Author-facing reads: no viewpoint (a rating is never story content, and the review items carry
the rated message as stored) and no principal. Every handler makes one ``Engine`` call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from epistrel.api.config_changes import IsoDateTime
from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.routes.feedback import FEEDBACK_TAG
from epistrel.core.feedback_reporting import (
    CURSOR_MAX,
    DEFAULT_PAGE,
    MAX_GROUPS,
    MAX_PAGE,
    REVIEW_TAGS,
    FeedbackAggregates,
    FeedbackGroupBy,
    FeedbackReviewList,
    FeedbackReviewPage,
)
from epistrel.core.ids import ULID_PATTERN

router = APIRouter(prefix="/feedback", tags=[FEEDBACK_TAG])

FLAG_ONLY = (
    "Feedback is flag-only: nothing in the engine reads these reports to change a turn, a "
    "profile or a guardrail."
)

FromQuery = Annotated[
    IsoDateTime | None,
    Query(
        alias="from",
        description="Ratings first given at or after this time (inclusive); none: all time.",
    ),
]
ToQuery = Annotated[
    IsoDateTime | None,
    Query(description="Ratings first given before this time (exclusive); none: up to now."),
]
StoryQuery = Annotated[
    str | None,
    Query(pattern=ULID_PATTERN, description="Only this story's ratings (404 when unknown)."),
]

AGGREGATES_DESCRIPTION = (
    "Feedback aggregates (FR-FB-3) over the current ratings, grouped by one dimension "
    "(`group_by`): `profile_version` (`<profile id>@<version>`), `fingerprint` (the "
    "configuration fingerprint), `model` (the `generation` purpose's model id), "
    "`content_rating` (the story's content rating id), `scenario` (the story's scenario id), "
    "`tag` (each reason tag; a rating counts under each of its tags, an untagged one under "
    "`null`) or `day` (UTC, `YYYY-MM-DD`, when the rating was first given). All but `day` are "
    "the context the rating recorded when it was first given; a missing value is the `null` "
    "key. Each group has `count` (ratings), `messages` (distinct rated messages, the signals' "
    "denominator: the regeneration rate is `signals.regenerations / messages`), `mean_rating` "
    "(two decimals), `histogram` (keys `1` to `5`), `tags` (present tags only) and `signals`: "
    "`regenerations` of the rated messages, `regen_after_low_rating` (ratings of 1 or 2 "
    "followed by a regeneration of the message within the deployment's "
    "`feedback_regen_window_s`, 300 s by default), `swaps_back` (swaps at the message's turn "
    "back to an older variant, untruncates included) and `rewinds` (truncates whose first "
    "discarded turn is the message). Every variant's ratings count, dormant and discarded "
    "ones too. `from` (inclusive) and `to` (exclusive) bound when a rating was first given "
    "(`from` after `to` is 422); `story_id` narrows to one story. Groups are ordered by "
    f"`count`, highest first, then `key` (`null` last), at most {MAX_GROUPS} (`truncated` says "
    "when there were more); `totals` counts every rating once. " + FLAG_ONLY
)


@router.get(
    "/aggregates",
    response_model=FeedbackAggregates,
    operation_id="feedback_aggregates",
    summary="Aggregate the feedback",
    description=AGGREGATES_DESCRIPTION,
)
async def feedback_aggregates(
    ctx: ContextDep,
    engine: EngineDep,
    group_by: Annotated[FeedbackGroupBy, Query(description="The one dimension to group by.")],
    from_: FromQuery = None,
    to: ToQuery = None,
    story_id: StoryQuery = None,
) -> FeedbackAggregates:
    return await engine.feedback_aggregates(ctx, group_by, start=from_, end=to, story_id=story_id)


def _review_description(which: FeedbackReviewList, what: str) -> str:
    tags = " or ".join(f"`{tag}`" for tag in REVIEW_TAGS[which])
    return (
        f"The {which.value} review list (FR-FB-3): the current ratings tagged {tags} ({what}), "
        "newest rating first, one item per rating (a rating carrying both lists' tags is on "
        "both), `limit` at a time; `cursor` (a page's `next_cursor`) continues with the same "
        "filters (422 `cursor_invalid` otherwise). Each item has the message's position and "
        "id, the rating with its tags, text and time, the principal who rated, the message's "
        "text and the player input it answered as stored (an erased span reads `[erased]`), "
        "and the context the message was produced under. Filters: `story_id`, `from` "
        "(inclusive) and `to` (exclusive) on when the rating was first given, and `tag` (one of "
        "the list's tags; another is 422). Ratings of a story blocked by a pending erasure are "
        "left out until it is reconciled. " + FLAG_ONLY
    )


async def _review(
    which: FeedbackReviewList,
    ctx: ContextDep,
    engine: EngineDep,
    *,
    story_id: str | None,
    from_: datetime | None,
    to: datetime | None,
    tag: str | None,
    limit: int,
    cursor: str | None,
) -> FeedbackReviewPage:
    return await engine.feedback_review(
        ctx,
        which,
        story_id=story_id,
        start=from_,
        end=to,
        tag=tag,
        limit=limit,
        cursor=cursor,
    )


LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE, description="Items per page.")]
CursorQuery = Annotated[
    str | None, Query(max_length=CURSOR_MAX, description="The previous page's `next_cursor`.")
]
TagQuery = Annotated[
    str | None, Query(max_length=64, description="Only the ratings carrying this list tag.")
]


@router.get(
    "/review/guardrail",
    response_model=FeedbackReviewPage,
    operation_id="feedback_review_guardrail",
    summary="List candidate guardrail misses",
    description=_review_description(FeedbackReviewList.GUARDRAIL, "candidate guardrail misses"),
)
async def feedback_review_guardrail(
    ctx: ContextDep,
    engine: EngineDep,
    story_id: StoryQuery = None,
    from_: FromQuery = None,
    to: ToQuery = None,
    tag: TagQuery = None,
    limit: LimitQuery = DEFAULT_PAGE,
    cursor: CursorQuery = None,
) -> FeedbackReviewPage:
    return await _review(
        FeedbackReviewList.GUARDRAIL,
        ctx,
        engine,
        story_id=story_id,
        from_=from_,
        to=to,
        tag=tag,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/review/memory",
    response_model=FeedbackReviewPage,
    operation_id="feedback_review_memory",
    summary="List candidate memory misses",
    description=_review_description(
        FeedbackReviewList.MEMORY, "candidate memory or verification misses"
    ),
)
async def feedback_review_memory(
    ctx: ContextDep,
    engine: EngineDep,
    story_id: StoryQuery = None,
    from_: FromQuery = None,
    to: ToQuery = None,
    tag: TagQuery = None,
    limit: LimitQuery = DEFAULT_PAGE,
    cursor: CursorQuery = None,
) -> FeedbackReviewPage:
    return await _review(
        FeedbackReviewList.MEMORY,
        ctx,
        engine,
        story_id=story_id,
        from_=from_,
        to=to,
        tag=tag,
        limit=limit,
        cursor=cursor,
    )
