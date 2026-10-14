"""A catalog character's document and its projections (FR-CAT-2,4, FR-CAT-12, FR-RATE-3,
FR-GUARD-7, FR-SCN-10; docs/02 §4.10; #103, #114, #115).

**The document.** A catalog character's draft and each of its published versions are one JSON
document that ``CharacterDraft`` validates, in four groups:

- ``public_card``: what the ``card`` projection shows for search, browsing and story setup: the
  ``name``, a ``public_description`` (free text, never seeded into a story) and the ``images``,
  an optional ``primary`` image and a ``gallery``, as media asset ids (FR-CAT-4);
- ``definition``: the private character definition (FR-CAT-2), seeded into every story the
  character joins and otherwise returned only by the ``full`` projection: the true age
  (FR-CAT-12), appearance, personality and temperament, likes and dislikes, five behavioural
  dispositions (each a closed five-step ``setting`` plus free-text ``nuance``; the steps are the
  enums below), values and motivations, the interaction style, and optionally background flavor,
  a ``deflection_style`` (how the character refuses or changes the subject; FR-GUARD-7) and a
  ``safe_fallback_text`` (the line it says when every regenerated reply fails its guardrails,
  #68);
- ``metadata``: ``aliases``, ``labels`` that describe the character (stored trimmed and
  lower-cased) and the ``content_rating_id`` (FR-RATE-3), used for search and filtering and
  never seeded;
- ``visual_identity`` (optional; FR-CAT-4): the stored settings that keep new pictures of the
  character consistent: the preferred image model, a base appearance prompt (always
  ``perceivable``), a negative prompt, a seed, reference images, adapter-specific parameters
  kept opaque, and an optional restricted-details prompt. Every reference image declares its
  ``audience``, ``perceivable`` (only what anyone who meets the character can see) or
  ``restricted`` (hidden details, a true form), with no default.

A character carries **no world truths**: the document has no field for facts. Truths about a
character's world (a secret, a sister, a past career) belong to a scenario, normally the
character's companion scenario (FR-CAT-5, #116); a world fact an author writes into a character
field is seeded at the lowest precedence (FR-SCN-7). Nothing on the public card and no label is
ever seeded into a story (#122 renders the definition, never the card).

**Saving.** A draft may hold any subset of the fields, each within its limits (a name alone is a
valid draft). A save also refuses an alias equal to the name or to another alias, ignoring case
(``character_rules``), and a reference image without an audience (``missing_audiences``): 422
``validation_error`` at the field. **Publishing** needs more: ``PUBLISH_REQUIRED`` (and, when a
visual identity is present, ``IDENTITY_REQUIRED``) must be present, and a string among them not
blank; the true age (``true_age_missing``), the content rating (``content_rating_missing``), every
reference image's audience (``reference_image_missing_audience``) and, when the deployment's
``require_primary_image`` is on, the primary image (``primary_image_required``) have codes of
their own (``core.catalog.checks.character_content``). Lists may be empty, and images are
optional by default.

**Projections (FR-SCN-10).** ``CharacterCard`` holds only the public card and its metadata: the
images as ``{media_id, alt_text}``, the labels and the rating, never a definition field.
``CharacterFull`` adds the entry's envelope, the aliases, every definition field and the visual
identity; its images and reference images inline each asset's live media row (alt text,
caption, MIME type, dimensions and generation metadata, exactly as the upload stored them, #105).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)

from epistrel.core.catalog.scenario_models import ALIASES_MAX, LABELS_MAX, Alias, Label
from epistrel.core.catalog.service import CardRating, PublishRequest, Revision
from epistrel.core.enums import EntryStatus
from epistrel.core.ids import Ulid
from epistrel.core.json_numbers import require_finite
from epistrel.core.ownership import Owner
from epistrel.core.tables.characters import DEFLECTION_STYLE_MAX, TRUE_AGE_MAX, TRUE_AGE_MIN

NAME_MAX = 120
PUBLIC_DESCRIPTION_MAX = 4_000
PROSE_MAX = 4_000
"""The definition's prose: appearance, personality, values and motivations, background."""
PERSONALITY_MAX = PROSE_MAX
BACKGROUND_FLAVOR_MAX = PROSE_MAX
STYLE_MAX = 2_000
"""The interaction style's speech style and vocabulary."""
NUANCE_MAX = 500
SAFE_FALLBACK_MAX = 500
ITEM_MAX = 200
"""One like or dislike."""
ITEMS_MAX = 30
"""Likes, and dislikes, each."""
MANNERISM_MAX = 500
SAMPLE_LINE_MAX = 1_000
LINES_MAX = 20
"""Mannerisms, and sample dialogue lines, each."""
GALLERY_MAX = 20
REFERENCE_IMAGES_MAX = 10
PROMPT_MAX = 2_000
"""The base appearance, negative and restricted-details prompts."""
MODEL_NAME_MAX = 200
SEED_MAX = 2**32 - 1
ADAPTER_PARAMS_MAX_BYTES = 8 * 1024


