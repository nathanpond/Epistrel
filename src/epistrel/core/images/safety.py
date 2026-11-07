"""The image safety context: who an image prompt is about, with their true ages (FR-IMG-12;
docs/02 §4.13's safety path; #257).

``build_safety_context(conn, request, story_position)`` resolves, deterministically, the union
of:

- **(a)** the subjects the prompt builder depicted (``depicted``) or the request declared
  (``declared``): a story character by its id, or by the persona or catalog character it was
  made from; for a catalog image, the entry's character. An id the story does not know is a
  ``validation_error``;
- **(b)** every character named in the prompt, negative prompt, caption or alt text
  (``named``), found by ``core.erasure.matcher`` (ADR-154) under its caseless fold
  (ADR-155/156) over the terms ``core.erasure.index`` gives a subject (names, aliases and
  name parts, plus the parts of its own name another character shares, which erasure leaves
  out). A candidate mention counts as much as a certain one: safety fails closed and no
  operator confirms candidates here. A name, alias or name part two characters share resolves
  to both.
  The universe is the story's characters (personas included, as the player characters made
  from them), or, for a catalog image, the entry's character(s): a character entry's own, a
  scenario's locked and default roles';
- **(c)** for a story image, the characters authoritatively present at its position
  (``present``, ``present_at``): routing presence from ``core.epistemic.scene.load_scene`` and
  that turn's moves up to the beat, through ``perception.turn_audience`` at the story's
  perception level, never the consumer's roster. A slot ``N⁺k`` uses the scene at the end of
  turn N (the start of turn N+1). With perception ``none`` (Lite, or the module off) every
  character is present, and so is an unplaced one, as ``placed_elsewhere`` rules.

Each subject carries its **current** true age (FR-CAT-12) at the image's position
(``core.catalog.ages``; a catalog image uses the version's age as written) and its
classification against the live ``minor_age_threshold`` (strict less-than). A depicted person
who resolves to no character (a story entity not yet modeled, ``modeling_deferred``, an unknown
name, "a young girl") is a **figure**; Core leaves figures out, and the Orchestrator's figure
classification adds them (``with_figures``), indeterminate counting as minor.

Labels are opaque (FR-IMG-13): ``subject_1`` … in order of first mention in the texts (the
prompt first), then the unmentioned subjects in creation order; figures are ``figure_1`` ….
The context holds character ids and ages and goes only to prompt-policy evaluation and the
image check (``image_jobs.safety_context``, never read back by any view), never to the
generation path (the import-linter contract ``images-generation-path-has-no-ages``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog import ages
from epistrel.core.catalog.checks.character_documents import name_of, rating_id_of
from epistrel.core.config_changes import read_settings
from epistrel.core.epistemic.perception import NONE, turn_audience
from epistrel.core.epistemic.scene import current_level, load_scene, load_scene_state
from epistrel.core.erasure import index
from epistrel.core.erasure.matcher import matcher, nfc
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.tables import characters, personas
from epistrel.core.tables.catalog import character_catalog, character_versions, scenario_catalog

type Classification = Literal["adult", "minor", "indeterminate"]
type Source = Literal["declared", "depicted", "named", "present", "figure"]

SUBJECT_PREFIX = "subject_"
FIGURE_PREFIX = "figure_"
MINOR_CLASSES: frozenset[str] = frozenset({"minor", "indeterminate"})
"""Classifications an age-conditioned policy counts as a minor (indeterminate fails closed)."""
SOURCE_RANK: Mapping[str, int] = {"declared": 0, "depicted": 0, "named": 1, "present": 2}
"""A subject found several ways keeps the first (a declaration, then a name, then presence)."""
TEXT_FIELDS = ("prompt", "negative_prompt", "caption", "alt_text")


@dataclass(frozen=True, slots=True)
class DeclaredSubject:
    """A subject the request declared or the prompt builder depicted: a story character's id
    (or the persona's or catalog character's it was made from), with the visual descriptors
    the prompt uses for it."""

    character_id: str
    visual_descriptors: tuple[str, ...] = ()
    depicted: bool = False
    """``True``: the prompt builder depicted it (#259); ``False``: the request declared it."""


@dataclass(frozen=True, slots=True)
class SafetyRequest:
    """What an image request gives the safety path: its kind and target, its texts and its
    declared subjects."""

    kind: Literal["story", "catalog"]
    prompt: str
    negative_prompt: str | None = None
    caption: str | None = None
    alt_text: str | None = None
    subjects: tuple[DeclaredSubject, ...] = ()
    story_id: str | None = None
    viewpoint: str | None = None
    entry_kind: str | None = None
    entry_id: str | None = None

    def texts(self) -> tuple[str, ...]:
        """The texts names are looked for in, the prompt first (empty ones as ``""``)."""
        return tuple(getattr(self, name) or "" for name in TEXT_FIELDS)


@dataclass(frozen=True, slots=True)
class ResolvedSubject:
    """One subject of the safety context. ``character_id`` and ``true_age`` are ``None`` for a
    figure; ``classification`` is ``None`` only for a figure Core has not classified."""

    label: str
    character_id: str | None
    true_age: int | None
    classification: Classification | None
    source: Source
    visual_descriptors: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    """How the texts name it, as written (each a span of the request's own texts)."""

    @property
    def minor(self) -> bool:
        return self.classification in MINOR_CLASSES

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "character_id": self.character_id,
            "true_age": self.true_age,
            "classification": self.classification,
            "source": self.source,
            "visual_descriptors": list(self.visual_descriptors),
            "mentions": list(self.mentions),
        }

    @classmethod
    def from_json(cls, found: Mapping[str, Any]) -> ResolvedSubject:
        return cls(
            label=str(found["label"]),
            character_id=found.get("character_id"),
            true_age=found.get("true_age"),
            classification=found.get("classification"),
            source=found["source"],
            visual_descriptors=tuple(found.get("visual_descriptors") or ()),
            mentions=tuple(found.get("mentions") or ()),
        )


