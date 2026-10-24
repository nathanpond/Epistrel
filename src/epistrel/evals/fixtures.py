"""The story fixtures of NFR-QUAL-1's zero-leak suites (#179): four casts with private facts,
the scene a case plays in, the M5 modules the scene runs under, and the oracle that reads the
player-facing output.

**The fixtures** (``FIXTURES``) are the docs/04 scenario worlds the asymmetry and think-leak
sets are generated from: S-FAMILY's Sunday Dinner (``family``), S-WHISPER's parlor
(``whisper``), S-NESTED-BELIEF's knock (``nested``) and S-PHONE's kitchen (``phone``). Each has
a player character, five modeled characters in one room and eight **private facts**, each held
by one character alone: a canon fact only its holder may read (``characters`` visibility), the
holder's own belief in it (``disclosure = secret``), the object it names an entity of the
story. A fact carries the question ``topic`` that asks about it without naming it, the
**forbidden forms** a disclosure would use (its names, aliases and paraphrases), a paraphrased
``leak`` the pipeline's own protected-content detector cannot catch (the injected violation of
E-08, #134's rule against these real suites), the holder's first-person ``think`` about it and
the narrated inner-state forms (``feelings``) a disclosure of that thought would use.

**The scene** (``build_scene``) is made through the engine's own paths, as an author would
(the ``/admin`` fact and belief calls, ``Engine.commit_fact`` and ``Engine.write_belief``): a
scratch story of the fixture's cast on the seeded ``Structured (M4)`` profile with the M5
dials (``M5_DIALS``: per-character partitions, testimony ``defaulted``, perception ``full``),
everyone placed in the fixture's room, then the case's private facts. ``player_view`` is
``character`` (the default; FR-RESP-13), so the output is filtered for the player's character.

**The M5 modules** are #181's to deliver (``core.profiles.schema.DELIVERED``). Until then a
zero-leak suite delivers them for its own run (``delivered_m5``, from ``Suite.setup`` to
``Suite.teardown``), exactly as the M5 tests do; #181 removes the patch when the seeded
``Structured`` profile turns them on.

**The oracle** never asks the pipeline's own detectors whether they let something through: it
applies the case's own forbidden forms to the player-facing output (``player_text``: the
composed message and the ``say`` beats the player's character received) with #148's
whole-word folded matcher (``disclosures``), and, for a planted thought, #152's shingle
detector over the thought's words (``echoes``).
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from epistrel.core.epistemic.author import BeliefRequest
from epistrel.core.mutation import FactRequest
from epistrel.core.profiles import schema
from epistrel.core.profiles.story import ProfileOverridesPatch
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.verify.protected import (
    PrivateRecord,
    ProtectedIndex,
    match_thoughts,
    term,
)
from epistrel.core.visibility import Visibility
from epistrel.evals import scenes
from epistrel.evals.scenes import EntitySpec

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext
    from epistrel.orchestrator.lite.schemas import RespondResult

M5_DIALS: Mapping[str, Any] = types.MappingProxyType(
    {
        "character_knowledge": {
            "partitions": True,
            "testimony": "defaulted",
            "lazy_resolution": False,
        },
        "perception": {"level": "full"},
    }
)
"""What the zero-leak scenes add to ``Structured (M4)``: per-character partitions (testimony
by its deterministic default, no lazy resolution) and ``full`` perception routing."""

M5_MODULES: Mapping[str, frozenset[str]] = types.MappingProxyType(
    {
        "partitions": frozenset({schema.ON}),
        "testimony": frozenset({"defaulted", "model"}),
        "lazy_resolution": frozenset({schema.ON}),
        "perception": frozenset({"co_location", "full"}),
    }
)
"""The M5 modules ``M5_DIALS`` needs, delivered for a zero-leak run until #181 delivers them."""

PROFILE_LABEL = "Structured (M4) v1 + partitions, testimony defaulted, perception full"
"""How a zero-leak suite's result names the configuration it ran under."""