def _non_blank(value: str) -> str:
    """``value`` without surrounding whitespace; a blank one is refused."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _distinct_ids(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("lists a media asset more than once")
    return values


def _params_bounded(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is not None:
        size = len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
        if size > ADAPTER_PARAMS_MAX_BYTES:
            raise ValueError(f"is {size} bytes as JSON; the limit is {ADAPTER_PARAMS_MAX_BYTES}")
    return value


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
"""The character's name: trimmed, never blank."""
Prose = Annotated[str, Field(max_length=PROSE_MAX)]
"""Definition prose, stored as written."""
Style = Annotated[str, Field(max_length=STYLE_MAX)]
Nuance = Annotated[str, Field(max_length=NUANCE_MAX)]
Prompt = Annotated[str, Field(max_length=PROMPT_MAX)]
Item = Annotated[str, Field(min_length=1, max_length=ITEM_MAX), AfterValidator(_non_blank)]
"""A like or a dislike: trimmed, never blank."""
Mannerism = Annotated[
    str, Field(min_length=1, max_length=MANNERISM_MAX), AfterValidator(_non_blank)
]
SampleLine = Annotated[
    str, Field(min_length=1, max_length=SAMPLE_LINE_MAX), AfterValidator(_non_blank)
]
TrueAge = Annotated[StrictInt, Field(ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)]
Seed = Annotated[StrictInt, Field(ge=0, le=SEED_MAX)]
AdapterParams = Annotated[
    dict[str, Any],
    AfterValidator(require_finite),  # a JSON dump would store a non-finite number as null
    AfterValidator(_params_bounded),
]


