"""What a story image may be built from: what its audience viewpoint perceived (FR-IMG-8,
FR-IMG-2, FR-IMG-5, NFR-PRIV-4; #263).

``story_perceived_inputs(conn, placement)`` reads the story at an image's position (a placement
``core.images.story_images.place_at`` checked) and returns plain data, built by allow-list, that
holds only what the audience viewpoint perceived:

- **Beats** (the anchor turn only: its active variant's beats through the position; a slot uses
  the whole turn N; earlier turns contribute only through the location and the cast). They are
  the turn as a history read returns it for the viewpoint (``core.history.filtered_turn_beats``,
  #177's audience filter): a character viewpoint gets what it perceived (an envelope as its
  envelope, "Jenni whispers something to Marcus"; an unperceived beat not at all), and never a
  ``think`` or a ``latent``; with perception ``none`` (Lite, and structured stories with shared
  knowledge) every beat less ``think`` and ``latent``. ``omniscient`` gets every beat but
  ``think`` (a ``latent`` included). Under Lite the beats are the player's input and, past it,
  the delivered message, exactly as the player was shown it.
- **The location**: the viewpoint character's own location as it holds it (its belief of its
  own ``located_at``, else the canon fact its viewpoint may read), by the place's name only,
  never canon's description of the place (which may hold a concealed feature); omitted when it
  holds none. ``omniscient``, and perception ``none``, use the scene's location (the player
  character's) with the scenario's description of it. Lite has no location.
- **The cast**: the characters the viewpoint perceives as present (those at its own location
  after the moves of the beats through the position; everyone when nobody is placed); under
  Lite the story characters the delivered text names. The viewpoint itself is in the cast
  unless the camera is its own ``first_person``. Each has an opaque label (``person 1`` …; names
  never enter the cast), its **apparent age band** (#170's ``age_band`` of its current age,
  computed here so no age crosses into the Orchestrator: a character viewpoint's own belief of
  the age wins, else the band of the current age with its third as "mid"), and its
  ``definition.appearance`` from the catalog version it was made from (authored ages redacted).
  **Identity** (#171): a character that is one side of a canon ``same_as`` with an entity that
  is not a story character (a disguise: "the masked stranger") is *unrecognised* unless the
  viewpoint holds that pairing (a canon fact its viewpoint reads, or its own belief; ``omniscient``
  and perception ``none`` hold every one). An unrecognised character is described only as the
  viewpoint perceives it, by the entity's name, and is named so in the beats too; none of its
  appearance, visual identity, seed or references is used. A recognised character's visual
  identity contributes its ``perceivable`` base appearance prompt, reference images (live media),
  add-ons, negative prompt and seed; ``restricted`` elements (references, add-ons and the
  restricted-details prompt) only for ``omniscient``.
- **The style and guidance**: the story's scenario's visual style and its rating's current image
  guidance (FR-SCN-11, FR-RATE-4).

**The protected match** (``viewpoint_matcher``): #148's protected-content index for the viewpoint
(``core.verify.protected.load_protected_index``: hidden canon, thunk candidates, hidden ``same_as``
pairings, other characters' beliefs and hidden States), plus the content of every active beat the
viewpoint did not perceive at content (unperceived beats, envelope-only utterances and other
characters' ``think`` and ``latent``, later turns included), as ``SHINGLE``-word shingles (#172's)
less every shingle of what it did perceive; an unperceived text of two or three words is one
whole-phrase entry, and a single word is not indexed (names stay covered by #148's alias match).
``omniscient`` has no protected index. Built per request at the current timeline revision.

Nothing here calls a model, and logs carry counts only (invariant 3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog import ages
from epistrel.core.enums import BeliefConfidenceLevel, OperationKind, OperationStatus
from epistrel.core.epistemic import perception
from epistrel.core.epistemic.context import age_band
from epistrel.core.erasure.matcher import fold
from epistrel.core.images.perceived import (
    PERCEIVABLE,
    perceivable_addons,
    redact_ages,
)
from epistrel.core.images.story_images import Placement, audience_character
from epistrel.core.membership import Position, active_operation
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT
from epistrel.core.tables import characters, events, media_assets, operations, stories
from epistrel.core.tables.characters import character_snapshots
from epistrel.core.tables.media import MediaState
from epistrel.core.tables.propositions import entities
from epistrel.core.tables.ratings import content_rating_versions, content_ratings
from epistrel.core.tables.story_images import CHARACTER_CAMERAS, CameraKind
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

RESTRICTED: Final = "restricted"
LABEL: Final = "person {}"
"""A cast member's opaque label: names never enter the cast (appearance text only)."""
SHINGLE: Final = 4
"""The word shingle the unperceived-beat index uses (#172's ``protected.SHINGLE``)."""
_THIRD = re.compile(r"\b(early|mid|late)\s+(?=[a-z]+ies\b)", re.IGNORECASE)
_DECADE = re.compile(
    r"\b(?:in\s+(?:his|her|their)\s+)?(twenties|thirties|forties|fifties|sixties)\b", re.IGNORECASE
)
_WORDS = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)
INPUT_KIND: Final = "player_input"
REPLY_KIND: Final = "message_rendered"
STRUCTURED_PREFIX: Final = "beat_"