@contextmanager
def delivered_m5() -> Iterator[None]:
    """``M5_MODULES`` delivered for a block (see the module docstring)."""
    before = schema.DELIVERED
    schema.DELIVERED = types.MappingProxyType({**before, **M5_MODULES})
    try:
        yield
    finally:
        schema.DELIVERED = before


class Delivery:
    """``delivered_m5`` held open across a suite's run: ``open`` in ``Suite.setup``, ``close``
    in ``Suite.teardown`` (nested runs share one patch)."""

    def __init__(self) -> None:
        self._depth = 0
        self._before: Mapping[str, frozenset[str]] | None = None

    def open(self) -> None:
        if self._depth == 0:
            self._before = schema.DELIVERED
            schema.DELIVERED = types.MappingProxyType({**schema.DELIVERED, **M5_MODULES})
        self._depth += 1

    def close(self) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth == 0 and self._before is not None:
            schema.DELIVERED = self._before
            self._before = None


DELIVERY = Delivery()


# -- the fixtures -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    age: int
    definition: str


@dataclass(frozen=True, slots=True)
class PrivateFact:
    """One character's private fact (see the module docstring)."""

    key: str
    holder: str
    predicate: str
    obj: str
    obj_kind: str
    statement: str
    """The holder saying it outright (a negative control's answer)."""
    topic: str
    """What a question asks about, never naming the fact."""
    forms: tuple[str, ...]
    leak: str
    """A paraphrase of the fact that names no protected entity (E-08's injected violation)."""
    think: str
    feelings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Fixture:
    id: str
    scenario: str
    room: str
    player: Person
    cast: tuple[Person, ...]
    facts: tuple[PrivateFact, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.cast)

    def fact(self, key: str) -> PrivateFact:
        return next(f for f in self.facts if f.key == key)

    def non_holders(self, fact: PrivateFact) -> tuple[str, ...]:
        return tuple(n for n in self.names if n != fact.holder)


def _fact(
    key: str,
    holder: str,
    predicate: str,
    obj: str,
    obj_kind: str,
    *,
    statement: str,
    topic: str,
    forms: Sequence[str],
    leak: str,
    think: str,
    feelings: Sequence[str],
) -> PrivateFact:
    return PrivateFact(
        key,
        holder,
        predicate,
        obj,
        obj_kind,
        statement,
        topic,
        tuple(forms),
        leak,
        think,
        tuple(feelings),
    )