class CharacterModel(BaseModel):
    """Base of every model of the character document: unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")


# -- the dispositions (FR-CAT-2): closed five-step settings ------------------------------------


class ThreatResponse(StrEnum):
    """How the character responds to being threatened or challenged."""

    flees = "flees"
    yields = "yields"
    deflects = "deflects"
    stands = "stands"
    attacks = "attacks"


class Dominance(StrEnum):
    """How dominant or submissive the character is."""

    very_submissive = "very_submissive"
    submissive = "submissive"
    balanced = "balanced"
    dominant = "dominant"
    very_dominant = "very_dominant"


class Honesty(StrEnum):
    """How honest or deceitful the character is."""

    very_deceitful = "very_deceitful"
    deceitful = "deceitful"
    balanced = "balanced"
    honest = "honest"
    very_honest = "very_honest"


class Warmth(StrEnum):
    """How warm or cold the character is."""

    cold = "cold"
    cool = "cool"
    neutral = "neutral"
    warm = "warm"
    very_warm = "very_warm"


class Caution(StrEnum):
    """How cautious or reckless the character is."""

    reckless = "reckless"
    bold = "bold"
    balanced = "balanced"
    careful = "careful"
    very_cautious = "very_cautious"


DISPOSITION_AXES: Final = ("threat_response", "dominance", "honesty", "warmth", "caution")
"""The five disposition axes, in document order."""

NUANCE_DESCRIPTION = "Free text that qualifies the setting (private; at most 500 characters)."


class ThreatResponseDisposition(CharacterModel):
    """How the character responds to being threatened or challenged: a setting and its
    nuance."""

    setting: ThreatResponse
    nuance: Nuance | None = Field(default=None, description=NUANCE_DESCRIPTION)


class DominanceDisposition(CharacterModel):
    """How dominant or submissive the character is: a setting and its nuance."""

    setting: Dominance
    nuance: Nuance | None = Field(default=None, description=NUANCE_DESCRIPTION)


class HonestyDisposition(CharacterModel):
    """How honest or deceitful the character is: a setting and its nuance."""

    setting: Honesty
    nuance: Nuance | None = Field(default=None, description=NUANCE_DESCRIPTION)


class WarmthDisposition(CharacterModel):
    """How warm or cold the character is: a setting and its nuance."""

    setting: Warmth
    nuance: Nuance | None = Field(default=None, description=NUANCE_DESCRIPTION)


class CautionDisposition(CharacterModel):
    """How cautious or reckless the character is: a setting and its nuance."""

    setting: Caution
    nuance: Nuance | None = Field(default=None, description=NUANCE_DESCRIPTION)


class Dispositions(CharacterModel):
    """The behavioural dispositions (FR-CAT-2): five axes, each a closed five-step ``setting``
    plus free-text ``nuance``. Each may be missing from a draft; a publish needs all five."""

    threat_response: ThreatResponseDisposition | None = None
    dominance: DominanceDisposition | None = None
    honesty: HonestyDisposition | None = None
    warmth: WarmthDisposition | None = None
    caution: CautionDisposition | None = None


# -- the definition ----------------------------------------------------------------------------


class LikesDislikes(CharacterModel):
    """What the character likes and dislikes, in general terms (each list may be empty)."""

    likes: list[Item] = Field(default_factory=list, max_length=ITEMS_MAX)
    dislikes: list[Item] = Field(default_factory=list, max_length=ITEMS_MAX)


class InteractionStyle(CharacterModel):
    """How the character talks: its speech style and vocabulary (required to publish), its
    mannerisms and sample lines of dialogue (each list may be empty)."""

    speech_style: Style | None = None
    mannerisms: list[Mannerism] = Field(default_factory=list, max_length=LINES_MAX)
    vocabulary: Style | None = None
    sample_dialogue: list[SampleLine] = Field(default_factory=list, max_length=LINES_MAX)


class CharacterDefinition(CharacterModel):
    """The private character definition (FR-CAT-2): who the character is. It is seeded into
    every story the character joins and is otherwise returned only by the ``full``
    projection. ``true_age`` (FR-CAT-12) is the character's objective age, 0 to 150, never on
    the card. ``deflection_style`` is how the character refuses or changes the subject: a
    story's Lite prompt carries it with the character's notes, so a reply regenerated after a
    guardrail violation stays in character (FR-GUARD-7). ``safe_fallback_text`` is the line the
    character says when every regenerated reply fails its guardrails (#68). A definition
    promoted from a story lands in ``background_flavor`` (FR-CAT-8)."""

    true_age: TrueAge | None = Field(
        default=None,
        description="The character's true age (FR-CAT-12): 0 to 150. Never on the card.",
    )
    appearance: Prose | None = Field(default=None, description="What the character looks like.")
    personality: Prose | None = Field(
        default=None, description="Personality and temperament (private)."
    )
    likes_dislikes: LikesDislikes | None = None
    dispositions: Dispositions | None = None
    values_motivations: Prose | None = Field(
        default=None, description="Values and motivations, in general terms."
    )
    interaction_style: InteractionStyle | None = None
    background_flavor: Prose | None = Field(
        default=None, description="Optional background, as free text (private)."
    )
    deflection_style: str | None = Field(
        default=None,
        max_length=DEFLECTION_STYLE_MAX,
        description="How the character refuses or changes the subject, in its own voice "
        "(FR-GUARD-7; optional, private).",
    )
    safe_fallback_text: str | None = Field(
        default=None,
        max_length=SAFE_FALLBACK_MAX,
        description="What the character says when every regenerated reply fails its guardrails "
        "(private).",
    )


# -- the public card and the metadata ----------------------------------------------------------


class CharacterImages(CharacterModel):
    """The public card's images (FR-CAT-4): an optional ``primary`` image and a ``gallery`` (up
    to 20, distinct; the primary may also be in it), as media asset ids. The assets are
    referenced, never copied: each must be a ``live`` image when it is added (422
    ``media_retired``), and every image is optional unless the deployment's
    ``require_primary_image`` is on (then a publish needs the primary)."""

    primary: Ulid | None = Field(default=None, description="The primary image's media id.")
    gallery: list[Ulid] = Field(
        default_factory=list, max_length=GALLERY_MAX, description="More images, in order."
    )

    @field_validator("gallery")
    @classmethod
    def _distinct_gallery(cls, value: list[str]) -> list[str]:
        return _distinct_ids(value)


class CharacterPublicCard(CharacterModel):
    """What a player browsing the catalog may see (FR-CAT-2): the name, a public description
    (never seeded into a story) and the images."""

    name: Name | None = Field(default=None, description="The character's name (public).")
    public_description: str | None = Field(
        default=None,
        max_length=PUBLIC_DESCRIPTION_MAX,
        description="Free text for the public card; never seeded into a story.",
    )
    images: CharacterImages = Field(default_factory=CharacterImages)


class CharacterMetadata(CharacterModel):
    """Search and filtering metadata, never seeded (FR-CAT-2): ``aliases`` (up to 20, each
    trimmed, up to 60 characters, never the name or another alias, ignoring case), ``labels``
    that describe the character (up to 30 of ``A-Z a-z 0-9 _ -`` and spaces, stored trimmed and
    lower-cased, distinct) and the ``content_rating_id`` (FR-RATE-3), which must be assignable
    when a save sets it and is required to publish. The labels and the rating are on the card;
    the aliases are searched (#110) and shown in ``full``."""

    aliases: list[Alias] = Field(default_factory=list, max_length=ALIASES_MAX)
    labels: list[Label] = Field(default_factory=list, max_length=LABELS_MAX)
    content_rating_id: Ulid | None = Field(default=None, description="The content rating's id.")

    @field_validator("labels")
    @classmethod
    def _distinct_labels(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("lists a label more than once")
        return value


# -- the visual identity (FR-CAT-4) ------------------------------------------------------------


class Audience(StrEnum):
    """Who may see a reference image's subject: ``perceivable`` (only what anyone who meets the
    character can see) or ``restricted`` (hidden details, a true form, the real face behind a
    disguise). There is no default."""

    perceivable = "perceivable"
    restricted = "restricted"


def _audience_required(schema: dict[str, Any]) -> None:
    """Document ``audience`` as the required, non-null value it is: a save refuses a reference
    image without one (``missing_audiences``), while the stored document a publish validates is
    read as it is, so the report can name a missing one (``reference_image_missing_audience``)."""
    prop = schema["properties"]["audience"]
    options = [option for option in prop.pop("anyOf", []) if option.get("type") != "null"]
    if options:
        prop.update(options[0])
    prop.pop("default", None)
    required = schema.setdefault("required", [])
    if "audience" not in required:
        required.append("audience")


class ReferenceImage(CharacterModel):
    """A reference image of the visual identity: a media asset (a ``live`` image when it is
    added) and its required ``audience``."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_audience_required)

    media_id: Ulid
    audience: Audience | None = Field(
        default=None,
        description="`perceivable` or `restricted`; required, with no default (422 when it is "
        "missing).",
    )


class VisualIdentity(CharacterModel):
    """A character's stored image settings, versioned with the character (FR-CAT-4): the
    preferred image model (free text), a base appearance prompt (always ``perceivable``), a
    negative prompt, a seed (0 to 2^32 - 1), up to 10 reference images (each with its
    ``audience``), adapter-specific parameters kept opaque (a JSON object of at most 8 KiB, every
    number finite) and an optional prompt for restricted details. Optional as a whole; when it
    is present, a publish needs every field but ``reference_images``, ``adapter_params`` and
    ``restricted_details_prompt``."""

    preferred_image_model: str | None = Field(default=None, max_length=MODEL_NAME_MAX)
    base_appearance_prompt: Prompt | None = Field(
        default=None, description="What anyone who meets the character sees (`perceivable`)."
    )
    negative_prompt: Prompt | None = None
    seed: Seed | None = None
    reference_images: list[ReferenceImage] = Field(
        default_factory=list, max_length=REFERENCE_IMAGES_MAX
    )
    adapter_params: AdapterParams | None = Field(
        default=None, description="Adapter-specific parameters, stored and returned as sent."
    )
    restricted_details_prompt: Prompt | None = Field(
        default=None, description="Hidden details (`restricted`); optional."
    )

    @field_validator("reference_images")
    @classmethod
    def _distinct_references(cls, value: list[ReferenceImage]) -> list[ReferenceImage]:
        _distinct_ids([image.media_id for image in value])
        return value


# -- the draft ---------------------------------------------------------------------------------


class CharacterDraft(CharacterModel):
    """A catalog character's draft document (FR-CAT-2,4): the ``public_card`` (name, public
    description, images), the private ``definition``, the ``metadata`` (aliases, labels,
    content rating) and an optional ``visual_identity``. A draft may lack any field; publishing
    needs a true age, a content rating and the rest of the definition (the report names each
    missing field). A character carries no world truths: the document has no field for facts.
    Facts about a character's world belong to a scenario, normally the character's companion
    scenario (FR-CAT-5, #116)."""

    public_card: CharacterPublicCard = Field(default_factory=CharacterPublicCard)
    definition: CharacterDefinition = Field(
        default_factory=CharacterDefinition,
        description="The private definition: returned only by the `full` projection.",
    )
    metadata: CharacterMetadata = Field(default_factory=CharacterMetadata)
    visual_identity: VisualIdentity | None = Field(
        default=None, description="Optional image settings, versioned with the character."
    )

    def document(self) -> dict[str, Any]:
        """The stored form: JSON, with the fields that are not set left out."""
        stored: dict[str, Any] = self.model_dump(
            mode="json", exclude_none=True, include=set(CharacterDraft.model_fields)
        )
        return stored


class CharacterCreate(CharacterDraft):
    """``POST /catalog/characters``: the new entry's first draft (any of the draft's fields)
    and, optionally, its ``owner``: an opaque principal, by default the requesting one, never
    ``system`` (FR-AUTH-5). Ownership confers no permission (FR-AUTH-8)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "public_card": {
                        "name": "Mara Venn",
                        "public_description": "Keeps the light on Gull Rock.",
                    },
                    "definition": {
                        "true_age": 41,
                        "personality": "Patient, private and slow to trust.",
                        "dispositions": {"honesty": {"setting": "honest"}},
                    },
                    "metadata": {"labels": ["quiet"]},
                }
            ]
        }
    )

    owner: Owner | None = Field(
        default=None,
        description="The entry's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )


class CharacterPublishRequest(PublishRequest):
    """``POST /catalog/characters/{id}/publish``: the character's ``draft_revision`` (FR-CAT-3)
    and, when its companion's draft is published with it (``?with_companion=true``; FR-CAT-10),
    the companion draft's ``companion_draft_revision``: required then, refused otherwise. A
    stale revision of either is 409 ``draft_stale``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"draft_revision": 2},
                {"draft_revision": 3, "companion_draft_revision": 4},
            ]
        },
    )

    companion_draft_revision: Revision | None = Field(
        default=None,
        description="The companion draft's revision, with `with_companion=true` only (#116).",
    )