@dataclass(frozen=True, slots=True)
class Figure:
    """A depicted person who resolves to no character, as the figure classification found it."""

    descriptor: str
    classification: Classification


@dataclass(frozen=True, slots=True)
class SafetyContext:
    """Every resolved subject (figures last), the live threshold it was classified against and,
    for a catalog image, the entry's content rating (its policies join the effective set)."""

    subjects: tuple[ResolvedSubject, ...] = ()
    threshold: int = 18
    rating_id: str | None = None
    kind: Literal["story", "catalog"] = "story"

    @property
    def characters(self) -> tuple[ResolvedSubject, ...]:
        return tuple(s for s in self.subjects if s.character_id is not None)

    @property
    def figures(self) -> tuple[ResolvedSubject, ...]:
        return tuple(s for s in self.subjects if s.character_id is None)

    @property
    def character_ids(self) -> tuple[str, ...]:
        return tuple(s.character_id for s in self.subjects if s.character_id is not None)

    @property
    def has_minor(self) -> bool:
        """Whether a subject is a minor, or a figure is indeterminate (FR-IMG-12)."""
        return any(s.minor for s in self.subjects)

    def with_figures(self, figures: Iterable[Figure]) -> SafetyContext:
        """This context with ``figures`` appended as ``figure_1`` … (replacing any before)."""
        found = tuple(
            ResolvedSubject(
                label=f"{FIGURE_PREFIX}{n}",
                character_id=None,
                true_age=None,
                classification=figure.classification,
                source="figure",
                visual_descriptors=(figure.descriptor,) if figure.descriptor else (),
            )
            for n, figure in enumerate(figures, start=1)
        )
        return replace(self, subjects=(*self.characters, *found))

    def with_descriptors(self, descriptors: Mapping[str, Sequence[str]]) -> SafetyContext:
        """This context with each labelled subject's visual descriptors extended by
        ``descriptors[label]`` (a consumer prompt's, from the figure classification)."""
        return replace(
            self,
            subjects=tuple(
                replace(
                    s,
                    visual_descriptors=tuple(
                        dict.fromkeys((*s.visual_descriptors, *descriptors.get(s.label, ())))
                    ),
                )
                for s in self.subjects
            ),
        )

    def as_json(self) -> dict[str, Any]:
        """The ``image_jobs.safety_context`` document."""
        return {
            "kind": self.kind,
            "threshold": self.threshold,
            "rating_id": self.rating_id,
            "subjects": [s.as_json() for s in self.subjects],
        }

    @classmethod
    def from_json(cls, found: Mapping[str, Any]) -> SafetyContext:
        return cls(
            subjects=tuple(ResolvedSubject.from_json(s) for s in found.get("subjects") or ()),
            threshold=int(found.get("threshold", 18)),
            rating_id=found.get("rating_id"),
            kind=found.get("kind", "story"),
        )


def classify(true_age: int | None, threshold: int) -> Classification | None:
    """A known character's classification: a minor when younger than ``threshold``."""
    if true_age is None:
        return None
    return "minor" if true_age < threshold else "adult"


# -- resolution -----------------------------------------------------------------------------