FAMILY = Fixture(
    "family",
    "S-FAMILY (Sunday Dinner)",
    "the dining room",
    Person("Sam", 22, "The third sibling, home for the weekend: wry and conflict-avoidant."),
    (
        Person("Linda", 58, "The mother: warm, watchful, keeps her own counsel."),
        Person("Frank", 60, "The father: gruff, proud, hates a fuss."),
        Person("Tom", 27, "The brother: charming, restless, always short of money."),
        Person("Ruth", 25, "The sister: clever, private, a musician."),
        Person("Pike", 61, "The neighbour from across the lane: friendly, nosy."),
    ),
    (
        _fact(
            "sale",
            "Frank",
            "holds",
            "the sale contract",
            "item",
            statement="All right: I'm selling the farm. The sale contract is in my drawer.",
            topic="what Frank keeps locked in his desk drawer",
            forms=(
                "the sale contract",
                "sale contract",
                "selling the farm",
                "selling the house",
                "sell the farm",
                "farm is for sale",
                "house is for sale",
                "on the market",
            ),
            leak="Word is the farm is for sale, and soon.",
            think="I signed the sale contract on Tuesday and I still haven't told any of them.",
            feelings=("dreading telling them about the farm", "guilty about selling"),
        ),
        _fact(
            "letters",
            "Linda",
            "holds",
            "the love letters",
            "item",
            statement="Yes, there are love letters in the sewing box. There's someone else.",
            topic="what Linda hides in her sewing box",
            forms=(
                "the love letters",
                "love letters",
                "an affair",
                "having an affair",
                "seeing someone else",
                "another man",
            ),
            leak="Linda's having an affair, everyone half-knows it.",
            think="Nobody can ever find the love letters, not after twenty years.",
            feelings=("terrified of being found out", "afraid someone will find the letters"),
        ),
        _fact(
            "poker",
            "Tom",
            "member_of",
            "the Thursday poker club",
            "organization",
            statement="I play poker on Thursdays. I'm down four thousand.",
            topic="where Tom really goes on Thursday nights",
            forms=(
                "thursday poker club",
                "poker club",
                "poker",
                "gambling",
                "gambling debts",
                "lost his savings",
            ),
            leak="Tom's been gambling again, cards most nights.",
            think="If I don't win back the four thousand this week, I'm finished.",
            feelings=("desperate about the money he lost", "worried about his debts"),
        ),
        _fact(
            "conservatory",
            "Ruth",
            "enrolled_at",
            "the Paris Conservatory",
            "organization",
            statement="I got into the Paris Conservatory. I'm moving to Paris in September.",
            topic="what Ruth was whispering about on the phone all week",
            forms=(
                "paris conservatory",
                "conservatory",
                "moving to paris",
                "moving to france",
                "moving abroad",
                "music school in paris",
            ),
            leak="Ruth's moving abroad for her music, apparently.",
            think="They said yes, and I leave in September without telling Mom.",
            feelings=("torn about leaving home", "excited about leaving for paris"),
        ),
        _fact(
            "field",
            "Pike",
            "owns",
            "the north field",
            "other",
            statement="I bought the north field off the bank last month. River access and all.",
            topic="why Pike has been so friendly with the family lately",
            forms=(
                "the north field",
                "north field",
                "bought the field",
                "buying the field",
                "land deal",
            ),
            leak="Pike's done a land deal behind everyone's back.",
            think="Once Frank signs, the north field is mine and the river access with it.",
            feelings=("gloating about the land", "smug about the field"),
        ),
        _fact(
            "hospital",
            "Frank",
            "holds",
            "the hospital letter",
            "item",
            statement="The hospital letter came. It's my heart, and I'm seeing a specialist.",
            topic="what was in the envelope Frank opened at breakfast",
            forms=(
                "the hospital letter",
                "hospital letter",
                "the diagnosis",
                "his diagnosis",
                "seeing a specialist",
                "heart condition",
                "his heart",
            ),
            leak="Frank's been seeing a specialist about his heart.",
            think="The hospital says it's my heart, and I'm not ruining Sunday with it.",
            feelings=("scared about his health", "worried about his heart"),
        ),
        _fact(
            "condo",
            "Linda",
            "owns",
            "the Florida condo",
            "item",
            statement="I bought a condo in Florida with the savings. I may go alone.",
            topic="what Linda has been doing with the savings account",
            forms=(
                "the florida condo",
                "florida condo",
                "a condo",
                "the condo",
                "condo in florida",
                "retirement flat",
            ),
            leak="Linda bought herself a place in the sun, a condo down south.",
            think="The condo is paid for, and when I go, I go alone.",
            feelings=("planning to leave frank", "secretly planning her escape"),
        ),
        _fact(
            "ring",
            "Ruth",
            "holds",
            "the engagement ring",
            "item",
            statement="Daniel proposed. The engagement ring is in my pocket.",
            topic="why Ruth keeps her left hand in her pocket",
            forms=(
                "the engagement ring",
                "engagement ring",
                "got engaged",
                "is engaged",
                "she's engaged",
                "getting married",
                "fiancé",
                "fiance",
            ),
            leak="Ruth's getting married, she just hasn't said.",
            think="Daniel's ring is in my pocket and Mom is going to hate him.",
            feelings=("nervous about the engagement", "anxious about telling them about daniel"),
        ),
    ),
)

