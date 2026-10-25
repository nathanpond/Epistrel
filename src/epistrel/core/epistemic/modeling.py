"""Lazy character modeling: who becomes a modeled character, and at what narrative weight
(FR-EPI-9,10,11,12, FR-CAT-7; docs/02 §4.4; #163).

**The triggers** (FR-EPI-9). A story entity becomes a modeled character (a ``characters`` row
with a partition under per-character knowledge) on exactly two triggers, and never on mere
appearance (a mention in narration, a jogger seen running past):

- ``interaction``, found deterministically in a turn's beats (``interactions``): an utterance
  addressed to the entity (a vocative: "Thanks, driver."; #161's ``addressee`` and
  ``whisper_to`` name characters already modeled), a ``do`` or narration whose verb is one of the
  interaction lexicon's (``detectors/interaction.json``: greet, wave, nod, hand, ask, tell, …, the
  #148 table mechanism) with a modeled character on one side and the entity on the other ("Jenni
  waves to the neighbor", "the neighbor waves at Jenni": one-sided acknowledgment by a cast member
  suffices), the player's own ``say``/``do`` included. A verb whose direct sense needs a word
  after its object (``direct_then``, #543) has it only then: "passes the waiter a note" hands
  something over while "passes the jogger on the path" is motion past (appearance), and "calls
  the waiter over" summons. A ``say`` merely *about* the entity, or
  words inside the speech, are appearance;
- ``salience``: the salience step (#146) lists the entity among its ``characters`` (stub
  scriptable), a render introduces it (#150's ``introduces``: the character is modeled at
  creation), or the author directs it (``POST /stories/{id}/entities/{eid}/model``).

``maybe_model(entity, trigger)`` is the one gate: an ``appearance`` trigger, an entity already
modeled (its ``entities.character_id`` is set), the player or a story that refuses new arrivals
(``allow_new_arrivals = false``) models nothing. Modeling creates the ``characters`` row (its
name, ``definition`` = the triggering beat's text, the next ``ordinal``, the one true age from
the turn's age repair, ``true_age_source = repaired``, ``weight = minor``, ``modeled_at`` = the
triggering beat's position) as an ordinary character: an existing ``entities`` row is adopted
(its id becomes the character's, so what canon already says of it stays said of the character);
an unresolved definite description or unnamed target ("the delivery driver") mints the
character's entity, the description its alias and its words its descriptors, so the next
"the driver" resolves to the same character (one persistent entity accruing memory). The row is
indexed for erasure in the same transaction (``SubjectWriter("characters", …)``), the
``character_defined`` control event and a ``character_modeled`` derived event
``{trigger, entity_id, position}`` are appended, and the canon facts it needs (its ``age``, its
``located_at`` in the scene) are written by ``core.turns.beats.write_character_facts``: the
epistemic model never writes canon (#160's gate). The new character joins the routed scene in the
same commit, so the triggering wave lands in its partition. Minors are exempt from the cast cap.

**Weight and sparsity** (FR-EPI-10). Modeling is liberal, weight conservative: a modeled
character is ``minor`` (the cast, the player and an author-created character are ``major``;
``POST /stories/{id}/characters/{cid}/weight`` promotes or demotes, prospectively; nothing
promotes automatically). A minor's partition accrues only from interactions it took part in
(``holds_memory``: the memory sink keeps a minor's memory of a beat it perceived at ``content``
only when it acted in it, was addressed (``addressee``, ``whisper_to``, a remote channel) or was
a party to its interaction; exposures and observation transitions are not filtered), its
consolidation window is ``minor_window`` (``max(1, window_memories // 5)``), its hot budget the
character budget x ``minor_hot_budget_fraction`` (a deployment setting, 0.2), its records score
x ``MINOR_SCORE_FACTOR`` (0.5) in retrieval, it is never the default render character and never
takes #162's model adjudication (it gets the default). FR-EPI-11: "minor" limits weight and
sparsity, never privacy: a minor's ``think`` and partition follow every visibility rule.

**The player** (FR-EPI-12) is ``major`` (``ck_characters_player_major``) and never modeled by
this module; it holds no partition (#159's writer refuses it) while others hold beliefs about it.

Log lines carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from importlib import resources
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Authority, CharacterWeight, Provenance
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records, reindex_story
from epistrel.core.erasure.records import character_record, entity_record, event_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.events.registry import EventPayload, lookup, validate
from epistrel.core.events.types import CharacterDefined, CharacterModeled
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, step_of
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.resolution.entities import EntitySpec, add_entities, descriptors_of
from epistrel.core.resolution.types import Mention
from epistrel.core.settings_schema import DEFAULT_MINOR_HOT_BUDGET_FRACTION
from epistrel.core.tables import characters, entities, entity_aliases, events
from epistrel.core.tables.characters import TRUE_AGE_MAX, TRUE_AGE_MIN

log = logging.getLogger(__name__)

Trigger = Literal["interaction", "salience", "appearance"]
INTERACTION: Trigger = "interaction"
SALIENCE: Trigger = "salience"
APPEARANCE: Trigger = "appearance"
TRIGGERS: tuple[Trigger, ...] = (INTERACTION, SALIENCE)
"""The two triggers that model (FR-EPI-9); ``appearance`` never does."""

Via = Literal["verb", "vocative", "salience", "introduction", "author"]

MINOR = CharacterWeight.MINOR
MAJOR = CharacterWeight.MAJOR
MINOR_SCORE_FACTOR = 0.5
"""What a minor holder's record's retrieval score is multiplied by (#141's ranking)."""
MINOR_WINDOW_DIVISOR = 5
MAX_MODELED_PER_TURN = 8
"""The most characters one turn models (the rest wait for a later interaction)."""
PHRASE_MAX = 200
CHARACTER_MODELED = "character_modeled"
MODELING_DEFERRED = "modeling_deferred"
"""Degradation: an entity a turn would model could not get its true age (the age repair was
unaffordable or implausible), so it stays unmodeled this turn."""


PARTITIONS_MODULE = "partitions"


def modeling_on(settings: ProfileSettings) -> bool:
    """Whether a structured turn models lazily (by interaction or the salience step's
    ``characters``): with per-character partitions only. Under shared knowledge a modeled
    character would have no partition to accrue, so only #150's introductions (and the author's
    directive) create characters there, as in M4."""
    return module_on(settings, PARTITIONS_MODULE)


def minor_window(window_memories: int) -> int:
    """A minor holder's consolidation window: ``max(1, window_memories // 5)`` (FR-EPI-10's
    aggressive consolidation; derived, no new dial)."""
    return max(1, window_memories // MINOR_WINDOW_DIVISOR)


async def minor_ids(conn: AsyncConnection, story_id: str) -> frozenset[str]:
    """The story's ``minor`` characters."""
    found = await conn.scalars(
        select(characters.c.id).where(
            characters.c.story_id == story_id, characters.c.weight == MINOR.value
        )
    )
    return frozenset(str(c) for c in found)


# -- the interaction lexicon -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verb:
    lemma: str
    pattern: re.Pattern[str]
    direct: bool
    preps: tuple[str, ...]
    then: frozenset[str] = frozenset()
    """The words one of which must follow the direct object (``direct_then``; empty: any)."""


@dataclass(frozen=True, slots=True)
class Lexicon:
    verbs: tuple[Verb, ...]
    particles: frozenset[str]
    openers: tuple[str, ...]
    articles: frozenset[str]
    persons: frozenset[str]


@cache
def lexicon() -> Lexicon:
    """The English interaction table (``core/verify/detectors/interaction.json``)."""
    source = resources.files("epistrel.core.verify").joinpath("detectors/interaction.json")
    table: dict[str, Any] = json.loads(source.read_text("utf-8"))
    verbs = []
    for entry in table["lemmas"]:
        forms = "|".join(sorted((re.escape(f) for f in entry["forms"]), key=lambda f: -len(f)))
        verbs.append(
            Verb(
                str(entry["lemma"]),
                re.compile(rf"\b(?:{forms})\b", re.IGNORECASE),
                bool(entry.get("direct", False)),
                tuple(sorted((str(p) for p in entry.get("preps", ())), key=lambda p: -len(p))),
                frozenset(str(w).casefold() for w in entry.get("direct_then", ())),
            )
        )
    return Lexicon(
        verbs=tuple(verbs),
        particles=frozenset(str(p).casefold() for p in table.get("particles", ())),
        openers=tuple(
            sorted((str(o).casefold() for o in table.get("vocative_openers", ())), key=len)[::-1]
        ),
        articles=frozenset(str(a).casefold() for a in table.get("articles", ())),
        persons=frozenset(str(p).casefold() for p in table.get("person_nouns", ())),
    )


# -- detection (pure) ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityInfo:
    """An entity as modeling sees it: its kind, its names (canonical and aliases) and the
    character it is (``None``: not modeled)."""

    id: str
    kind: str
    name: str
    character_id: str | None = None
    names: tuple[str, ...] = ()

    @property
    def modeled(self) -> bool:
        return self.character_id is not None


@dataclass(frozen=True, slots=True)
class Party:
    """One side of an interaction: the entity a mention or name resolved to, or (``entity_id``
    ``None``) a definite description no entity matched; ``phrase`` its words in the text."""

    entity_id: str | None
    phrase: str
    span: tuple[int, int] = (0, 0)
    uncertain_of: tuple[str, ...] = ()
    """#171: the entities an ambiguous mention of the phrase had as candidates (a description
    that uncertainly matched existing ones); empty otherwise."""


@dataclass(frozen=True, slots=True)
class Interaction:
    """One interaction found in a beat: how (``verb`` with its lemma, or a ``vocative``), and
    its two parties (``subject`` ``None`` when the beat names none)."""

    via: Literal["verb", "vocative"]
    subject: Party | None
    object: Party
    lemma: str | None = None


_OPEN_QUOTES = "\"'" + chr(0x201C) + chr(0x2018)
_CLOSE_QUOTES = "\"'" + chr(0x201D) + chr(0x2019)
_WORD = re.compile(r"[A-Za-z][A-Za-z'" + chr(0x2019) + r"-]*")
_SPACE = re.compile(r"\s+")


def _skip_space(text: str, pos: int) -> int:
    m = _SPACE.match(text, pos)
    return m.end() if m else pos


def _mention_at(mentions: Sequence[Mention], start: int, *, end: bool = False) -> Mention | None:
    for m in mentions:
        if m.entity_id is not None and (m.span[1] if end else m.span[0]) == start:
            return m
    return None


def _name_at(
    text: str, pos: int, entities: Mapping[str, EntityInfo], *, ending: bool = False
) -> Party | None:
    """An entity named (by a canonical name or alias, whole words, longest first) starting at
    ``pos`` (or ending there, ``ending``)."""
    best: tuple[int, Party] | None = None
    folded = text.casefold()
    for info in entities.values():
        for name in {info.name, *info.names}:
            key = name.strip().casefold()
            if not key:
                continue
            start = pos - len(key) if ending else pos
            if start < 0 or folded[start : start + len(key)] != key:
                continue
            stop = start + len(key)
            if (start > 0 and folded[start - 1].isalnum()) or (
                stop < len(folded) and folded[stop].isalnum()
            ):
                continue
            if best is None or len(key) > best[0]:
                best = (len(key), Party(info.id, text[start:stop], (start, stop)))
    return None if best is None else best[1]


def _description_at(text: str, pos: int, lex: Lexicon, *, bare: bool = False) -> Party | None:
    """A definite description starting at ``pos``: an article, then up to three words the last
    of which is a person noun ("the delivery driver"); ``bare`` also takes a person noun alone
    (a vocative: "Thanks, driver")."""
    words = [(m.group(), m.start(), m.end()) for m in _WORD.finditer(text, pos)][:4]
    if not words or words[0][1] != pos:
        return None
    first = words[0][0].casefold()
    if first in lex.articles:
        tail = words[1:]
        for n, (word, _, stop) in enumerate(tail[:3]):
            gap = text[words[n][2] : tail[n][1]]
            if gap.strip():
                return None  # punctuation between the words: no phrase
            if word.casefold() in lex.persons:
                return Party(None, text[pos:stop], (pos, stop))
        return None
    if bare and first in lex.persons:
        return Party(None, words[0][0], (words[0][1], words[0][2]))
    return None


def _party_at(
    text: str,
    pos: int,
    mentions: Sequence[Mention],
    entities: Mapping[str, EntityInfo],
    lex: Lexicon,
    *,
    bare: bool = False,
) -> Party | None:
    mention = _mention_at(mentions, pos)
    if mention is not None:
        assert mention.entity_id is not None  # noqa: S101 - a resolved mention
        return Party(mention.entity_id, text[mention.span[0] : mention.span[1]], mention.span)
    found = _name_at(text, pos, entities) or _description_at(text, pos, lex, bare=bare)
    if found is not None and found.entity_id is None:
        # #171: a description the resolver found ambiguous among existing entities.
        doubt = next(
            (
                m.candidates
                for m in mentions
                if m.entity_id is None and m.candidates and m.span[0] == pos
            ),
            (),
        )
        if doubt:
            found = replace(found, uncertain_of=tuple(doubt))
    return found


def _party_before(
    text: str,
    end: int,
    mentions: Sequence[Mention],
    entities: Mapping[str, EntityInfo],
    lex: Lexicon,
) -> Party | None:
    """The party whose words end right before ``end`` (an adverb or particle between allowed)."""
    head = text[:end].rstrip()
    last = list(_WORD.finditer(head))
    if last and (last[-1].group().casefold().endswith("ly") or last[-1].group() in lex.particles):
        head = head[: last[-1].start()].rstrip()
    stop = len(head)
    mention = _mention_at(mentions, stop, end=True)
    if mention is not None:
        assert mention.entity_id is not None  # noqa: S101 - a resolved mention
        return Party(mention.entity_id, text[mention.span[0] : mention.span[1]], mention.span)
    named = _name_at(text, stop, entities, ending=True)
    if named is not None:
        return named
    words = list(_WORD.finditer(head))
    for back in range(2, min(4, len(words)) + 1):
        start = words[-back].start()
        found = _description_at(text, start, lex)
        if found is not None and found.span[1] == stop:
            return found
    return None


def _followed_by(text: str, target: Party, verb: Verb) -> bool:
    """Whether the word after ``target`` is one ``verb``'s direct sense needs (``then``)."""
    word = _WORD.match(text, _skip_space(text, target.span[1]))
    return word is not None and word.group().casefold() in verb.then


def _verb_interactions(
    text: str,
    mentions: Sequence[Mention],
    entities: Mapping[str, EntityInfo],
    lex: Lexicon,
    actor: str | None,
) -> list[Interaction]:
    found: list[Interaction] = []
    for verb in lex.verbs:
        for hit in verb.pattern.finditer(text):
            pos = _skip_space(text, hit.end())
            word = _WORD.match(text, pos)
            if word is not None and word.group().casefold() in lex.particles:
                pos = _skip_space(text, word.end())
            target: Party | None = None
            for prep in verb.preps:
                m = re.match(rf"{re.escape(prep)}\b", text[pos:], re.IGNORECASE)
                if m:
                    target = _party_at(
                        text, _skip_space(text, pos + m.end()), mentions, entities, lex
                    )
                    break
            else:
                if verb.direct:
                    target = _party_at(text, pos, mentions, entities, lex)
                    if target is not None and verb.then and not _followed_by(text, target, verb):
                        # #543: "passes the jogger on the path" is motion past, not a hand-over.
                        target = None
            if target is None:
                continue
            subject = _party_before(text, hit.start(), mentions, entities, lex)
            if subject is None and actor is not None:
                subject = Party(actor, "")
            found.append(Interaction("verb", subject, target, verb.lemma))
    return found


def _vocatives(
    text: str,
    mentions: Sequence[Mention],
    entities: Mapping[str, EntityInfo],
    lex: Lexicon,
    speaker: str | None,
) -> list[Interaction]:
    """Who an utterance addresses by name: the phrase opening it (after an opener such as
    "Thanks,") or closing it after a comma, followed by punctuation or the end."""
    starts: list[int] = []
    lead = len(text) - len(text.lstrip(" \t\n" + _OPEN_QUOTES))
    starts.append(lead)
    folded = text.casefold()
    for opener in lex.openers:
        if (
            folded.startswith(opener, lead)
            and not folded[lead + len(opener) : lead + len(opener) + 1].isalnum()
        ):
            after = lead + len(opener)
            m = re.match(r"[\s,!]+", text[after:])
            if m:
                starts.append(after + m.end())
            break
    comma = text.rfind(",")
    if comma >= 0:
        starts.append(_skip_space(text, comma + 1))
    found: list[Interaction] = []
    seen: set[tuple[int, int]] = set()
    for pos in starts:
        party = _party_at(text, pos, mentions, entities, lex, bare=True)
        if party is None or party.span in seen:
            continue
        rest = text[party.span[1] :].lstrip(" \t" + _CLOSE_QUOTES)
        if rest and rest[0] not in ",.!?;:":
            continue
        if party.entity_id is not None and party.entity_id == speaker:
            continue
        seen.add(party.span)
        speaker_party = Party(speaker, "") if speaker is not None else None
        found.append(Interaction("vocative", speaker_party, party))
    return found


def interactions(
    kind: str,
    text: str,
    mentions: Sequence[Mention] = (),
    *,
    actor: str | None = None,
    entities: Mapping[str, EntityInfo] | None = None,
) -> list[Interaction]:
    """The direct interactions one beat states (see the module docstring): vocatives for a
    ``say`` (its words are speech: a verb inside them is appearance), interaction verbs for a
    ``do`` (the actor the subject unless the text names one) and a narration (the subject the
    phrase before the verb). ``think`` and ``latent`` interact with nobody. Pure."""
    lex = lexicon()
    known = entities or {}
    if kind == "say":
        return _vocatives(text, mentions, known, lex, actor)
    if kind == "do":
        return _verb_interactions(text, mentions, known, lex, actor)
    if kind == "narration":
        return _verb_interactions(text, mentions, known, lex, None)
    return []


def is_interaction(kind: str, text: str) -> bool:
    """Whether a beat's text states an interaction at all: an interaction verb in a ``do`` or
    narration, a vocative in a ``say`` (the memory sink's test for a minor its mentions name)."""
    lex = lexicon()
    if kind == "say":
        return bool(_vocatives(text, (), {}, lex, None))
    if kind in ("do", "narration"):
        return any(verb.pattern.search(text) for verb in lex.verbs)
    return False


# -- planning (pure) -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BeatView:
    """One beat of a turn as modeling reads it: kind, number, actor (a character id; ``None``
    for narration), text and its resolved mentions."""

    kind: str
    beat: int
    text: str
    actor: str | None = None
    mentions: tuple[Mention, ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    """An entity a turn would model: the trigger and how it was found, the triggering beat and
    its text, the entity (``None``: a description the commit mints an entity for) and its name
    (#150's descriptive-phrase rule: "the delivery driver")."""

    trigger: Trigger
    via: Via
    beat: int
    definition: str
    name: str
    entity_id: str | None = None
    uncertain_of: tuple[str, ...] = ()
    """#171: the modeled characters a minted description uncertainly matched (an identity
    proposal each, once it is modeled)."""

    @property
    def key(self) -> str:
        return self.entity_id or self.name.casefold()

    @property
    def clue(self) -> str:
        """What the age repair reads: the triggering beat's text and the descriptor."""
        return f"{self.definition} ({self.name})"[: PHRASE_MAX * 4]


def _unmodeled_person(party: Party | None, entities: Mapping[str, EntityInfo]) -> bool:
    if party is None:
        return False
    if party.entity_id is None:
        return bool(party.phrase.strip())
    info = entities.get(party.entity_id)
    return info is not None and not info.modeled and info.kind == "person"


def _modeled(party: Party | None, entities: Mapping[str, EntityInfo]) -> bool:
    if party is None or party.entity_id is None:
        return False
    info = entities.get(party.entity_id)
    return info is not None and info.modeled


def _name_of(party: Party, entities: Mapping[str, EntityInfo]) -> str:
    if party.entity_id is not None and party.entity_id in entities:
        return entities[party.entity_id].name
    phrase = " ".join(party.phrase.split())[:PHRASE_MAX]
    first = phrase.split(" ", 1)[0].casefold()
    if first in ("a", "an", "that", "this"):
        phrase = "the " + phrase.split(" ", 1)[1]
    elif first != "the":
        phrase = f"the {phrase}"
    return phrase


def candidates(
    beats: Sequence[BeatView],
    entities: Mapping[str, EntityInfo],
    *,
    salient: Sequence[str] = (),
) -> list[Candidate]:
    """The entities a turn's beats would model, in beat order, each once (see the module
    docstring): every interaction between a modeled character and an unmodeled person, then
    every name or descriptive phrase the salience step marked as a character (found in the beat
    that names it). Pure."""
    found: dict[str, Candidate] = {}
    for beat in sorted(beats, key=lambda b: b.beat):
        for hit in interactions(
            beat.kind, beat.text, beat.mentions, actor=beat.actor, entities=entities
        ):
            sides = (hit.subject, hit.object)
            for one, other in (sides, sides[::-1]):
                if _modeled(one, entities) and _unmodeled_person(other, entities):
                    assert other is not None  # noqa: S101 - checked above
                    candidate = Candidate(
                        INTERACTION,
                        hit.via,
                        beat.beat,
                        beat.text,
                        _name_of(other, entities),
                        other.entity_id,
                        tuple(
                            e for e in other.uncertain_of if e in entities and entities[e].modeled
                        ),
                    )
                    found.setdefault(candidate.key, candidate)
    for phrase in salient:
        wanted = " ".join(phrase.split())[:PHRASE_MAX]
        ordered = sorted(beats, key=lambda b: b.beat)
        where = next(
            (b for b in ordered if wanted and wanted.casefold() in b.text.casefold()), None
        )
        if where is None:
            continue  # a name no beat of the turn states: never modeled on a guess
        start = where.text.casefold().index(wanted.casefold())
        named = _name_at(where.text, start, entities)
        party = (
            named
            if named is not None and named.span[1] - named.span[0] >= len(wanted)
            else Party(None, wanted, (start, start + len(wanted)))
        )
        if not _unmodeled_person(party, entities):
            continue
        candidate = Candidate(
            SALIENCE, "salience", where.beat, where.text, _name_of(party, entities), party.entity_id
        )
        found.setdefault(candidate.key, candidate)
    return list(found.values())[:MAX_MODELED_PER_TURN]


# -- reading -------------------------------------------------------------------------------------


async def load_entities(
    conn: AsyncConnection, story_id: str, *, linked: Mapping[str, str] | None = None
) -> dict[str, EntityInfo]:
    """The story's entities with their names and the characters they are; ``linked`` maps an
    entity canon's ``same_as`` makes a modeled character to that character (#171,
    ``core.resolution.identity.linked_characters``)."""
    rows = list(
        await conn.execute(
            select(
                entities.c.id, entities.c.kind, entities.c.canonical_name, entities.c.character_id
            ).where(entities.c.story_id == story_id)
        )
    )
    aliases: dict[str, list[str]] = {}
    for row in await conn.execute(
        select(entity_aliases.c.entity_id, entity_aliases.c.alias).where(
            entity_aliases.c.story_id == story_id
        )
    ):
        aliases.setdefault(str(row.entity_id), []).append(str(row.alias))
    found = {
        str(r.id): EntityInfo(
            id=str(r.id),
            kind=str(r.kind),
            name=str(r.canonical_name),
            character_id=None if r.character_id is None else str(r.character_id),
            names=tuple(dict.fromkeys(aliases.get(str(r.id), ()))),
        )
        for r in rows
    }
    # #171: an entity canon links to a modeled character through ``same_as`` is that
    # character (the stranger, once canon holds that he is Bobby): it is never modeled anew.
    # The caller reads the links (this module never reads canon, FR-EPI-1).
    for entity_id, character_id in (linked or {}).items():
        if entity_id in found and found[entity_id].character_id is None:
            found[entity_id] = replace(found[entity_id], character_id=character_id)
    return found


# -- writing ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """A candidate with its true age (FR-CAT-12: from the turn's age repair, or the author)."""

    candidate: Candidate
    age: int
    age_source: Literal["declared", "repaired"] = "repaired"
    weight: CharacterWeight = MINOR


@dataclass(frozen=True, slots=True)
class Modeled:
    """A character modeling created: its id (an adopted entity's), name, trigger, the entity it
    is, the beat that triggered it, its age and weight and the position it was modeled at."""

    character_id: str
    name: str
    trigger: Trigger
    entity_id: str
    beat: int
    age: int
    weight: CharacterWeight
    position: Position
    via: Via = "verb"
    definition: str = ""
    uncertain_of: tuple[str, ...] = ()

    def report(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "trigger": self.trigger,
            "entity_id": self.entity_id,
            "beat": self.beat,
        }


@dataclass
class ModelingPlan:
    """What a turn's modeling step decided before the commit: the requests ready to model (their
    ages found) and the candidates deferred (``modeling_deferred``)."""

    ready: list[ModelRequest] = field(default_factory=list)
    deferred: list[Candidate] = field(default_factory=list)

    def report(self, modeled: Sequence[Modeled]) -> dict[str, Any]:
        return {
            "modeled": [m.report() for m in modeled],
            "deferred": [{"beat": c.beat, "trigger": c.trigger} for c in self.deferred],
        }


async def _append(
    conn: AsyncConnection,
    story_id: str,
    operation: Any,
    type_: str,
    payload: EventPayload,
    beat: int | None,
) -> str:
    """Append one event of ``operation`` in its commit transaction (the caller holds the story
    lock: a commit step); its category is the registry's."""
    body = validate(type_, payload).model_dump(mode="json")
    category = lookup(type_).category
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == story_id)
    )
    event_id = new_ulid()
    await conn.execute(
        insert(events).values(
            id=event_id,
            story_id=story_id,
            seq=int(last or 0) + 1,
            operation_id=operation.operation_id,
            category=category.value,
            narrative_turn=operation.narrative_turn,
            turn_variant=operation.turn_variant,
            beat=beat,
            step=step_of(beat, operation.operation_id),
            principal=getattr(operation, "principal", None) or "system",
            type=type_,
            payload=body,
            provenance=Provenance.SYSTEM.value,
            authority=Authority.AUTHORITATIVE.value,
        )
    )
    await index_records(
        conn,
        [
            event_record(
                SimpleNamespace(id=event_id, story_id=story_id, character_id=None, payload=body)
            )
        ],
    )
    return event_id


async def _unique_name(conn: AsyncConnection, story_id: str, name: str, ordinal: int) -> str:
    """``name``, or ``name`` with the new character's ordinal appended when the story has it."""
    taken = await conn.scalar(
        select(characters.c.id).where(
            characters.c.story_id == story_id, func.lower(characters.c.name) == name.casefold()
        )
    )
    return name if taken is None else f"{name} {ordinal}"


async def maybe_model(
    conn: AsyncConnection,
    operation: Any,
    request: ModelRequest,
    *,
    position: Position,
    allow_new_arrivals: bool = True,
    base_revision: int | None = None,
) -> Modeled | None:
    """Model ``request``'s entity in ``operation``'s commit transaction, or answer ``None`` (see
    the module docstring for the gate). Writes the ``characters`` row, adopts or mints its
    entity, indexes both, and appends ``character_defined`` and ``character_modeled``; the
    caller writes its canon facts (``core.turns.beats.write_character_facts``)."""
    story_id = operation.story_id
    candidate = request.candidate
    if story_id is None or candidate.trigger not in TRIGGERS or not allow_new_arrivals:
        return None
    if not TRUE_AGE_MIN <= request.age <= TRUE_AGE_MAX:
        raise InvalidRequest(f"a true age is {TRUE_AGE_MIN} to {TRUE_AGE_MAX}", {"field": "age"})
    adopted: Any = None
    if candidate.entity_id is not None:
        adopted = (
            await conn.execute(
                select(entities).where(
                    entities.c.story_id == story_id, entities.c.id == candidate.entity_id
                )
            )
        ).one_or_none()
        if adopted is None or adopted.character_id is not None:
            return None  # gone, or modeled since the turn read it
    else:
        # A description a character (or another turn) has since taken is that character.
        existing = await conn.scalar(
            select(characters.c.id).where(
                characters.c.story_id == story_id,
                func.lower(characters.c.name) == candidate.name.casefold(),
            )
        )
        if existing is not None:
            return None
    last = await conn.scalar(
        select(func.max(characters.c.ordinal)).where(characters.c.story_id == story_id)
    )
    ordinal = int(last if last is not None else -1) + 1
    name = await _unique_name(conn, story_id, candidate.name, ordinal)
    character_id = str(adopted.id) if adopted is not None else new_ulid()
    await guard.check_content(
        conn,
        story_id,
        [guard.Content((name, candidate.definition), frozenset())],
        base_revision=base_revision,
        path=__name__,
    )
    row = {
        "id": character_id,
        "story_id": story_id,
        "ordinal": ordinal,
        "name": name,
        "definition": candidate.definition,
        "true_age": request.age,
        "true_age_source": request.age_source,
        "age_recorded_at": func.transaction_timestamp(),
        "is_player": False,
        "weight": request.weight.value,
        **position.columns("modeled"),
    }
    await conn.execute(insert(characters).values(row))
    if adopted is not None:
        await conn.execute(
            update(entities).where(entities.c.id == character_id).values(character_id=character_id)
        )
        await index_records(
            conn,
            [
                entity_record(
                    SimpleNamespace(
                        id=character_id,
                        story_id=story_id,
                        canonical_name=adopted.canonical_name,
                        character_id=character_id,
                        descriptors=adopted.descriptors,
                    )
                )
            ],
        )
    else:
        bare = candidate.name.split(" ", 1)[1] if candidate.name.startswith("the ") else name
        await add_entities(
            conn,
            story_id,
            [
                EntitySpec(
                    kind="person",
                    canonical_name=name,
                    aliases=((bare, "detected"),) if bare.casefold() != name.casefold() else (),
                    descriptors=descriptors_of(bare),
                    character_id=character_id,
                )
            ],
            operation_id=operation.operation_id,
        )
    await index_records(
        conn,
        [
            character_record(
                SimpleNamespace(
                    id=character_id,
                    story_id=story_id,
                    name=name,
                    definition=candidate.definition,
                    safe_fallback_text=None,
                    deflection_style=None,
                    persona_snapshot=None,
                )
            )
        ],
    )
    await _append(
        conn,
        story_id,
        operation,
        "character_defined",
        CharacterDefined(character_id=character_id, name=name, is_player=False),
        None,
    )
    await _append(
        conn,
        story_id,
        operation,
        CHARACTER_MODELED,
        CharacterModeled(
            trigger=candidate.trigger,
            entity_id=character_id,
            position=position.columns("at"),
        ),
        position.beat or None,
    )
    log.debug("character modeled: story=%s character=%s", story_id, character_id)
    return Modeled(
        character_id=character_id,
        name=name,
        trigger=candidate.trigger,
        entity_id=character_id,
        beat=candidate.beat,
        age=request.age,
        weight=request.weight,
        position=position,
        via=candidate.via,
        definition=candidate.definition,
        uncertain_of=candidate.uncertain_of if adopted is None else (),
    )


async def model_all(
    conn: AsyncConnection,
    operation: Any,
    requests: Iterable[ModelRequest],
    *,
    allow_new_arrivals: bool = True,
    base_revision: int | None = None,
) -> list[Modeled]:
    """``maybe_model`` for each request (each at its triggering beat's position), then the
    story's records scanned again for the new names (FR-ADMIN-6; #123)."""
    made: list[Modeled] = []
    for request in requests:
        position = Position(
            int(operation.narrative_turn or 0),
            int(operation.turn_variant or 0),
            0,
            request.candidate.beat,
            0,
        )
        found = await maybe_model(
            conn,
            operation,
            request,
            position=position,
            allow_new_arrivals=allow_new_arrivals,
            base_revision=base_revision,
        )
        if found is not None:
            made.append(found)
    if made and operation.story_id is not None:
        await reindex_story(conn, operation.story_id)
        # #171: a minted character that uncertainly matched an existing one may be it: an
        # identity proposal per pair, never a merge (M7's amendments merge).
        from epistrel.core.propositions.registry import file_identity_proposal

        for m in made:
            for other in m.uncertain_of:
                if other != m.character_id:
                    await file_identity_proposal(
                        conn,
                        operation.story_id,
                        (m.character_id, other),
                        reason="uncertain_match",
                        evidence={"beat": m.beat},
                    )
    return made


async def record_introduced(
    conn: AsyncConnection, operation: Any, introduced: Sequence[tuple[str, int]]
) -> list[Modeled]:
    """The ``character_modeled`` event of each character a render introduced (#150's
    ``introduces`` is a salience trigger: the character was modeled at creation), given as
    ``(character_id, beat)``; answers them as ``Modeled`` for the turn's report."""
    story_id = operation.story_id
    if story_id is None or not introduced:
        return []
    made: list[Modeled] = []
    rows = {
        str(r.id): r
        for r in await conn.execute(
            select(characters.c.id, characters.c.name, characters.c.true_age).where(
                characters.c.story_id == story_id,
                characters.c.id.in_([c for c, _ in introduced]),
            )
        )
    }
    for character_id, beat in introduced:
        row = rows.get(character_id)
        if row is None:
            continue
        position = Position(
            int(operation.narrative_turn or 0), int(operation.turn_variant or 0), 0, beat, 0
        )
        await _append(
            conn,
            story_id,
            operation,
            CHARACTER_MODELED,
            CharacterModeled(
                trigger=SALIENCE, entity_id=character_id, position=position.columns("at")
            ),
            beat or None,
        )
        made.append(
            Modeled(
                character_id=character_id,
                name=str(row.name),
                trigger=SALIENCE,
                entity_id=character_id,
                beat=beat,
                age=int(row.true_age),
                weight=MINOR,
                position=position,
                via="introduction",
            )
        )
    return made


# -- sparsity ----------------------------------------------------------------------------------


def holds_memory(
    holder: str,
    *,
    actor: str | None,
    addressed: Iterable[str],
    participants: Iterable[str],
    kind: str,
    text: str,
    content: bool,
    names: Mapping[str, str] | None = None,
) -> bool:
    """Whether a ``minor`` holder keeps a memory of a beat it perceived (FR-EPI-10's sparsity):
    at ``content`` fidelity only, and only when it acted in the beat, was addressed by it, or is
    a party to the interaction it states (named as a party, or among the beat's resolved
    participants when the beat states an interaction). An envelope never writes a minor's
    memory."""
    if not content:
        return False
    if holder == actor or holder in set(addressed):
        return True
    known = {
        cid: EntityInfo(cid, "person", name, character_id=cid)
        for cid, name in (names or {}).items()
    }
    for hit in interactions(kind, text, actor=actor, entities=known):
        if holder in (getattr(hit.subject, "entity_id", None), hit.object.entity_id):
            return True
    return holder in set(participants) and is_interaction(kind, text)


__all__ = [
    "APPEARANCE",
    "CHARACTER_MODELED",
    "DEFAULT_MINOR_HOT_BUDGET_FRACTION",
    "INTERACTION",
    "MAJOR",
    "MAX_MODELED_PER_TURN",
    "MINOR",
    "MINOR_SCORE_FACTOR",
    "MODELING_DEFERRED",
    "SALIENCE",
    "TRIGGERS",
    "BeatView",
    "Candidate",
    "EntityInfo",
    "Interaction",
    "ModelRequest",
    "Modeled",
    "ModelingPlan",
    "Party",
    "Trigger",
    "candidates",
    "holds_memory",
    "interactions",
    "is_interaction",
    "lexicon",
    "load_entities",
    "maybe_model",
    "minor_ids",
    "minor_window",
    "model_all",
    "modeling_on",
    "record_introduced",
]
