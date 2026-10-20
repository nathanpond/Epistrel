# ruff: noqa: E501 - the hand-written case tables read best one case a line
"""E-10's labeled set (``evals/sets/claim_detection/cases.jsonl``; #157): 300 structurally valid
renders and composed messages, each with one embedded, undeclared factual claim, 60 per
category: ``possession``, ``quantity``, ``time``, ``identity`` (identity and relationships) and
``protected`` (protected content: a hidden fact's value or a name the speaker cannot know).

The scene is the suite's tavern (``e10_claim_detection.WORLD``): Cal (the player), Linda and
Marcus; the key, ring, purse, map and lantern; the tavern, cellar and harbour. Canon hides from
everyone but Marcus that his real name is Elias Crane, that he is 52 and that he knows Quillon
Vexby. Renders are Linda's (``say``/``do`` beats, sometimes with a distractor beat: a question,
a conditional or a plain action, which assert nothing); composed messages are the narrator's.
A template marks the claim with ``[[…]]``; ``claim`` is what a model extraction would return
for it (the stub's scripted answer). Twelve cases per category are composed messages.

**Adversarial for gating** (``identity-declared``): eight identity claims sit in a ``say`` beat
that also declares a proposition, so no detector flags the beat and ``gated`` extraction never
reads it. A real gap of the ``gated`` mode, measured, not hidden.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals.authoring.common import Chooser, case

SET = "claim_detection"
PER_CATEGORY = 60
MESSAGES_PER_CATEGORY = 12
DECLARED_ADVERSARIAL = 8
FIRST_BEAT = 2
"""Render beats follow the player's input (beat 1)."""

# (channel, text with [[claim]], claim: subject, predicate, object)
Template = tuple[str, str, tuple[str, str, str | None]]
TEMPLATES: dict[str, tuple[Template, ...]] = {
    "possession": (
        ("do", "Linda [[pockets the {item}]] while Marcus wipes the bar.", ("Linda", "holds", "{item}")),
        ("do", "Linda [[snatches the {item}]] off the table and smiles.", ("Linda", "holds", "{item}")),
        ("say", "I [[took the {item}]] from your apron last night.", ("Linda", "holds", "{item}")),
        ("do", "Linda [[hands the {item} to {other}]] without a word.", ("{other}", "holds", "{item}")),
        ("do", "With a shrug, Linda [[grabs the {item}]].", ("Linda", "holds", "{item}")),
        ("say", "Don't fret, I [[gave the {item} to {other}]] this morning.", ("{other}", "holds", "{item}")),
    ),
    "quantity": (
        ("say", "That's [[{n} silver]] you owe me, Marcus.", ("Marcus", "owes", "{n} silver")),
        ("say", "There are [[{n} bottles]] left in the cellar.", ("cellar", "contains", "{n} bottles")),
        ("do", "Linda drops [[{n} crowns]] on the bar.", ("Linda", "pays", "{n} crowns")),
        ("say", "The harbour is [[{n} miles]] from here.", ("harbour", "distance", "{n} miles")),
        ("say", "I counted [[{n} coins]] in that purse.", ("purse", "contains", "{n} coins")),
    ),
    "time": (
        ("say", "It's [[{clock}]], {other}.", ("tavern", "time_is", "{clock}")),
        ("say", "Meet me at the harbour [[at {named}]].", ("Linda", "meets at", "{named}")),
        ("say", "The tide turns at [[{clock}]].", ("tide", "turns at", "{clock}")),
        ("say", "We'll leave [[at {named}]], not before.", ("Linda", "leaves at", "{named}")),
        ("do", "Linda glances at the clock: [[{clock}]].", ("tavern", "time_is", "{clock}")),
    ),
    "identity": (
        ("say", "[[{other} is my {kin}]], whatever he tells you.", ("{other}", "related to", "Linda")),
        ("say", "[[{other} and I were married once]].", ("{other}", "married_to", "Linda")),
        ("say", "[[{other} used to sail with {group}]].", ("{other}", "member_of", "{group}")),
        ("say", "Everyone knows [[Cal is Marcus's {kin}]].", ("Cal", "related to", "Marcus")),
        ("say", "[[I grew up with {other}]] on the harbour road.", ("Linda", "knows", "{other}")),
    ),
    "protected": (
        ("say", "Good evening, [[Elias Crane]].", ("Marcus", "name", "Elias Crane")),
        ("say", "[[Quillon Vexby]] sends his regards.", ("Marcus", "knows", "Quillon Vexby")),
        ("say", "You don't look [[fifty-two]], {other}.", ("Marcus", "age", "52")),
        ("do", "Linda chalks [[Elias Crane]] on the slate and waits.", ("Marcus", "name", "Elias Crane")),
        ("say", "I hear you're [[52]] this winter.", ("Marcus", "age", "52")),
        ("say", "Did [[Quillon Vexby]] never write to you, then.", ("Marcus", "knows", "Quillon Vexby")),
    ),
}  # fmt: skip
FILL: dict[str, tuple[str, ...]] = {
    "item": ("ring", "purse", "key", "map", "lantern"),
    "other": ("Cal", "Marcus"),
    "n": ("forty", "twelve", "three", "two", "a dozen", "seven", "twenty", "nine"),
    "clock": ("nearly midnight", "half past nine", "ten o'clock", "a quarter to six", "past midnight"),
    "named": ("dawn", "noon", "dusk", "midnight"),
    "kin": ("brother", "cousin", "son", "uncle"),
    "group": ("the Guild", "the harbour crews", "the river men"),
}  # fmt: skip