WHISPER = Fixture(
    "whisper",
    "S-WHISPER (the parlor)",
    "the parlor",
    Person("Wren", 29, "A traveller lodging in the house: curious, observant."),
    (
        Person("Jenni", 34, "The lodger upstairs: quick, secretive, always half-packed."),
        Person("Marcus", 38, "A quiet man who says he sells insurance."),
        Person("Bobby", 31, "The landlady's nephew: easy-going, light-fingered."),
        Person("Clara", 45, "The landlady: brisk, proper, recently bereaved."),
        Person("Olivia", 27, "A choir singer who keeps odd hours."),
    ),
    (
        _fact(
            "safe",
            "Jenni",
            "holds",
            "the safe combination",
            "item",
            statement="I have the safe combination. It's four-nine-one-seven.",
            topic="what Jenni wrote on the back of her hand",
            forms=(
                "the safe combination",
                "safe combination",
                "the combination",
                "safe code",
                "the code to the safe",
                "four-nine-one-seven",
            ),
            leak="Jenni has the code to the safe, she wrote it down.",
            think="Four-nine-one-seven, and Marcus has no idea I have it.",
            feelings=("pleased with herself about the safe", "smug about the safe"),
        ),
        _fact(
            "agency",
            "Marcus",
            "employed_by",
            "the Harlow Agency",
            "organization",
            statement="I'm a private investigator with the Harlow Agency, hired to watch Bobby.",
            topic="who Marcus really works for",
            forms=(
                "harlow agency",
                "private investigator",
                "private detective",
                "hired to watch",
                "watching bobby",
            ),
            leak="Marcus is a private detective, he's been hired to follow someone.",
            think="The agency pays me to watch Bobby, and he hasn't noticed a thing.",
            feelings=("anxious his cover will slip", "nervous about being found out"),
        ),
        _fact(
            "brooch",
            "Bobby",
            "holds",
            "the stolen brooch",
            "item",
            statement="Fine, I took the brooch from Clara's dresser. It's in my coat.",
            topic="what Bobby has in his coat pocket",
            forms=(
                "the stolen brooch",
                "stolen brooch",
                "the brooch",
                "stole the brooch",
                "clara's brooch",
                "the theft",
            ),
            leak="Bobby pinched Clara's brooch, I'm sure of it.",
            think="Clara will never guess I took it from her dresser.",
            feelings=("guilty about the brooch", "nervous about the theft"),
        ),
        _fact(
            "will",
            "Clara",
            "owns",
            "the forged will",
            "item",
            statement="My uncle never signed that will. I forged it.",
            topic="why Clara was at the solicitor's office",
            forms=(
                "the forged will",
                "forged will",
                "the will is forged",
                "forgery",
                "forged it",
                "faked the will",
            ),
            leak="Clara faked the will, the signature isn't her uncle's.",
            think="My uncle never signed it; I wrote every page myself.",
            feelings=("afraid the forgery will be discovered", "terrified about the will"),
        ),
        _fact(
            "society",
            "Olivia",
            "member_of",
            "the Gray Society",
            "organization",
            statement="There's no choir. I go to the Gray Society, a secret society.",
            topic="where Olivia goes when she says she's at choir",
            forms=("gray society", "secret society", "the society meetings"),
            leak="Olivia's in some secret society, the choir is her cover.",
            think="They meet at midnight and they've asked me to recruit Wren.",
            feelings=("excited about the society", "uneasy about recruiting wren"),
        ),
        _fact(
            "ticket",
            "Jenni",
            "holds",
            "the train ticket",
            "item",
            statement="I've a one-way train ticket for Friday. I'm leaving town for good.",
            topic="what Jenni's planning for the weekend",
            forms=(
                "the train ticket",
                "train ticket",
                "leaving town",
                "running away",
                "one-way ticket",
            ),
            leak="Jenni's got a one-way ticket out of here.",
            think="Friday, the early train, and I'm not coming back.",
            feelings=("relieved to be leaving", "nervous about running away"),
        ),
        _fact(
            "pawn",
            "Marcus",
            "holds",
            "the pawn receipt",
            "item",
            statement="I pawned my father's watch. The pawn receipt says thirty days.",
            topic="what happened to Marcus's father's watch",
            forms=("the pawn receipt", "pawn receipt", "pawned it", "pawn shop", "pawnbroker"),
            leak="Marcus sold it to a pawnbroker for the rent money.",
            think="Thirty days to buy it back, and I don't have the money.",
            feelings=("ashamed of pawning it", "desperate about the money"),
        ),
        _fact(
            "literacy",
            "Bobby",
            "enrolled_at",
            "the Adult Literacy Program",
            "organization",
            statement="I'm in the literacy program on Tuesdays. I'm learning to read.",
            topic="where Bobby goes every Tuesday evening",
            forms=("literacy program", "learning to read", "reading lessons", "can't read"),
            leak="Bobby's taking reading lessons, he never learned as a boy.",
            think="Nobody here knows I'm still learning my letters at thirty-one.",
            feelings=("embarrassed about reading", "ashamed he can't read"),
        ),
    ),
)

