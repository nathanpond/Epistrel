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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.errors import EpistrelError, InvalidRequest, ViewpointProtectedContent
from epistrel.core.ids import Ulid
from epistrel.core.images.perceived import (
    BaseAppearanceInputs,
    CatalogPerceived,
    EntryKind,
    EntryTarget,
    read_perceived,
)
from epistrel.core.images.story_images import BeatPosition, Placement, SlotPosition
from epistrel.core.images.story_perceived import (
    StoryPerceived,
    ViewpointMatcher,
    story_perceived_inputs,
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
    "story": "a story image of a moment, as its viewer perceived it",
}
PURPOSE_RATIOS: Final[Mapping[str, str]] = {"primary": "1:1", "gallery": "1:1", "cover": "16:9"}
"""The aspect ratio when neither the identity nor the deployment sets a size or ratio."""
STORY_PURPOSE: Final = "story"
STORY_BEATS_CHARS: Final = 6_000
"""The most perceived-beat text one story image prompt input carries (#263)."""
FRAMING: Final[Mapping[str, str]] = {
    "third_person": "third-person view of the scene",
    "establishing": "wide establishing shot of the location",
    "over_the_shoulder": "over the shoulder of {}",
    "first_person": "first-person view through the eyes of {}, who is not shown",
}
"""A story image's framing text by camera kind (FR-IMG-5): composition only."""
REWRITE_INSTRUCTION: Final = (
    "Describe only visible appearance and actions; use no proper names and quote no speech."
)
"""The one rewrite after a protected hit (#263): fixed, never the hit term."""

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
    version. A story image's target is ``StoryImagePromptTarget`` (#263)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["character", "scenario"]
    id: Ulid
    draft: StrictBool = False
    version: StrictInt | None = Field(default=None, ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def _one_source(self) -> ImagePromptTarget:
        if self.draft and self.version is not None:
            raise ValueError("name the draft or a version, not both")
        return self


class StoryImagePromptTarget(BaseModel):
    """A story image's target (``kind: story``; #263): the story, the narrative ``position``
    (``{turn, beat}`` after a beat of an active turn, the player input being beat 1, or
    ``{slot_after_turn}`` after the story's last completed turn), the ``audience_viewpoint``
    whose perception the prompt is built from (``character:<id>`` or ``omniscient``) and the
    optional ``camera_pov`` (composition only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["story"]
    story_id: Ulid
    position: BeatPosition | SlotPosition
    audience_viewpoint: str = Field(min_length=1, max_length=64)
    camera_pov: dict[str, Any] | None = None


class ImagePromptRequest(BaseModel):
    """``POST /image-prompts``'s body: the target and, for a catalog target, the image's
    purpose (``primary`` or ``gallery`` for a character, ``cover`` for a scenario). A story
    target takes no purpose."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {"target": {"kind": "character", "id": "01J8ZQ6T7Y3K5V9W2X4N6P8R0S"},
                 "purpose": "primary"},
                {"target": {"kind": "scenario", "id": "01J8ZQ6T7Y3K5V9W2X4N6P8R0T", "draft": True},
                 "purpose": "cover"},
                {"target": {"kind": "story", "story_id": "01J8ZQ6T7Y3K5V9W2X4N6P8R0V",
                            "position": {"turn": 3, "beat": 2},
                            "audience_viewpoint": "character:01J8ZQ6T7Y3K5V9W2X4N6P8R0W"}},
            ]
        },
    )  # fmt: skip

    target: Annotated[ImagePromptTarget | StoryImagePromptTarget, Field(discriminator="kind")]
    purpose: Literal["primary", "gallery", "cover"] | None = None

    @model_validator(mode="after")
    def _purpose(self) -> ImagePromptRequest:
        if isinstance(self.target, StoryImagePromptTarget):
            if self.purpose is not None:
                raise ValueError("a story image's prompt takes no purpose")
        elif self.purpose is None:
            raise ValueError("a catalog image's prompt names its purpose")
        return self


class ResolvedTarget(BaseModel):
    """The target as it was built: the version used, or the draft revision."""

    kind: Literal["character", "scenario"]
    id: str
    version: int | None
    draft_revision: int | None


class StoryDepicted(BaseModel):
    """A subject a built story image prompt depicts (FR-IMG-9): a story character, by id."""

    kind: Literal["character"] = "character"
    id: str


class BuiltImagePrompt(BaseModel):
    """A built image prompt and its parameters, for a consumer that generates its own images
    (FR-IMG-2): the prompt, the negative prompt, the seed, the ``perceivable`` reference image
    ids, and exactly one of ``size`` and ``aspect_ratio`` (a story image's: neither, the
    deployment's defaults apply); and the resolved ``target``. A story image's adds the
    ``depicted_subjects`` and its ``viewpoint_basis`` (``filtered_inputs``; #263). No model,
    fingerprint or add-on."""

    prompt: str
    negative_prompt: str | None
    seed: int | None
    reference_image_ids: list[str]
    size: str | None
    aspect_ratio: str | None
    target: ResolvedTarget | StoryImagePromptTarget
    depicted_subjects: list[StoryDepicted] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    viewpoint_basis: Literal["filtered_inputs"] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )


