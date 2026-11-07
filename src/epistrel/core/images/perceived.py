"""What a catalog image may be built from: the perceivable inputs of a catalog entry
(FR-IMG-2, FR-CAT-4, FR-RATE-4, FR-SCN-11; #259).

``catalog_perceived_inputs(conn, entry_kind, entry_id, version=…, draft_revision=…, purpose=…)``
reads one entry's draft or published version and returns plain data that is safe for an image
prompt. A catalog image is public-card content: its audience is anyone browsing. So the result
is built by allow-list, and it holds only:

- **for a character** (``primary`` or ``gallery``): its ``definition.appearance``, trimmed and
  with every authored exact age redacted (``redact_ages``); the **apparent age band** of its
  true age (#170's ``age_band`` over ``ages.catalog_true_age``). The band is computed here, and
  the true age never leaves this module. Also its visual identity's ``base_appearance_prompt``,
  ``negative_prompt``, ``seed`` and ``preferred_image_model``, its ``perceivable`` reference
  images that are still ``live`` media, in order, and its ``perceivable`` add-ons
  (``adapter_params.addons``). ``size`` and ``aspect_ratio`` come from ``adapter_params`` when
  they match #254's patterns. The visual style is the companion scenario's: its draft for a
  character draft; for version N, the companion version pinned to N (the latest such), else the
  companion's latest published version, else none;
- **for a scenario** (``cover``): its public description and visual style only. A cover
  depicts no slot characters;
- **the rating's image guidance** (FR-RATE-4): a version's recorded rating version, or for a
  draft the current version of its rating (retired ratings still resolve).

``restricted`` reference images and add-ons, ``restricted_details_prompt``, every other
``adapter_params`` key, the definition's private fields and the true age are never read into
the result. Refusals are 422 ``validation_error`` at the field: no true age
(``definition.true_age``, since the prompt check needs it and fails closed), no rating
(``metadata.content_rating_id``), no appearance and no base prompt (``definition.appearance``),
and a cover with neither a public description nor a style (``public_description``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog import ages
from epistrel.core.catalog.checks.character_documents import rating_id_of
from epistrel.core.epistemic.context import age_band
from epistrel.core.errors import (
    CharacterNotFound,
    DraftStale,
    EpistrelError,
    InvalidRequest,
    NotPublished,
    ScenarioNotFound,
)
from epistrel.core.tables import media_assets
from epistrel.core.tables.catalog import (
    character_catalog,
    character_versions,
    scenario_catalog,
    scenario_versions,
)
from epistrel.core.tables.media import MediaState
from epistrel.core.tables.ratings import content_rating_versions, content_ratings

type EntryKind = Literal["character", "scenario"]
type ImagePurpose = Literal["primary", "gallery", "cover"]

PURPOSES: Final[Mapping[str, frozenset[str]]] = {
    "character": frozenset({"primary", "gallery"}),
    "scenario": frozenset({"cover"}),
}
"""The purposes each entry kind's images may have."""
PERCEIVABLE: Final = "perceivable"
SIZE_PATTERN: Final = re.compile(r"^\d{2,5}x\d{2,5}$")
ASPECT_RATIO_PATTERN: Final = re.compile(r"^\d{1,2}:\d{1,2}$")
"""#254's patterns (``orchestrator.images.port``), restated here because Core cannot import the
Orchestrator. A unit test asserts the two agree."""


# -- authored ages --------------------------------------------------------------------------

_UNITS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    "sixteen|seventeen|eighteen|nineteen"
)
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_NUMBER = (
    rf"(?:\d{{1,3}}|(?:a|one)[\s-]hundred(?:[\s-]and[\s-](?:{_TENS}|{_UNITS}))?"
    rf"|(?:{_TENS})(?:[\s-](?:{_UNITS}))?|{_UNITS})"
)
_SEP = r"(?:\s*-\s*|\s+)"
AGE_PHRASES: Final = (
    re.compile(rf"\b{_NUMBER}{_SEP}(?:years?|yrs?){_SEP}old\b", re.IGNORECASE),
    re.compile(rf"\b{_NUMBER}{_SEP}(?:years?|yrs?)(?:\s+of\s+age)?\b", re.IGNORECASE),
    re.compile(rf"\b(?:aged|age)\s+{_NUMBER}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,3}\s*(?:yo|y/o|y\.o\.)(?=\W|$)", re.IGNORECASE),
)
"""Authored exact-age phrases: "a 34-year-old", "thirty-four years old", "aged 34", "34 yo"."""