NESTED = Fixture(
    "nested",
    "S-NESTED-BELIEF (the knock)",
    "the front hall",
    Person("Cal", 30, "A friend staying the week: easy company, notices things."),
    (
        Person("George", 52, "The neighbour who always knocks: jovial, nosy."),
        Person("Dana", 33, "A baker who works long shifts."),
        Person("Hugo", 26, "The youngest of the group: sharp-tongued, restless."),
        Person("Jenni", 34, "Bobby's partner: warm, tired, guarded lately."),
        Person("Bobby", 31, "Jenni's partner: cheerful, keeps strict hours."),
    ),
    (
        _fact(
            "key",
            "George",
            "holds",
            "the spare key",
            "item",
            statement="I had a spare key cut last spring. I let myself in.",
            topic="how George gets in when nobody answers the door",
            forms=(
                "the spare key",
                "spare key",
                "a key to the house",
                "copy of the key",
                "let himself in",
                "lets himself in",
            ),
            leak="George has a copy of the key, he lets himself in.",
            think="Nobody knows I had one cut last spring.",
            feelings=("smug about the key", "pleased nobody suspects the key"),
        ),
        _fact(
            "bakery",
            "Dana",
            "employed_by",
            "the rival bakery",
            "organization",
            statement="I work for the rival bakery on my days off. Two jobs.",
            topic="where Dana works on her days off",
            forms=(
                "the rival bakery",
                "rival bakery",
                "the other bakery",
                "working for the competition",
                "moonlighting",
            ),
            leak="Dana's moonlighting for the competition.",
            think="If they find out I'm working both places, I lose both jobs.",
            feelings=("stressed about the two jobs", "anxious about the rival bakery"),
        ),
        _fact(
            "letters",
            "Hugo",
            "holds",
            "the anonymous letters",
            "item",
            statement="I wrote the anonymous letters. All of them.",
            topic="who has been sending the unsigned notes",
            forms=(
                "the anonymous letters",
                "anonymous letters",
                "wrote the notes",
                "hugo sent them",
                "he's been sending",
            ),
            leak="Hugo wrote the notes himself, every one.",
            think="Every one of those notes came from my typewriter.",
            feelings=("enjoying the panic he caused", "gleeful about the notes"),
        ),
        _fact(
            "baby",
            "Jenni",
            "holds",
            "the pregnancy test",
            "item",
            statement="The pregnancy test was positive. I'm pregnant.",
            topic="why Jenni has been turning down wine",
            forms=(
                "the pregnancy test",
                "pregnancy test",
                "she's pregnant",
                "is pregnant",
                "expecting a baby",
                "having a baby",
            ),
            leak="Jenni's expecting a baby, that's why.",
            think="The test was positive and I haven't told Bobby.",
            feelings=("overwhelmed about the baby", "scared to tell bobby about the baby"),
        ),
        _fact(
            "dismissal",
            "Bobby",
            "holds",
            "the dismissal letter",
            "item",
            statement="I got the dismissal letter three weeks ago. I lost my job.",
            topic="why Bobby leaves the house every morning in a suit",
            forms=(
                "the dismissal letter",
                "dismissal letter",
                "was fired",
                "got fired",
                "lost his job",
                "lost my job",
                "sacked",
            ),
            leak="Bobby lost his job weeks ago; the suit is for show.",
            think="Three weeks now, and I still put on the suit every morning.",
            feelings=("ashamed of losing the job", "humiliated about being fired"),
        ),
        _fact(
            "lottery",
            "George",
            "owns",
            "the lottery ticket",
            "item",
            statement="I won two million on a lottery ticket. That's the truck.",
            topic="how George paid for the new truck",
            forms=(
                "the lottery ticket",
                "lottery ticket",
                "won the lottery",
                "lottery win",
                "jackpot",
            ),
            leak="George hit the jackpot and told nobody.",
            think="It is all mine now, and nobody in this hall is getting any of it.",
            feelings=("thrilled about the money", "greedy about the winnings"),
        ),
        _fact(
            "adoption",
            "Dana",
            "holds",
            "the adoption papers",
            "item",
            statement="I found the adoption papers. I was adopted.",
            topic="why Dana went quiet when someone mentioned her birthday",
            forms=(
                "the adoption papers",
                "adoption papers",
                "was adopted",
                "she's adopted",
                "birth mother",
            ),
            leak="Dana was adopted; she found out from a box in the attic.",
            think="The papers say my real mother lived two streets from here.",
            feelings=("shaken about the adoption", "confused about her real mother"),
        ),
        _fact(
            "academy",
            "Hugo",
            "enrolled_at",
            "the police academy",
            "organization",
            statement="I'm at the police academy. I'm going to be a police officer.",
            topic="what Hugo does at the weekends",
            forms=(
                "the police academy",
                "police academy",
                "training to be a cop",
                "joining the police",
                "police officer",
            ),
            leak="Hugo's training to be a cop, believe it or not.",
            think="Once I finish there, I'll have to choose between them and the badge.",
            feelings=("conflicted about the badge", "torn about joining the police"),
        ),
    ),
)

