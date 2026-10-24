"""Entities, the alias registry and the mention resolver (FR-PROP-6, FR-RET-2; #139).

**Entities.** Every story character has an entity with the character's id (``character_id``
too), written beside its ``characters`` row, Lite included: ``core.stories`` (blank creation,
``POST /stories/{id}/characters``) and ``core.genesis.create`` (a scenario's cast) build its
``character_spec`` and call ``add_entities`` in the same transaction. Its aliases are the
canonical name (``canonical``), the catalog character's ``metadata.aliases``
(``catalog_alias``), the role's ``name_overrides`` (``name_override``: the canonical override
and its aliases, added to the catalog's) and the persona's name (``persona``); its gender is
the persona's (``man``/``woman``…), else the one gender noun its appearance uses, else none;
its descriptors are its appearance's words without the stoplist. A scenario's
``world.locations[]`` (``place``) and ``world.entities[]`` (their ``kind``, else ``other``) are
built by ``scenario_entity_specs``, which #149's structured genesis writes; their local ids are
kept as aliases. Places and objects first mentioned in canon are #144's (``source =
canonized``).

**The resolver** (``resolve_mentions``) is pure and deterministic: it reads an
``EntityIndex`` (loaded once per operation, ``core.resolution.context``) and the
``ResolutionContext``, never the database and never a model. Every mention span is returned,
in text order:

1. **Names and aliases**: exact and case-folded (NFKC, possessive ``'s`` stripped), whole
   words, longest match first. A single word of a multi-word alias is an implicit alias
   (``implicit_token``), outranked by an explicit one; honorifics and stopwords never are. A
   name two entities share is ``uncertain(ambiguous)`` with both listed.
2. **First and second person**: ``I``/``me``/``my`` are the speaker (a narrator has none);
   ``you`` is the one other entity present (#137's scene; with no scene, the one other actor of
   the context window), the narrator's ``you`` is the player character; none or several is
   ``uncertain``.
3. **Pronouns** bind to the most recent compatible entity: earlier in the same sentence, then
   earlier sentences, then the context window's beats (most recent first; a beat's entities
   are its actor and the names it mentions). ``he``/``she`` need a compatible gender (an entity
   with none is compatible with either), ``it`` a non-person, ``they`` anything; the speaker is
   never a third person's antecedent. Two compatible entities at the nearest level is
   ``uncertain(ambiguous)``; none is ``no_antecedent`` (or ``gender_mismatch`` when the only
   candidates had the other gender).
4. **Descriptions**: ``the <word>{1,3}`` whose words overlap an entity's descriptors (its
   gender's basic noun counts: ``the woman``); the best overlap wins, a tie is ambiguous, no
   overlap is no mention. **Relational descriptions** (``his brother``, ``Bobby's mother``)
   resolve the possessor and follow one ``relationships`` hop (#137) of the relation noun's
   predicate; none or several is ``uncertain``.
5. **Groups** (``those guys``, ``we``) are mentions of kind ``group`` with no entity, for #141's
   occasion lookup. Capitalized words that match nothing (not sentence-initial, not a time or
   stop word) are ``uncertain(unknown_name)``.

Confidence: 1.0 an alias, 0.8 a pronoun or person, 0.6 a description. No fuzzy matching in M4.
The lexicon (``lexicon.json``) is English-only and a data table. No visibility filter in M4.

**Identity** (M5, #171): registered aliases are holder-agnostic, but which entities are one
referent is not. ``ResolutionContext.identities`` (``core.resolution.identity.identities_for``,
loaded for the speaker's holder) gives each resolved mention its ``representative_id``: "the
stranger" is Bobby for canon and for Bobby once the reveal is theirs, and stays the stranger
for Jenni.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from importlib import resources
from itertools import pairwise
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.resolution.identity import Identities
from epistrel.core.resolution.types import APOSTROPHE, Mention, MentionKind, Reason, ReasonCode

NARRATOR = "narrator"
PLAYER = "player"
"""The speakers that are not an entity id: ``player`` stands for the player character."""

CONTEXT_BEATS = 20
"""The context window: the last 20 beats (#135's working memory) plus the text's own spans."""

ALIAS_CONFIDENCE = 1.0
PRONOUN_CONFIDENCE = 0.8
DESCRIPTION_CONFIDENCE = 0.6

WORD = re.compile(r"[^\W_]+(?:['" + APOSTROPHE + r"][^\W_]+)*'?", re.UNICODE)
SENTENCE_END = re.compile(r"[.!?]+")


@dataclass(frozen=True, slots=True)
class Lexicon:
    stopwords: frozenset[str]
    honorifics: frozenset[str]
    pronouns: Mapping[str, str]
    possessives: frozenset[str]
    first_person: frozenset[str]
    second_person: frozenset[str]
    group_pronouns: frozenset[str]
    demonstratives: frozenset[str]
    group_nouns: frozenset[str]
    gender_nouns: Mapping[str, frozenset[str]]
    relations: Mapping[str, Mapping[str, str]]


@cache
def lexicon() -> Lexicon:
    """The English lexicon (``lexicon.json``)."""
    raw = json.loads(
        resources.files("epistrel.core.resolution").joinpath("lexicon.json").read_text("utf-8")
    )
    return Lexicon(
        stopwords=frozenset(raw["stopwords"]),
        honorifics=frozenset(raw["honorifics"]),
        pronouns=dict(raw["pronouns"]),
        possessives=frozenset(raw["possessive_pronouns"]),
        first_person=frozenset(raw["first_person"]),
        second_person=frozenset(raw["second_person"]),
        group_pronouns=frozenset(raw["group_pronouns"]),
        demonstratives=frozenset(raw["demonstratives"]),
        group_nouns=frozenset(raw["group_nouns"]),
        gender_nouns={k: frozenset(v) for k, v in raw["gender_nouns"].items()},
        relations={k: dict(v) for k, v in raw["relations"].items()},
    )


BASIC_GENDER_NOUNS = {
    "masculine": frozenset({"man", "guy", "gentleman"}),
    "feminine": frozenset({"woman", "lady"}),
}
"""The nouns a description matches an entity by its gender alone (``the woman``)."""


def normalize(word: str) -> str:
    """A word's matching form: NFKC, case-folded, the possessive ``'s`` (or a plural's ``'``)
    stripped."""
    folded = unicodedata.normalize("NFKC", word).casefold().replace(APOSTROPHE, "'")
    if folded.endswith("'s"):
        return folded[:-2]
    return folded.rstrip("'")


def possessive(word: str) -> bool:
    folded = word.replace(APOSTROPHE, "'")
    return folded.endswith(("'s", "'S", "s'"))


def words_of(text: str) -> list[str]:
    """The normalized words of ``text``."""
    return [normalize(m.group()) for m in WORD.finditer(text)]


def descriptors_of(text: str | None) -> tuple[str, ...]:
    """The words of ``text`` a description can match: alphabetic, three letters or more, not
    in the stoplist; in order, without repeats."""
    if not text:
        return ()
    stop = lexicon().stopwords
    return tuple(
        dict.fromkeys(w for w in words_of(text) if len(w) >= 3 and w.isalpha() and w not in stop)
    )


def gender_of(*, persona_gender: str | None = None, appearance: str | None = None) -> str | None:
    """``masculine``/``feminine`` from the persona's gender, else from the one gender the
    appearance's nouns name (both or neither: ``None``)."""
    nouns = lexicon().gender_nouns
    if persona_gender:
        word = normalize(persona_gender.strip())
        for gender in ("masculine", "feminine"):
            if word in nouns[gender] or word in {gender, gender[0]}:
                return gender
        return None
    found = {g for g in ("masculine", "feminine") if set(words_of(appearance or "")) & nouns[g]}
    return found.pop() if len(found) == 1 else None


# -- entity specs (what the writers record) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """An entity to record: kind, canonical name, ``(alias, source)`` pairs, gender,
    descriptors, and the character it is (its entity id is the character's)."""

    kind: str
    canonical_name: str
    aliases: tuple[tuple[str, str], ...] = ()
    gender: str | None = None
    descriptors: tuple[str, ...] = ()
    character_id: str | None = None

    @property
    def entity_id(self) -> str | None:
        return self.character_id


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def character_spec(
    character_id: str,
    name: str,
    *,
    catalog_content: Mapping[str, Any] | None = None,
    name_overrides: Mapping[str, Any] | None = None,
    persona: Mapping[str, Any] | None = None,
) -> EntitySpec:
    """The entity of a story character (see the module docstring for its aliases, gender and
    descriptors). ``catalog_content`` is the catalog version's document, ``name_overrides`` the
    role's ``{canonical, aliases}``, ``persona`` the persona snapshot."""
    content = catalog_content or {}
    definition = content.get("definition") if isinstance(content.get("definition"), Mapping) else {}
    metadata = content.get("metadata") if isinstance(content.get("metadata"), Mapping) else {}
    appearance = definition.get("appearance") if isinstance(definition, Mapping) else None
    aliases: list[tuple[str, str]] = []
    if isinstance(metadata, Mapping):
        aliases += [(a, "catalog_alias") for a in _strings(metadata.get("aliases"))]
    overrides = name_overrides or {}
    canonical = overrides.get("canonical")
    if isinstance(canonical, str) and canonical.strip():
        aliases.append((canonical, "name_override"))
    aliases += [(a, "name_override") for a in _strings(overrides.get("aliases"))]
    persona_gender: str | None = None
    if persona:
        if isinstance(persona.get("name"), str):
            aliases.append((persona["name"], "persona"))
        gender = persona.get("gender")
        persona_gender = gender if isinstance(gender, str) else None
    return EntitySpec(
        kind="person",
        canonical_name=name,
        aliases=tuple(aliases),
        gender=gender_of(
            persona_gender=persona_gender,
            appearance=appearance if isinstance(appearance, str) else None,
        ),
        descriptors=descriptors_of(appearance if isinstance(appearance, str) else None),
        character_id=character_id,
    )


def _slug_alias(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip()


def scenario_entity_specs(world: Any) -> list[EntitySpec]:
    """The entities of a scenario's world (a ``ScenarioWorld`` or its JSON): each location a
    ``place``, each entity its ``kind`` (default ``other``), with its local id and aliases as
    aliases (``scenario_entity``) and its description's descriptors."""
    data = world.model_dump(mode="json") if hasattr(world, "model_dump") else dict(world or {})
    specs: list[EntitySpec] = []
    for location in data.get("locations") or []:
        specs.append(
            EntitySpec(
                kind="place",
                canonical_name=location["name"],
                aliases=((_slug_alias(location["id"]), "scenario_entity"),),
                descriptors=descriptors_of(location.get("description")),
            )
        )
    for entity in data.get("entities") or []:
        specs.append(
            EntitySpec(
                kind=entity.get("kind") or "other",
                canonical_name=entity["name"],
                aliases=(
                    (_slug_alias(entity["id"]), "scenario_entity"),
                    *((a, "scenario_entity") for a in _strings(entity.get("aliases"))),
                ),
                descriptors=descriptors_of(entity.get("description")),
            )
        )
    return specs


async def add_entities(
    conn: AsyncConnection,
    story_id: str,
    specs: Iterable[EntitySpec],
    *,
    operation_id: str | None,
) -> list[str]:
    """Record ``specs`` in the caller's transaction (``core.propositions.store.add_entity``:
    guarded and indexed); answers their ids."""
    from epistrel.core.propositions.store import add_entity

    return [
        await add_entity(
            conn,
            story_id,
            spec.kind,
            spec.canonical_name,
            aliases=spec.aliases,
            character_id=spec.character_id,
            entity_id=spec.entity_id,
            gender=spec.gender,
            descriptors=spec.descriptors,
            operation_id=operation_id,
        )
        for spec in specs
    ]


# -- the index -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityEntry:
    """One entity as the resolver sees it."""

    id: str
    kind: str
    canonical_name: str
    gender: str | None = None
    descriptors: tuple[str, ...] = ()
    character_id: str | None = None
    aliases: tuple[tuple[str, str], ...] = ()


Key = tuple[str, ...]


class EntityIndex:
    """A story's alias registry in memory: explicit alias keys and the implicit single-word
    keys of multi-word aliases, each to the entities (and alias sources) it names."""

    def __init__(self, entries: Iterable[EntityEntry]) -> None:
        self.entries: dict[str, EntityEntry] = {e.id: e for e in entries}
        lex = lexicon()
        self.explicit: dict[Key, dict[str, str]] = {}
        self.implicit: dict[Key, set[str]] = {}
        for entry in self.entries.values():
            for alias, source in ((entry.canonical_name, "canonical"), *entry.aliases):
                key = tuple(words_of(alias))
                if not key:
                    continue
                self.explicit.setdefault(key, {}).setdefault(entry.id, source)
                if len(key) > 1:
                    for word in key:
                        if word not in lex.stopwords and word not in lex.honorifics:
                            self.implicit.setdefault((word,), set()).add(entry.id)
        self.longest = max((len(k) for k in self.explicit), default=0)

    def get(self, entity_id: str) -> EntityEntry | None:
        return self.entries.get(entity_id)

    def lookup(self, key: Key) -> tuple[dict[str, str], bool] | None:
        """The entities ``key`` names and whether it is explicit (``None``: no alias)."""
        if key in self.explicit:
            return self.explicit[key], True
        if key in self.implicit:
            return {e: "implicit" for e in self.implicit[key]}, False
        return None


@dataclass(frozen=True, slots=True)
class ContextBeat:
    """A beat of the context window: its text and its actor (an entity id, or ``None``)."""

    text: str
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class Relation:
    """One relationship row (#137): ``subject`` ``predicate`` (a name) ``object``."""

    subject: str
    predicate: str
    object: str


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """What a resolution reads besides the text: the index, the context window (oldest
    first), the entities present (#137's scene; ``None``: no scene known), the player
    character, and the relationships one hop away."""

    index: EntityIndex
    beats: Sequence[ContextBeat] = ()
    present: frozenset[str] | None = None
    player_id: str | None = None
    relations: Sequence[Relation] = ()
    identities: Identities | None = None
    """The ``same_as`` the speaker's holder holds (#171, ``core.resolution.identity``): each
    resolved mention's ``representative_id``. ``None``: none read (every entity stands for
    itself)."""
    composing: frozenset[str] = frozenset()
    """#484 (owner's option (a)): on the composition path, the characters whose beats the
    message composes. The narrator's "other character" excludes them, so a weak texture claim
    about a render's own character is surfaced as on that render's beats."""


# -- tokens ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Token:
    start: int
    end: int
    raw: str
    word: str
    sentence: int
    first: bool
    """The sentence's first word."""
    taken: bool = False


def _tokens(text: str) -> list[_Token]:
    found: list[_Token] = []
    sentence = 0
    last_end = 0
    for match in WORD.finditer(text):
        between = text[last_end : match.start()]
        if SENTENCE_END.search(between):
            sentence += 1
        first = not found or bool(SENTENCE_END.search(between))
        found.append(
            _Token(
                match.start(), match.end(), match.group(), normalize(match.group()), sentence, first
            )
        )
        last_end = match.end()
    return found


def _adjacent(text: str, a: _Token, b: _Token) -> bool:
    """Only spaces between two tokens (no punctuation)."""
    return not text[a.end : b.start].strip()


# -- the resolver ----------------------------------------------------------------------------


@dataclass(slots=True)
class _State:
    text: str
    tokens: list[_Token]
    speaker: str | None
    context: ResolutionContext
    mentions: list[Mention] = field(default_factory=list)
    by_token: dict[int, Mention] = field(default_factory=dict)
    owners: list[str] = field(default_factory=list)
    """The possessors whose relationships the relational pass consulted, in text order."""


def _speaker_id(speaker: str, context: ResolutionContext) -> str | None:
    if speaker == NARRATOR:
        return None
    if speaker == PLAYER:
        return context.player_id
    return speaker


def _add(state: _State, first: int, last: int, mention: Mention) -> None:
    for i in range(first, last + 1):
        state.tokens[i].taken = True
        state.by_token[i] = mention
    state.mentions.append(mention)


def _alias_pass(state: _State) -> None:
    index = state.context.index
    tokens = state.tokens
    i = 0
    while i < len(tokens):
        hit = None
        for length in range(min(index.longest, len(tokens) - i), 0, -1):
            window = tokens[i : i + length]
            if any(not _adjacent(state.text, a, b) for a, b in pairwise(window)):
                continue
            # A possessive ends a name: "Bobby's" can end one, never sit inside one.
            if any(possessive(t.raw) for t in window[:-1]):
                continue
            found = index.lookup(tuple(t.word for t in window))
            if found is not None:
                hit = (length, *found)
                break
        if hit is None:
            i += 1
            continue
        length, named, explicit = hit
        span = (tokens[i].start, tokens[i + length - 1].end)
        if tokens[i + length - 1].raw.replace(APOSTROPHE, "'").endswith("'s"):
            span = (span[0], span[1] - 2)
        ids = tuple(sorted(named))
        canonical = any(source == "canonical" for source in named.values())
        kind = MentionKind.NAME if canonical or not explicit else MentionKind.ALIAS
        rule = "alias" if explicit else "implicit_token"
        if len(ids) == 1:
            mention = Mention(span, kind, ids[0], ids, ALIAS_CONFIDENCE, None, rule)
        else:
            mention = Mention(span, kind, None, ids, 0.0, Reason(ReasonCode.AMBIGUOUS, rule), rule)
        _add(state, i, i + length - 1, mention)
        i += length


def _compatible(entry: Any, pronoun_class: str) -> bool:
    if pronoun_class == "any":
        return True
    if pronoun_class == "thing":
        return bool(entry.kind != "person")
    if entry.kind != "person" and entry.gender is None:
        return False
    return entry.gender is None or entry.gender == pronoun_class


def _pools(state: _State, at: int) -> list[list[str]]:
    """Antecedent pools, nearest first: earlier in the sentence, earlier sentences (most recent
    first), then the context window's beats (most recent first)."""
    token = state.tokens[at]
    by_sentence: dict[int, list[str]] = {}
    for i in range(at):
        mention = state.by_token.get(i)
        if mention is not None and mention.entity_id is not None:
            ids = by_sentence.setdefault(state.tokens[i].sentence, [])
            if mention.entity_id not in ids:
                ids.append(mention.entity_id)
    pools = [by_sentence[s] for s in sorted(by_sentence, reverse=True) if s <= token.sentence]
    index = state.context.index
    for beat in reversed(state.context.beats):
        ids = [] if beat.actor is None else [beat.actor]
        ids += [e for e in _names_in(beat.text, index) if e not in ids]
        if ids:
            pools.append(ids)
    return pools


def _names_in(text: str, index: EntityIndex) -> list[str]:
    """The entities a context beat names unambiguously (aliases only)."""
    probe = _State(text, _tokens(text), None, ResolutionContext(index))
    _alias_pass(probe)
    return [m.entity_id for m in probe.mentions if m.entity_id is not None]


def _bind(
    state: _State, at: int, pronoun_class: str
) -> tuple[str | None, tuple[str, ...], Reason | None]:
    index = state.context.index
    mismatched = False
    for pool in _pools(state, at):
        candidates = [e for e in pool if e != state.speaker]
        compatible = []
        for entity_id in candidates:
            entry = index.get(entity_id)
            if entry is not None and _compatible(entry, pronoun_class):
                compatible.append(entity_id)
            elif entry is not None:
                mismatched = True
        if len(compatible) == 1:
            return compatible[0], tuple(compatible), None
        if len(compatible) > 1:
            return None, tuple(sorted(compatible)), Reason(ReasonCode.AMBIGUOUS, "pronoun")
    code = ReasonCode.GENDER_MISMATCH if mismatched else ReasonCode.NO_ANTECEDENT
    return None, (), Reason(code, "pronoun")


def _addressee(state: _State) -> tuple[str | None, tuple[str, ...], Reason | None]:
    context = state.context
    if state.speaker is None:
        if context.player_id is not None:
            return context.player_id, (context.player_id,), None
        return None, (), Reason(ReasonCode.NO_ANTECEDENT, "addressee")
    if context.present is not None:
        pool = set(context.present)
    else:
        pool = {b.actor for b in context.beats if b.actor is not None}
    others = sorted(
        e
        for e in pool - {state.speaker}
        if (entry := context.index.get(e)) is not None and entry.kind == "person"
    )
    if len(others) == 1:
        return others[0], tuple(others), None
    if not others:
        return None, (), Reason(ReasonCode.NO_ANTECEDENT, "addressee")
    return None, tuple(others), Reason(ReasonCode.AMBIGUOUS, "addressee")


def _person_pass(state: _State) -> None:
    lex = lexicon()
    for i, token in enumerate(state.tokens):
        if token.taken:
            continue
        word = token.word
        span = (token.start, token.end)
        if word in lex.first_person:
            if state.speaker is None:
                mention = Mention(
                    span,
                    MentionKind.FIRST_PERSON,
                    None,
                    (),
                    0.0,
                    Reason(ReasonCode.NO_ANTECEDENT, "speaker"),
                    "speaker",
                )
            else:
                mention = Mention(
                    span,
                    MentionKind.FIRST_PERSON,
                    state.speaker,
                    (state.speaker,),
                    PRONOUN_CONFIDENCE,
                    None,
                    "speaker",
                )
        elif word in lex.second_person:
            entity, candidates, reason = _addressee(state)
            mention = Mention(
                span,
                MentionKind.SECOND_PERSON,
                entity,
                candidates,
                PRONOUN_CONFIDENCE if entity else 0.0,
                reason,
                "addressee",
            )
        elif word in lex.group_pronouns:
            mention = Mention(span, MentionKind.GROUP, None, (), 0.0, None, "group")
        elif word in lex.demonstratives:
            nxt = state.tokens[i + 1] if i + 1 < len(state.tokens) else None
            if (
                nxt is None
                or nxt.word not in lex.group_nouns
                or not _adjacent(state.text, token, nxt)
            ):
                continue
            mention = Mention(
                (token.start, nxt.end), MentionKind.GROUP, None, (), 0.0, None, "group"
            )
            _add(state, i, i + 1, mention)
            continue
        elif word in lex.pronouns:
            entity, candidates, reason = _bind(state, i, lex.pronouns[word])
            mention = Mention(
                span,
                MentionKind.PRONOUN,
                entity,
                candidates,
                PRONOUN_CONFIDENCE if entity else 0.0,
                reason,
                "pronoun",
            )
        else:
            continue
        _add(state, i, i, mention)


def _descriptor_score(entry: EntityEntry, words: Sequence[str]) -> int:
    own = set(entry.descriptors) | BASIC_GENDER_NOUNS.get(entry.gender or "", frozenset())
    return len(own & set(words))


def _relation_pass(state: _State) -> None:
    lex = lexicon()
    tokens = state.tokens
    index = state.context.index
    for i, token in enumerate(tokens):
        if token.taken or token.word not in lex.relations:
            continue
        j = i - 1
        if j >= 0 and not tokens[j].taken and tokens[j].word not in lex.stopwords:
            j -= 1  # one adjective: "his older brother"
        if j < 0 or not _adjacent(state.text, tokens[j], tokens[j + 1]):
            continue
        owner_mention = state.by_token.get(j)
        is_possessor = tokens[j].word in lex.possessives or possessive(tokens[j].raw)
        if owner_mention is None or not is_possessor:
            continue
        relation = lex.relations[token.word]
        span = (token.start, token.end)
        owner = owner_mention.entity_id
        if owner is None:
            reason = Reason(ReasonCode.NO_ANTECEDENT, "relation_owner")
            _add(
                state,
                i,
                i,
                Mention(span, MentionKind.DESCRIPTION, None, (), 0.0, reason, "relation"),
            )
            continue
        state.owners.append(owner)
        found: set[str] = set()
        for row in state.context.relations:
            if row.predicate != relation["predicate"]:
                continue
            role = relation["role"]
            if role in ("subject", "either") and row.object == owner:
                found.add(row.subject)
            if role in ("object", "either") and row.subject == owner:
                found.add(row.object)
        found.discard(owner)
        gender = relation.get("gender")
        if gender is not None:
            found = {
                e
                for e in found
                if (entry := index.get(e)) is None or entry.gender in (None, gender)
            }
        ids = tuple(sorted(found))
        if len(ids) == 1:
            mention = Mention(
                span, MentionKind.DESCRIPTION, ids[0], ids, DESCRIPTION_CONFIDENCE, None, "relation"
            )
        else:
            code = ReasonCode.AMBIGUOUS if ids else ReasonCode.UNRESOLVED_ENTITY
            mention = Mention(
                span, MentionKind.DESCRIPTION, None, ids, 0.0, Reason(code, "relation"), "relation"
            )
        _add(state, i, i, mention)


def _description_pass(state: _State) -> None:
    lex = lexicon()
    tokens = state.tokens
    index = state.context.index
    for i, token in enumerate(tokens):
        if token.taken or token.word != "the":
            continue
        words: list[int] = []
        k = i + 1
        while k < len(tokens) and len(words) < 3:
            t = tokens[k]
            if t.taken or t.word in lex.stopwords or not _adjacent(state.text, tokens[k - 1], t):
                break
            words.append(k)
            k += 1
        if not words:
            continue
        phrase = [tokens[k].word for k in words]
        scores = {
            e.id: score
            for e in index.entries.values()
            if (score := _descriptor_score(e, phrase)) > 0 and e.id != state.speaker
        }
        if not scores:
            continue
        best = max(scores.values())
        top = tuple(sorted(e for e, s in scores.items() if s == best))
        last = max(k for k in words if tokens[k].word in _matched_words(index, top, phrase))
        span = (token.start, tokens[last].end)
        if len(top) == 1:
            mention = Mention(
                span,
                MentionKind.DESCRIPTION,
                top[0],
                top,
                DESCRIPTION_CONFIDENCE,
                None,
                "descriptor",
            )
        else:
            mention = Mention(
                span,
                MentionKind.DESCRIPTION,
                None,
                top,
                0.0,
                Reason(ReasonCode.AMBIGUOUS, "descriptor"),
                "descriptor",
            )
        _add(state, i, last, mention)


def _matched_words(index: EntityIndex, ids: Sequence[str], phrase: Sequence[str]) -> set[str]:
    matched: set[str] = set()
    for entity_id in ids:
        entry = index.get(entity_id)
        if entry is not None:
            own = set(entry.descriptors) | BASIC_GENDER_NOUNS.get(entry.gender or "", frozenset())
            matched |= own & set(phrase)
    return matched


def _unknown_pass(state: _State) -> None:
    from epistrel.core.resolution.time import calendar_words

    lex = lexicon()
    skip = lex.stopwords | lex.honorifics | calendar_words() | set(lex.pronouns)
    tokens = state.tokens
    i = 0
    while i < len(tokens):
        token = tokens[i]
        capitalized = token.raw[:1].isupper() and token.raw[:1].isalpha()
        if token.taken or not capitalized or token.first or token.word in skip or token.word == "i":
            i += 1
            continue
        last = i
        while (
            last + 1 < len(tokens)
            and not tokens[last + 1].taken
            and tokens[last + 1].raw[:1].isupper()
            and _adjacent(state.text, tokens[last], tokens[last + 1])
            and not possessive(tokens[last].raw)
        ):
            last += 1
        span = (token.start, tokens[last].end)
        if tokens[last].raw.replace(APOSTROPHE, "'").endswith("'s"):
            span = (span[0], span[1] - 2)
        reason = Reason(ReasonCode.UNKNOWN_NAME, "name")
        _add(state, i, last, Mention(span, MentionKind.NAME, None, (), 0.0, reason, "unknown"))
        i = last + 1


def _resolve(text: str, speaker: str, context: ResolutionContext) -> _State:
    state = _State(text, _tokens(text), None, context)
    state.speaker = _speaker_id(speaker, context)
    _alias_pass(state)
    # Pronouns bind in text order, so an earlier pronoun's entity is a later one's antecedent.
    _person_pass(state)
    _relation_pass(state)
    _description_pass(state)
    _unknown_pass(state)
    return state


def resolve_mentions(text: str, speaker: str, context: ResolutionContext) -> list[Mention]:
    """Every mention in ``text``, in order, as ``speaker`` (an entity id, ``narrator`` or
    ``player``) says it, against ``context`` (see the module docstring for the rules). With
    ``context.identities`` (#171), a resolved mention whose entity the speaker's holder links to
    another carries its class's ``representative_id``; its ``entity_id`` stays the entity the
    words name."""
    found = sorted(_resolve(text, speaker, context).mentions, key=lambda m: m.span)
    identities = context.identities
    if not identities:
        return found
    out: list[Mention] = []
    for mention in found:
        if mention.entity_id is not None:
            rep = identities.rep(mention.entity_id)
            if rep != mention.entity_id:
                mention = replace(mention, representative_id=rep)
        out.append(mention)
    return out


def has_relation_noun(text: str) -> bool:
    """Whether ``text`` holds a relation noun (``brother``, ``wife``…): the only words whose
    resolution reads ``ResolutionContext.relations``."""
    nouns = lexicon().relations
    return any(token.word in nouns for token in _tokens(text))


def relation_owners(text: str, speaker: str, context: ResolutionContext) -> tuple[str, ...]:
    """The entities whose relationships resolving ``text`` consults (#451): the possessors of
    its relational descriptions (``his brother``, ``Bobby's mother``), each once, in text order.
    The relational pass reads only the ``relations`` rows that touch a possessor, so a
    resolution against the rows of these entities equals one against every row, once the set is
    closed: a possessor can itself be a relational description (``his brother's wife``), so
    ``core.resolution.context`` reads the rows of these, resolves again and stops when no new
    one appears. Empty, without running the resolver, for a text with no relation noun."""
    if not has_relation_noun(text):
        return ()
    return tuple(dict.fromkeys(_resolve(text, speaker, context).owners))


def resolve_reference(phrase: str, speaker: str, context: ResolutionContext) -> Mention:
    """The one entity a phrase (a proposition sketch's argument, #140) names: its first entity
    mention, or ``uncertain(unresolved_entity)`` over the whole phrase when it has none."""
    for mention in resolve_mentions(phrase, speaker, context):
        if mention.kind is not MentionKind.GROUP:
            return mention
    return Mention(
        (0, len(phrase)),
        MentionKind.NAME,
        None,
        (),
        0.0,
        Reason(ReasonCode.UNRESOLVED_ENTITY, "no_mention"),
        "none",
    )


__all__ = [
    "CONTEXT_BEATS",
    "NARRATOR",
    "PLAYER",
    "ContextBeat",
    "EntityEntry",
    "EntityIndex",
    "EntitySpec",
    "Relation",
    "ResolutionContext",
    "add_entities",
    "character_spec",
    "descriptors_of",
    "gender_of",
    "has_relation_noun",
    "lexicon",
    "normalize",
    "relation_owners",
    "resolve_mentions",
    "resolve_reference",
    "scenario_entity_specs",
    "words_of",
]
