"""The image prompt builder: a catalog image's prompt from perceivable inputs, returned without
generating (FR-IMG-2, FR-CAT-4, FR-RATE-4, FR-SCN-11; #259).

1. **The view** (``build_catalog_image_inputs``): Core reads the entry
   (``core.images.perceived``) and returns only what anyone browsing could see. That covers each
   depicted character's appearance (authored ages redacted), its **apparent age band** (#170's,
   computed in Core, so no true age crosses into the Orchestrator), its ``perceivable``
   references and add-ons, its seed and base appearance prompt, and the scenario's visual
   style and the rating's image guidance. ``PerceivedView`` has no age and no restricted field
   (a structural test walks it), and this module imports neither ``core.images.safety`` nor
   ``core.catalog.ages`` (the import-linter contract ``images-generation-path-has-no-ages``).
2. **The prompt** (``write_image_prompt``): one ``image_prompt`` call (#53's
   ``structured_call``, temperature 0, a 30 s cap) on ``image_prompt.j2``, whose fixed
   sections are the purpose, the subjects, the scene, the visual style and the rating's image
   guidance. Its answer is ``{prompt}``. Afterwards, deterministically, the style is appended
   when the prompt lacks it, and so is each subject's band. A failure, a timeout or an answer
   that never validates is 503 ``image_prompt_unavailable``, and no job is created.
3. **The parameters**, deterministic and never through the model: ``negative_prompt`` is the
   identity's terms, then the style's, split on commas and deduplicated ignoring case. ``seed``
   is the identity's. ``reference_image_ids`` are the identity's ``perceivable`` live references.
   ``size`` or ``aspect_ratio`` comes from the identity, else the configured adapter's
   ``default_size``, else its configured ``default_aspect_ratio``, else per purpose
   (``primary``/``gallery`` ``1:1``, ``cover`` ``16:9``). Exactly one of them is set.

``draft_base_appearance_prompt`` drafts a character's ``base_appearance_prompt`` from its
appearance alone, through the purpose's ``base_appearance`` variant. A drafted text that states
an age or uses a restricted-details term is regenerated once, then 503.

The safety check (#257's ``check_image_prompt``) runs on the built prompt in the Engine,
from a safety context it builds itself from the target. Logs carry counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.errors import EpistrelError, InvalidRequest
from epistrel.core.ids import Ulid
from epistrel.core.images.perceived import (
    BaseAppearanceInputs,
    CatalogPerceived,
    EntryKind,
    EntryTarget,
    read_perceived,
)
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    ImagePromptInputs,
    ImagePromptResult,
    ImagePromptSubject,
)
from epistrel.orchestrator.models.structured import Uncertain

if TYPE_CHECKING:
    from epistrel.orchestrator.models.access import ModelAccessLayer

logger = logging.getLogger(__name__)

PURPOSE: Final = "image_prompt"
BASE_APPEARANCE_VARIANT: Final = "base_appearance"
WRITE_DEADLINE_S: Final = 30.0
"""The cap on one prompt-writing call, its #53 retries included."""
SCENES: Final[Mapping[str, str]] = {
    "primary": "single-subject portrait, plain background",
    "gallery": "the character in a characteristic everyday setting",
    "cover": "establishing view of the setting; no people as subjects",
}
"""The scene each purpose depicts (a constant: nothing story-specific)."""
PURPOSE_TEXT: Final[Mapping[str, str]] = {
    "primary": "a catalog character's primary image",
    "gallery": "a catalog character's gallery image",
    "cover": "a scenario's cover image",
}
PURPOSE_RATIOS: Final[Mapping[str, str]] = {"primary": "1:1", "gallery": "1:1", "cover": "16:9"}
"""The aspect ratio when neither the identity nor the deployment sets a size or ratio."""

type Reason = Literal["model_unavailable", "invalid_output"]


class ImagePromptUnavailable(EpistrelError):
    """The ``image_prompt`` model could not write the prompt: it failed or timed out
    (``reason: model_unavailable``), or its answer never validated after #53's retries or
    stated what it may not (``invalid_output``). Nothing was created. ``details``: ``purpose``
    (``image_prompt``) and ``reason``."""

    code = "image_prompt_unavailable"
    http_status = 503

    def __init__(self, reason: Reason, message: str | None = None) -> None:
        super().__init__(
            message or "the image prompt could not be written; try again",
            {"purpose": PURPOSE, "reason": reason},
        )


