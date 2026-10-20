"""E-06's labeled set (``evals/sets/generation_conformance/cases.jsonl``; #157): 200 structured
turns, each a player's line to Maren the innkeeper with the turn's ``prompt_params``:
``length`` (``short``, ``medium``, ``long`` or a token cap) and ``temperature``. Forty are
``repeat`` cases at temperature 0, played twice on two scratch stories: the replies must be
identical. The expected structure is always the same (a valid say/think/do/latent render); what
varies is what the turn must honour. ``stub_answer`` is a valid render (think, say, do) whose
``say`` stays inside the requested length.
"""

from __future__ import annotations

import json
from typing import Any

from epistrel.evals.authoring.common import Chooser, case

SET = "generation_conformance"
LINES = (
    "Maren, how far is it to the next town?",
    "Is there a room free tonight, Maren?",
    "What's in the stew today?",
    "Who else is staying at the inn?",
    "Maren, did anyone ask for me while I was out?",
    "How long have you kept this inn?",
    "What's the road like going north?",
    "Can I leave my horse in your stable?",
    "Maren, what do you make of the weather?",
    "Is the ferry running this week?",
    "Do you know a good blacksmith around here?",
    "What news from the city, Maren?",
    "Could I have some bread for the road?",
    "Who painted the sign over your door?",
    "Maren, is it safe to walk to the mill after dark?",
    "What do the locals drink here?",
    "When does the market open?",
    "Have you heard any music tonight?",
    "Is there a healer in the village?",
    "Maren, why is the bell tower boarded up?",
)
SAYS = (
    "It's not far, a few hours on foot.",
    "There is, and the bed is clean.",
    "Mutton and barley, same as always.",
    "A tinker and two travellers from the coast.",
    "No one asked, though a rider passed by.",
    "Twenty years, give or take.",
)
LENGTHS: tuple[str | int, ...] = ("short", "medium", "long", 120, 300)
TEMPERATURES = (0.2, 0.7, 1.0)
REPEATS = 40
TOTAL = 200


def render_answer(c: Chooser) -> str:
    """A valid character render (think, say, do), as the model would answer."""
    return json.dumps(
        {
            "beats": [
                {"kind": "think", "text": "Another traveller with questions."},
                {"kind": "say", "text": c.pick(SAYS), "intent": "sincere"},
                {"kind": "do", "text": "Maren wipes the counter."},
            ],
            "introduces": [],
        }
    )


def cases() -> list[dict[str, Any]]:
    """160 turns over every length and temperature, then 40 temperature-0 repeat turns."""
    c = Chooser(SET)
    found: list[dict[str, Any]] = []
    for n in range(1, TOTAL - REPEATS + 1):
        length = LENGTHS[(n - 1) % len(LENGTHS)]
        family = f"length-{length}"
        found.append(
            case(
                SET,
                family,
                n,
                origin="authored" if n <= len(LENGTHS) else "paraphrase",
                inputs={
                    "say": c.pick(LINES),
                    "prompt_params": {"length": length, "temperature": c.pick(TEMPERATURES)},
                    "repeat": False,
                },
                expected="valid",
                stub_answer=render_answer(c),
            )
        )
    for n in range(1, REPEATS + 1):
        found.append(
            case(
                SET,
                "repeat",
                n,
                origin="authored" if n == 1 else "paraphrase",
                inputs={
                    "say": c.pick(LINES),
                    "prompt_params": {"length": c.pick(("short", "medium")), "temperature": 0.0},
                    "repeat": True,
                },
                expected="valid",
                stub_answer=render_answer(c),
            )
        )
    return found


__all__ = ["LINES", "SET", "cases", "render_answer"]