@dataclass(slots=True)
class _Candidate:
    character_id: str
    ordinal: int
    true_age: int | None
    names: tuple[str, ...]
    parts: tuple[str, ...]
    aliases_of: tuple[str, ...] = ()
    """Other ids a declaration may name it by (its persona, its catalog character)."""
    source: str | None = None
    first: tuple[int, int] | None = None
    mentions: list[str] = field(default_factory=list)
    descriptors: list[str] = field(default_factory=list)

    def found(self, source: str) -> None:
        if self.source is None or SOURCE_RANK[source] < SOURCE_RANK[self.source]:
            self.source = source


def _scan(candidates: Sequence[_Candidate], texts: Sequence[str]) -> None:
    """Mark every candidate the texts name (certain and candidate mentions alike)."""
    for candidate in candidates:
        look = matcher(candidate.names, candidate.parts)
        for at, value in enumerate(texts):
            if not value:
                continue
            text = nfc(value)
            for mention in look.mentions(value):
                candidate.found("named")
                where = (at, mention.start)
                if candidate.first is None or where < candidate.first:
                    candidate.first = where
                span = text[mention.start : mention.end]
                if span not in candidate.mentions:
                    candidate.mentions.append(span)


def _declare(candidates: Sequence[_Candidate], declared: Sequence[DeclaredSubject]) -> None:
    by_id: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        for key in (candidate.character_id, *candidate.aliases_of):
            by_id.setdefault(key, []).append(candidate)
    for index_, subject in enumerate(declared):
        found = by_id.get(subject.character_id)
        if not found:
            raise InvalidRequest(
                "a depicted subject names no character of the image's target",
                {"field": f"body.depicted_subjects[{index_}]"},
            )
        for candidate in found:
            candidate.found("depicted" if subject.depicted else "declared")
            candidate.descriptors.extend(
                d for d in subject.visual_descriptors if d not in candidate.descriptors
            )


def _resolved(candidates: Sequence[_Candidate], threshold: int) -> tuple[ResolvedSubject, ...]:
    chosen = [c for c in candidates if c.source is not None]
    mentioned = sorted((c for c in chosen if c.first is not None), key=lambda c: c.first or (0, 0))
    rest = sorted((c for c in chosen if c.first is None), key=lambda c: c.ordinal)
    return tuple(
        ResolvedSubject(
            label=f"{SUBJECT_PREFIX}{n}",
            character_id=c.character_id,
            true_age=c.true_age,
            # A character with no age on record (a catalog draft without one) fails closed.
            classification=classify(c.true_age, threshold) or "indeterminate",
            source=c.source,  # type: ignore[arg-type]
            visual_descriptors=tuple(c.descriptors),
            mentions=tuple(c.mentions),
        )
        for n, c in enumerate([*mentioned, *rest], start=1)
    )


def image_position(origin: Mapping[str, Any]) -> Position:
    """A story image's five-column position from its origin: ``origin.position`` when given
    (``{turn, variant, slot_k, beat, step}``), else the end of its target turn and variant (every
    beat's moves, so presence errs toward applying)."""
    given = origin.get("position")
    if isinstance(given, Mapping) and given.get("turn") is not None:
        return Position(
            int(given["turn"]),
            int(given.get("variant") or 0),
            int(given.get("slot_k") or 0),
            int(given.get("beat") or 0),
            int(given.get("step") or 0),
        )
    return Position(
        int(origin.get("turn") or 0), int(origin.get("variant") or 0), 0, POSITION_MAX, 0
    )


async def present_at(
    conn: AsyncConnection,
    story_id: str,
    position: Position,
    cast: Iterable[str],
    actors: Iterable[str] = (),
) -> frozenset[str]:
    """The characters of ``cast`` authoritatively present at ``position`` (see the module
    docstring): everyone with perception ``none``; otherwise everyone but those routing places
    outside the audience of the position's scene, the ``actors`` (the image's viewpoint and its
    resolved subjects) included as #541's actors are."""
    everyone = frozenset(cast)
    level = await current_level(conn, story_id)
    if level == NONE:
        return everyone
    if position.slot_k:
        scene = await load_scene_state(conn, story_id, position.turn + 1)
        moves: dict[int, tuple[tuple[str, str | None], ...]] = {}
    else:
        scene, staged = await load_scene(conn, story_id, position.turn)
        moves = {beat: step for beat, step in staged.items() if beat <= position.beat}
    wanted = tuple(a for a in dict.fromkeys(actors) if a in scene.characters)
    if scene.location is None and all(scene.at(a) is None for a in wanted):
        return everyone
    audience = turn_audience(scene, wanted, moves, (), level)
    return frozenset(c for c in everyone if c not in scene.placements or c in audience)