# -- the request and the answer (``POST /image-prompts``; ``Engine.build_image_prompt``) -----


class ImagePromptTarget(BaseModel):
    """What the prompt is for: a catalog character or scenario, at its draft (``draft: true``,
    its current revision), a published ``version``, or with neither its latest published
    version. #263 adds ``kind: story``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["character", "scenario"]
    id: Ulid
    draft: bool = False
    version: int | None = Field(default=None, ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def _one_source(self) -> ImagePromptTarget:
        if self.draft and self.version is not None:
            raise ValueError("name the draft or a version, not both")
        return self


class ImagePromptRequest(BaseModel):
    """``POST /image-prompts``'s body: the target and the image's purpose (``primary`` or
    ``gallery`` for a character, ``cover`` for a scenario)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {"target": {"kind": "character", "id": "01J8ZQ6T7Y3K5V9W2X4N6P8R0S"},
                 "purpose": "primary"},
                {"target": {"kind": "scenario", "id": "01J8ZQ6T7Y3K5V9W2X4N6P8R0T", "draft": True},
                 "purpose": "cover"},
            ]
        },
    )  # fmt: skip

    target: ImagePromptTarget
    purpose: Literal["primary", "gallery", "cover"]


class ResolvedTarget(BaseModel):
    """The target as it was built: the version used, or the draft revision."""

    kind: Literal["character", "scenario"]
    id: str
    version: int | None
    draft_revision: int | None


class BuiltImagePrompt(BaseModel):
    """A built image prompt and its parameters, for a consumer that generates its own images
    (FR-IMG-2): the prompt, the negative prompt, the seed, the ``perceivable`` reference image
    ids, and exactly one of ``size`` and ``aspect_ratio``; and the resolved ``target``. No
    model, fingerprint or add-on."""

    prompt: str
    negative_prompt: str | None
    seed: int | None
    reference_image_ids: list[str]
    size: str | None
    aspect_ratio: str | None
    target: ResolvedTarget


