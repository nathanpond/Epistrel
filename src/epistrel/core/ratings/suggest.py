"""The rating suggestion's deterministic half (FR-RATE-8; #121): the entry it reviews, the
elements that entry is made of, the deterministic concerns, and the advisory table.

**Advisory, and read by nothing.** The table never changes a rating, blocks a publish or gates
a story: no guardrail, publish, story-creation or rating-change path imports this module (the
import-linter contract ``rating-suggestion-is-advisory`` forbids it). ``core.ratings`` does not
re-export it either. It writes nothing.

**The entry** (``load_entry``) is a catalog character or scenario: its draft (``"draft"``), a
published version (``n``), or with neither the latest published version, else the draft. A
scenario brings the definitions of its ``default`` and ``locked`` roles' characters at their
pinned version, else their latest published one; a character that is missing, unpublished or
archived is skipped and listed ``unavailable`` (``character_unavailable``).

**Elements** are every text of the entry a rating's policy can be about, each with its kind and
location (#111's location: the ``entry`` and ``version``, the ``role``, the dotted ``field``,
the ``seed_id``; a medium's ``media_id``):

- ``field``: a text of the entry itself (the public card, labels and aliases, the world, the
  opening, the director's material, the visual style and the stored structures; a character's
  definition and image prompts);
- ``seed``: a seed's text (``seed_id``); ``reveal``: a planned reveal's truth or conditions;
- ``role``: a role's setup description, public label, relationships, name overrides and
  starting values;
- ``cast_character``: a default or locked role's character's text (its ``entry`` and
  ``version`` are the character's);
- ``media_caption``: a medium's caption, alt text and generation prompts (``media_id``), or a
  scenario video's own caption.

**Deterministic concerns** (``deterministic_concerns``) are every element a rating's
deterministic policy (word list, name list, pattern) breaks under either text target, found
without a model call; the rest of each row comes from the ``rating_suggestion`` purpose
(``epistrel.orchestrator.ratings.suggest``).

**The table** (``RatingSuggestion``) has one row per rating, in display order: ``appropriate``
is ``false`` exactly when the row has a concern, ``null`` when its model call failed and nothing
deterministic was found, else ``true``. Each concern names exactly one of a ``policy_id`` or a
quoted description ``clause``, the element, and why. It quotes private fields, so it is an
author-facing read like the ``full`` projection (FR-SCN-10).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.catalog import characters as catalog_characters
from epistrel.core.catalog import scenarios as catalog_scenarios
from epistrel.core.catalog.checks.input_policy import policy_texts
from epistrel.core.catalog.checks.scenario import filled_roles
from epistrel.core.catalog.models import CharacterFull
from epistrel.core.catalog.scenario_models import ScenarioDraft, ScenarioFull
from epistrel.core.catalog.service import Projection
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import CharacterNotFound, NotPublished
from epistrel.core.policies.evaluate import evaluate, matched_span
from epistrel.core.policies.model import ModelAssistedRule, Policy, Target
from epistrel.core.tables import media_assets
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

ElementKind = Literal["field", "seed", "role", "reveal", "cast_character", "media_caption"]
EntryType = Literal["character", "scenario"]
VersionLabel = StrictInt | Literal["draft"]
RowError = Literal["model_unavailable", "deadline", "invalid_response"]

DRAFT: Literal["draft"] = "draft"
CHARACTER_UNAVAILABLE: Literal["character_unavailable"] = "character_unavailable"
TEXT_TARGETS = (Target.TEXT_INPUT, Target.TEXT_OUTPUT)
"""Deterministic policies are evaluated under both text targets: an entry's text is both what
a story is given and what it may come to say."""
EXPLANATION_MAX = 2_000
SPAN_MAX = 200
MEDIA_FIELDS = ("caption", "alt_text")
GENERATION_PROMPTS = ("prompt", "negative_prompt")
"""The generation-metadata keys whose text is reviewed (``generation_metadata.<key>``)."""
SKIPPED_KEYS = frozenset({"setting", "media_id", "audience", "content_rating_id", "seed"})
"""Keys of a character's groups that hold no prose: a disposition's setting, ids, enums."""


# -- the response ----------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ElementLocation(_Model):
    """Where an element sits (#111's location): the catalog ``entry`` and its ``version``
    (``"draft"`` for a draft; for a cast character, the character and the version used), and
    where they apply the ``role``, the dotted ``field``, the ``seed_id`` and the ``media_id``."""

    entry: str
    version: VersionLabel
    role: str | None = None
    field: str | None = None
    seed_id: str | None = None
    media_id: str | None = None


class Element(_Model):
    """The part of the entry a concern names: its kind and its location."""

    kind: ElementKind
    location: ElementLocation


class Concern(_Model):
    """One conflict between the entry and a rating: the policy (``policy_id``) or the quoted
    rating-description ``clause`` it breaks (exactly one), the element, and why."""

    policy_id: str | None = None
    clause: str | None = None
    element: Element
    explanation: str = Field(max_length=EXPLANATION_MAX)

    @model_validator(mode="after")
    def _one_basis(self) -> Concern:
        if (self.policy_id is None) == (self.clause is None):
            raise ValueError("a concern names exactly one of policy_id and clause")
        return self


class SuggestionRow(_Model):
    """One rating's row. ``appropriate``: ``false`` when the row has a concern, ``null`` when
    its model call failed (``error``) and nothing deterministic was found, else ``true``.
    ``truncated``: the entry was longer than the model's context, so it saw the private text
    first and not all of it. ``dropped_concerns``: concerns the model gave that named a policy,
    clause or element it was not given, which were dropped."""

    rating_id: str
    rating_name: str
    rating_version: str
    appropriate: bool | None
    concerns: list[Concern]
    error: RowError | None = None
    truncated: bool = False
    dropped_concerns: int = 0


class EvaluatedRating(_Model):
    """A rating the table evaluated, at the version it used (the version's id)."""

    rating_id: str
    rating_version: str


class EntryRef(_Model):
    """The entry reviewed: its type, id, and the version (``"draft"`` for the draft)."""

    type: EntryType
    id: str
    version: VersionLabel


class UnavailableCharacter(_Model):
    """A default or locked role's character the review could not read (missing, unpublished or
    archived), skipped."""

    role: str
    entry: str
    version: int | None
    reason: Literal["character_unavailable"] = CHARACTER_UNAVAILABLE


class RatingSuggestion(_Model):
    """The advisory table (FR-RATE-8): one row per rating, the rating versions it evaluated,
    the entry, the cast characters it skipped, and the model calls it made."""

    advisory: Literal[True] = True
    entry: EntryRef
    evaluated: list[EvaluatedRating]
    rows: list[SuggestionRow]
    unavailable: list[UnavailableCharacter] = Field(default_factory=list)
    model_calls: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int = 0


class SuggestionRequest(_Model):
    """``POST …/rating-suggestion``'s body: the version to review (``"draft"`` for the draft;
    omitted: the latest published version, else the draft)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"version": "draft"}, {"version": 2}, {}]},
    )

    version: VersionLabel | None = Field(default=None, examples=["draft", 2])

    @model_validator(mode="after")
    def _positive(self) -> SuggestionRequest:
        if isinstance(self.version, int) and not 1 <= self.version <= 2**31 - 1:
            raise ValueError("version must be a published version number or 'draft'")
        return self


# -- the entry -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextElement:
    """One element with its text; ``private`` elements come first when the model's context is
    too small for all of them."""

    element: Element
    text: str
    private: bool


@dataclass(frozen=True, slots=True)
class LoadedEntry:
    """The entry under review: what it is, its elements, its characters' true ages (in role
    order, for age-conditioned policies) and the cast characters it skipped."""

    entry: EntryRef
    elements: tuple[TextElement, ...]
    true_ages: tuple[tuple[str, int], ...] = ()
    unavailable: tuple[UnavailableCharacter, ...] = ()


@dataclass
class _Collector:
    entry: str
    version: VersionLabel
    found: list[TextElement] = field(default_factory=list)
    seen: set[tuple[str, str]] = field(default_factory=set)

    def add(
        self,
        kind: ElementKind,
        text: str | None,
        *,
        private: bool,
        entry: str | None = None,
        version: VersionLabel | None = None,
        **where: str | None,
    ) -> None:
        if text is None or not text.strip():
            return
        location = ElementLocation(
            entry=entry or self.entry,
            version=self.version if version is None else version,
            **where,
        )
        element = Element(kind=kind, location=location)
        key = (kind, location.model_dump_json())
        if key in self.seen:
            return
        self.seen.add(key)
        self.found.append(TextElement(element, text, private))


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """``(field, text)`` for every prose string under ``value``, skipping ``SKIPPED_KEYS``."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in SKIPPED_KEYS:
                yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _character_texts(full: CharacterFull) -> Iterator[tuple[str, str, bool]]:
    """``(field, text, private)`` for a character's own texts, by its document's paths."""
    yield "public_card.name", full.name or "", False
    yield "public_card.public_description", full.public_description or "", False
    for index, label in enumerate(full.labels):
        yield f"metadata.labels[{index}]", label, False
    for index, alias in enumerate(full.aliases):
        yield f"metadata.aliases[{index}]", alias, True
    definition = full.definition.model_dump(mode="json", exclude_none=True)
    for path, text in _strings(definition, "definition"):
        yield path, text, True
    identity = full.visual_identity
    if identity is not None:
        for name in ("base_appearance_prompt", "negative_prompt", "restricted_details_prompt"):
            yield f"visual_identity.{name}", getattr(identity, name) or "", True


def _media(collect: _Collector, media_id: str, row: Mapping[str, Any]) -> None:
    for name in MEDIA_FIELDS:
        value = row.get(name)
        collect.add(
            "media_caption",
            value if isinstance(value, str) else None,
            private=False,
            media_id=media_id,
            field=name,
        )
    metadata = row.get("generation_metadata")
    if isinstance(metadata, Mapping):
        for key in GENERATION_PROMPTS:
            value = metadata.get(key)
            collect.add(
                "media_caption",
                value if isinstance(value, str) else None,
                private=True,
                media_id=media_id,
                field=f"generation_metadata.{key}",
            )


def character_elements(full: CharacterFull, collect: _Collector) -> None:
    """A character's elements: its own texts as ``field`` and its images' (the primary, the
    gallery and the reference images) as ``media_caption``."""
    for path, text, private in _character_texts(full):
        collect.add("field", text, private=private, field=path)
    images = [full.images.primary, *full.images.gallery]
    if full.visual_identity is not None:
        images.extend(full.visual_identity.reference_images)
    for image in images:
        if image is not None:
            _media(collect, image.media_id, image.model_dump(mode="json"))


def _cast_character(collect: _Collector, role: str, full: CharacterFull) -> None:
    version: VersionLabel = full.version if full.version is not None else DRAFT
    for path, text, _private in _character_texts(full):
        collect.add(
            "cast_character",
            text,
            private=True,
            entry=full.id,
            version=version,
            role=role,
            field=path,
        )


def _scenario_kind(field_path: str, role: str | None, seed_id: str | None) -> ElementKind:
    if field_path.startswith("drivers.planned_reveals"):
        return "reveal"
    if seed_id is not None:
        return "seed"
    if role is not None:
        return "role"
    return "field"


def scenario_elements(draft: ScenarioDraft, collect: _Collector) -> None:
    """A scenario's own elements: the public card and labels (public), then every text
    #111's input check reviews, then the visual style (private)."""
    card = draft.public_card
    collect.add("field", card.name, private=False, field="public_card.name")
    collect.add(
        "field", card.public_description, private=False, field="public_card.public_description"
    )
    for index, label in enumerate(draft.metadata.labels):
        collect.add("field", label, private=False, field=f"metadata.labels[{index}]")
    for index, role in enumerate(draft.cast.roles):
        collect.add(
            "role",
            role.public_label,
            private=False,
            role=role.id,
            field=f"cast.roles[{index}].public_label",
        )
    for index, video in enumerate(card.media.videos):
        collect.add(
            "media_caption",
            video.caption,
            private=False,
            media_id=video.media_id,
            field=f"public_card.media.videos[{index}].caption",
        )
    for text_at in policy_texts(draft):
        public = text_at.field.endswith(".setup_description")
        collect.add(
            _scenario_kind(text_at.field, text_at.role, text_at.seed_id),
            text_at.text,
            private=not public,
            role=text_at.role,
            field=text_at.field,
            seed_id=text_at.seed_id,
        )
    for name in ("prompt", "negative_prompt"):
        collect.add(
            "field", getattr(draft.visual_style, name), private=True, field=f"visual_style.{name}"
        )


async def _media_rows(conn: AsyncConnection, media_ids: Sequence[str]) -> dict[str, Any]:
    wanted = sorted(set(media_ids))
    if not wanted:
        return {}
    rows = await conn.execute(
        select(
            media_assets.c.id,
            media_assets.c.alt_text,
            media_assets.c.caption,
            media_assets.c.generation_metadata,
        ).where(media_assets.c.id.in_(wanted))
    )
    return {str(row.id): dict(row._mapping) for row in rows}


def _scenario_media(draft: ScenarioDraft) -> list[str]:
    media = draft.public_card.media
    found = [media.cover] if media.cover is not None else []
    return [*found, *media.gallery, *(video.media_id for video in media.videos)]


def draft_of(full: ScenarioFull) -> ScenarioDraft:
    """The scenario document a ``full`` read shows."""
    return ScenarioDraft.model_validate(
        full.model_dump(mode="json", include=set(ScenarioDraft.model_fields))
    )


async def _read(read: Any, entry_id: str, version: VersionLabel | None) -> tuple[Any, VersionLabel]:
    if version == DRAFT:
        return await read(entry_id, draft=True), DRAFT
    if version is not None:
        found = await read(entry_id, version=version)
        return found, version
    try:
        found = await read(entry_id)
    except NotPublished:
        return await read(entry_id, draft=True), DRAFT
    return found, found.version


async def load_entry(
    db: AsyncEngine, entry_type: EntryType, entry_id: str, version: VersionLabel | None
) -> LoadedEntry:
    """The entry to review (see the module docstring): 404 for an unknown entry or version."""

    async def read_character(character_id: str, **options: Any) -> CharacterFull:
        found = await catalog_characters.get(
            db, character_id, projection=Projection.FULL, **options
        )
        if not isinstance(found, CharacterFull):  # pragma: no cover - projection=FULL
            raise TypeError("a full character read answered a card")
        return found

    if entry_type == "character":
        full, label = await _read(read_character, entry_id, version)
        collect = _Collector(entry_id, label)
        character_elements(full, collect)
        age = full.definition.true_age
        return LoadedEntry(
            entry=EntryRef(type="character", id=entry_id, version=label),
            elements=tuple(collect.found),
            true_ages=() if age is None else ((entry_id, age),),
        )

    async def read_scenario(scenario_id: str, **options: Any) -> ScenarioFull:
        found = await catalog_scenarios.get(db, scenario_id, projection=Projection.FULL, **options)
        if not isinstance(found, ScenarioFull):  # pragma: no cover - projection=FULL
            raise TypeError("a full scenario read answered a card")
        return found

    scenario, label = await _read(read_scenario, entry_id, version)
    draft = draft_of(scenario)
    collect = _Collector(entry_id, label)
    scenario_elements(draft, collect)
    async with db.connect() as conn:
        rows = await _media_rows(conn, _scenario_media(draft))
    for media_id in _scenario_media(draft):
        row = rows.get(media_id)
        if row is not None:
            _media(collect, media_id, row)
    ages: list[tuple[str, int]] = []
    unavailable: list[UnavailableCharacter] = []
    for _index, role, character in filled_roles(draft):
        try:
            member = await read_character(character.catalog_id, version=character.version)
        except CharacterNotFound, NotPublished:
            member = None
        if member is None or member.status == EntryStatus.ARCHIVED:
            unavailable.append(
                UnavailableCharacter(
                    role=role.id, entry=character.catalog_id, version=character.version
                )
            )
            continue
        _cast_character(collect, role.id, member)
        if member.definition.true_age is not None:
            ages.append((member.id, member.definition.true_age))
    return LoadedEntry(
        entry=EntryRef(type="scenario", id=entry_id, version=label),
        elements=tuple(collect.found),
        true_ages=tuple(ages),
        unavailable=tuple(unavailable),
    )


# -- deterministic concerns and the rows ------------------------------------------------------


def text_policies(policies: Iterable[Policy]) -> tuple[Policy, ...]:
    """The policies that apply to text (input or output), in order."""
    return tuple(p for p in policies if any(t in p.applies_to for t in TEXT_TARGETS))


def deterministic(policies: Iterable[Policy]) -> tuple[Policy, ...]:
    return tuple(p for p in text_policies(policies) if not isinstance(p.rule, ModelAssistedRule))


def _explanation(policy: Policy, text: str) -> str:
    span = matched_span(text, policy)
    found = f' (matched "{span[:SPAN_MAX]}")' if span else ""
    return f"{policy.name}: {policy.message}{found}"[:EXPLANATION_MAX]


def deterministic_concerns(
    elements: Sequence[TextElement], policies: Iterable[Policy]
) -> list[Concern]:
    """Every (deterministic policy, element) the element's text breaks under either text target,
    in element order then policy order: no model call."""
    checked = deterministic(policies)
    by_id = {policy.id: policy for policy in checked}
    concerns: list[Concern] = []
    for item in elements:
        broken: list[str] = []
        for target in TEXT_TARGETS:
            for violation in evaluate(item.text, checked, target):
                if violation.policy_id not in broken:
                    broken.append(violation.policy_id)
        for policy_id in sorted(broken, key=list(by_id).index):
            concerns.append(
                Concern(
                    policy_id=policy_id,
                    element=item.element,
                    explanation=_explanation(by_id[policy_id], item.text),
                )
            )
    return concerns


def row(
    *,
    rating_id: str,
    rating_name: str,
    rating_version: str,
    concerns: Sequence[Concern],
    error: RowError | None = None,
    truncated: bool = False,
    dropped: int = 0,
) -> SuggestionRow:
    """A row, its ``appropriate`` derived: ``false`` with a concern, ``null`` after a failed
    model call with none, else ``true``."""
    appropriate: bool | None = False if concerns else (None if error is not None else True)
    return SuggestionRow(
        rating_id=rating_id,
        rating_name=rating_name,
        rating_version=rating_version,
        appropriate=appropriate,
        concerns=list(concerns),
        error=error,
        truncated=truncated,
        dropped_concerns=dropped,
    )


def log_table(table: RatingSuggestion) -> None:
    """The INFO line of a suggestion: the entry's id, the row count and the latency only (never
    a concern, a text or a rating's content; invariant 3)."""
    log.info(
        "rating suggestion",
        entry_id=table.entry.id,
        rows=len(table.rows),
        elapsed_ms=table.elapsed_ms,
    )


__all__ = [
    "CHARACTER_UNAVAILABLE",
    "DRAFT",
    "Concern",
    "Element",
    "ElementKind",
    "ElementLocation",
    "EntryRef",
    "EntryType",
    "EvaluatedRating",
    "LoadedEntry",
    "RatingSuggestion",
    "RowError",
    "SuggestionRequest",
    "SuggestionRow",
    "TextElement",
    "UnavailableCharacter",
    "character_elements",
    "deterministic",
    "deterministic_concerns",
    "draft_of",
    "load_entry",
    "log_table",
    "row",
    "scenario_elements",
    "text_policies",
]