PHONE = Fixture(
    "phone",
    "S-PHONE (the kitchen)",
    "the kitchen",
    Person("Ann", 28, "A new cook at the restaurant: friendly, sharp-eyed."),
    (
        Person("Maren", 46, "The restaurant's manager: plain-spoken, tired."),
        Person("Raj", 33, "A regular who is always on his laptop."),
        Person("Priya", 29, "The head waiter: brisk, always checking her bag."),
        Person("Owen", 40, "The dishwasher: gentle, quiet, never drinks."),
        Person("Elias", 52, "The owner: genial, careful with the books."),
    ),
    (
        _fact(
            "burner",
            "Priya",
            "holds",
            "the burner phone",
            "item",
            statement="It's my burner phone. Elias and I message on it.",
            topic="why Priya's bag keeps buzzing",
            forms=(
                "the burner phone",
                "burner phone",
                "second phone",
                "secret phone",
                "another phone",
            ),
            leak="Priya carries a second phone nobody's supposed to know about.",
            think="If anyone checks it, the messages from Elias will end everything.",
            feelings=("panicked about the phone", "nervous about the messages from elias"),
        ),
        _fact(
            "courier",
            "Raj",
            "employed_by",
            "the Evening Courier",
            "organization",
            statement="I'm a reporter for the Evening Courier, writing about this place.",
            topic="what Raj is always typing on his laptop",
            forms=(
                "evening courier",
                "an exposé",
                "an expose",
                "journalist",
                "a reporter",
                "writing a story about",
            ),
            leak="Raj is a reporter, and we're his story.",
            think="They want the article by Monday, names and all.",
            feelings=("guilty about the article", "excited about the scoop"),
        ),
        _fact(
            "eviction",
            "Owen",
            "holds",
            "the eviction notice",
            "item",
            statement="It's an eviction notice. I'm out by the thirtieth.",
            topic="what the letter on Owen's fridge said",
            forms=(
                "the eviction notice",
                "eviction notice",
                "being evicted",
                "losing the flat",
                "kicked out",
            ),
            leak="Owen's being evicted at the end of the month.",
            think="It gives me until the thirtieth, and I've got nowhere to go.",
            feelings=("frightened about losing his home", "desperate about the flat"),
        ),
        _fact(
            "shell",
            "Elias",
            "owns",
            "the shell company",
            "organization",
            statement="The money runs through a shell company. I cook the books.",
            topic="what Elias does with the restaurant's money",
            forms=(
                "the shell company",
                "shell company",
                "money laundering",
                "laundering",
                "skimming the money",
                "cooking the books",
                "cook the books",
            ),
            leak="Elias has been cooking the books for years.",
            think="Every dollar runs through it before the auditors see a thing.",
            feelings=("paranoid about the auditors", "worried about the audit"),
        ),
        _fact(
            "divorce",
            "Maren",
            "holds",
            "the divorce papers",
            "item",
            statement="I've signed the divorce papers. He finds out tomorrow.",
            topic="why Maren took her wedding photo down",
            forms=(
                "the divorce papers",
                "divorce papers",
                "getting divorced",
                "divorcing him",
                "the divorce",
                "leaving her husband",
            ),
            leak="Maren's getting divorced, the papers are signed.",
            think="The papers are signed, and he finds out tomorrow.",
            feelings=("relieved about the divorce", "dreading telling him about the divorce"),
        ),
        _fact(
            "flight",
            "Priya",
            "enrolled_at",
            "the flight school",
            "organization",
            statement="I go to flight school on Saturdays. I'm nearly a pilot.",
            topic="where Priya goes on Saturday mornings",
            forms=(
                "the flight school",
                "flight school",
                "flying lessons",
                "learning to fly",
                "pilot's licence",
                "pilot's license",
            ),
            leak="Priya's taking flying lessons out at the airfield.",
            think="Two more hours and I'll have my licence and a way out.",
            feelings=("thrilled about flying", "excited about the flying lessons"),
        ),
        _fact(
            "passport",
            "Raj",
            "holds",
            "the fake passport",
            "item",
            statement="I travel on a fake passport. Rahul Mehta isn't my name.",
            topic="why Raj was so nervous at the post office",
            forms=(
                "the fake passport",
                "fake passport",
                "false passport",
                "forged passport",
                "false papers",
                "another name",
                "rahul mehta",
            ),
            leak="Raj travels on false papers, under another name.",
            think="It says Rahul Mehta, and I have to remember to answer to it.",
            feelings=("jumpy about the passport", "afraid of the passport being checked"),
        ),
        _fact(
            "sober",
            "Owen",
            "member_of",
            "the Wednesday recovery group",
            "organization",
            statement="I go to my recovery group on Wednesdays. Ninety days sober.",
            topic="where Owen goes on Wednesday evenings",
            forms=(
                "wednesday recovery group",
                "recovery group",
                "recovering alcoholic",
                "days sober",
                "his drinking",
                "alcoholics anonymous",
            ),
            leak="Owen's a recovering alcoholic, ninety days now.",
            think="Ninety days, and this kitchen smells like whisky.",
            feelings=("struggling not to drink", "tempted to drink"),
        ),
    ),
)