def redact_ages(text: str, true_age: int | None = None) -> str:
    """``text`` without authored exact ages (the planner's gate default): every phrase of
    ``AGE_PHRASES`` and the true age as a standalone number are removed, then the spacing and
    punctuation left behind are tidied."""
    found = text
    for pattern in AGE_PHRASES:
        found = pattern.sub(" ", found)
    if true_age is not None:
        found = re.sub(rf"(?<![\w.]){true_age}(?![\w.])", " ", found)
    found = re.sub(r"\(\s*\)", "", found)
    found = re.sub(r"[ \t]+", " ", found)
    found = re.sub(r" +([,.;:!?])", r"\1", found)
    found = re.sub(r"([,;:])(?=[,.;:])", "", found)
    return re.sub(r" {2,}", " ", found).strip(" ,;:")


# -- the result -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerceivedEntrySubject:
    """One depicted character as anyone browsing sees it (no age, nothing restricted)."""

    description: str
    age_band: str
    base_appearance_prompt: str | None
    reference_image_ids: tuple[str, ...]
    seed: int | None
    adapter_addons: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CatalogPerceived:
    """A catalog entry's perceivable image inputs (see the module docstring)."""

    entry_kind: EntryKind
    entry_id: str
    version: int | None
    draft_revision: int | None
    purpose: str
    subjects: tuple[PerceivedEntrySubject, ...]
    public_description: str | None
    visual_style: str | None
    style_negative: str | None
    rating_image_guidance: str
    identity_negative: str | None
    size: str | None
    aspect_ratio: str | None
    preferred_image_model: str | None


# -- reading --------------------------------------------------------------------------------


def _group(document: Mapping[str, Any] | None, *path: str) -> Mapping[str, Any]:
    found: Any = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def _text(value: Any) -> str | None:
    """A non-blank string, trimmed; ``None`` otherwise."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _seed(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def perceivable_addons(identity: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The identity's add-ons whose ``audience`` is exactly ``perceivable`` (any other, or none,
    is left out), in order."""
    addons = _group(identity, "adapter_params").get("addons")
    return tuple(
        dict(item)
        for item in (addons if isinstance(addons, list) else [])
        if isinstance(item, Mapping) and item.get("audience") == PERCEIVABLE
    )


async def _live_references(conn: AsyncConnection, identity: Mapping[str, Any]) -> tuple[str, ...]:
    """The identity's ``perceivable`` reference images that are ``live`` media, in order."""
    wanted = [
        str(item["media_id"])
        for item in identity.get("reference_images") or ()
        if isinstance(item, Mapping)
        and item.get("audience") == PERCEIVABLE
        and isinstance(item.get("media_id"), str)
    ]
    if not wanted:
        return ()
    live = {
        str(found)
        for found in await conn.scalars(
            select(media_assets.c.id).where(
                media_assets.c.id.in_(sorted(set(wanted))),
                media_assets.c.state == MediaState.LIVE.value,
            )
        )
    }
    return tuple(dict.fromkeys(m for m in wanted if m in live))


def _dimensions(identity: Mapping[str, Any]) -> tuple[str | None, str | None]:
    params = _group(identity, "adapter_params")
    size, ratio = params.get("size"), params.get("aspect_ratio")
    return (
        size if isinstance(size, str) and SIZE_PATTERN.match(size) else None,
        ratio if isinstance(ratio, str) and ASPECT_RATIO_PATTERN.match(ratio) else None,
    )