async def _story_candidates(
    conn: AsyncConnection, story_id: str, position: Position
) -> list[_Candidate]:
    rows = {
        str(r.id): r
        for r in await conn.execute(
            select(
                characters.c.id,
                characters.c.ordinal,
                characters.c.name,
                characters.c.persona_id,
                characters.c.source_catalog_id,
            ).where(characters.c.story_id == story_id)
        )
    }
    true_ages = await ages.true_ages_at(conn, story_id, position)
    found: list[_Candidate] = []
    for subject in await index.story_subjects(conn, story_id):
        row = rows.get(subject.id)
        if row is None:
            continue
        found.append(
            _Candidate(
                character_id=subject.id,
                ordinal=int(row.ordinal),
                true_age=true_ages.get(subject.id),
                names=subject.terms,
                # Every part of its own name, shared ones too: a first name two characters share
                # resolves to both (erasure leaves shared parts out; safety fails closed).
                parts=tuple(dict.fromkeys((*subject.parts, *index.name_parts(str(row.name))))),
                aliases_of=tuple(
                    str(v) for v in (row.persona_id, row.source_catalog_id) if v is not None
                ),
            )
        )
    return sorted(found, key=lambda c: c.ordinal)


def _aliases(document: Mapping[str, Any] | None) -> list[str]:
    found = (document or {}).get("metadata")
    aliases = found.get("aliases") if isinstance(found, Mapping) else None
    return [a for a in aliases or () if isinstance(a, str)]


def _catalog_candidate(
    ordinal: int, entry_id: str, document: Mapping[str, Any] | None
) -> _Candidate:
    name = name_of(document)
    names = [*([name] if name else []), *_aliases(document)]
    return _Candidate(
        character_id=entry_id,
        ordinal=ordinal,
        true_age=ages.catalog_true_age(document),
        names=tuple(names),
        parts=tuple(index.name_parts(name)) if name else (),
    )


async def _catalog_candidates(
    conn: AsyncConnection, entry_kind: str | None, entry_id: str | None
) -> tuple[list[_Candidate], str | None, bool]:
    """The entry's character(s), its rating, and whether the entry's own character is always a
    subject (a character entry's images always depict it, FR-IMG-9)."""
    if entry_id is None:
        return [], None, False
    if entry_kind == "character":
        draft = await conn.scalar(
            select(character_catalog.c.draft).where(character_catalog.c.id == entry_id)
        )
        if draft is None:
            return [], None, False
        return [_catalog_candidate(0, entry_id, draft)], rating_id_of(draft), True
    draft = await conn.scalar(
        select(scenario_catalog.c.draft).where(scenario_catalog.c.id == entry_id)
    )
    if not isinstance(draft, Mapping):
        return [], None, False
    cast = draft.get("cast")
    roles = cast.get("roles") if isinstance(cast, Mapping) else None
    found: list[_Candidate] = []
    for ordinal, role in enumerate(roles if isinstance(roles, list) else []):
        if not isinstance(role, Mapping) or role.get("fill") not in ("locked", "default"):
            continue
        character = role.get("character")
        if not isinstance(character, Mapping) or not isinstance(character.get("catalog_id"), str):
            continue
        catalog_id = str(character["catalog_id"])
        version = character.get("version")
        if version is None:
            version = await conn.scalar(
                select(character_catalog.c.current_version).where(
                    character_catalog.c.id == catalog_id
                )
            )
        document: Any = None
        if version is not None:
            document = await conn.scalar(
                select(character_versions.c.content).where(
                    character_versions.c.catalog_id == catalog_id,
                    character_versions.c.version == int(version),
                )
            )
        if document is None:
            document = await conn.scalar(
                select(character_catalog.c.draft).where(character_catalog.c.id == catalog_id)
            )
        found.append(_catalog_candidate(ordinal, catalog_id, document))
    return found, rating_id_of(draft), False


async def build_safety_context(
    conn: AsyncConnection, request: SafetyRequest, story_position: Position | None
) -> SafetyContext:
    """The request's safety context without figures (see the module docstring)."""
    threshold = (await read_settings(conn)).minor_age_threshold
    texts = request.texts()
    if request.kind == "catalog":
        candidates, rating_id, own = await _catalog_candidates(
            conn, request.entry_kind, request.entry_id
        )
        if own:
            for candidate in candidates:
                candidate.found("depicted")
        _declare(candidates, request.subjects)
        _scan(candidates, texts)
        return SafetyContext(_resolved(candidates, threshold), threshold, rating_id, kind="catalog")
    if request.story_id is None:
        raise InvalidRequest("a story image names its story", {"field": "origin.story_id"})
    from epistrel.core.world_clock import HEAD  # here: the clock reads States, which import Core

    position = story_position or HEAD
    candidates = await _story_candidates(conn, request.story_id, position)
    _declare(candidates, request.subjects)
    _scan(candidates, texts)
    if story_position is not None:
        actors = [c.character_id for c in candidates if c.source is not None]
        if request.viewpoint is not None:
            actors.insert(0, request.viewpoint)
        present = await present_at(
            conn, request.story_id, story_position, [c.character_id for c in candidates], actors
        )
        for candidate in candidates:
            if candidate.character_id in present:
                candidate.found("present")
    return SafetyContext(_resolved(candidates, threshold), threshold, None, kind="story")


