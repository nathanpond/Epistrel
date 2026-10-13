"""Message feedback (FR-FB-1,2; #112): a principal's rating of one generated message.

- ``PUT /stories/{id}/messages/{message_id}/feedback``: rate the message (201 for the
  principal's first rating, 200 when it replaces one or repeats it).
- ``GET /stories/{id}/messages/{message_id}/feedback``: the requesting principal's own rating,
  with its history.
- ``DELETE /stories/{id}/messages/{message_id}/feedback``: retract it (204, also when there is
  none).

The writes are flagged ``mutating=False`` (``deps.route_flags``): a rating is idempotent by
itself and records no operation, so the routes take no ``Idempotency-Key`` and check no
expected revision; their methods still require ``X-Epistrel-Principal``, and so does the read,
whose answer is that principal's own rating. Every handler makes one ``Engine`` call.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi import status as http_status

from epistrel.api.deps import ContextDep, EngineDep, MessageId, StoryId, route_flags
from epistrel.core.feedback import Feedback, FeedbackRequest

FEEDBACK_TAG = "feedback"
PATH = "/stories/{story_id}/messages/{message_id}/feedback"

router = APIRouter(tags=[FEEDBACK_TAG])

NEVER_READ = (
    "Feedback is an audit record that changes nothing in the story: no view, prompt, retrieval "
    "or summary reads it, and no amount of it changes how the next message is generated."
)

RATE_DESCRIPTION = (
    "Rate a generated message as the requesting principal (FR-FB-1): `rating` 1 to 5, up to ten "
    "`tags` from the deployment's `feedback_tags` (trimmed and lower-cased; an unknown one is "
    "422 `unknown_feedback_tag` with `details.unknown_tags`), and optional `text`, trimmed, at "
    "most the deployment's `feedback_text_max_chars` characters (2000 by default; 422 "
    "`validation_error` beyond). `message_id` is the message's `message_rendered` event id; a "
    "message of another story, or the consumer's own narration, is 404. Each principal has one "
    "rating per message: the first is 201, with the context the message was produced under "
    "(its operation, configuration fingerprint, profile version, models per purpose, "
    "content-rating version and quality status, as recorded then); a different value replaces "
    "it (200) and the earlier value joins `history`; the same value again changes nothing "
    "(200). A regenerated message is a new message and starts unrated. Allowed on an archived "
    "story and while a turn runs. The write is idempotent by itself, so it takes no "
    "`Idempotency-Key`. " + NEVER_READ
)
READ_DESCRIPTION = (
    "The requesting principal's own rating of the message, with the `history` of its earlier "
    "values, newest first; `rating` is null when the principal has none. The read needs "
    "`X-Epistrel-Principal`, whose rating it answers (400 `principal_required`), and no "
    "viewpoint. " + NEVER_READ
)
RETRACT_DESCRIPTION = (
    "Retract the requesting principal's rating of the message: it is removed and its final value "
    "stays in the history. 204 also when there is no rating to retract; 404 for an unknown "
    "message. A rating after a retraction is a new one (201). The call takes no "
    "`Idempotency-Key`. " + NEVER_READ
)


@router.put(
    PATH,
    response_model=Feedback,
    status_code=http_status.HTTP_201_CREATED,
    responses={
        http_status.HTTP_200_OK: {
            "model": Feedback,
            "description": "The principal's rating existed: replaced, or the same value again.",
        }
    },
    operation_id="rate_message",
    summary="Rate a message",
    description=RATE_DESCRIPTION,
    **route_flags(mutating=False),
)
async def rate_message(
    story_id: StoryId,
    message_id: MessageId,
    body: FeedbackRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> Feedback:
    write = await engine.rate_message(ctx, story_id, message_id, body)
    if not write.created:
        response.status_code = http_status.HTTP_200_OK
    return write.feedback


@router.get(
    PATH,
    response_model=Feedback,
    operation_id="get_feedback",
    summary="Read your rating of a message",
    description=READ_DESCRIPTION,
)
async def get_feedback(
    story_id: StoryId, message_id: MessageId, ctx: ContextDep, engine: EngineDep
) -> Feedback:
    return await engine.get_feedback(ctx, story_id, message_id)


@router.delete(
    PATH,
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="retract_feedback",
    summary="Retract your rating of a message",
    description=RETRACT_DESCRIPTION,
    **route_flags(mutating=False),
)
async def retract_feedback(
    story_id: StoryId, message_id: MessageId, ctx: ContextDep, engine: EngineDep
) -> Response:
    await engine.retract_feedback(ctx, story_id, message_id)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
