"""Q-06's story world (#913): a harbour town whose people are tied by relationships, and the
scene a relationship-recall case plays in.

**The world** (``HARBOUR``) is the Gull, Gullhaven's inn: the player Wren, back after years
away, asks the innkeeper Maren about the town's people. Eight more characters are tied by
``RELATIONSHIPS``, objective canon facts on the relationships projection (``married_to``,
``parent_of``, ``sibling_of``, ``friend_of``, ``enemy_of``), three of them Maren's own. No
character's definition mentions a relationship, so the relationships reach a render only
through the engine's reads.

**The scene** (``build_scene``) is made through the engine's own paths, as an author would: a
scratch story of the cast on the run's preset (``scenes.structured``), Wren and Maren placed in
the inn (the others elsewhere), then every relationship as an objective fact.

In a **deep** case, the person the question asks about first knows ``TOWNSFOLK`` (24 people of
the town, plain entities): more current canon as a subject than retrieval reads about one
entity (``CANON_PER_ENTITY``, 20), so the canon arm is full of acquaintances and carries no
relationship, as in a long-running story. In a **shallow** case the arm may carry a
relationship stored with the asked person as its subject (a symmetric fact is stored with the
lower entity id as subject); ``canon_carries`` says whether it can. Every author fact is also
a semantic memory, which retrieval's alias and lexical arms may find at either depth.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from epistrel.core.mutation import FactRequest
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.evals import scenes
from epistrel.evals.fixtures import Person, Scene
from epistrel.evals.scenes import EntitySpec

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

PLACE = "the Gull"
PLAYER = Person(
    "Wren", 34, "A sailor home in Gullhaven after ten years at sea: curious, a little rusty."
)
SPEAKER = "Maren"
"""The character every case asks and renders. In a render's retrieval she is the speaker, so
her own relationships stay in a ``message``-scoped profile."""
CAST: tuple[Person, ...] = (
    Person("Maren", 52, "The innkeeper of the Gull: talkative, shrewd, hears everything."),
    Person("Bobby", 38, "A fisherman who owns the blue boat at the end of the quay."),
    Person("Carol", 36, "The town's baker, up before anyone else."),
    Person("Odile", 64, "A net-mender with sharp eyes and a sharper tongue."),
    Person("Silas", 45, "A fisherman with the second-best catch in the harbour."),
    Person("Tamsin", 29, "The schoolteacher, new to the job and keen."),
    Person("Hugo", 31, "The blacksmith, broad and slow to speak."),
    Person("Ines", 30, "The midwife, always on her way somewhere."),
    Person("Jory", 58, "The retired harbourmaster, who still walks the quay every morning."),
)


@dataclass(frozen=True, slots=True)
class Relationship:
    """One objective relationship fact: ``subject`` ``predicate`` ``object`` (core names)."""

    subject: str
    predicate: str
    object: str

    @property
    def symmetric(self) -> bool:
        return CORE_BY_NAME[self.predicate].symmetric

    def touches(self, name: str) -> bool:
        return name in (self.subject, self.object)

    def other(self, name: str) -> str:
        return self.object if name == self.subject else self.subject


RELATIONSHIPS: tuple[Relationship, ...] = (
    Relationship("Bobby", "married_to", "Carol"),
    Relationship("Odile", "parent_of", "Bobby"),
    Relationship("Silas", "enemy_of", "Bobby"),
    Relationship("Tamsin", "sibling_of", "Hugo"),
    Relationship("Hugo", "married_to", "Ines"),
    Relationship("Ines", "friend_of", "Carol"),
    Relationship("Jory", "parent_of", "Tamsin"),
    Relationship("Jory", "parent_of", "Hugo"),
    Relationship("Maren", "friend_of", "Jory"),
    Relationship("Maren", "sibling_of", "Odile"),
    Relationship("Maren", "parent_of", "Ines"),
)

TOWNSFOLK: tuple[str, ...] = tuple(
    "Abel Penhale,Bridget Nance,Cador Rowe,Demelza Hosking,Edern Pascoe,Fenella Tremayne,"  # noqa: SIM905 - a name list reads best as text
    "Gwen Couch,Hedley Rosevear,Isolde Kitto,Jago Polglaze,Kerra Trudgeon,Lowen Bolitho,"
    "Merryn Jenkin,Nessa Pengelly,Oswin Hicks,Perran Laity,Quenby Mitchell,Ruan Tonkin,"
    "Senara Bray,Tegen Curnow,Ulric Opie,Veryan Retallick,Wenna Spargo,Yestin Moyle".split(",")
)
"""The 24 people a deep case's asked person knows: more than ``CANON_PER_ENTITY`` (20)."""