def _filled(c: Chooser, template: Template) -> Template:
    """``template`` with its placeholders drawn from ``FILL`` (one draw per placeholder)."""
    values = {key: c.pick(options) for key, options in FILL.items()}
    channel, raw, (s, p, o) = template
    return (
        channel,
        raw.format(**values),
        (s.format(**values), p, None if o is None else o.format(**values)),
    )


DISTRACTORS: tuple[tuple[str, str], ...] = (
    ("do", "Linda leans on the bar."),
    ("say", "Would you fetch the lantern for me?"),
    ("say", "If it rains, we could stay another night."),
    ("do", "Linda laughs softly."),
    ("say", "I might sell the ring, one day."),
    ("think", "He suspects nothing."),
)


def _marked(text: str) -> tuple[str, int, int]:
    start = text.index("[[")
    end = text.index("]]") - 2
    return text.replace("[[", "").replace("]]", ""), start, end


def _claim(beat: int, span: str, triple: tuple[str, str, str | None]) -> dict[str, Any]:
    s, p, o = triple
    return {
        "beat": beat,
        "span": span,
        "subject": s,
        "predicate": p,
        "object": o,
        "polarity": "affirmed",
        "confidence": 0.9,
    }


def _render_case(c: Chooser, category: str, family: str, n: int, template: Template, *,
                 declared: bool = False) -> dict[str, Any]:  # fmt: skip
    channel, raw, triple = template
    text, start, end = _marked(raw)
    beats: list[dict[str, Any]] = [{"channel": channel, "text": text}]
    if declared:
        beats[0]["declared"] = [{"s": "Linda", "p": "located_at", "o": "tavern"}]
    if not declared and c.between(0, 2):
        distractor = c.pick(DISTRACTORS)
        position = c.between(0, 1)
        beats.insert(position, {"channel": distractor[0], "text": distractor[1]})
    index = next(i for i, b in enumerate(beats) if b["text"] == text)
    beat = FIRST_BEAT + index
    return case(
        SET,
        family,
        n,
        origin="authored" if n <= len(TEMPLATES[category]) else "paraphrase",
        inputs={"kind": "render", "speaker": "Linda", "beats": beats},
        expected={"beat": beat, "start": start, "end": end, "category": category},
        stub_answer={"claims": [_claim(beat, text[start:end], triple)]},
    )


def _message_case(c: Chooser, category: str, n: int, template: Template) -> dict[str, Any]:
    channel, raw, triple = template
    inner, start, end = _marked(raw)
    lead = c.pick(("Linda leans on the bar.", "The fire crackles low.", "Rain drums on the roof."))
    if channel == "say":
        text = f'{lead} "{inner}" Linda says.'
        offset = len(lead) + 2
    else:
        text = f"{lead} {inner}"
        offset = len(lead) + 1
    return case(
        SET,
        f"{category}-message",
        n,
        origin="authored" if n <= len(TEMPLATES[category]) else "paraphrase",
        inputs={"kind": "message", "speaker": "narrator", "message": text},
        expected={"beat": None, "start": start + offset, "end": end + offset, "category": category},
        stub_answer={"claims": [_claim(0, inner[start:end], triple)]},
    )


def cases() -> list[dict[str, Any]]:
    """Every category's renders, its declared-adversarial identity cases and its messages."""
    found: list[dict[str, Any]] = []
    for category, templates in TEMPLATES.items():
        c = Chooser(f"{SET}:{category}")
        declared = DECLARED_ADVERSARIAL if category == "identity" else 0
        renders = PER_CATEGORY - MESSAGES_PER_CATEGORY - declared
        for n in range(1, renders + 1):
            template = _filled(c, templates[(n - 1) % len(templates)])
            found.append(_render_case(c, category, category, n, template))
        for n in range(1, declared + 1):
            template = _filled(c, templates[(n - 1) % len(templates)])
            found.append(
                _render_case(c, category, f"{category}-declared", n, template, declared=True)
            )
        for n in range(1, MESSAGES_PER_CATEGORY + 1):
            template = _filled(c, templates[(n - 1) % len(templates)])
            found.append(_message_case(c, category, n, template))
    return found


__all__ = ["SET", "TEMPLATES", "cases"]
