"""Helpers for the story-image rating checks (#272): a scratch database with media (#118's
``rating_db`` with a media root), a stricter rating whose own policy judges image outputs by
model, and the stub image checker configured over REST.

The rating checks run the job worker, so these tests use a scratch database
(``tests/rating_change_support``)."""

from __future__ import annotations

from typing import Any

from epistrel.core.policies.model import Policy
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.orchestrator.policies.schemas import PolicyFields
from tests.image_support import StubImageChecker
from tests.model_support import admin_client, put
from tests.policy_support import OPERATOR, unique
from tests.rating_change_support import Scratch, rows, scalar

MINORS = "minors_no_sexual_content"


async def image_rating(s: Scratch) -> tuple[str, Policy]:
    """A new rating (inheriting nothing) whose one policy is model-assisted and judges image
    outputs only: the stricter rating a tightening moves a story to."""
    rating = await create_rating(
        s.db, RatingFields(name=unique("image-rating")), principal=OPERATOR.principal or ""
    )
    body = PolicyFields.model_validate(
        {
            "scope": "rating",
            "scope_id": rating.rating.id,
            "name": unique("no-storms"),
            "rule": {"type": "model_assisted", "prompt": "No storms in pictures."},
            "applies_to": ["image_output"],
            "message": "No storms in pictures.",
        }
    )
    made = (await s.lib.policies_create(OPERATOR, body)).policy
    current = rating.rating
    await update_rating(
        s.db,
        current.id,
        RatingReplacement(
            name=current.name,
            description=current.description,
            display_order=current.display_order,
            prompt_guidance=current.prompt_guidance,
            image_guidance=current.image_guidance,
            policy_ids=[made.id],
            inherits_from=[],
        ),
        principal=OPERATOR.principal or "",
    )
    return current.id, made


async def configure_checker(s: Scratch) -> StubImageChecker:
    """An ``image_check`` model (the stub's ``stub-vision``) set over REST; its answers are
    scripted on the returned checker."""
    async with admin_client(s.settings, s.lib) as admin:
        await put(admin, "image_check", {"model": "stub-vision"})
    return StubImageChecker(s.lib.models.stub)


def answer(s: Scratch, verdicts: dict[str, str]) -> StubImageChecker:
    """The stub checker answering only ``verdicts`` from now on (earlier answers dropped)."""
    s.lib.models.stub.reset()
    checker = StubImageChecker(s.lib.models.stub)
    checker.answer(verdicts)  # type: ignore[arg-type]
    return checker


async def stamp(s: Scratch, story_id: str, turn: int, variant: int = 1) -> Any:
    """The ``checked_guardrails`` stamp of the story's turn ``turn`` (variant ``variant``)."""
    return await scalar(
        s.db,
        "SELECT checked_guardrails FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND narrative_turn = :t AND turn_variant = :v",
        s=story_id,
        t=turn,
        v=variant,
    )


async def output_verdicts(s: Scratch, story_id: str) -> dict[tuple[str, str], str]:
    """The stored output verdicts of the story's images: ``{(image id, policy id): verdict}``."""
    found = await rows(
        s.db,
        "SELECT record_key, policy_id, verdict FROM guardrail_check_results "
        "WHERE story_id = :s AND record_key LIKE :pattern",
        s=story_id,
        pattern="image:%" + ":output",
    )
    return {
        (str(r.record_key).removeprefix("image:").removesuffix(":output"), str(r.policy_id)): str(
            r.verdict
        )
        for r in found
    }


__all__ = [
    "MINORS",
    "answer",
    "configure_checker",
    "image_rating",
    "output_verdicts",
    "stamp",
]
