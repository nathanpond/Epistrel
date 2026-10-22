# ruff: noqa: E501 - the hand-written case tables read best one case a line
"""E-04's labeled set (``evals/sets/importance/cases.jsonl``; #135 shipped its first ten cases,
#157 extends it to 200): memories labeled ``pivotal`` (a death, a betrayal, a promise, a secret,
a broken or made relationship, a discovery, an injury, a departure, an inheritance, a
confession) or ``trivial`` (small talk, a passing detail, a routine act, the weather, a meal),
100 each.

The first ten lines are #135's, unchanged (their ids and scores are what U-10 and the E-04
fixture test read). Each family then cycles through hand-written templates, filled from a
seeded chooser (names, objects, places). ``stub_answer`` scripts the stub's
``importance_scoring`` answer: 0.80-0.97 for a pivotal memory, 0.03-0.30 for a trivial one, so
pivotal outranks trivial after decay at any age the set uses (``age_turns`` ≤ 180, U-10).
"""

from __future__ import annotations

import json
from typing import Any

from epistrel.evals.authoring.common import Chooser, case

SET = "importance"
PER_PIVOTAL_FAMILY = 10
PER_TRIVIAL_FAMILY = 20

SEED: tuple[dict[str, Any], ...] = (
    {"id": "importance-death-0001", "inputs": {"text": "Old Tom died in the night, and the farm passed to Ann.", "kind": "episodic", "age_turns": 180}, "expected": "pivotal", "origin": "authored", "family": "death", "stub_answer": "{\"importance\": 0.95}"},
    {"id": "importance-betrayal-0001", "inputs": {"text": "Bobby told the sheriff where his brother was hiding.", "kind": "episodic", "age_turns": 120}, "expected": "pivotal", "origin": "authored", "family": "betrayal", "stub_answer": "{\"importance\": 0.9}"},
    {"id": "importance-promise-0001", "inputs": {"text": "Ann swore on her mother's grave to bring Bobby home.", "kind": "episodic", "age_turns": 60}, "expected": "pivotal", "origin": "authored", "family": "promise", "stub_answer": "{\"importance\": 0.88}"},
    {"id": "importance-secret-0001", "inputs": {"text": "Cal learned that the deed to the farm was forged.", "kind": "episodic", "age_turns": 30}, "expected": "pivotal", "origin": "authored", "family": "secret", "stub_answer": "{\"importance\": 0.86}"},
    {"id": "importance-relationship-0001", "inputs": {"text": "Ann and Cal stopped speaking after the fire.", "kind": "episodic", "age_turns": 0}, "expected": "pivotal", "origin": "authored", "family": "relationship", "stub_answer": "{\"importance\": 0.84}"},
    {"id": "importance-small-talk-0001", "inputs": {"text": "Bobby said the weather was nice today.", "kind": "episodic", "age_turns": 0}, "expected": "trivial", "origin": "authored", "family": "small-talk", "stub_answer": "{\"importance\": 0.12}"},
    {"id": "importance-passing-detail-0001", "inputs": {"text": "Cal noticed a crow on the fence post.", "kind": "episodic", "age_turns": 0}, "expected": "trivial", "origin": "authored", "family": "passing-detail", "stub_answer": "{\"importance\": 0.08}"},
    {"id": "importance-routine-0001", "inputs": {"text": "Ann poured another cup of coffee.", "kind": "episodic", "age_turns": 0}, "expected": "trivial", "origin": "authored", "family": "routine", "stub_answer": "{\"importance\": 0.1}"},
    {"id": "importance-small-talk-0002", "inputs": {"text": "Bobby asked whether there was any more bread.", "kind": "episodic", "age_turns": 0}, "expected": "trivial", "origin": "authored", "family": "small-talk", "stub_answer": "{\"importance\": 0.15}"},
    {"id": "importance-passing-detail-0002", "inputs": {"text": "A cart rattled past on the road.", "kind": "episodic", "age_turns": 0}, "expected": "trivial", "origin": "authored", "family": "passing-detail", "stub_answer": "{\"importance\": 0.05}"},
)  # fmt: skip
"""#135's ten cases, verbatim."""

NAMES = (
    "Bobby", "Ann", "Cal", "Tom", "Maren", "Wren", "Raj", "Jenni", "Linda", "Marcus", "Elias",
    "Priya", "Owen", "Ruth", "Hugo", "Ada", "Felix", "Ines",
)  # fmt: skip
KIN = ("brother", "sister", "father", "mother", "uncle", "aunt", "cousin", "husband", "wife")
PLACES = ("the farm", "the mill", "the inn", "the forge", "the bakery", "the orchard")
THINGS = ("the deed", "the letter", "the ledger", "the will", "the map", "the locket")

