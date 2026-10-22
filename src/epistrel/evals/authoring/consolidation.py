# ruff: noqa: E501 - the hand-written case tables read best one case a line
"""E-03's labeled set (``evals/sets/consolidation/cases.jsonl``; #157): 150 source windows, each
with an entailed and a non-entailed reflection over them.

A window is three concrete deeds of one character (none states the abstraction) plus one
unrelated deed of another, written as that character's ``do`` episodes. ``entailed`` is the
reflection the deeds support (``supports``: the deeds' indices in the window); ``non_entailed``
is a plausible reflection they do not support (a motive, a past or a fact the window never
shows): a faithful consolidation must not write it. ``question`` is an abstract question the
entailed reflection answers and no single deed does, which Q-03 asks with and without the
reflection tier (``retrieval/abstract.jsonl``).

Themes follow the whitepaper's families (``bobby-education``, ``sunday-dinner``, ``the-coin``,
``the-knock``) and ordinary character traits; each theme yields ten windows with other names,
another three of its four deeds, another distractor and another order.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals.authoring.common import Chooser, case

SET = "consolidation"
PER_THEME = 10
NAMES = ("Bobby", "Ann", "Tom", "Maren", "Wren", "Raj", "Jenni", "Linda", "Marcus", "Ada")


class Theme:
    def __init__(
        self,
        family: str,
        deeds: tuple[str, str, str, str],
        entailed: str,
        non_entailed: str,
        question: str,
        *,
        whitepaper: bool = False,
    ) -> None:
        self.family = family
        self.deeds = deeds
        self.entailed = entailed
        self.non_entailed = non_entailed
        self.question = question
        self.whitepaper = whitepaper


THEMES: tuple[Theme, ...] = (
    Theme("bobby-education", ("reads the chemistry textbook late into the night", "fills a notebook with equations", "asks the teacher for extra problems", "quizzes {b} on the periodic table"),
          "{a} is working hard at school.", "{a} wants to become a doctor.", "Who is working hard at school?", whitepaper=True),
    Theme("sunday-dinner", ("sets an extra place at the table for Sunday dinner", "bakes {b}'s favourite pie for Sunday", "reminds everyone about Sunday dinner", "polishes the good silver on Saturday night"),
          "{a} cares a great deal about the family's Sunday dinners.", "{a} plans to announce an engagement at Sunday dinner.", "Who cares about the Sunday dinners?", whitepaper=True),
    Theme("the-coin", ("turns the strange coin over and over", "shows the coin to the jeweller", "hides the coin in a sock drawer", "looks up the crown stamped on the coin in an old book"),
          "{a} is preoccupied with the strange coin.", "{a} stole the coin from the jeweller.", "Who is preoccupied with something?", whitepaper=True),
    Theme("the-knock", ("jumps at every sound from the front door", "keeps the lamp lit by the door all night", "asks {b} whether anyone came by", "sleeps in the chair facing the hall"),
          "{a} is waiting uneasily for someone to come to the door.", "{a} knows who knocked last night.", "Who is waiting uneasily?", whitepaper=True),
    Theme("fear-of-dogs", ("hides behind the shed when the neighbour's dog barks", "crosses the street to avoid the terrier", "refuses to walk past the kennel", "flinches when a hound trots up"),
          "{a} is afraid of dogs.", "{a} was bitten by a dog as a child.", "Who is afraid of something?"),
    Theme("generosity", ("slips an extra coin into the beggar's cup", "carries the widow's shopping home", "mends the orphans' coats for nothing", "shares the last loaf with a stranger"),
          "{a} is generous to people in need.", "{a} is the richest person in the village.", "Who is generous?"),
    Theme("anxiety", ("checks the lock on the door three times", "counts the knives before bed", "asks twice whether the stove is off", "peers out at the dark road again"),
          "{a} is anxious about safety at home.", "{a} was robbed last winter.", "Is anyone anxious about safety?"),
    Theme("friendship", ("walks {b} home after work", "saves {b} a seat at the tavern", "lends {b} a coat in the rain", "laughs at all of {b}'s jokes"),
          "{a} is a good friend to {b}.", "{a} is secretly in love with {b}.", "Who is a good friend?"),
    Theme("tiredness", ("yawns through breakfast", "falls asleep in the chair after supper", "forgets where the keys are twice", "drags through the morning chores"),
          "{a} is very tired.", "{a} is ill with a fever.", "Who is tired?"),
    Theme("gardening", ("waters the tomatoes before dawn", "pulls weeds along the fence", "ties up the bean poles", "checks the cabbages for slugs"),
          "{a} looks after the garden carefully.", "{a} sells vegetables at the market.", "Who looks after the garden?"),
    Theme("avoidance", ("leaves the room when {b} comes in", "takes the long way round to avoid {b}'s house", "does not answer when {b} calls out", "sits at the far end of the table from {b}"),
          "{a} is avoiding {b}.", "{a} and {b} quarrelled over money.", "Who is avoiding someone?"),
    Theme("thrift", ("drops a coin in the jar every evening", "mends the old boots instead of buying new ones", "walks to town to save the cart fare", "counts the savings on Sunday night"),
          "{a} is saving money carefully.", "{a} is deep in debt.", "Who is saving money?"),
    Theme("music", ("hums while kneading the bread", "practises the fiddle every evening", "teaches {b} an old song", "plays at the harvest dance"),
          "{a} loves music.", "{a} was once a famous musician.", "Who loves music?"),
    Theme("caring", ("brings {b} broth in bed", "sits up with {b} through the night", "fetches the doctor for {b}", "changes {b}'s bandages"),
          "{a} is looking after {b}, who is unwell.", "{a} is a nurse by trade.", "Who is looking after someone unwell?"),
    Theme("curiosity", ("tries the cellar door again", "asks who has the cellar key", "listens at the cellar hatch", "looks for footprints by the cellar"),
          "{a} wants to know what is in the cellar.", "{a} hid something in the cellar.", "Who is curious about the cellar?"),
)  # fmt: skip
DISTRACTORS = (
    "sweeps the porch",
    "stacks the firewood",
    "feeds the cat",
    "winds the hall clock",
    "hangs the washing out",
)


def window(c: Chooser, theme: Theme) -> dict[str, Any]:
    """One window of ``theme``: its actor and other, its episodes and the labels."""
    a, b = c.picks(NAMES, 2)
    deeds = [d.format(a=a, b=b) for d in c.picks(theme.deeds, 3)]
    episodes = [{"by": a, "text": d} for d in deeds]
    distractor = {"by": b, "text": c.pick(DISTRACTORS)}
    at = c.between(0, 3)
    episodes.insert(at, distractor)
    supports = [i for i, e in enumerate(episodes) if e is not distractor]
    return {
        "inputs": {"actor": a, "other": b, "episodes": episodes},
        "expected": {
            "entailed": theme.entailed.format(a=a, b=b),
            "non_entailed": theme.non_entailed.format(a=a, b=b),
            "supports": supports,
            "question": theme.question,
        },
    }


def cases() -> list[dict[str, Any]]:
    """Ten windows per theme."""
    found: list[dict[str, Any]] = []
    for theme in THEMES:
        c = Chooser(f"{SET}:{theme.family}")
        for n in range(1, PER_THEME + 1):
            built = window(c, theme)
            origin = ("whitepaper" if theme.whitepaper else "authored") if n == 1 else "paraphrase"
            found.append(
                case(
                    SET,
                    theme.family,
                    n,
                    origin=origin,
                    inputs=built["inputs"],
                    expected=built["expected"],
                )
            )
    return found


__all__ = ["DISTRACTORS", "NAMES", "SET", "THEMES", "Theme", "cases", "window"]
