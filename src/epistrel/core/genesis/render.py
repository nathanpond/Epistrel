"""The Lite prompt's two scenario blocks, and a catalog character's author notes (FR-LITE-1,
FR-SCN-7,9,11; #122).

A story created from a scenario plays under Lite with the scenario in its prompt. Every block
here is rendered from the story's own snapshots (``stories.scenario_snapshot``,
``character_snapshots.content``) and its cast, never from the catalog, so editing or
republishing a catalog entry after creation changes no prompt of the story. Nothing is stored:
``core.turns`` renders the blocks on every turn and the read set hashes them (#47).

**The scenario text** (``scenario_text``, the ``## Scenario`` block): the world description as
prose; the named places and things (``Name (also known as Y): description``); the seeded
memories grouped by holder (world memories under ``Everyone knows:``, a role's under
``<Name> knows:``, beliefs under ``<Name> believes:``, an unfilled optional role's under
``Other people``), each with its audience and disclosure as an instruction sentence; the
relationships (``<Name> is <Name>'s <relationship>``); the other names a role gives its
character; the player persona's note (``### Player: <name>``); each role's starting place; and
last ``Present at the start: <names> at <place>``. The block closes with the sentence that
scenario facts prevail over a character's own description (``PREVAIL``). A role seed that
asserts personality (#111's heuristic) is rendered as it stands, followed once per role by the
sentence that it yields to the character (``merge.yielding``): personality comes from the
character, facts from the scenario. Deferred-only items (story rules, State trackers, the
clock, scheduled events, a planned reveal's hidden truth) are not text here.

**The director notes** (``director_notes``, the ``## Author notes (not facts)`` block): the plot
hooks, objectives, director guidance, planned reveals (each hidden truth, who may learn it and
when), milestones and endings, under their own headings and the standing sentence that these
are intentions, not facts the characters know.

**A catalog character's author notes** (``definition_notes``): its private definition as prose
(appearance, personality, values and motivations, background, the five disposition axes as
fixed phrases, likes and dislikes, speech style, vocabulary, mannerisms, sample lines). Its true
age (FR-CAT-12), its visual identity, its safe fallback and its deflection style are left out:
the template renders the deflection style from the story's own column (#115).

The public card, the labels, the recommendation and the visual style are never rendered.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.scenario_models import (
    WORLD_HOLDER,
    ScenarioDraft,
    ScenarioRole,
    Seed,
)
from epistrel.core.genesis import merge
from epistrel.core.tables import characters, stories

SCENARIO_HEADING: Final = "## Scenario"
DIRECTOR_HEADING: Final = "## Author notes (not facts)"
PREVAIL: Final = (
    "Where these facts disagree with a character's own description, the scenario's facts prevail."
)
"""The sentence that closes the scenario text (FR-SCN-7 under Lite: no conflict detection, the
scenario's facts are rendered first and prevail)."""
DIRECTOR_STANDING: Final = "These are the author's intentions, not facts the characters know."
EVERYONE: Final = "Everyone knows:"
OTHER_PEOPLE: Final = "Other people:"
BELIEF_KINDS: Final = frozenset({"belief", "nested"})

DISPOSITION_PHRASES: Final[Mapping[str, Mapping[str, str]]] = {
    "threat_response": {
        "flees": "When threatened or challenged, they flee.",
        "yields": "When threatened or challenged, they give way.",
        "deflects": "When threatened or challenged, they deflect.",
        "stands": "When threatened or challenged, they stand their ground.",
        "attacks": "When threatened or challenged, they strike back.",
    },
    "dominance": {
        "very_submissive": "They defer to others almost always.",
        "submissive": "They tend to defer to others.",
        "balanced": "They neither dominate nor defer.",
        "dominant": "They tend to take charge.",
        "very_dominant": "They take charge of every room they are in.",
    },
    "honesty": {
        "very_deceitful": "They lie readily and often.",
        "deceitful": "They lie when it suits them.",
        "balanced": "They are as honest as most people.",
        "honest": "They are honest.",
        "very_honest": "They are scrupulously honest.",
    },
    "warmth": {
        "cold": "They are cold toward others.",
        "cool": "They are reserved and cool toward others.",
        "neutral": "They are neither warm nor cold.",
        "warm": "They are warm toward others.",
        "very_warm": "They are openly affectionate and warm.",
    },
    "caution": {
        "reckless": "They are reckless.",
        "bold": "They are bold and take risks.",
        "balanced": "They weigh risks like most people.",
        "careful": "They are careful.",
        "very_cautious": "They are very cautious.",
    },
}
"""Each disposition axis's settings (#115's closed five steps) as the fixed sentence the author
notes carry; a nuance follows its sentence."""


@dataclass(frozen=True, slots=True)
class StoryMember:
    """A story character as the renderer sees it: its id, its story name (a role's canonical
    name override already applied), its role, whether it is the player, and the player's
    persona copy (``characters.persona_snapshot``)."""

    id: str
    name: str
    role_id: str | None
    is_player: bool = False
    persona: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RenderedScenario:
    """The two blocks of one story's prompt (each ``""`` for a story with no scenario)."""

    scenario_text: str
    director_notes: str


# -- helpers --------------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _names(names: Sequence[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _sentence(text: str) -> str:
    stripped = text.strip()
    return stripped if stripped.endswith((".", "!", "?")) else f"{stripped}."


def _relationship(name: str) -> str:
    """A relationship name as prose: ``mother_of`` is ``mother``, ``best_friend`` is
    ``best friend``."""
    word = name.strip()
    if word.endswith("_of"):
        word = word[: -len("_of")]
    return word.replace("_", " ").strip() or name


@dataclass(frozen=True, slots=True)
class _Roles:
    """Who fills each role: the role's story member, or its public label when it is unfilled."""

    draft: ScenarioDraft
    by_role: Mapping[str, StoryMember]

    def name(self, role_id: str) -> str:
        member = self.by_role.get(role_id)
        if member is not None:
            return member.name
        for role in self.draft.cast.roles:
            if role.id == role_id:
                return role.public_label
        return role_id

    def filled(self, role: ScenarioRole) -> bool:
        return role.id in self.by_role


def _roles(draft: ScenarioDraft, cast: Iterable[StoryMember]) -> _Roles:
    return _Roles(draft, {m.role_id: m for m in cast if m.role_id is not None})


# -- seeds ----------------------------------------------------------------------------------


def audience_sentence(seed: Seed, holder: str | None, roles: _Roles) -> str | None:
    """The instruction a seed's audience and disclosure render as (``None``: nothing to say).

    ``self`` alone is ``Known only to <Name>``; named roles (and ``self``) are ``Known to
    <Names>``, a named group joins by its name; ``acquaintances`` is ``known to those who know
    <Name> well`` (not resolved to names under Lite); ``perceivable`` is ``evident to anyone who
    meets <Name>``; ``public`` says nothing. ``guarded`` adds ``<Name> does not volunteer it``,
    ``secret`` ``<Name> conceals it and may lie to protect it``."""
    audience = list(seed.audience or ())
    parts: list[str] = []
    if "public" not in audience:
        knowers: list[str] = []
        if holder is not None and "self" in audience:
            knowers.append(holder)
        for token in audience:
            if token in ("self", "public", "acquaintances", "perceivable"):
                continue
            knowers.append(
                roles.name(token)
                if any(r.id == token for r in roles.draft.cast.roles)
                else f"the {token}"
            )
        levels = [t for t in audience if t in ("acquaintances", "perceivable")]
        if knowers == [holder] and holder is not None and len(audience) == 1:
            parts.append(f"Known only to {holder}")
        elif knowers and not (knowers == [holder] and levels):
            parts.append(f"Known to {_names(knowers)}")
        if holder is not None and "acquaintances" in audience:
            parts.append(f"known to those who know {holder} well")
        if holder is not None and "perceivable" in audience:
            parts.append(f"evident to anyone who meets {holder}")
    if holder is not None and seed.disclosure == "guarded":
        parts.append(f"{holder} does not volunteer it")
    elif holder is not None and seed.disclosure == "secret":
        parts.append(f"{holder} conceals it and may lie to protect it")
    if not parts:
        return None
    first, *rest = parts
    return _sentence("; ".join([first[0].upper() + first[1:], *rest]))


def _seed_line(seed: Seed, holder: str | None, roles: _Roles) -> str:
    said = _sentence(seed.text)
    note = audience_sentence(seed, holder, roles)
    return f"- {said}" if note is None else f"- {said} ({note})"


def _held_seeds(draft: ScenarioDraft, roles: _Roles) -> list[str]:
    """The seeded memories, grouped by holder in scenario-world-then-role order."""
    lines: list[str] = []
    world = [s for s in draft.starting_state.seeded_memories if s.holder in (None, WORLD_HOLDER)]
    held: dict[str, list[Seed]] = {}
    for seed in draft.starting_state.seeded_memories:
        if seed.holder not in (None, WORLD_HOLDER):
            held.setdefault(str(seed.holder), []).append(seed)
    if world:
        lines.append(EVERYONE)
        lines.extend(_seed_line(s, None, roles) for s in world)
    others: list[str] = []
    for role in draft.cast.roles:
        seeds = [*held.pop(role.id, []), *role.seeded_memories]
        if not seeds:
            continue
        if not roles.filled(role):
            for seed in seeds:
                others.append(f"- {role.public_label}: {_sentence(seed.text)}")
            continue
        name = roles.name(role.id)
        known = [s for s in seeds if s.kind not in BELIEF_KINDS]
        believed = [s for s in seeds if s.kind in BELIEF_KINDS]
        if known:
            lines.append(f"{name} knows:")
            lines.extend(_seed_line(s, name, roles) for s in known)
        if believed:
            lines.append(f"{name} believes:")
            lines.extend(_seed_line(s, name, roles) for s in believed)
        if any(merge.asserts_personality(s.text) for s in role.seeded_memories):
            lines.append(merge.yielding(name))
    for seeds in held.values():  # a holder no role has: world facts
        others.extend(f"- {_sentence(s.text)}" for s in seeds)
    if others:
        lines.append(OTHER_PEOPLE)
        lines.extend(others)
    return lines


# -- the scenario text ----------------------------------------------------------------------


def _places(draft: ScenarioDraft) -> list[str]:
    lines: list[str] = []
    if draft.world.locations:
        lines.append("Places:")
        for location in draft.world.locations:
            description = _text(location.description)
            lines.append(
                f"- {location.name}: {description}" if description else f"- {location.name}"
            )
    if draft.world.entities:
        lines.append("Named things:")
        for entity in draft.world.entities:
            name = entity.name
            if entity.aliases:
                name = f"{name} (also known as {_names(list(entity.aliases))})"
            description = _text(entity.description)
            lines.append(f"- {name}: {description}" if description else f"- {name}")
    return lines


def _relationships(draft: ScenarioDraft, roles: _Roles) -> list[str]:
    lines = [
        f"- {roles.name(role.id)} is {roles.name(link.to_role)}'s {_relationship(link.name)}."
        for role in draft.cast.roles
        for link in role.relationships
    ]
    return ["Relationships:", *lines] if lines else []


def _other_names(draft: ScenarioDraft, roles: _Roles) -> list[str]:
    lines = [
        f"- {roles.name(role.id)} is also called {_names(list(role.name_overrides.aliases))}."
        for role in draft.cast.roles
        if role.name_overrides is not None and role.name_overrides.aliases and roles.filled(role)
    ]
    return ["Other names:", *lines] if lines else []


def age_band(age: int) -> str:
    """How an age reads to someone who does not know it: ``in their early twenties``."""
    if age < 13:
        return "a child"
    if age < 20:
        return "a teenager"
    decade = (age // 10) * 10
    words = {
        20: "twenties",
        30: "thirties",
        40: "forties",
        50: "fifties",
        60: "sixties",
        70: "seventies",
        80: "eighties",
        90: "nineties",
    }
    if decade not in words:
        return "very old"
    part = age % 10
    stage = "early" if part <= 3 else "mid" if part <= 6 else "late"
    return f"in their {stage} {words[decade]}"


def _acquaintances(draft: ScenarioDraft, roles: _Roles, role_id: str) -> list[str]:
    """The filled roles a relationship links to ``role_id`` (either way), in role order."""
    linked: list[str] = []
    for role in draft.cast.roles:
        if role.id == role_id or not roles.filled(role):
            continue
        outgoing = any(link.to_role == role_id for link in role.relationships)
        own = next((r for r in draft.cast.roles if r.id == role_id), None)
        incoming = own is not None and any(link.to_role == role.id for link in own.relationships)
        if outgoing or incoming:
            linked.append(roles.name(role.id))
    return linked


def _player_notes(draft: ScenarioDraft, roles: _Roles, cast: Sequence[StoryMember]) -> list[str]:
    lines: list[str] = []
    for member in cast:
        persona = member.persona
        if not member.is_player or persona is None:
            continue
        age = persona.get("age")
        gender = _text(persona.get("gender"))
        lines.append(f"### Player: {member.name}")
        lines.append(f"Name: {member.name}")
        if gender is not None:
            lines.append(f"Gender: {gender}")
        if isinstance(age, int):
            lines.append(f"Age: {age}")
        description = _text(persona.get("description"))
        if description is not None:
            lines.append(f"Description: {description}")
        if member.role_id is not None and isinstance(age, int):
            known = _acquaintances(draft, roles, member.role_id)
            band = age_band(age)
            if gender is not None:
                seen = f"{gender}, {band}"
            else:
                seen = band if band.startswith("a ") else f"someone {band}"
            if known:
                lines.append(
                    f"Known to {_names(known)}: {member.name}'s name and exact age; others "
                    f"perceive {seen}."
                )
            else:
                lines.append(f"Others perceive {seen}.")
    return lines


def _starts(draft: ScenarioDraft, roles: _Roles) -> list[str]:
    places = {location.id: location.name for location in draft.world.locations}
    lines = [
        f"- {roles.name(role.id)}: {places.get(role.starting_location, role.starting_location)}"
        for role in draft.cast.roles
        if role.starting_location is not None and roles.filled(role)
    ]
    found = ["Where each starts:", *lines] if lines else []
    opening = draft.starting_state.opening
    present = [roles.name(r) for r in opening.roles_present if r in roles.by_role]
    if present:
        line = f"Present at the start: {_names(present)}"
        if opening.starting_location is not None:
            line += f" at {places.get(opening.starting_location, opening.starting_location)}"
        found.append(_sentence(line))
    return found


def scenario_text(document: Mapping[str, Any], cast: Sequence[StoryMember]) -> str:
    """The ``## Scenario`` block of a story created from ``document`` (see the module
    docstring); ``""`` when it has nothing to say."""
    draft = ScenarioDraft.model_validate(document)
    roles = _roles(draft, cast)
    sections: list[list[str]] = []
    world = _text(draft.world.description)
    if world is not None:
        sections.append([world])
    for section in (
        _places(draft),
        _held_seeds(draft, roles),
        _relationships(draft, roles),
        _other_names(draft, roles),
        _player_notes(draft, roles, cast),
        _starts(draft, roles),
    ):
        if section:
            sections.append(section)
    if not sections:
        return ""
    body = "\n\n".join("\n".join(section) for section in sections)
    return f"{SCENARIO_HEADING}\n{body}\n\n{PREVAIL}"


# -- the director notes ---------------------------------------------------------------------


def director_notes(document: Mapping[str, Any], cast: Sequence[StoryMember]) -> str:
    """The ``## Author notes (not facts)`` block (see the module docstring); ``""`` when the
    scenario has no drivers."""
    draft = ScenarioDraft.model_validate(document)
    roles = _roles(draft, cast)
    drivers = draft.drivers
    sections: list[str] = []

    def listed(heading: str, items: Sequence[str]) -> None:
        if items:
            sections.append("\n".join([f"### {heading}", *(f"- {i}" for i in items)]))

    listed("Plot hooks", drivers.plot_hooks)
    listed("Objectives", drivers.objectives)
    guidance = _text(drivers.director_guidance)
    if guidance is not None:
        sections.append(f"### Guidance\n{guidance}")
    reveals: list[str] = []
    for reveal in drivers.planned_reveals:
        line = _sentence(reveal.truth.text)
        if reveal.allowed_roles:
            line += f" Who may learn it: {_names([roles.name(r) for r in reveal.allowed_roles])}."
        conditions = _text(reveal.conditions)
        if conditions is not None:
            line += f" When: {_sentence(conditions)}"
        reveals.append(line)
    listed("Planned reveals", reveals)
    listed("Milestones", drivers.milestones)
    listed("Endings", drivers.endings)
    if not sections:
        return ""
    return "\n\n".join([f"{DIRECTOR_HEADING}\n{DIRECTOR_STANDING}", *sections])


def render(document: Mapping[str, Any] | None, cast: Sequence[StoryMember]) -> RenderedScenario:
    """Both blocks for a story (empty for a story with no scenario snapshot)."""
    if document is None:
        return RenderedScenario(scenario_text="", director_notes="")
    return RenderedScenario(
        scenario_text=scenario_text(document, cast), director_notes=director_notes(document, cast)
    )


async def read_rendered(conn: AsyncConnection, story_id: str) -> RenderedScenario:
    """Both blocks of ``story_id``'s prompt, rendered now from its scenario snapshot and its
    cast (call inside the turn's read snapshot): what ``core.turns`` and the read set read."""
    document = await conn.scalar(
        select(stories.c.scenario_snapshot).where(stories.c.id == story_id)
    )
    if not isinstance(document, Mapping):
        return RenderedScenario(scenario_text="", director_notes="")
    rows = await conn.execute(
        select(
            characters.c.id,
            characters.c.name,
            characters.c.role_id,
            characters.c.is_player,
            characters.c.persona_snapshot,
        )
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    cast = [
        StoryMember(
            id=str(r.id),
            name=str(r.name),
            role_id=r.role_id,
            is_player=bool(r.is_player),
            persona=r.persona_snapshot if isinstance(r.persona_snapshot, Mapping) else None,
        )
        for r in rows
    ]
    return render(document, cast)


# -- a catalog character's author notes -----------------------------------------------------


def _items(values: Any) -> list[str]:
    return [v.strip() for v in values or () if isinstance(v, str) and v.strip()]


def definition_notes(name: str, content: Mapping[str, Any]) -> str:
    """A catalog character's author notes from its snapshot ``content`` (see the module
    docstring): the private definition as prose, never the public card, the labels, the true
    age, the visual identity or the deflection style."""
    found = content.get("definition")
    definition: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    lines: list[str] = [f"{name}."]
    for label, key in (
        ("Appearance", "appearance"),
        ("Personality", "personality"),
        ("Values and motivations", "values_motivations"),
        ("Background", "background_flavor"),
    ):
        value = _text(definition.get(key))
        if value is not None:
            lines.append(f"{label}: {value}")
    axes = definition.get("dispositions")
    if isinstance(axes, Mapping):
        phrases: list[str] = []
        for axis, settings in DISPOSITION_PHRASES.items():
            value = axes.get(axis)
            if not isinstance(value, Mapping):
                continue
            phrase = settings.get(str(value.get("setting")))
            if phrase is None:
                continue
            nuance = _text(value.get("nuance"))
            phrases.append(f"{phrase} {_sentence(nuance)}" if nuance else phrase)
        if phrases:
            lines.append("Dispositions: " + " ".join(phrases))
    likes = definition.get("likes_dislikes")
    if isinstance(likes, Mapping):
        for label, key in (("Likes", "likes"), ("Dislikes", "dislikes")):
            items = _items(likes.get(key))
            if items:
                lines.append(f"{label}: {', '.join(items)}")
    style = definition.get("interaction_style")
    if isinstance(style, Mapping):
        for label, key in (("Speech", "speech_style"), ("Vocabulary", "vocabulary")):
            value = _text(style.get(key))
            if value is not None:
                lines.append(f"{label}: {value}")
        for label, key in (("Mannerisms", "mannerisms"), ("Sample lines", "sample_dialogue")):
            items = _items(style.get(key))
            if items:
                lines.append(f"{label}: " + " | ".join(items))
    return "\n".join(lines)


__all__ = [
    "DIRECTOR_HEADING",
    "DIRECTOR_STANDING",
    "DISPOSITION_PHRASES",
    "PREVAIL",
    "SCENARIO_HEADING",
    "RenderedScenario",
    "StoryMember",
    "age_band",
    "audience_sentence",
    "definition_notes",
    "director_notes",
    "read_rendered",
    "render",
    "scenario_text",
]