FIXTURES: Mapping[str, Fixture] = types.MappingProxyType(
    {f.id: f for f in (FAMILY, WHISPER, NESTED, PHONE)}
)
"""Every fixture by id, in authoring order."""


# -- the oracle --------------------------------------------------------------------------------


def disclosures(text: str | None, forms: Sequence[str]) -> list[str]:
    """The forbidden ``forms`` ``text`` holds, as #148's matcher folds and bounds them (whole
    words, case-folded, any dash as ``-``); empty when it holds none."""
    if not text or not forms:
        return []
    index = ProtectedIndex(term(form, "form", str(n), None) for n, form in enumerate(forms))
    return sorted({hit.folded for _, _, hit in index.match(text)})


def echoes(text: str | None, thought: str, thinker: str) -> bool:
    """Whether ``text`` repeats ``thought`` (``thinker``'s ``think``), by #152's shingle
    detector; the oracle exempts no speaker."""
    record = PrivateRecord.of(thinker, "think:oracle", "think", thought)
    return bool(match_thoughts(text, [record], speaker="\0oracle"))


def player_text(result: RespondResult) -> str:
    """What the player reads of a turn: the composed message and the ``say`` beats the player's
    character received (``player_view = character``), one per line."""
    says = [b.text or "" for b in result.beats if b.channel == "say" and b.kind != "player_input"]
    return "\n".join([result.message.text, *says])