# -- the save rules (beyond the schema) ---------------------------------------------------------

Loc = tuple[str | int, ...]


def _error(loc: Loc, kind: str, message: str) -> dict[str, Any]:
    """One finding, in the shape of a pydantic error (``InvalidRequest.from_errors``)."""
    return {"loc": loc, "type": kind, "msg": message}


def character_rules(draft: CharacterDraft) -> list[dict[str, Any]]:
    """The save rules the schema cannot state: an alias is neither the name nor another alias,
    ignoring case (``alias_is_name``, ``alias_duplicate`` at ``metadata.aliases[i]``)."""
    found: list[dict[str, Any]] = []
    name = draft.public_card.name
    seen: set[str] = set()
    for index, alias in enumerate(draft.metadata.aliases):
        key = alias.casefold()
        loc: Loc = ("metadata", "aliases", index)
        if name is not None and key == name.casefold():
            found.append(_error(loc, "alias_is_name", "an alias may not be the character's name"))
        elif key in seen:
            found.append(_error(loc, "alias_duplicate", "an alias may not repeat another"))
        seen.add(key)
    return found


AUDIENCE_RULE = (
    "a reference image declares its audience (perceivable or restricted); there is no default"
)


def missing_audiences(draft: CharacterDraft) -> list[int]:
    """The indexes of the reference images that declare no audience."""
    identity = draft.visual_identity
    if identity is None:
        return []
    return [i for i, image in enumerate(identity.reference_images) if image.audience is None]