# -- the result -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoryCharacter:
    """One cast member as the viewpoint perceives it (see the module docstring). Its
    ``character_id`` (``subject_ids``) goes only to the safety context and the asset's depicted
    subjects, never to the prompt."""

    character_id: str
    label: str
    age_band: str
    appearance: str
    recognised: bool
    base_appearance_prompt: str | None = None
    reference_image_ids: tuple[str, ...] = ()
    seed: int | None = None
    negative_prompt: str | None = None
    adapter_addons: tuple[Mapping[str, Any], ...] = ()
    subject_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoryPerceived:
    """A story image's perceived inputs (see the module docstring)."""

    story_id: str
    viewpoint: str
    lite: bool
    level: str
    beats: tuple[str, ...]
    location: str | None
    cast: tuple[StoryCharacter, ...]
    camera: Mapping[str, Any]
    camera_label: str | None
    visual_style: str | None
    style_negative: str | None
    rating_image_guidance: str

    @property
    def perceived_nothing(self) -> bool:
        """The viewpoint perceived nothing here: no beat, no cast, no location."""
        return not self.beats and not self.cast and self.location is None

    def by_id(self, character_id: str) -> StoryCharacter | None:
        return next((c for c in self.cast if c.character_id == character_id), None)


# -- small reads ----------------------------------------------------------------------------