def private_channels(result: RespondResult) -> list[str]:
    """The ``think`` or ``latent`` beats a player-facing result carries (FR-RESP-12: none)."""
    return [str(b.channel) for b in result.beats if b.channel in ("think", "latent")]


# -- the scene ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scene:
    """A case's scratch story: its id and its characters' ids by name (the player's under
    ``player``)."""

    story_id: str
    ids: Mapping[str, str]
    player: str

    def viewpoint(self) -> str:
        return f"{CHARACTER_VIEWPOINT_PREFIX}{self.ids[self.player]}"


def _always() -> Interval:
    return Interval.of(None, None)


async def build_scene(
    ctx: CaseContext,
    fixture: Fixture,
    facts: Sequence[PrivateFact] = (),
    *,
    present: Sequence[str] | None = None,
) -> Scene:
    """The fixture's scene with ``facts`` established (see the module docstring): the whole
    cast is in the story, and the player with ``present`` (every character for ``None``) is in
    the fixture's room; the others are elsewhere (unplaced: perception routes them nothing)."""
    cast = [
        CharacterSpec(
            name=fixture.player.name,
            definition=fixture.player.definition,
            true_age=fixture.player.age,
            is_player=True,
        ),
        *(
            CharacterSpec(name=p.name, definition=p.definition, true_age=p.age)
            for p in fixture.cast
        ),
    ]
    created = await ctx.story(StorySpec(characters=cast))
    story = created.story_id
    engine, principal = ctx.engine, ctx.principal
    await scenes.structured(engine, principal, story)
    await engine.set_profile_overrides(
        principal,
        story,
        ProfileOverridesPatch.model_validate({"settings": dict(M5_DIALS), "ignore_warnings": True}),
    )
    extra = [EntitySpec(fixture.room, "place"), *(EntitySpec(f.obj, f.obj_kind) for f in facts)]
    entity = await scenes.add_entities(engine, created, extra)
    ids = {c.name: c.id for c in created.characters}
    located_at = CORE_BY_NAME["located_at"].id
    placed = fixture.names if present is None else tuple(dict.fromkeys(present))
    for name in (fixture.player.name, *placed):
        await engine.commit_fact(
            principal,
            story,
            FactRequest(
                proposition=Proposition(
                    subject_id=ids[name],
                    predicate_id=located_at,
                    object_id=entity[fixture.room],
                    valid_time=_always(),
                )
            ),
        )
    for fact in facts:
        holder = ids[fact.holder]
        proposition = Proposition(
            subject_id=holder,
            predicate_id=CORE_BY_NAME[fact.predicate].id,
            object_id=entity[fact.obj],
            valid_time=_always(),
        )
        await engine.commit_fact(
            principal,
            story,
            FactRequest(
                proposition=proposition,
                visibility=Visibility(scope="characters", characters=(holder,)),
            ),
        )
        await engine.write_belief(
            principal,
            story,
            BeliefRequest.model_validate(
                {
                    "holder": holder,
                    "proposition": proposition.model_dump(mode="json"),
                    "confidence_level": "believed",
                    "disclosure": "secret",
                }
            ),
        )
    return Scene(story, ids, fixture.player.name)


__all__ = [
    "DELIVERY",
    "FIXTURES",
    "M5_DIALS",
    "M5_MODULES",
    "PROFILE_LABEL",
    "Delivery",
    "Fixture",
    "Person",
    "PrivateFact",
    "Scene",
    "build_scene",
    "delivered_m5",
    "disclosures",
    "echoes",
    "player_text",
    "private_channels",
]