class BaseAppearanceRequest(BaseModel):
    """``POST …/draft/visual-identity/draft-base-prompt``'s optional body: the draft revision
    the caller holds (409 ``draft_stale`` when the draft has moved on)."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"examples": [{"draft_revision": 3}, {}]}
    )

    draft_revision: int | None = Field(default=None, ge=1)


class DraftedBaseAppearance(BaseModel):
    """A drafted ``base_appearance_prompt``, not saved: the author saves it (and may edit it)
    through an ordinary draft save at ``draft_revision``."""

    base_appearance_prompt: str
    draft_revision: int


# -- the view -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerceivedSubject:
    """A depicted subject as the image's audience perceives it: how it looks, its apparent age
    band, its base appearance prompt, its ``perceivable`` reference images, its seed and its
    ``perceivable`` add-ons (``adapter_addons``, never sent to the model or returned). #263
    labels story subjects (``label``)."""

    description: str
    age_band: str
    base_appearance_prompt: str | None
    reference_image_ids: tuple[str, ...]
    seed: int | None
    adapter_addons: tuple[Mapping[str, Any], ...] = ()
    label: str | None = None


@dataclass(frozen=True, slots=True)
class PerceivedView:
    """Everything an image prompt may be built from (see the module docstring). The fields
    after ``purpose`` are parameters, not model input: they never reach ``image_prompt`` and
    the route never returns them as such (#260 builds ``ImageRequest`` from them)."""

    subjects: tuple[PerceivedSubject, ...]
    scene: str
    visual_style: str | None
    rating_image_guidance: str
    purpose: str
    identity_negative: str | None = None
    style_negative: str | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    preferred_image_model: str | None = None


@dataclass(frozen=True, slots=True)
class ImagePrompt:
    """A built image prompt and its parameters: what a consumer that generates its own images
    needs. Exactly one of ``size`` and ``aspect_ratio`` is set."""

    prompt: str
    negative_prompt: str | None
    seed: int | None
    reference_image_ids: tuple[str, ...]
    size: str | None
    aspect_ratio: str | None


def resolve_dimensions(
    purpose: str,
    size: str | None,
    aspect_ratio: str | None,
    deployment: ImageGenerationParams | None,
) -> tuple[str | None, str | None]:
    """``(size, aspect_ratio)``, exactly one set: the identity's (a size wins), else the
    configured adapter's ``default_size``, else its configured ``default_aspect_ratio``, else
    the purpose's ratio. ``deployment`` is ``None`` with no adapter configured. The built-in
    ``1:1`` of #254 does not count as configured."""
    if size is not None:
        return size, None
    if aspect_ratio is not None:
        return None, aspect_ratio
    if deployment is not None:
        if deployment.default_size is not None:
            return deployment.default_size, None
        if deployment.default_aspect_ratio is not None:
            return None, deployment.default_aspect_ratio
    return None, PURPOSE_RATIOS[purpose]


def _scene(perceived: CatalogPerceived) -> str:
    scene = SCENES[perceived.purpose]
    if perceived.public_description:
        return f"{scene}. {perceived.public_description}"
    return scene


def view_of(
    perceived: CatalogPerceived, deployment: ImageGenerationParams | None = None
) -> PerceivedView:
    """The ``PerceivedView`` of Core's perceivable inputs."""
    size, ratio = resolve_dimensions(
        perceived.purpose, perceived.size, perceived.aspect_ratio, deployment
    )
    return PerceivedView(
        subjects=tuple(
            PerceivedSubject(
                description=s.description,
                age_band=s.age_band,
                base_appearance_prompt=s.base_appearance_prompt,
                reference_image_ids=s.reference_image_ids,
                seed=s.seed,
                adapter_addons=s.adapter_addons,
            )
            for s in perceived.subjects
        ),
        scene=_scene(perceived),
        visual_style=perceived.visual_style,
        rating_image_guidance=perceived.rating_image_guidance,
        purpose=perceived.purpose,
        identity_negative=perceived.identity_negative,
        style_negative=perceived.style_negative,
        size=size,
        aspect_ratio=ratio,
        preferred_image_model=perceived.preferred_image_model,
    )


async def build_catalog_image_inputs(
    conn: AsyncConnection,
    entry_kind: EntryKind,
    entry_id: str,
    *,
    version: int | None = None,
    draft_revision: int | None = None,
    purpose: str,
    deployment: ImageGenerationParams | None = None,
) -> PerceivedView:
    """The catalog entry's ``PerceivedView`` (see the module docstring): exactly one of its
    published ``version`` and its draft at ``draft_revision``. ``deployment`` is the configured
    image adapter's params (``None`` when none is configured)."""
    if (version is None) == (draft_revision is None):
        raise InvalidRequest(
            "name exactly one of a version and a draft revision", {"field": "body.target"}
        )
    target = EntryTarget(entry_kind, entry_id, version, draft_revision)
    return view_of(await read_perceived(conn, target, purpose=purpose), deployment)


# -- the prompt -----------------------------------------------------------------------------


def merge_negative(*sources: str | None) -> str | None:
    """The sources' comma-separated terms, trimmed, without empties, deduplicated ignoring case
    (the first spelling kept), in order; ``None`` without any."""
    seen: dict[str, str] = {}
    for source in sources:
        for term in (source or "").split(","):
            term = term.strip()
            if term and term.casefold() not in seen:
                seen[term.casefold()] = term
    return ", ".join(seen.values()) or None


def prompt_inputs(view: PerceivedView) -> ImagePromptInputs:
    """``image_prompt``'s payload: the subjects (description, band, base prompt), the scene, the
    style, the guidance and the purpose. Never a parameter, a reference or an add-on."""
    return ImagePromptInputs(
        subjects=[
            ImagePromptSubject(
                description=s.description,
                age_band=s.age_band,
                base_appearance_prompt=s.base_appearance_prompt or "",
            )
            for s in view.subjects
        ],
        scene=view.scene,
        visual_style=view.visual_style or "",
        rating_image_guidance=view.rating_image_guidance,
        purpose=PURPOSE_TEXT.get(view.purpose, view.purpose),
    )


def completed(prompt: str, view: PerceivedView) -> str:
    """The model's prompt with what it must state appended when missing: the visual style
    (``", " + style``, compared ignoring case) and each subject's band (``", apparent age:
    <band>"``)."""
    found = prompt.strip()
    style = (view.visual_style or "").strip()
    if style and style.casefold() not in found.casefold():
        found = f"{found}, {style}"
    for subject in view.subjects:
        if subject.age_band and subject.age_band.casefold() not in found.casefold():
            found = f"{found}, apparent age: {subject.age_band}"
    return found


async def _call(
    models: ModelAccessLayer,
    inputs: ImagePromptInputs,
    *,
    variant: str | None = None,
    params: ModelParams | None = None,
) -> str:
    """One ``image_prompt`` call's validated prompt: ``ImagePromptUnavailable`` otherwise."""
    budget = TurnBudget(deadline_s=WRITE_DEADLINE_S)
    try:
        async with asyncio.timeout(WRITE_DEADLINE_S):
            result = await models.structured_call(
                PURPOSE,
                ImagePromptResult,
                inputs,
                params=params or ModelParams(temperature=0.0, timeout=WRITE_DEADLINE_S),
                budget=budget,
                variant=variant,
            )
    except ModelError, TimeoutError:
        raise ImagePromptUnavailable("model_unavailable") from None
    if isinstance(result, Uncertain):
        raise ImagePromptUnavailable("invalid_output")
    return result.prompt


async def write_image_prompt(models: ModelAccessLayer, view: PerceivedView) -> ImagePrompt:
    """The image prompt for ``view`` and its parameters (see the module docstring)."""
    written = await _call(models, prompt_inputs(view))
    prompt = completed(written, view)
    subject = view.subjects[0] if view.subjects else None
    references = tuple(r for s in view.subjects for r in s.reference_image_ids)
    logger.debug(
        "image prompt written: subjects=%d refs=%d chars=%d",
        len(view.subjects),
        len(references),
        len(prompt),
    )
    return ImagePrompt(
        prompt=prompt,
        negative_prompt=merge_negative(view.identity_negative, view.style_negative),
        seed=None if subject is None else subject.seed,
        reference_image_ids=tuple(dict.fromkeys(references)),
        size=view.size,
        aspect_ratio=view.aspect_ratio,
    )


async def draft_base_appearance_prompt(
    models: ModelAccessLayer, inputs: BaseAppearanceInputs
) -> str:
    """A drafted ``base_appearance_prompt`` from the draft's appearance alone (FR-CAT-4): no
    style, no guidance, no band. A text that states an age or uses a restricted-details term is
    regenerated once (at the purpose's own temperature), then ``ImagePromptUnavailable``
    (``invalid_output``)."""
    payload = ImagePromptInputs(subjects=[ImagePromptSubject(description=inputs.appearance)])
    for attempt in range(2):
        params = None if attempt == 0 else ModelParams(timeout=WRITE_DEADLINE_S)
        text = (
            await _call(models, payload, variant=BASE_APPEARANCE_VARIANT, params=params)
        ).strip()
        if not inputs.leaks(text):
            logger.debug(
                "base appearance prompt drafted: attempts=%d chars=%d", attempt + 1, len(text)
            )
            return text
    logger.info("base appearance prompt refused: attempts=2")
    raise ImagePromptUnavailable(
        "invalid_output", "the drafted base appearance prompt stated an age or a hidden detail"
    )


__all__ = [
    "BASE_APPEARANCE_VARIANT",
    "PURPOSE_RATIOS",
    "SCENES",
    "WRITE_DEADLINE_S",
    "BaseAppearanceRequest",
    "BuiltImagePrompt",
    "DraftedBaseAppearance",
    "ImagePrompt",
    "ImagePromptRequest",
    "ImagePromptTarget",
    "ImagePromptUnavailable",
    "PerceivedSubject",
    "PerceivedView",
    "ResolvedTarget",
    "build_catalog_image_inputs",
    "completed",
    "draft_base_appearance_prompt",
    "merge_negative",
    "prompt_inputs",
    "resolve_dimensions",
    "view_of",
    "write_image_prompt",
]
