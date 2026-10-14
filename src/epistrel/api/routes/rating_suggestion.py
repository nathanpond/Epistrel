"""What ``POST /catalog/{characters|scenarios}/{id}/rating-suggestion`` shares between the two
catalog kinds (FR-RATE-8; #121): its description, its parameters and the refusal of a
``projection``. Each kind's route module serves the route itself (one ``Engine`` call)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, Query

from epistrel.core.errors import InvalidRequest
from epistrel.core.ratings.suggest import SuggestionRequest

DESCRIPTION = (
    "Suggest content ratings for the {noun} (FR-RATE-8): a table with **one row for every "
    "rating** in the deployment (`include_retired=true`: the retired ones too), in display "
    "order, never a single answer, since ratings need not form a line. Each row says whether "
    "the rating is `appropriate` and, for a no, every concern: exactly one of the `policy_id` "
    "or the rating-description `clause` the entry breaks, the `element` that breaks it (`kind` "
    "`field`, `seed`, `role`, `reveal`, `cast_character` or `media_caption`, and its "
    "`location`), and an `explanation`. The judgment is limited to content policy: a rating is "
    "appropriate unless the entry breaks its policies, and nothing is ever too mild for a "
    "rating. Deterministic policies are evaluated directly; the rest goes to one "
    "`rating_suggestion` model call per rating (its model is the purpose's own row, else "
    "`guardrail_check`'s, else `default`'s). A call that fails or runs past the 20 s deadline "
    "leaves its row's `error` set (`model_unavailable`, `deadline` or `invalid_response`) and "
    "`appropriate` null unless a deterministic concern was found; the answer is still 200. "
    '{inputs}The body\'s `version` names a published version or `"draft"`; without it the '
    "latest published version is reviewed, else the draft (404 for an unknown {noun} or "
    "version). `evaluated` records the rating versions used.\n\n"
    "**Advisory** (`advisory: true`): it never changes a rating or blocks a publish, and no "
    "guardrail, publish, story-creation or rating-change path reads it; output guardrails apply "
    "whatever it says. It writes nothing and takes no `Idempotency-Key`.\n\n"
    "**Author-facing:** its concerns quote private fields (seeds, roles, the director's "
    "material, a character's definition), like the `full` projection; the `card`/`full` "
    "distinction does not apply, and a `projection` parameter is refused (422)."
)
SCENARIO_INPUTS = (
    "A scenario's review covers its own content and the definitions of its default and locked "
    "roles' characters (their pinned version, else the latest published one); a character that "
    "cannot be read is skipped and listed in `unavailable`. "
)

IncludeRetired = Annotated[
    bool, Query(description="Add a row for every retired rating too (default: live ones only).")
]
RefusedProjection = Annotated[str | None, Query(include_in_schema=False)]
Request = Annotated[SuggestionRequest | None, Body()]


def refuse_projection(projection: str | None) -> None:
    """A ``projection`` does not apply to the table (it is author-facing as a whole): 422."""
    if projection is not None:
        raise InvalidRequest(
            "a rating suggestion has no projection: it is an author-facing read",
            {"field": "query.projection"},
        )


__all__ = [
    "DESCRIPTION",
    "SCENARIO_INPUTS",
    "IncludeRetired",
    "RefusedProjection",
    "Request",
    "refuse_projection",
]