# -- uploads (#258) ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadSubject:
    """A subject an upload declares (``core.media.assets.DepictedSubject``): its kind
    (``character``, ``catalog_character`` or ``persona``) and id."""

    kind: str
    id: str


async def _declared_candidate(
    conn: AsyncConnection, ordinal: int, subject: UploadSubject
) -> _Candidate:
    """A declared subject as a candidate with its true age (``None``, so indeterminate, when it
    does not exist yet: an upload may declare a subject created later, and safety fails closed)."""
    if subject.kind == "character":
        row = (
            await conn.execute(
                select(characters.c.story_id, characters.c.name).where(
                    characters.c.id == subject.id
                )
            )
        ).one_or_none()
        if row is not None:
            from epistrel.core.world_clock import HEAD  # here: the clock reads States

            age = (await ages.true_ages_at(conn, str(row.story_id), HEAD)).get(subject.id)
            name = str(row.name)
            return _Candidate(subject.id, ordinal, age, (name,), tuple(index.name_parts(name)))
    elif subject.kind == "catalog_character":
        version = await conn.scalar(
            select(character_catalog.c.current_version).where(character_catalog.c.id == subject.id)
        )
        document: Any = None
        if version is not None:
            document = await conn.scalar(
                select(character_versions.c.content).where(
                    character_versions.c.catalog_id == subject.id,
                    character_versions.c.version == int(version),
                )
            )
        if document is None:
            document = await conn.scalar(
                select(character_catalog.c.draft).where(character_catalog.c.id == subject.id)
            )
        if document is not None:
            return _catalog_candidate(ordinal, subject.id, document)
    elif subject.kind == "persona":
        row = (
            await conn.execute(
                select(personas.c.name, personas.c.age).where(personas.c.id == subject.id)
            )
        ).one_or_none()
        if row is not None:
            name = str(row.name)
            return _Candidate(
                subject.id, ordinal, int(row.age), (name,), tuple(index.name_parts(name))
            )
    return _Candidate(subject.id, ordinal, None, (), ())


async def build_upload_safety_context(
    conn: AsyncConnection,
    declared: Sequence[UploadSubject],
    *,
    caption: str | None,
    alt_text: str | None,
) -> SafetyContext:
    """An uploaded image's safety context (FR-IMG-3,12; #258): its declared subjects, each with
    its true age (a story character's current one, a catalog character's as written, a persona's
    age; a subject that does not exist yet is indeterminate), plus every catalog character or
    persona its caption or alt text names (``index.catalog_subjects``, as the upload's subject
    index finds them). An upload has no story position, so no presence."""
    threshold = (await read_settings(conn)).minor_age_threshold
    texts = (caption or "", alt_text or "")
    candidates: list[_Candidate] = []
    seen: set[str] = set()
    for subject in dict.fromkeys(declared):
        candidate = await _declared_candidate(conn, len(candidates), subject)
        candidate.found("declared")
        candidates.append(candidate)
        seen.add(subject.id)
    if any(texts):
        for found in await index.catalog_subjects(conn):
            look = found.matcher()
            if found.id in seen or not any(look.mentions(t) for t in texts if t):
                continue
            candidate = await _declared_candidate(
                conn, len(candidates), UploadSubject(found.kind.value, found.id)
            )
            candidate.names, candidate.parts = found.terms, found.parts
            candidates.append(candidate)
            seen.add(found.id)
    _scan(candidates, texts)
    return SafetyContext(_resolved(candidates, threshold), threshold, None, kind="catalog")


__all__ = [
    "FIGURE_PREFIX",
    "MINOR_CLASSES",
    "SUBJECT_PREFIX",
    "Classification",
    "DeclaredSubject",
    "Figure",
    "ResolvedSubject",
    "SafetyContext",
    "SafetyRequest",
    "Source",
    "UploadSubject",
    "build_safety_context",
    "build_upload_safety_context",
    "classify",
    "image_position",
    "present_at",
]