class BaseAppearanceRequest(BaseModel):
    """``POST …/draft/visual-identity/draft-base-prompt``'s optional body: the draft revision
    the caller holds (409 ``draft_stale`` when the draft has moved on)."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"examples": [{"draft_revision": 3}, {}]}
    )

    draft_revision: StrictInt | None = Field(default=None, ge=1)


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
    negative_prompt: str | None = None
    """A story subject's ``perceivable`` negative prompt (#263; a catalog view's is the
    identity's, ``PerceivedView.identity_negative``)."""
    subject_ids: tuple[str, ...] = ()
    """A story subject's character id (#263): the depicted subject the safety context starts
    from and the asset records (FR-IMG-9), never model input."""


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
    beats: tuple[str, ...] = ()
    """A story image's perceived beats (#263), in order."""
    location: str | None = None
    """A story image's location as its viewpoint perceived it (#263)."""
    camera_framing: str | None = None
    """A story image's framing text (#263): composition only."""
    camera_subject: str | None = None
    """The character id of a character camera's subject (#263; parameters only: its seed and
    references come first), never model input."""


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
    references_dropped: tuple[str, ...] = ()
    """A story image's perceivable references past ``MAX_REFERENCE_IMAGES`` (#263), recorded."""


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


def prompt_inputs(view: PerceivedView, *, instruction: str = "") -> ImagePromptInputs:
    """``image_prompt``'s payload: the subjects (label, description, band, base prompt), the
    scene, the style, the guidance and the purpose, and a story image's location, perceived beats
    (the nearest ``STORY_BEATS_CHARS`` kept) and framing. Never a parameter, a reference, an
    add-on or an id."""
    return ImagePromptInputs(
        subjects=[
            ImagePromptSubject(
                description=s.description,
                age_band=s.age_band,
                base_appearance_prompt=s.base_appearance_prompt or "",
                label=s.label or "",
            )
            for s in view.subjects
        ],
        scene=view.scene,
        visual_style=view.visual_style or "",
        rating_image_guidance=view.rating_image_guidance,
        purpose=PURPOSE_TEXT.get(view.purpose, view.purpose),
        beats=list(capped_beats(view.beats)),
        location=view.location or "",
        camera=view.camera_framing or "",
        instruction=instruction,
    )


def capped_beats(beats: Sequence[str], limit: int | None = None) -> tuple[str, ...]:
    """``beats`` within ``limit`` characters (``STORY_BEATS_CHARS``), keeping the beats nearest
    the position (the last ones) and dropping the oldest first."""
    cap = STORY_BEATS_CHARS if limit is None else limit
    kept: list[str] = []
    used = 0
    for beat in reversed(beats):
        if used + len(beat) > cap:
            break
        kept.append(beat)
        used += len(beat)
    return tuple(reversed(kept))


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


# -- story images (FR-IMG-8, FR-IMG-2, FR-IMG-5; #263) --------------------------------------


def framing(camera: Mapping[str, Any], label: str | None) -> str:
    """The framing text of a camera POV (``FRAMING``): composition only."""
    kind = str(camera.get("kind") or "third_person")
    text = FRAMING.get(kind, FRAMING["third_person"])
    return text.format(label or "the viewer") if "{}" in text else text


def story_view(perceived: StoryPerceived) -> PerceivedView:
    """The ``PerceivedView`` of a story image's perceived inputs (Core's
    ``core.images.story_perceived``): the cast as subjects (label, perceived appearance, band,
    and a recognised character's ``perceivable`` identity), the scene's beats, location and
    framing, the style and the guidance. Size and aspect ratio stay the deployment's."""
    return PerceivedView(
        subjects=tuple(
            PerceivedSubject(
                description=c.appearance,
                age_band=c.age_band,
                base_appearance_prompt=c.base_appearance_prompt,
                reference_image_ids=c.reference_image_ids,
                seed=c.seed,
                adapter_addons=c.adapter_addons,
                label=c.label,
                negative_prompt=c.negative_prompt,
                subject_ids=c.subject_ids,
            )
            for c in perceived.cast
        ),
        scene="",
        visual_style=perceived.visual_style,
        rating_image_guidance=perceived.rating_image_guidance,
        purpose=STORY_PURPOSE,
        identity_negative=None,
        style_negative=perceived.style_negative,
        beats=perceived.beats,
        location=perceived.location,
        camera_framing=framing(perceived.camera, perceived.camera_label),
        camera_subject=(
            None
            if perceived.camera.get("character_id") is None
            else str(perceived.camera["character_id"])
        ),
    )