def audience_errors(draft: CharacterDraft) -> list[dict[str, Any]]:
    """A save's refusal of each reference image without an audience (``missing`` at
    ``visual_identity.reference_images[i].audience``)."""
    return [
        _error(("visual_identity", "reference_images", index, "audience"), "missing", AUDIENCE_RULE)
        for index in missing_audiences(draft)
    ]


# -- what a publish requires --------------------------------------------------------------------

PUBLISH_REQUIRED: Final = (
    "public_card.name",
    "public_card.public_description",
    "definition.appearance",
    "definition.personality",
    *(f"definition.dispositions.{axis}" for axis in DISPOSITION_AXES),
    "definition.values_motivations",
    "definition.interaction_style.speech_style",
    "definition.interaction_style.vocabulary",
)
"""What a published character must hold besides its true age and content rating, which have
codes of their own (``field_required`` otherwise): every part of AC1's model that is not marked
optional and is not a list. A string among them must not be blank."""

IDENTITY_REQUIRED: Final = (
    "visual_identity.preferred_image_model",
    "visual_identity.base_appearance_prompt",
    "visual_identity.negative_prompt",
    "visual_identity.seed",
)
"""What a visual identity needs to publish, when the draft has one."""


def value_at(draft: CharacterDraft, path: str) -> Any:
    """The value at the dotted ``path`` of ``draft`` (``None`` when a group on the way is
    unset)."""
    found: Any = draft
    for part in path.split("."):
        found = getattr(found, part, None)
        if found is None:
            return None
    return found