def _group(document: Any, *path: str) -> Mapping[str, Any]:
    found = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _seed(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _story(conn: AsyncConnection, story_id: str) -> Any:
    return (
        await conn.execute(
            select(
                stories.c.content_rating_id,
                stories.c.scenario_snapshot,
                stories.c.timeline_revision,
            ).where(stories.c.id == story_id)
        )
    ).one()


async def rating_image_guidance(conn: AsyncConnection, rating_id: str | None) -> str:
    """The story rating's current image guidance (FR-RATE-4; ``""`` without one)."""
    if rating_id is None:
        return ""
    found = await conn.scalar(
        select(content_rating_versions.c.image_guidance)
        .join(
            content_ratings,
            content_ratings.c.current_version_id == content_rating_versions.c.version_id,
        )
        .where(content_ratings.c.id == rating_id)
    )
    return "" if found is None else str(found)


def story_style(snapshot: Any) -> tuple[str | None, str | None]:
    """The story's scenario's visual style: its prompt and negative prompt (FR-SCN-11)."""
    style = _group(snapshot, "visual_style")
    return _text(style.get("prompt")), _text(style.get("negative_prompt"))


async def style_and_guidance(
    conn: AsyncConnection, story_id: str
) -> tuple[str | None, str | None, str]:
    """``(visual style, style negative, rating image guidance)`` of the story: what every story
    image prompt carries, a consumer's included (FR-IMG-2, FR-RATE-4)."""
    story = await _story(conn, story_id)
    style, negative = story_style(story.scenario_snapshot)
    rating = None if story.content_rating_id is None else str(story.content_rating_id)
    return style, negative, await rating_image_guidance(conn, rating)


async def _anchor(conn: AsyncConnection, story_id: str, turn: int) -> str | None:
    """The operation of turn ``turn``'s active variant (``None`` before the first turn)."""
    if turn < 1:
        return None
    found = await conn.scalar(
        select(operations.c.operation_id).where(
            operations.c.story_id == story_id,
            operations.c.narrative_turn == turn,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
            active_operation(operations),
        )
    )
    return None if found is None else str(found)


def _line(item: Any, names: Mapping[str, str]) -> str:
    """One beat as the prompt's scene lists it: an envelope as its envelope, a ``say`` as who
    said what, a ``do`` as who did what, anything else as its text."""
    text = str(item.text).strip()
    if item.kind == REPLY_KIND or getattr(item, "fidelity", None) == "envelope":
        return text
    who = names.get(str(item.actor_character_id or ""))
    if who is None or item.channel in ("narration", "latent"):
        return text
    if item.channel == "say":
        return f"{who} says: {text}"
    return f"{who}: {text}"


async def _beats(
    conn: AsyncConnection, placement: Placement, names: Mapping[str, str]
) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
    """The viewpoint's beats of the anchor turn through the position as the scene lists them,
    whether the turn is a Lite one, and the beats' own texts (see the module docstring)."""
    from epistrel.core.history import filtered_turn_beats

    operation_id = await _anchor(conn, placement.story_id, placement.turn)
    if operation_id is None:
        return (), False, ()
    found = await filtered_turn_beats(
        conn, placement.story_id, operation_id, placement.audience_viewpoint
    )
    items = [i for i in found.items if i.channel != "think"]
    # A Lite turn records no structured beat (whatever the viewpoint perceived of it).
    lite = not await conn.scalar(
        select(
            exists().where(
                events.c.operation_id == operation_id,
                events.c.type.startswith(STRUCTURED_PREFIX),
            )
        )
    )
    kept: list[str] = []
    texts: list[str] = []
    for item in items:
        if not lite and item.kind == REPLY_KIND:
            continue  # a structured turn's assembled message restates its beats
        beat = int(item.beat or 0)
        if not placement.slot and beat > placement.beat:
            continue
        line = _line(item, names)
        if line:
            kept.append(line)
            texts.append(str(item.text))
    return tuple(kept), lite, tuple(texts)


# -- the cast -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Identity:
    """A recognised character's visual-identity elements (see the module docstring)."""

    base: str | None = None
    references: tuple[str, ...] = ()
    seed: int | None = None
    negative: str | None = None
    addons: tuple[Mapping[str, Any], ...] = ()


async def _live(conn: AsyncConnection, wanted: Sequence[str]) -> tuple[str, ...]:
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


async def _identity(
    conn: AsyncConnection, document: Any, true_age: int | None, *, omniscient: bool
) -> _Identity:
    identity = _group(document, "visual_identity")
    audiences = {PERCEIVABLE, RESTRICTED} if omniscient else {PERCEIVABLE}
    references = [
        str(item["media_id"])
        for item in identity.get("reference_images") or ()
        if isinstance(item, Mapping)
        and item.get("audience") in audiences
        and isinstance(item.get("media_id"), str)
    ]
    base = _text(identity.get("base_appearance_prompt"))
    if omniscient:
        restricted = _text(identity.get("restricted_details_prompt"))
        if restricted is not None:
            base = f"{base}; {restricted}" if base else restricted
    if base is not None:
        base = redact_ages(base, true_age) or None
    addons = perceivable_addons(identity)
    if omniscient:
        found = _group(identity, "adapter_params").get("addons")
        addons = tuple(
            dict(a)
            for a in (found if isinstance(found, list) else [])
            if isinstance(a, Mapping) and a.get("audience") in audiences
        )
    return _Identity(
        base=base,
        references=await _live(conn, references),
        seed=_seed(identity.get("seed")),
        negative=_text(identity.get("negative_prompt")),
        addons=addons,
    )


def _mid(band: str) -> str:
    """A band with its third as "mid" (the viewpoint does not know which third)."""
    return _THIRD.sub("mid ", band)


def believed_band(value: Any) -> str | None:
    """A believed age as a band: a number's #170 band; a decade without its third as that
    decade's "mid" ("thirties" → "mid thirties"); a text naming a band as written."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return age_band(int(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        return age_band(int(text))
    found = _THIRD.search(text)
    decade = _DECADE.search(text)
    if decade is None:
        return None
    word = decade.group(1).lower()
    if found is not None:
        return f"{found.group(1).lower()} {word}"
    return f"mid {word}"


async def _believed_age(
    conn: AsyncConnection, story_id: str, holder: str, subject: str
) -> str | None:
    from epistrel.core.epistemic import partitions
    from epistrel.core.propositions.vocabulary import AGE

    held = await partitions.partition(
        conn,
        story_id,
        holder,
        subjects=[subject],
        predicate=AGE,
        levels=[
            BeliefConfidenceLevel.SUSPECTED,
            BeliefConfidenceLevel.BELIEVED,
            BeliefConfidenceLevel.KNOWN,
        ],
        order="confidence",
        limit=1,
    )
    for belief in held:
        literal = belief.proposition.literal
        if literal is not None and belief.proposition.polarity.value == "affirmed":
            return believed_band(literal.value)
    return None


async def _present(
    conn: AsyncConnection, placement: Placement, cast: Sequence[str], level: str
) -> tuple[list[str], str | None, perception.SceneState | None]:
    """Who the viewpoint perceives as present, its place (an entity id) and the scene."""
    from epistrel.core.epistemic.scene import load_scene, load_scene_state

    if placement.slot:
        scene = await load_scene_state(conn, placement.story_id, placement.turn + 1)
    else:
        scene, staged = await load_scene(conn, placement.story_id, placement.turn)
        for beat in sorted(staged):
            if beat <= placement.beat:
                scene = scene.moved(staged[beat])
    viewer = audience_character(placement.audience_viewpoint)
    where = scene.location if viewer is None or level == perception.NONE else scene.at(viewer)
    if not scene.placements:
        return list(cast), where, scene
    if where is None:
        if viewer is None:
            return list(cast), None, scene
        return ([viewer] if viewer in cast else []), None, scene
    # Routing's own co-located set (#161): the viewpoint's location's occupants, or the scene's.
    here = perception.co_located_set(
        scene, None if viewer is None or level == perception.NONE else viewer
    )
    return [c for c in cast if c in here], where, scene


def _named(text: str, names: Mapping[str, str]) -> list[str]:
    """The characters ``names`` whose name (or a part of it) ``text`` names, whole words."""
    folded = fold(text)
    found: list[str] = []
    for character_id, name in names.items():
        parts = [name, *name.split()]
        if any(
            re.search(rf"(?<!\w){re.escape(fold(p))}(?!\w)", folded) for p in parts if len(p) > 1
        ):
            found.append(character_id)
    return found


async def _location_text(
    conn: AsyncConnection,
    placement: Placement,
    where: str | None,
    snapshot: Any,
    *,
    own: bool,
) -> str | None:
    """The location's name, with the scenario's description of it unless ``own`` (a character
    viewpoint's own location: its name only)."""
    viewer = audience_character(placement.audience_viewpoint)
    place = where
    if own and viewer is not None:
        place = await _held_location(conn, placement.story_id, viewer)
    if place is None:
        return None
    name = await conn.scalar(select(entities.c.canonical_name).where(entities.c.id == place))
    if name is None:
        return None
    text = f"the {name}"
    if own:
        return text
    for location in _group(snapshot, "world").get("locations") or ():
        if not isinstance(location, Mapping):
            continue
        if str(location.get("name") or "").casefold() == str(name).casefold():
            described = _text(location.get("description"))
            if described:
                return f"{text}: {described}"
    return text


async def _held_location(conn: AsyncConnection, story_id: str, viewer: str) -> str | None:
    """Where the viewpoint character holds that it is: its own belief, else the canon fact its
    viewpoint may read (the player character holds no partition)."""
    from epistrel.core.epistemic import partitions
    from epistrel.core.propositions.vocabulary import LOCATED_AT
    from epistrel.core.readset import KeyedLookup, StructuredReadSet

    held = await partitions.partition(
        conn,
        story_id,
        viewer,
        subjects=[viewer],
        predicate=LOCATED_AT,
        levels=[BeliefConfidenceLevel.BELIEVED, BeliefConfidenceLevel.KNOWN],
        order="confidence",
        limit=1,
    )
    for belief in held:
        if belief.proposition.object_id is not None:
            return str(belief.proposition.object_id)
    lookup = KeyedLookup(conn, StructuredReadSet())
    facts = await lookup.canon_at(
        story_id,
        predicate=LOCATED_AT,
        subjects=[viewer],
        viewpoint=f"{CHARACTER_VIEWPOINT_PREFIX}{viewer}",
    )
    current = [
        f
        for f in facts
        if f.proposition.polarity.value == "affirmed" and f.proposition.object_id is not None
    ]
    if not current:
        return None
    latest = max(current, key=lambda f: (f.narrative_turn or 0, f.beat or 0, str(f.id)))
    return str(latest.proposition.object_id)


async def _pairs(
    conn: AsyncConnection, story_id: str, holder: str, *, beliefs: bool, viewpoint: str
) -> frozenset[tuple[str, str]]:
    from epistrel.core.readset import KeyedLookup, StructuredReadSet
    from epistrel.core.resolution.identity import identities_for

    found = await identities_for(
        KeyedLookup(conn, StructuredReadSet()),
        story_id,
        holder,
        beliefs=beliefs,
        viewpoint=viewpoint,
    )
    return frozenset(found.pairs())


async def _disguises(
    conn: AsyncConnection,
    story_id: str,
    viewpoint: str,
    viewer: str | None,
    characters_: Mapping[str, Any],
    *,
    shared: bool,
) -> dict[str, str]:
    """Each story character the viewpoint does not recognise, and what it perceives it as: a
    character one side of a canon ``same_as`` (#171) with another entity (a disguise, "the masked
    stranger") whose pairing the viewpoint does not hold is seen as that entity, by its name.
    ``omniscient`` and perception ``none`` recognise everyone."""
    from epistrel.core.resolution.identity import CANON

    if shared:
        return {}
    canon_pairs = await _pairs(conn, story_id, CANON, beliefs=False, viewpoint=OMNISCIENT)
    if not canon_pairs:
        return {}
    held: frozenset[tuple[str, str]] = frozenset()
    if viewer is not None:
        held = await _pairs(
            conn,
            story_id,
            viewer,
            beliefs=viewer in characters_ and not bool(characters_[viewer].is_player),
            viewpoint=viewpoint,
        )
    unrecognised: dict[str, str] = {}
    for pair in sorted(canon_pairs):
        if pair in held:
            continue
        a, b = pair
        for character_id, other in ((a, b), (b, a)):
            if character_id in characters_ and other not in characters_:
                unrecognised.setdefault(character_id, other)
    if not unrecognised:
        return {}
    named = {
        str(r.id): str(r.canonical_name)
        for r in await conn.execute(
            select(entities.c.id, entities.c.canonical_name).where(
                entities.c.id.in_(sorted(set(unrecognised.values())))
            )
        )
    }
    return {c: _the(named.get(e, "a stranger")) for c, e in unrecognised.items()}


def _the(name: str) -> str:
    found = name.strip()
    if found.lower().startswith(("the ", "a ", "an ")):
        return found
    return f"the {found}"


async def story_perceived_inputs(conn: AsyncConnection, placement: Placement) -> StoryPerceived:
    """The story image's perceived inputs (see the module docstring)."""
    from epistrel.core.epistemic.scene import current_level

    story_id = placement.story_id
    viewpoint = placement.audience_viewpoint
    omniscient = viewpoint == OMNISCIENT
    viewer = audience_character(viewpoint)
    rows = {
        str(r.id): r
        for r in await conn.execute(
            select(characters.c.id, characters.c.name, characters.c.is_player, characters.c.ordinal)
            .where(characters.c.story_id == story_id)
            .order_by(characters.c.ordinal)
        )
    }
    order = list(rows)
    story = await _story(conn, story_id)
    level = await current_level(conn, story_id)
    shared = omniscient or level == perception.NONE
    disguised = await _disguises(conn, story_id, viewpoint, viewer, rows, shared=shared)
    # The viewpoint names an unrecognised character as what it perceives it as (#171).
    names = {c: disguised.get(c, str(r.name)) for c, r in rows.items()}
    beats, lite, texts = await _beats(conn, placement, names)
    location: str | None = None
    if lite:
        present = _named("\n".join(texts), {c: str(r.name) for c, r in rows.items()})
        if viewer is not None and viewer not in present:
            present.insert(0, viewer)
        present = [c for c in order if c in present]
    else:
        present, where, _scene = await _present(conn, placement, order, level)
        location = await _location_text(
            conn, placement, where, story.scenario_snapshot, own=not shared
        )
    camera = dict(placement.camera_pov)
    camera_subject = camera.get("character_id")
    if camera.get("kind") == CameraKind.FIRST_PERSON.value and camera_subject == viewer:
        present = [c for c in present if c != viewer]
    snapshots = {
        str(r.character_id): r.content
        for r in await conn.execute(
            select(
                character_snapshots.c.character_id,
                character_snapshots.c.content,
            ).where(character_snapshots.c.story_id == story_id)
        )
    }
    position = (
        Position(placement.turn, placement.variant, 1, 0, 0)
        if placement.slot
        else Position(placement.turn, placement.variant, 0, placement.beat, 0)
    )
    current = await ages.true_ages_at(conn, story_id, position)
    cast: list[StoryCharacter] = []
    for n, character_id in enumerate(present, start=1):
        document = snapshots.get(character_id)
        true_age = current.get(character_id)
        band = None
        if viewer is not None and not shared and viewer != character_id:
            band = await _believed_age(conn, story_id, viewer, character_id)
        if band is None:
            exact = age_band(true_age)
            band = (exact if shared or viewer == character_id else _mid(exact or "")) or ""
        disguise = disguised.get(character_id)
        if disguise is not None:
            # Unrecognised: what the viewpoint perceives (#171's descriptor), and none of the
            # character's own appearance, visual identity, seed or references.
            cast.append(
                StoryCharacter(
                    character_id=character_id,
                    label=LABEL.format(n),
                    age_band=band,
                    appearance=disguise,
                    recognised=False,
                    subject_ids=(character_id,),
                )
            )
            continue
        appearance = _text(_group(document, "definition").get("appearance")) or ""
        appearance = redact_ages(appearance, true_age) if appearance else ""
        identity = await _identity(conn, document, true_age, omniscient=omniscient)
        cast.append(
            StoryCharacter(
                character_id=character_id,
                label=LABEL.format(n),
                age_band=band,
                appearance=appearance,
                recognised=True,
                base_appearance_prompt=identity.base,
                reference_image_ids=identity.references,
                seed=identity.seed,
                negative_prompt=identity.negative,
                adapter_addons=identity.addons,
                subject_ids=(character_id,),
            )
        )
    label = None
    if camera_subject is not None:
        subject = next((c for c in cast if c.character_id == camera_subject), None)
        label = "an unseen person" if subject is None else subject.label
    style, style_negative = story_style(story.scenario_snapshot)
    rating = None if story.content_rating_id is None else str(story.content_rating_id)
    log.debug(
        "story image inputs read",
        story_id=story_id,
        beats=len(beats),
        cast=len(cast),
        unrecognised=len(disguised),
        located=location is not None,
    )
    return StoryPerceived(
        story_id=story_id,
        viewpoint=viewpoint,
        lite=lite,
        level=str(level),
        beats=beats,
        location=location,
        cast=tuple(cast),
        camera=camera,
        camera_label=label,
        visual_style=style,
        style_negative=style_negative,
        rating_image_guidance=await rating_image_guidance(conn, rating),
    )


def camera_perceived(perceived: StoryPerceived) -> bool:
    """Whether a character camera's subject is one the viewpoint perceives as present (itself
    included): FR-IMG-5's presence rule, as the viewpoint perceives it."""
    subject = perceived.camera.get("character_id")
    if perceived.camera.get("kind") not in {k.value for k in CHARACTER_CAMERAS}:
        return True
    viewer = audience_character(perceived.viewpoint)
    return subject == viewer or perceived.by_id(str(subject)) is not None


# -- the protected match --------------------------------------------------------------------


def _folded_words(text: str) -> list[str]:
    return [fold(m.group()).replace("\u2019", "'") for m in _WORDS.finditer(text)]


def _shingles(words: Sequence[str], n: int = SHINGLE) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


@dataclass(frozen=True, slots=True)
class UnperceivedIndex:
    """The viewpoint's unperceived-beat index (see the module docstring)."""

    shingles: frozenset[tuple[str, ...]] = frozenset()
    phrases: frozenset[tuple[str, ...]] = frozenset()

    def __len__(self) -> int:
        return len(self.shingles) + len(self.phrases)

    @classmethod
    def of(cls, unperceived: Iterable[str], perceived: Iterable[str]) -> UnperceivedIndex:
        seen_words = [_folded_words(t) for t in perceived]
        seen = set().union(*(_shingles(w) for w in seen_words)) if seen_words else set()
        seen_text = [" ".join(w) for w in seen_words]
        shingles: set[tuple[str, ...]] = set()
        phrases: set[tuple[str, ...]] = set()
        for text in unperceived:
            words = _folded_words(text)
            if len(words) >= SHINGLE:
                shingles |= _shingles(words) - seen
            elif len(words) >= 2:
                joined = " ".join(words)
                if not any(f" {joined} " in f" {t} " for t in seen_text):
                    phrases.add(tuple(words))
        return cls(frozenset(shingles), frozenset(phrases))

    def hit(self, text: str | None) -> bool:
        if not text:
            return False
        words = _folded_words(text)
        if self.shingles and _shingles(words) & self.shingles:
            return True
        joined = f" {' '.join(words)} "
        return any(f" {' '.join(p)} " in joined for p in self.phrases)


@dataclass(frozen=True, slots=True)
class ViewpointMatcher:
    """The protected match for one viewpoint (see the module docstring): ``hit(text)`` is true
    when ``text`` holds a protected term (#148) or repeats an unperceived beat."""

    viewpoint: str
    protected: Any = None
    unperceived: UnperceivedIndex = field(default_factory=UnperceivedIndex)

    def hit(self, text: str | None) -> bool:
        if not text:
            return False
        if self.protected is not None and self.protected.match(text):
            return True
        return self.unperceived.hit(text)

    def first_hit(self, fields: Mapping[str, str | None]) -> str | None:
        """The first of ``fields`` (name → text) that hits, by name; ``None`` when none does."""
        return next((name for name, value in fields.items() if self.hit(value)), None)


async def _active_turns(conn: AsyncConnection, story_id: str) -> list[str]:
    return [
        str(op)
        for op in await conn.scalars(
            select(operations.c.operation_id)
            .where(
                operations.c.story_id == story_id,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == OperationStatus.COMMITTED.value,
                active_operation(operations),
            )
            .order_by(operations.c.narrative_turn)
        )
    ]


async def unperceived_index(
    conn: AsyncConnection, story_id: str, viewpoint: str
) -> UnperceivedIndex:
    """Every active turn's beats the viewpoint did not perceive at content, against what it did
    (see the module docstring)."""
    from epistrel.core.history import filtered_turn_beats

    unperceived: list[str] = []
    perceived: list[str] = []
    for operation_id in await _active_turns(conn, story_id):
        everything = (await filtered_turn_beats(conn, story_id, operation_id, OMNISCIENT)).items
        mine = (await filtered_turn_beats(conn, story_id, operation_id, viewpoint)).items
        held = {
            (str(i.message_id), i.channel): i.text
            for i in mine
            if getattr(i, "fidelity", None) in (None, "content") and i.kind != REPLY_KIND
        }
        perceived += [str(i.text) for i in mine]
        for item in everything:
            if item.kind == REPLY_KIND:
                continue
            if held.get((str(item.message_id), item.channel)) != item.text:
                unperceived.append(str(item.text))
    return UnperceivedIndex.of(unperceived, perceived)


async def viewpoint_matcher(
    conn: AsyncConnection, story_id: str, viewpoint: str, *, max_terms: int | None = None
) -> ViewpointMatcher:
    """The viewpoint's protected match (see the module docstring): nothing for ``omniscient``."""
    from epistrel.core.resolution.context import load_index
    from epistrel.core.verify.protected import load_protected_index

    viewer = audience_character(viewpoint)
    if viewer is None:
        return ViewpointMatcher(viewpoint)
    index = await load_index(conn, story_id)
    kwargs: dict[str, Any] = {} if max_terms is None else {"max_terms": max_terms}
    protected = await load_protected_index(conn, story_id, viewer, index=index, **kwargs)
    unperceived = await unperceived_index(conn, story_id, viewpoint)
    log.debug(
        "story image protected match built",
        story_id=story_id,
        terms=0 if protected is None else len(protected),
        unperceived=len(unperceived),
    )
    return ViewpointMatcher(viewpoint, protected, unperceived)


__all__ = [
    "SHINGLE",
    "StoryCharacter",
    "StoryPerceived",
    "UnperceivedIndex",
    "ViewpointMatcher",
    "believed_band",
    "camera_perceived",
    "rating_image_guidance",
    "story_perceived_inputs",
    "story_style",
    "style_and_guidance",
    "unperceived_index",
    "viewpoint_matcher",
]