async def build_story_image_inputs(conn: AsyncConnection, placement: Placement) -> PerceivedView:
    """A story image's ``PerceivedView`` (FR-IMG-8): only what its audience viewpoint perceived
    at the placement's position (``core.images.story_perceived``)."""
    return story_view(await story_perceived_inputs(conn, placement))


def with_guidance(prompt: str, view: PerceivedView) -> str:
    """A story image prompt with the rating's image guidance appended as its own section when
    it lacks it (FR-RATE-4)."""
    guidance = view.rating_image_guidance.strip()
    if guidance and guidance.casefold() not in prompt.casefold():
        return f"{prompt}\n\nImage guidance: {guidance}"
    return prompt


def consumer_prompt(text: str, visual_style: str | None, guidance: str) -> str:
    """A consumer's story image prompt as it is sent (FR-IMG-2, FR-RATE-4): the consumer's text
    verbatim, then the scenario's visual style and the rating's image guidance, each a section
    of its own."""
    found = text.strip()
    if visual_style:
        found = f"{found}\n\nVisual style: {visual_style}"
    if guidance.strip():
        found = f"{found}\n\nImage guidance: {guidance.strip()}"
    return found


def story_parameters(
    view: PerceivedView,
) -> tuple[str | None, int | None, tuple[str, ...], tuple[str, ...]]:
    """A story image's deterministic parameters: ``(negative_prompt, seed, references,
    dropped)``. The negative prompt merges the recognised subjects' and the style's; the seed is
    the camera subject's, else the one recognised subject's that has one, else none; the
    references are the recognised subjects' (the camera subject's first, then the cast's order),
    at most ``MAX_REFERENCE_IMAGES``, the rest recorded as dropped."""
    from epistrel.orchestrator.images.port import MAX_REFERENCE_IMAGES

    def subject_of(character_id: str | None) -> PerceivedSubject | None:
        if character_id is None:
            return None
        return next((s for s in view.subjects if character_id in s.subject_ids[:1]), None)

    camera = subject_of(view.camera_subject)
    ordered = [*([camera] if camera is not None else []), *(s for s in view.subjects)]
    references = tuple(dict.fromkeys(r for s in ordered for r in s.reference_image_ids))
    seeded = [s for s in view.subjects if s.seed is not None]
    seed = (
        camera.seed
        if camera is not None and camera.seed is not None
        else seeded[0].seed
        if len(seeded) == 1
        else None
    )
    negative = merge_negative(*(s.negative_prompt for s in view.subjects), view.style_negative)
    return (
        negative,
        seed,
        references[:MAX_REFERENCE_IMAGES],
        references[MAX_REFERENCE_IMAGES:],
    )


async def write_story_image_prompt(
    models: ModelAccessLayer, view: PerceivedView, matcher: ViewpointMatcher
) -> ImagePrompt:
    """A story image's prompt and parameters (FR-IMG-8): one ``image_prompt`` call on the
    filtered inputs, the style, the bands and the guidance appended when missing, then the
    viewpoint's protected match. A hit gets one rewrite (the same inputs with
    ``REWRITE_INSTRUCTION``), then 422 ``viewpoint_protected_content`` (``field: prompt``). The
    negative prompt is matched too. A model failure is 503 ``image_prompt_unavailable``."""
    negative, seed, references, dropped = story_parameters(view)
    instruction = ""
    for attempt in range(2):
        written = await _call(models, prompt_inputs(view, instruction=instruction))
        prompt = with_guidance(completed(written, view), view)
        hit = matcher.first_hit({"prompt": prompt, "negative_prompt": negative})
        if hit is None:
            logger.debug(
                "story image prompt written: subjects=%d refs=%d chars=%d attempts=%d",
                len(view.subjects),
                len(references),
                len(prompt),
                attempt + 1,
            )
            return ImagePrompt(
                prompt=prompt,
                negative_prompt=negative,
                seed=seed,
                reference_image_ids=references,
                size=None,
                aspect_ratio=None,
                references_dropped=dropped,
            )
        logger.info("story image prompt hit the protected match: field=%s", hit)
        if hit != "prompt":
            break
        instruction = REWRITE_INSTRUCTION
    raise ViewpointProtectedContent(
        "the image prompt holds content its audience viewpoint does not hold",
        {"field": "prompt" if hit == "prompt" else hit},
    )


__all__ = [
    "BASE_APPEARANCE_VARIANT",
    "FRAMING",
    "PURPOSE_RATIOS",
    "REWRITE_INSTRUCTION",
    "SCENES",
    "STORY_BEATS_CHARS",
    "STORY_PURPOSE",
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
    "StoryDepicted",
    "StoryImagePromptTarget",
    "build_catalog_image_inputs",
    "build_story_image_inputs",
    "capped_beats",
    "completed",
    "consumer_prompt",
    "draft_base_appearance_prompt",
    "framing",
    "merge_negative",
    "prompt_inputs",
    "resolve_dimensions",
    "story_parameters",
    "story_view",
    "view_of",
    "with_guidance",
    "write_image_prompt",
    "write_story_image_prompt",
]