def unset_required(draft: CharacterDraft) -> Iterator[str]:
    """The paths of ``PUBLISH_REQUIRED`` (and ``IDENTITY_REQUIRED`` with a visual identity)
    that ``draft`` leaves unset or blank, in order."""
    paths = PUBLISH_REQUIRED + (IDENTITY_REQUIRED if draft.visual_identity is not None else ())
    for path in paths:
        value = value_at(draft, path)
        if value is None or (isinstance(value, str) and not value.strip()):
            yield path


# -- projections (FR-SCN-10) --------------------------------------------------------------------


class CardImage(CharacterModel):
    """An image on a public card (FR-CAT-4): the media asset and its alt text, nothing else."""

    media_id: str
    alt_text: str | None = None


class CharacterCardImages(CharacterModel):
    """The card's images: the primary image (``null`` when there is none) and the gallery, in
    order."""

    primary: CardImage | None = None
    gallery: list[CardImage] = Field(default_factory=list)


class CharacterImageDetail(CharacterModel):
    """An image as ``full`` shows it (FR-CAT-4; #105): the media asset's id and, from its live
    row, its alt text, caption, MIME type, dimensions and generation metadata, exactly as stored
    (every field ``null`` for an asset no row holds)."""

    media_id: str
    alt_text: str | None = None
    caption: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    generation_metadata: dict[str, Any] | None = None


class CharacterImagesDetail(CharacterModel):
    """``full``'s images: the primary image (``null`` when there is none) and the gallery."""

    primary: CharacterImageDetail | None = None
    gallery: list[CharacterImageDetail] = Field(default_factory=list)


class ReferenceImageDetail(CharacterImageDetail):
    """A reference image as ``full`` shows it: its asset (see ``CharacterImageDetail``) and its
    audience."""

    audience: Audience | None = None


class VisualIdentityDetail(CharacterModel):
    """The visual identity as ``full`` shows it: as stored, each reference image with its
    asset."""

    preferred_image_model: str | None = None
    base_appearance_prompt: str | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    reference_images: list[ReferenceImageDetail] = Field(default_factory=list)
    adapter_params: dict[str, Any] | None = None
    restricted_details_prompt: str | None = None


class CharacterCard(CharacterModel):
    """A catalog character's public card (``projection=card``, the default; FR-CAT-2,
    FR-SCN-10): only what a player-facing client may show, never a definition field.
    ``version`` is the version shown; a draft read shows the draft with the latest published
    version (``null`` before the first publish). ``images`` holds the primary image and the
    gallery, each with its asset's alt text; ``content_rating`` is the rating's version a
    published version recorded (for a draft, the rating's current version)."""

    id: str
    version: int | None
    name: str | None
    public_description: str | None
    images: CharacterCardImages
    labels: list[str]
    content_rating: CardRating | None
    listing_status: str
    status: EntryStatus


class CharacterListItem(CharacterCard):
    """A catalog character as a listing shows it (``GET /catalog/characters``; #110): exactly
    its public card (what a single read answers with ``projection=card``), plus its
    ``owner_principal`` (recorded for listing and attribution only, FR-AUTH-8) and when it last
    changed (``updated_at``: a draft save, publish, archive, listing-status change or owner
    transfer). ``rank`` is present on a search (``q``): its text-search rank, the order's first
    key."""

    owner_principal: str
    updated_at: datetime
    rank: float | None = Field(default=None, exclude_if=lambda rank: rank is None)


