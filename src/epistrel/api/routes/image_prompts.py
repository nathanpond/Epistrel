"""``POST /image-prompts``: a catalog image's prompt and parameters, built without generating
(FR-IMG-2, FR-CAT-4, FR-RATE-4; #259).

For a consumer that makes its own images. A thin handler over ``Engine.build_image_prompt``
(``orchestrator.images.prompt_builder``, where the body and answer models live, since the Engine
takes the route's shape). It needs only the service credential and no image adapter. It is
non-mutating (``route_flags(mutating=False)``): it creates no job, reservation or cost row, and
stores nothing under an ``Idempotency-Key``. #263 adds the story variant (``target.kind =
story``).
"""

from __future__ import annotations

from fastapi import APIRouter

from epistrel.api.deps import ContextDep, EngineDep, route_flags
from epistrel.orchestrator.images.prompt_builder import BuiltImagePrompt, ImagePromptRequest

router = APIRouter(prefix="/image-prompts", tags=["images"])

DESCRIPTION = (
    "Builds the prompt and parameters Epistrel would generate a catalog image from, and "
    "returns them without generating (FR-IMG-2): for a consumer that makes its own images. "
    "The `target` is a catalog `character` (purpose `primary` or `gallery`) or `scenario` "
    "(purpose `cover`): its draft (`draft: true`, at its current revision), a published "
    "`version`, or with neither its latest published version. Archived and unlisted entries "
    "work.\n\n"
    "A catalog image is public-card content, so its prompt uses only what anyone browsing "
    "could see: the character's appearance (with authored exact ages left out), its "
    '**apparent age band** ("mid thirties"), never its age, its base appearance prompt, and '
    "its `perceivable` reference images. A scenario cover uses the public description only "
    "and depicts no slot characters. `restricted` reference images and add-ons and the "
    "restricted-details prompt never reach it. The visual style (a character's companion "
    "scenario's, or the scenario's own) and the entry's rating image guidance shape every "
    "image prompt and no text prompt. The `image_prompt` model writes the prompt. The "
    "`negative_prompt` (the identity's terms, then the style's, deduplicated), the `seed`, the "
    "`reference_image_ids` and the size never pass through the model. Exactly one of `size` "
    "and `aspect_ratio` is set: the identity's, else the configured image adapter's default "
    "size, else its default aspect ratio, else `1:1` (`primary`, `gallery`) or `16:9` "
    "(`cover`). `target` answers the version or draft revision used.\n\n"
    "Then the prompt is checked against every image-prompt policy, judged by true ages "
    "(#257). A violation is 422 `guardrail_violation` with `violations: [{policy_id, name, "
    "message}]`, and the refused prompt is never returned. It needs no image adapter or image "
    "checker, creates no job, reservation or cost row, and stores nothing under an "
    "`Idempotency-Key`. Two calls may answer different prompts: a generation job builds its "
    "prompt once and stores it.\n\n"
    "404 for an unknown entry or version. 422 `not_published` (never published, and neither "
    "`draft` nor `version` given), and 422 `validation_error` for `draft` with `version`, a "
    "purpose the kind's images cannot have (`body.purpose`), or a character without a true age "
    "(`definition.true_age`), a content rating (`metadata.content_rating_id`) or an appearance "
    "(`definition.appearance`). 503 `image_prompt_unavailable` (`details: {purpose: "
    "image_prompt, reason: model_unavailable | invalid_output}`) when the prompt cannot be "
    "written, 503 `model_unavailable` when the prompt check cannot reach its model, and 503 "
    "`erasure_pending` while a pending catalog-scope erasure reaches the entry."
)


@router.post(
    "",
    response_model=BuiltImagePrompt,
    operation_id="build_image_prompt",
    summary="Build an image prompt without generating",
    description=DESCRIPTION,
    **route_flags(mutating=False),
)
async def build_image_prompt(
    body: ImagePromptRequest, ctx: ContextDep, engine: EngineDep
) -> BuiltImagePrompt:
    return await engine.build_image_prompt(ctx, body)