def names() -> tuple[str, ...]:
    return tuple(p.name for p in CAST)


def related(name: str, predicate: str, *, as_object: bool | None = None) -> list[str]:
    """The people ``name`` is tied to by ``predicate``: for a directed predicate, the objects
    of ``name``'s facts (``as_object`` false), the subjects of the facts about ``name``
    (``as_object`` true); a symmetric predicate's other end either way."""
    found: list[str] = []
    for r in RELATIONSHIPS:
        if r.predicate != predicate or not r.touches(name):
            continue
        if r.symmetric or as_object is None:
            found.append(r.other(name))
        elif as_object and r.object == name:
            found.append(r.subject)
        elif not as_object and r.subject == name:
            found.append(r.object)
    return found


def canon_carries(ids: Mapping[str, str], named: str, answer: str) -> bool:
    """Whether retrieval's canon arm can carry the relationship between ``named`` and
    ``answer`` in a shallow case: some fact between them is stored with ``named`` as its
    subject (a symmetric fact: the lower entity id is the subject)."""
    for r in RELATIONSHIPS:
        if {r.subject, r.object} != {named, answer}:
            continue
        stored = min(r.subject, r.object, key=lambda n: ids[n]) if r.symmetric else r.subject
        if stored == named:
            return True
    return False


def _always() -> Interval:
    return Interval.of(None, None)


async def _commit(ctx: CaseContext, story: str, subject: str, predicate: str, obj: str) -> None:
    await ctx.engine.commit_fact(
        ctx.principal,
        story,
        FactRequest(
            proposition=Proposition(
                subject_id=subject,
                predicate_id=CORE_BY_NAME[predicate].id,
                object_id=obj,
                valid_time=_always(),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class WorldScene(Scene):
    """A case's scratch story (``fixtures.Scene``) with the entity ids by name."""

    entities: Mapping[str, str]


async def build_scene(ctx: CaseContext, *, deep: Sequence[str] = ()) -> WorldScene:
    """The world's scene (module docstring); each of ``deep`` first knows ``TOWNSFOLK``."""
    cast = [
        CharacterSpec(
            name=PLAYER.name, definition=PLAYER.definition, true_age=PLAYER.age, is_player=True
        ),
        *(CharacterSpec(name=p.name, definition=p.definition, true_age=p.age) for p in CAST),
    ]
    created = await ctx.story(StorySpec(characters=cast))
    story = created.story_id
    await scenes.structured(ctx.engine, ctx.principal, story)
    extra = [EntitySpec(PLACE, "place"), *(EntitySpec(t) for t in TOWNSFOLK if deep)]
    entity = await scenes.add_entities(ctx.engine, created, extra)
    for name in (PLAYER.name, SPEAKER):
        await _commit(ctx, story, entity[name], "located_at", entity[PLACE])
    for name in dict.fromkeys(deep):
        for person in TOWNSFOLK:
            await _commit(ctx, story, entity[name], "knows", entity[person])
    for r in RELATIONSHIPS:
        await _commit(ctx, story, entity[r.subject], r.predicate, entity[r.object])
    ids = {c.name: c.id for c in created.characters}
    return WorldScene(story, ids, PLAYER.name, entities=entity)


__all__ = [
    "CAST",
    "PLACE",
    "PLAYER",
    "RELATIONSHIPS",
    "SPEAKER",
    "TOWNSFOLK",
    "Relationship",
    "WorldScene",
    "build_scene",
    "canon_carries",
    "names",
    "related",
]