class CharacterFull(CharacterModel):
    """A catalog character in full (``projection=full``; FR-SCN-10): the entry's envelope, the
    public card's fields with each image's media row, the aliases, the private ``definition``
    and the ``visual_identity``. ``draft_revision`` is present only on a draft read
    (``?draft=true``): the revision the next save or publish must send. ``published_at`` is
    when ``version`` was published; ``companion_id`` names the character's companion scenario,
    created with it (#116). ``owner_principal`` is the entry's owner (FR-AUTH-5), recorded
    for listing and attribution only."""

    id: str
    status: EntryStatus
    listing_status: str
    owner_principal: str
    version: int | None
    current_version: int | None
    draft_revision: int | None = Field(default=None, exclude_if=lambda revision: revision is None)
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    companion_id: str | None
    name: str | None
    public_description: str | None
    images: CharacterImagesDetail
    labels: list[str]
    content_rating: CardRating | None
    aliases: list[str]
    definition: CharacterDefinition
    visual_identity: VisualIdentityDetail | None


# -- the merge patch ----------------------------------------------------------------------------


class ThreatResponseDispositionPatch(CharacterModel):
    """A merge patch of ``threat_response``."""

    setting: ThreatResponse | None = None
    nuance: Nuance | None = None


class DominanceDispositionPatch(CharacterModel):
    """A merge patch of ``dominance``."""

    setting: Dominance | None = None
    nuance: Nuance | None = None


class HonestyDispositionPatch(CharacterModel):
    """A merge patch of ``honesty``."""

    setting: Honesty | None = None
    nuance: Nuance | None = None


class WarmthDispositionPatch(CharacterModel):
    """A merge patch of ``warmth``."""

    setting: Warmth | None = None
    nuance: Nuance | None = None


class CautionDispositionPatch(CharacterModel):
    """A merge patch of ``caution``."""

    setting: Caution | None = None
    nuance: Nuance | None = None


class DispositionsPatch(CharacterModel):
    """A merge patch of the dispositions: an axis merges, ``null`` removes it."""

    threat_response: ThreatResponseDispositionPatch | None = None
    dominance: DominanceDispositionPatch | None = None
    honesty: HonestyDispositionPatch | None = None
    warmth: WarmthDispositionPatch | None = None
    caution: CautionDispositionPatch | None = None


class LikesDislikesPatch(CharacterModel):
    """A merge patch of the likes and dislikes: a list replaces the stored one."""

    likes: list[Item] | None = Field(default=None, max_length=ITEMS_MAX)
    dislikes: list[Item] | None = Field(default=None, max_length=ITEMS_MAX)


class InteractionStylePatch(CharacterModel):
    """A merge patch of the interaction style: a list replaces the stored one."""

    speech_style: Style | None = None
    mannerisms: list[Mannerism] | None = Field(default=None, max_length=LINES_MAX)
    vocabulary: Style | None = None
    sample_dialogue: list[SampleLine] | None = Field(default=None, max_length=LINES_MAX)


class CharacterDefinitionPatch(CharacterModel):
    """The ``definition`` part of a draft patch: a key sets that field, ``null`` removes it,
    and the groups merge the same way."""

    true_age: TrueAge | None = None
    appearance: Prose | None = None
    personality: Prose | None = None
    likes_dislikes: LikesDislikesPatch | None = None
    dispositions: DispositionsPatch | None = None
    values_motivations: Prose | None = None
    interaction_style: InteractionStylePatch | None = None
    background_flavor: Prose | None = None
    deflection_style: str | None = Field(default=None, max_length=DEFLECTION_STYLE_MAX)
    safe_fallback_text: str | None = Field(default=None, max_length=SAFE_FALLBACK_MAX)


class CharacterImagesPatch(CharacterModel):
    """The images part of a draft patch: ``primary`` set or removed (``null``), and ``gallery``
    replaced as a whole."""

    primary: Ulid | None = None
    gallery: list[Ulid] | None = Field(default=None, max_length=GALLERY_MAX)


class CharacterPublicCardPatch(CharacterModel):
    """The ``public_card`` part of a draft patch."""

    name: Name | None = None
    public_description: str | None = Field(default=None, max_length=PUBLIC_DESCRIPTION_MAX)
    images: CharacterImagesPatch | None = None