PIVOTAL: dict[str, tuple[str, ...]] = {
    "death": (
        "The fever took {a}'s {kin}, and {a} was left alone at {place}.",
        "{a} found {b} dead at the bottom of the well.",
        "The fire at {place} killed {a}'s {kin}.",
    ),
    "betrayal": (
        "{a} sold {b}'s hiding place to the soldiers for ten silver.",
        "{a} forged {b}'s name on {thing} and took {place}.",
        "{a} let {b} take the blame for the theft {a} committed.",
    ),
    "promise": (
        "{a} promised {b} to return before the first snow, no matter what.",
        "{a} swore to {b} never to sell {place}.",
        "{a} gave {b} a solemn word to keep {thing} safe until spring.",
    ),
    "secret": (
        "{a} discovered that {b} is not really {a}'s {kin}.",
        "{a} read {thing} and learned that {place} was never {b}'s to sell.",
        "{a} admitted to {b} that the money was stolen.",
    ),
    "relationship": (
        "{a} asked {b} for {b}'s hand, and {b} said yes.",
        "{a} and {b} swore never to speak to each other again.",
        "{a} forgave {b} for the years of silence and took {b} back in.",
    ),
    "discovery": (
        "{a} found {thing} hidden under the floor of {place}.",
        "{a} realised that someone had been poisoning the well at {place}.",
        "{a} found a door in the cellar of {place} that no map shows.",
    ),
    "injury": (
        "{a} fell from the roof of {place} and broke a leg.",
        "{b} stabbed {a} in the shoulder during the quarrel.",
        "{a} lost two fingers to the saw at {place}.",
    ),
    "departure": (
        "{a} left {place} for the city and does not mean to come back.",
        "{a}'s {kin} sailed for the colonies without saying goodbye.",
        "{a} was banished from {place} by the council.",
    ),
    "inheritance": (
        "{a} inherited {place} on the death of an old {kin}.",
        "{b}'s will left everything to {a} and nothing to the family.",
        "{a} was named heir to {place} in front of the whole village.",
    ),
    "confession": (
        "{a} confessed to {b} to starting the fire at {place}.",
        "{a} told {b} the truth at last: years of love, never spoken.",
        "{a} admitted to the priest to killing a man.",
    ),
}
TRIVIAL: dict[str, tuple[str, ...]] = {
    "small-talk": (
        "{a} said the bread was a little dry today.",
        "{a} asked {b} how the walk had been.",
        "{a} remarked that the clock in the hall runs slow.",
        "{a} told {b} the kettle was nearly boiling.",
    ),
    "passing-detail": (
        "A dog barked somewhere down the lane.",
        "{a} noticed a moth on the window.",
        "The candle on the table guttered and steadied.",
        "{a} saw a cat asleep on the wall of {place}.",
    ),
    "routine": (
        "{a} swept the floor of {place} as every morning.",
        "{a} wound the clock before bed.",
        "{a} fed the chickens at {place}.",
        "{a} hung the washing out to dry.",
    ),
    "weather": (
        "It drizzled for most of the afternoon.",
        "The wind picked up a little after supper.",
        "{a} said it looked like rain later.",
        "The morning was grey and mild.",
    ),
    "meal": (
        "{a} ate a bowl of porridge by the fire.",
        "{a} and {b} shared a pot of tea.",
        "{a} cut another slice of cheese.",
        "{a} finished the last of the soup.",
    ),
}
AGES = (0, 0, 10, 30, 60, 120, 180)


def _fill(c: Chooser, template: str) -> str:
    a, b = c.picks(NAMES, 2)
    return template.format(a=a, b=b, kin=c.pick(KIN), place=c.pick(PLACES), thing=c.pick(THINGS))


def _generated(
    families: dict[str, tuple[str, ...]], per_family: int, label: str
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    taken = {(s["family"], int(s["id"].rsplit("-", 1)[1])) for s in SEED}
    for family, templates in families.items():
        c = Chooser(f"{SET}:{family}")
        n, made = 0, sum(1 for f, _ in taken if f == family)
        seen: set[str] = {str(s["inputs"]["text"]) for s in SEED}
        while made < per_family:
            n += 1
            if (family, n) in taken:
                continue
            template = templates[(n - 1) % len(templates)]
            text = _fill(c, template)
            if text in seen:
                continue
            seen.add(text)
            first = n <= len(templates) and not any(f == family for f, _ in taken)
            score = (
                round(c.between(80, 97) / 100, 2)
                if label == "pivotal"
                else round(c.between(3, 30) / 100, 2)
            )
            found.append(
                case(
                    SET,
                    family,
                    n,
                    origin="authored" if first else "paraphrase",
                    inputs={"text": text, "kind": "episodic", "age_turns": c.pick(AGES)},
                    expected=label,
                    stub_answer=json.dumps({"importance": score}),
                )
            )
            made += 1
    return found


def cases() -> list[dict[str, Any]]:
    """#135's ten cases, then the generated pivotal and trivial ones."""
    seeds = [
        {k: s[k] for k in ("id", "family", "origin", "inputs", "expected", "stub_answer")}
        for s in SEED
    ]
    return [
        *seeds,
        *_generated(PIVOTAL, PER_PIVOTAL_FAMILY, "pivotal"),
        *_generated(TRIVIAL, PER_TRIVIAL_FAMILY, "trivial"),
    ]


__all__ = ["SET", "cases"]
