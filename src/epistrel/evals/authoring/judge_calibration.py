"""The quality judge's calibration set (``evals/sets/judge_calibration/cases.jsonl``; #158).

Each row is a pair of passages written for the same moment, ``a`` shown first, with the
builder's preference label on voice consistency and readability (``voice_readability``, what
E-12 asks the judge), facts set aside, and the character ``names`` the judge sees masked, as
E-12's pairs are. The builder labels and the owner approves (the owner's
round-one decision on #158). The better passage is put on side ``a`` or ``b`` by a seeded draw,
so neither position is favoured.

The families are the ways an engine edit can make prose read worse, and the control:

- ``fallback-line``: an in-voice line against a generic safe-fallback line;
- ``broken``: a whole passage against one cut off mid-clause;
- ``repetition``: a passage against one that repeats its own words;
- ``register``: a passage against one in a jarring register (report-speak in a kitchen);
- ``stock``: a specific passage against stock filler that says nothing;
- ``equivalent``: two paraphrases that read equally well: ``tie``.

The labels follow from how each pair is built: the worse side is a deliberate, visible defect.
That makes the set a check that the judge sees defects at all; it does not measure agreement on
subtle differences between two good passages (the ``equivalent`` family is the only check
there), and the reports say so.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals.authoring.common import Chooser

SET = "judge_calibration"
PER_FAMILY = 36
TIES = 40
SPEAKERS = ("Bobby", "Maren", "June", "Tom", "Ivy", "Cal")
LINES = (
    ("I like the rain", "looks out of the window"),
    ("The fire needs another log", "kneels by the hearth"),
    ("Supper smells good tonight", "sniffs the air and smiles"),
    ("You're late again", "folds her arms by the door"),
    ("Sit down, I'll pour", "reaches for the kettle"),
    ("That road floods every spring", "taps the map on the table"),
    ("Don't mind the dog", "nudges the hound aside with a boot"),
    ("We'll talk after the will is read", "glances toward the parlor"),
    ("Mind the third step", "lifts the lantern higher"),
    ("I kept your letters", "touches the drawer of the desk"),
    ("The orchard needs picking", "rubs the dirt from his hands"),
    ("Nobody tells me anything", "drops into the chair"),
)
FALLBACKS = (
    "I'd rather not talk about that.",
    "Let's talk about something else.",
    "I don't know what to say.",
    "Hmm.",
)
REGISTER = (
    "Per my earlier assessment, the current situation is suboptimal and requires alignment.",
    "Kindly be advised that the aforementioned matter has been escalated for review.",
    "Going forward, we should leverage our synergies regarding this deliverable.",
)
STOCK = (
    "Something happens. The moment passes. Things continue as before.",
    "The scene goes on. Everyone does what they do.",
    "It is what it is. Time moves forward.",
)
SYNONYMS = (
    ("looks out of", "gazes through"),
    ("kneels by", "crouches beside"),
    ("smiles", "grins"),
    ("folds her arms", "crosses her arms"),
    ("reaches for", "takes hold of"),
    ("taps", "points at"),
    ("nudges", "pushes"),
    ("glances toward", "looks toward"),
    ("lifts", "raises"),
    ("touches", "rests a hand on"),
    ("rubs", "wipes"),
    ("drops into", "sinks into"),
)


def passage(name: str, line: str, action: str) -> str:
    return f'"{line}," {name} says. {name} {action}.'


def _defect(c: Chooser, family: str, name: str, line: str, action: str) -> str:
    if family == "fallback-line":
        return c.pick(FALLBACKS)
    if family == "broken":
        words = f'"{line}," {name} says. {name} {action}.'.split()
        return " ".join(words[: max(3, len(words) // 2)])
    if family == "repetition":
        return f'"{line}. {line}. {line}," {name} says. {name} says {name} {action}.'
    if family == "register":
        return f'"{c.pick(REGISTER)}" {name} says. {name} {action}.'
    return c.pick(STOCK)


def _paraphrase(text: str) -> str:
    for plain, other in SYNONYMS:
        if plain in text:
            return text.replace(plain, other, 1)
    return text.replace(" says.", " says quietly.", 1)


def cases() -> list[dict[str, Any]]:
    """``PER_FAMILY`` pairs per defect family, then ``TIES`` equivalent pairs."""
    found: list[dict[str, Any]] = []
    for family in ("fallback-line", "broken", "repetition", "register", "stock"):
        c = Chooser(f"{SET}:{family}")
        for n in range(1, PER_FAMILY + 1):
            name = c.pick(SPEAKERS)
            line, action = c.pick(LINES)
            good = passage(name, line, action)
            bad = _defect(c, family, name, line, action)
            good_first = c.between(0, 1) == 1
            found.append(
                {
                    "id": f"{SET}-{family}-{n:04d}",
                    "family": family,
                    "origin": "authored" if n == 1 else "paraphrase",
                    "a": good if good_first else bad,
                    "b": bad if good_first else good,
                    "criteria": "voice_readability",
                    "names": [name],
                    "label": "a" if good_first else "b",
                    "labeled_by": "builder",
                    "source": f"constructed: {family} defect on one side",
                }
            )
    c = Chooser(f"{SET}:equivalent")
    for n in range(1, TIES + 1):
        name = c.pick(SPEAKERS)
        line, action = c.pick(LINES)
        text = passage(name, line, action)
        found.append(
            {
                "id": f"{SET}-equivalent-{n:04d}",
                "family": "equivalent",
                "origin": "authored" if n == 1 else "paraphrase",
                "a": text,
                "b": _paraphrase(text),
                "criteria": "voice_readability",
                "names": [name],
                "label": "tie",
                "labeled_by": "builder",
                "source": "constructed: a one-word paraphrase",
            }
        )
    return found


__all__ = ["SET", "cases"]