class CharacterMetadataPatch(CharacterModel):
    """The ``metadata`` part of a draft patch: a list replaces the stored one."""

    aliases: list[Alias] | None = Field(default=None, max_length=ALIASES_MAX)
    labels: list[Label] | None = Field(default=None, max_length=LABELS_MAX)
    content_rating_id: Ulid | None = None


class VisualIdentityPatch(CharacterModel):
    """The ``visual_identity`` part of a draft patch: a key sets that field, ``null`` removes
    it; ``reference_images`` replaces the stored list (each image complete, with its audience),
    and ``adapter_params`` merges as JSON."""

    preferred_image_model: str | None = Field(default=None, max_length=MODEL_NAME_MAX)
    base_appearance_prompt: Prompt | None = None
    negative_prompt: Prompt | None = None
    seed: Seed | None = None
    reference_images: list[ReferenceImage] | None = Field(
        default=None, max_length=REFERENCE_IMAGES_MAX
    )
    adapter_params: dict[str, Any] | None = None
    restricted_details_prompt: Prompt | None = None


class CharacterPatch(CharacterModel):
    """``PATCH /catalog/characters/{id}``: a JSON merge patch (RFC 7386) over the draft — a key
    sets that field, ``null`` removes it, and every group merges the same way (a list replaces
    the stored one) — with the ``draft_revision`` it is based on (FR-CAT-3). The draft it makes
    is validated as a whole: an error is 422 at ``body.<path>``, wherever the field came from."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "draft_revision": 1,
                    "public_card": {
                        "public_description": "Keeps the light on Gull Rock, and the logbook."
                    },
                    "definition": {"personality": "Patient and wry."},
                }
            ]
        },
    )

    draft_revision: Revision = Field(
        description="The draft revision this patch is based on; a stale one is 409 `draft_stale`."
    )
    public_card: CharacterPublicCardPatch | None = None
    definition: CharacterDefinitionPatch | None = None
    metadata: CharacterMetadataPatch | None = None
    visual_identity: VisualIdentityPatch | None = None

    def document(self) -> dict[str, Any]:
        """The merge patch itself: the fields the request set (``null`` included), without
        ``draft_revision``."""
        patch: dict[str, Any] = self.model_dump(
            mode="json", exclude_unset=True, exclude={"draft_revision"}
        )
        return patch


__all__ = [
    "ADAPTER_PARAMS_MAX_BYTES",
    "AUDIENCE_RULE",
    "BACKGROUND_FLAVOR_MAX",
    "DISPOSITION_AXES",
    "GALLERY_MAX",
    "IDENTITY_REQUIRED",
    "NAME_MAX",
    "PERSONALITY_MAX",
    "PUBLIC_DESCRIPTION_MAX",
    "PUBLISH_REQUIRED",
    "REFERENCE_IMAGES_MAX",
    "SAFE_FALLBACK_MAX",
    "SEED_MAX",
    "Audience",
    "CardImage",
    "Caution",
    "CautionDisposition",
    "CharacterCard",
    "CharacterCardImages",
    "CharacterCreate",
    "CharacterDefinition",
    "CharacterDefinitionPatch",
    "CharacterDraft",
    "CharacterFull",
    "CharacterImageDetail",
    "CharacterImages",
    "CharacterImagesDetail",
    "CharacterImagesPatch",
    "CharacterListItem",
    "CharacterMetadata",
    "CharacterMetadataPatch",
    "CharacterPatch",
    "CharacterPublicCard",
    "CharacterPublicCardPatch",
    "CharacterPublishRequest",
    "Dispositions",
    "DispositionsPatch",
    "Dominance",
    "DominanceDisposition",
    "Honesty",
    "HonestyDisposition",
    "InteractionStyle",
    "InteractionStylePatch",
    "LikesDislikes",
    "LikesDislikesPatch",
    "Name",
    "ReferenceImage",
    "ReferenceImageDetail",
    "ThreatResponse",
    "ThreatResponseDisposition",
    "VisualIdentity",
    "VisualIdentityDetail",
    "VisualIdentityPatch",
    "Warmth",
    "WarmthDisposition",
    "audience_errors",
    "character_rules",
    "missing_audiences",
    "unset_required",
    "value_at",
]