async def rating_guidance(
    conn: AsyncConnection, document: Mapping[str, Any], version_id: str | None, *, versioned: bool
) -> str:
    """The rating's image guidance: the version a published version recorded
    (``version_id``), or for a draft the current version of its rating. 422 at
    ``metadata.content_rating_id`` when there is none."""
    guidance: Any = None
    if versioned and version_id is not None:
        guidance = await conn.scalar(
            select(content_rating_versions.c.image_guidance).where(
                content_rating_versions.c.version_id == version_id
            )
        )
    elif not versioned and (rating_id := rating_id_of(document)) is not None:
        guidance = await conn.scalar(
            select(content_rating_versions.c.image_guidance)
            .join(
                content_ratings,
                content_ratings.c.current_version_id == content_rating_versions.c.version_id,
            )
            .where(content_ratings.c.id == rating_id)
        )
    if guidance is None:
        raise InvalidRequest(
            "an image prompt needs the entry's content rating: there is no image guidance to "
            "apply without it",
            {"field": "metadata.content_rating_id"},
        )
    return str(guidance)


def _style(document: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    style = _group(document, "visual_style")
    return _text(style.get("prompt")), _text(style.get("negative_prompt"))


async def companion_style(
    conn: AsyncConnection, character_id: str, version: int | None
) -> tuple[str | None, str | None]:
    """The visual style of the character's companion scenario (the owner's round two): its
    draft's for a character draft (``version`` ``None``); for version N, the companion version
    pinned to N (the latest such), else its latest published version, else none."""
    companion = (
        await conn.execute(
            select(
                scenario_catalog.c.id, scenario_catalog.c.draft, scenario_catalog.c.current_version
            ).where(scenario_catalog.c.companion_of == character_id)
        )
    ).one_or_none()
    if companion is None:
        return None, None
    if version is None:
        return _style(companion.draft)
    content = await conn.scalar(
        select(scenario_versions.c.content)
        .where(
            scenario_versions.c.catalog_id == companion.id,
            scenario_versions.c.pinned_character_id == character_id,
            scenario_versions.c.pinned_character_version == version,
        )
        .order_by(scenario_versions.c.version.desc())
        .limit(1)
    )
    if content is None and companion.current_version is not None:
        content = await conn.scalar(
            select(scenario_versions.c.content).where(
                scenario_versions.c.catalog_id == companion.id,
                scenario_versions.c.version == companion.current_version,
            )
        )
    return _style(content) if isinstance(content, Mapping) else (None, None)


async def character_perceived(
    conn: AsyncConnection,
    entry_id: str,
    document: Mapping[str, Any],
    *,
    version: int | None,
    draft_revision: int | None,
    rating_version_id: str | None,
    purpose: str,
) -> CatalogPerceived:
    """A character's perceivable inputs (see the module docstring)."""
    true_age = ages.catalog_true_age(document)
    if true_age is None:
        raise InvalidRequest(
            "an image prompt needs the character's true age: the prompt check judges by it",
            {"field": "definition.true_age"},
        )
    band = age_band(true_age)
    assert band is not None  # noqa: S101 - a true age has a band
    guidance = await rating_guidance(
        conn, document, rating_version_id, versioned=version is not None
    )
    identity = _group(document, "visual_identity")
    appearance = _text(_group(document, "definition").get("appearance"))
    description = redact_ages(appearance, true_age) if appearance else ""
    base = _text(identity.get("base_appearance_prompt"))
    if base is not None:
        base = redact_ages(base, true_age) or None
    if not description and base is None:
        raise InvalidRequest(
            "an image prompt needs the character's appearance or a base appearance prompt",
            {"field": "definition.appearance"},
        )
    style, style_negative = await companion_style(conn, entry_id, version)
    size, ratio = _dimensions(identity)
    subject = PerceivedEntrySubject(
        description=description,
        age_band=band,
        base_appearance_prompt=base,
        reference_image_ids=await _live_references(conn, identity),
        seed=_seed(identity.get("seed")),
        adapter_addons=perceivable_addons(identity),
    )
    return CatalogPerceived(
        entry_kind="character",
        entry_id=entry_id,
        version=version,
        draft_revision=draft_revision,
        purpose=purpose,
        subjects=(subject,),
        public_description=None,
        visual_style=style,
        style_negative=style_negative,
        rating_image_guidance=guidance,
        identity_negative=_text(identity.get("negative_prompt")),
        size=size,
        aspect_ratio=ratio,
        preferred_image_model=_text(identity.get("preferred_image_model")),
    )


async def scenario_perceived(
    conn: AsyncConnection,
    entry_id: str,
    document: Mapping[str, Any],
    *,
    version: int | None,
    draft_revision: int | None,
    rating_version_id: str | None,
    purpose: str,
) -> CatalogPerceived:
    """A scenario cover's inputs: its public description and visual style only."""
    public = _text(_group(document, "public_card").get("public_description"))
    style, style_negative = _style(document)
    if public is None and style is None:
        raise InvalidRequest(
            "a cover prompt needs the scenario's public description or its visual style",
            {"field": "public_description"},
        )
    guidance = await rating_guidance(
        conn, document, rating_version_id, versioned=version is not None
    )
    return CatalogPerceived(
        entry_kind="scenario",
        entry_id=entry_id,
        version=version,
        draft_revision=draft_revision,
        purpose=purpose,
        subjects=(),
        public_description=public,
        visual_style=style,
        style_negative=style_negative,
        rating_image_guidance=guidance,
        identity_negative=None,
        size=None,
        aspect_ratio=None,
        preferred_image_model=None,
    )


def check_purpose(entry_kind: str, purpose: str) -> None:
    """422 at ``body.purpose`` for a purpose the kind's images cannot have (``cover`` on a
    character, ``primary``/``gallery`` on a scenario)."""
    allowed = PURPOSES.get(entry_kind, frozenset())
    if purpose not in allowed:
        raise InvalidRequest(
            f"a {entry_kind} image's purpose is one of {', '.join(sorted(allowed))}",
            {"field": "body.purpose"},
        )


async def catalog_perceived_inputs(
    conn: AsyncConnection,
    entry_kind: EntryKind,
    entry_id: str,
    document: Mapping[str, Any],
    *,
    version: int | None,
    draft_revision: int | None,
    rating_version_id: str | None,
    purpose: str,
) -> CatalogPerceived:
    """The entry's perceivable inputs from ``document`` (its draft, at ``draft_revision``, or
    its published ``version``, which recorded ``rating_version_id``); see the module
    docstring."""
    check_purpose(entry_kind, purpose)
    read = character_perceived if entry_kind == "character" else scenario_perceived
    return await read(
        conn,
        entry_id,
        document,
        version=version,
        draft_revision=draft_revision,
        rating_version_id=rating_version_id,
        purpose=purpose,
    )


# -- the entry ------------------------------------------------------------------------------


def _tables(entry_kind: str) -> tuple[Any, Any, str, type[EpistrelError], str]:
    if entry_kind == "character":
        return (
            character_catalog,
            character_versions,
            "character_id",
            CharacterNotFound,
            "catalog character",
        )
    return scenario_catalog, scenario_versions, "scenario_id", ScenarioNotFound, "scenario"


@dataclass(frozen=True, slots=True)
class EntryTarget:
    """What an image prompt is built from: the entry's draft at ``draft_revision`` or its
    published ``version`` (exactly one is set)."""

    entry_kind: EntryKind
    entry_id: str
    version: int | None
    draft_revision: int | None


async def resolve_target(
    conn: AsyncConnection,
    entry_kind: EntryKind,
    entry_id: str,
    *,
    draft: bool = False,
    version: int | None = None,
) -> EntryTarget:
    """The entry's draft (``draft``: at its current revision), the ``version`` named, or with
    neither its latest published version: 404 for an unknown entry, 422 ``not_published`` for
    an entry never published (archived and unlisted entries resolve like any other)."""
    catalog, _, key, missing, noun = _tables(entry_kind)
    entry = (
        await conn.execute(
            select(catalog.c.draft_revision, catalog.c.current_version).where(
                catalog.c.id == entry_id
            )
        )
    ).one_or_none()
    if entry is None:
        raise missing(f"{noun} {entry_id} does not exist", {key: entry_id})
    if draft:
        return EntryTarget(entry_kind, entry_id, None, int(entry.draft_revision))
    if version is not None:
        return EntryTarget(entry_kind, entry_id, version, None)
    if entry.current_version is None:
        raise NotPublished(
            f"{noun} {entry_id} has no published version; build from its draft "
            "(`draft: true`) instead",
            {key: entry_id},
        )
    return EntryTarget(entry_kind, entry_id, int(entry.current_version), None)


async def entry_document(
    conn: AsyncConnection, target: EntryTarget
) -> tuple[Mapping[str, Any], str | None]:
    """The target's document and, for a version, the rating version it recorded: 404 for an
    unknown entry or version, 409 ``draft_stale`` for a draft no longer at the revision."""
    catalog, versions, key, missing, noun = _tables(target.entry_kind)
    entry = (
        await conn.execute(
            select(catalog.c.draft, catalog.c.draft_revision).where(catalog.c.id == target.entry_id)
        )
    ).one_or_none()
    if entry is None:
        raise missing(f"{noun} {target.entry_id} does not exist", {key: target.entry_id})
    if target.version is None:
        if target.draft_revision is not None and target.draft_revision != entry.draft_revision:
            raise DraftStale(
                f"the draft of {noun} {target.entry_id} is at revision {entry.draft_revision}, "
                f"not {target.draft_revision}; read it again and retry",
                {
                    key: target.entry_id,
                    "draft_revision": target.draft_revision,
                    "current_draft_revision": int(entry.draft_revision),
                },
            )
        return (entry.draft if isinstance(entry.draft, Mapping) else {}), None
    row = (
        await conn.execute(
            select(versions.c.content, versions.c.content_rating_version_id).where(
                versions.c.catalog_id == target.entry_id, versions.c.version == target.version
            )
        )
    ).one_or_none()
    if row is None:
        raise missing(
            f"{noun} {target.entry_id} has no version {target.version}",
            {key: target.entry_id, "version": target.version},
        )
    content = row.content if isinstance(row.content, Mapping) else {}
    rating = None if row.content_rating_version_id is None else str(row.content_rating_version_id)
    return content, rating


async def read_perceived(
    conn: AsyncConnection, target: EntryTarget, *, purpose: str
) -> CatalogPerceived:
    """``catalog_perceived_inputs`` for ``target``, read in ``conn``."""
    check_purpose(target.entry_kind, purpose)
    document, rating_version_id = await entry_document(conn, target)
    return await catalog_perceived_inputs(
        conn,
        target.entry_kind,
        target.entry_id,
        document,
        version=target.version,
        draft_revision=target.draft_revision,
        rating_version_id=rating_version_id,
        purpose=purpose,
    )


# -- drafting a base appearance prompt (FR-CAT-4) -------------------------------------------


@dataclass(frozen=True, slots=True)
class BaseAppearanceInputs:
    """What drafting a character's base appearance prompt starts from: the draft's appearance
    with its authored ages redacted, the draft's revision, and the check a drafted text must
    pass (no age, no restricted term). The true age and the restricted terms stay inside
    ``check``: the caller never sees them."""

    appearance: str
    draft_revision: int
    _true_age: int | None
    _terms: frozenset[str]

    def leaks(self, text: str) -> bool:
        """Whether ``text`` states an age or uses a restricted-details term."""
        return leaks(text, self._true_age, self._terms)


async def base_appearance_inputs(
    conn: AsyncConnection, character_id: str, draft_revision: int | None
) -> BaseAppearanceInputs:
    """The draft's inputs for drafting its base appearance prompt: 404 for an unknown
    character, 409 ``draft_stale`` for a stale ``draft_revision``, 422 at
    ``definition.appearance`` without an appearance."""
    entry = (
        await conn.execute(
            select(character_catalog.c.draft, character_catalog.c.draft_revision).where(
                character_catalog.c.id == character_id
            )
        )
    ).one_or_none()
    if entry is None:
        raise CharacterNotFound(
            f"catalog character {character_id} does not exist", {"character_id": character_id}
        )
    if draft_revision is not None and draft_revision != entry.draft_revision:
        raise DraftStale(
            f"the draft of catalog character {character_id} is at revision "
            f"{entry.draft_revision}, not {draft_revision}; read it again and retry",
            {
                "character_id": character_id,
                "draft_revision": draft_revision,
                "current_draft_revision": int(entry.draft_revision),
            },
        )
    document = entry.draft if isinstance(entry.draft, Mapping) else {}
    true_age = ages.catalog_true_age(document)
    appearance = _text(_group(document, "definition").get("appearance"))
    redacted = redact_ages(appearance, true_age) if appearance else ""
    if not redacted:
        raise InvalidRequest(
            "drafting a base appearance prompt needs the draft's appearance",
            {"field": "definition.appearance"},
        )
    restricted = _text(_group(document, "visual_identity").get("restricted_details_prompt"))
    return BaseAppearanceInputs(
        appearance=redacted,
        draft_revision=int(entry.draft_revision),
        _true_age=true_age,
        _terms=restricted_terms(restricted, appearance),
    )


STOPWORDS: Final = frozenset(
    {
        "about", "above", "after", "again", "also", "always", "around", "been", "before",
        "behind", "being", "below", "beneath", "between", "both", "from", "have", "having",
        "here", "hers", "herself", "himself", "into", "just", "like", "more", "most", "much",
        "only", "other", "over", "same", "some", "such", "than", "that", "their", "theirs",
        "them", "themselves", "then", "there", "these", "they", "this", "those", "through",
        "under", "until", "upon", "very", "were", "what", "when", "where", "which", "while",
        "with", "within", "without", "would", "your", "yours",
    }
)  # fmt: skip
"""Words a restricted-term check ignores: too common to reveal anything."""


def restricted_terms(restricted: str | None, appearance: str | None) -> frozenset[str]:
    """The restricted-details words a drafted base prompt may not use: each word of four or
    more letters of ``restricted`` (lower-cased, stopwords left out) that ``appearance`` does
    not hold."""
    if not restricted:
        return frozenset()
    seen = set(re.findall(r"[a-z]+", (appearance or "").lower()))
    return frozenset(
        word
        for word in re.findall(r"[a-z]+", restricted.lower())
        if len(word) >= 4 and word not in STOPWORDS and word not in seen
    )


AGE_DIGITS: Final = re.compile(r"\b\d{1,3}\s*-?\s*(?:years?|yrs?|yo|y/o)\b", re.IGNORECASE)


def leaks(text: str, true_age: int | None, terms: Sequence[str] | frozenset[str]) -> bool:
    """Whether a drafted base prompt states an age (the true age as a standalone number, or a
    number of years) or uses a restricted term (whole word, ignoring case)."""
    if AGE_DIGITS.search(text):
        return True
    if true_age is not None and re.search(rf"\b{true_age}\b", text):
        return True
    words = set(re.findall(r"[a-z]+", text.lower()))
    return any(term in words for term in terms)


__all__ = [
    "AGE_PHRASES",
    "PURPOSES",
    "BaseAppearanceInputs",
    "CatalogPerceived",
    "EntryKind",
    "EntryTarget",
    "ImagePurpose",
    "PerceivedEntrySubject",
    "base_appearance_inputs",
    "catalog_perceived_inputs",
    "check_purpose",
    "companion_style",
    "entry_document",
    "leaks",
    "perceivable_addons",
    "rating_guidance",
    "read_perceived",
    "redact_ages",
    "resolve_target",
    "restricted_terms",
]
