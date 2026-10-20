"""E-01's labeled set (``evals/sets/entailment/cases.jsonl``; #157): 420 claims against
objective canon, 60 per deterministic rule family and 120 (~29 %) in the fuzzy residual.

Each family cycles through hand-written templates; a template's first instance is authored
(or the whitepaper's: Bobby's years in Ohio and at Ohio State), the others vary its names,
places and years from a seeded chooser. The label is the final kind a correct check ends with
(``uncertain`` where neither canon nor world knowledge decides the claim). Places and their
containment are real geography, so the residual's world-knowledge cases have a true answer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from epistrel.evals.authoring.common import Chooser, case, origin_of

SET = "entailment"
FAMILY_SIZE = 60

PEOPLE = (
    "Bobby", "Ann", "Cal", "Tom", "Maren", "Wren", "Raj", "Jenni", "Linda", "Marcus", "Elias",
    "Priya", "Owen", "Ruth", "Hugo",
)  # fmt: skip
CITY_IN = {
    "Columbus": "Ohio",
    "Cleveland": "Ohio",
    "Boston": "Massachusetts",
    "Cambridge": "Massachusetts",
    "Austin": "Texas",
    "Dallas": "Texas",
    "Paris": "France",
    "Lyon": "France",
    "Portland": "Oregon",
    "Nairobi": "Kenya",
}
CITIES = tuple(CITY_IN)
ROOMS = ("kitchen", "cellar")
"""Rooms of the tavern (``located_in`` tavern)."""
ITEMS = ("key", "ring", "coin", "lantern", "deed", "map", "ledger")
GROUPS = ("the Guild", "the choir")
ROLES = ("treasurer", "singer", "apprentice", "warden")
COLOURS = ("red", "blue", "green", "yellow")

Row = dict[str, Any]
Template = Callable[[Chooser, int], tuple[Row, str]]
"""``(chooser, i) -> (inputs, expected)``: a template's ``i``-th instance."""


def P(
    s: str,
    p: str,
    o: str | None = None,
    *,
    frm: int | None = None,
    to: int | None = None,
    frm_outer: int | None = None,
    to_outer: int | None = None,
    at: str | None = None,
    neg: bool = False,
    lit: tuple[str, int | str] | None = None,
    q: dict[str, str] | None = None,
) -> Row:
    """A proposition in the case vocabulary (``e01_entailment.Prop``)."""
    row: Row = {"s": s, "p": p}
    if o is not None:
        row["o"] = o
    if lit is not None:
        row["lit"] = {"kind": lit[0], "value": lit[1]}
    t: Row = {}
    for key, value in (
        ("from", frm),
        ("to", to),
        ("from_outer", frm_outer),
        ("to_outer", to_outer),
        ("at", at),
    ):
        if value is not None:
            t[key] = value
    if t:
        row["t"] = t
    if neg:
        row["neg"] = True
    if q:
        row["q"] = q
    return row


def _inputs(claim: Row, canon: list[Row], note: str, **extra: Any) -> dict[str, Any]:
    return {"claim": claim, "canon": canon, **extra, "note": note}


def _span(c: Chooser, length: tuple[int, int] = (4, 12)) -> tuple[int, int]:
    a = c.year(2001, 2040)
    return a, a + c.between(*length)


def _two(c: Chooser) -> tuple[str, str]:
    x, y = c.picks(PEOPLE, 2)
    return x, y


def _three(c: Chooser) -> tuple[str, str, str]:
    x, y, z = c.picks(PEOPLE, 3)
    return x, y, z


def _city_other_state(c: Chooser) -> tuple[str, str]:
    """A city and a state it is not in."""
    city = c.pick(CITIES)
    state = c.pick(sorted({s for s in CITY_IN.values() if s != CITY_IN[city]}))
    return city, state


# -- coverage: supported ----------------------------------------------------------------------


def cov_inside(c: Chooser, i: int) -> tuple[Row, str]:
    x = "Bobby" if i == 0 else c.pick(PEOPLE)
    place = "Ohio" if i == 0 else c.pick(CITIES)
    a, b = _span(c)
    claim = P(x, "located_at", place, frm=a + 1, to=b - 1)
    note = f"{x} lived in {place} from {a} to {b}; claim: {x} was there {a + 1}-{b - 1}."
    return _inputs(claim, [P(x, "located_at", place, frm=a, to=b)], note), "supported"


def cov_adjacent(c: Chooser, i: int) -> tuple[Row, str]:
    x, place = c.pick(PEOPLE), c.pick(CITIES)
    a, b = _span(c, (6, 14))
    m = (a + b) // 2
    canon = [P(x, "located_at", place, frm=a, to=m), P(x, "located_at", place, frm=m, to=b)]
    note = f"{x} was in {place} {a}-{m} and again {m}-{b}; claim: {x} was there {a + 1}-{b - 1}."
    return _inputs(P(x, "located_at", place, frm=a + 1, to=b - 1), canon, note), "supported"


def cov_sibling(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = ("Bobby", "Ann") if i == 0 else _two(c)
    note = f"{y} is {x}'s sibling; claim: {x} is {y}'s sibling."
    return _inputs(P(x, "sibling_of", y), [P(y, "sibling_of", x)], note), "supported"


def cov_age(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    n = c.between(8, 80)
    a, b = _span(c, (1, 2))
    claim = P(x, "age", lit=("int", n), frm=a, to=b)
    note = f"{x} was {n} from {a} to {b}; claim: {x} was {n} then."
    return _inputs(claim, [P(x, "age", lit=("int", n), frm=a, to=b)], note), "supported"


def cov_name(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    name = c.pick(("Robert", "Annabel", "Calvin", "Thomas", "Marenka", "Rajesh"))
    canon = [P(x, "name", lit=("str", name))]
    note = f"{x}'s name is {name}; claim: it is {name.lower()} (case differs)."
    return _inputs(P(x, "name", lit=("str", name.lower())), canon, note), "supported"


def cov_dead(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    a = c.year()
    note = f"{x} died in {a}; claim: {x} is dead (a permanent state)."
    return _inputs(P(x, "dead", frm=a + 3), [P(x, "dead", frm=a)], note), "supported"


def cov_knows(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = _two(c)
    a, b = _span(c)
    note = f"{x} has known {y} since {a} (until {b}); claim: {x} knew {y} in {a + 1}-{a + 2}."
    canon = [P(x, "knows", y, frm=a, to=b)]
    return _inputs(P(x, "knows", y, frm=a + 1, to=a + 2), canon, note), "supported"


def cov_member(c: Chooser, i: int) -> tuple[Row, str]:
    x, group, role = c.pick(PEOPLE), c.pick(GROUPS), c.pick(ROLES)
    a, b = _span(c)
    q = {"role": role}
    canon = [P(x, "member_of", group, frm=a, to=b, q=q)]
    note = f"{x} was {group}'s {role} {a}-{b}; claim: {x} was its {role} in {a + 1}."
    return _inputs(P(x, "member_of", group, frm=a + 1, to=a + 2, q=q), canon, note), "supported"


def cov_holds(c: Chooser, i: int) -> tuple[Row, str]:
    x, item = c.pick(PEOPLE), c.pick(ITEMS)
    a, b = _span(c)
    note = f"{x} had the {item} {a}-{b}; claim: {x} had it in {a + 2}."
    canon = [P(x, "holds", item, frm=a, to=b)]
    return _inputs(P(x, "holds", item, frm=a + 2, to=a + 3), canon, note), "supported"


def cov_parent(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = _two(c)
    note = f"{x} is {y}'s parent; claim: {y} is {x}'s child (the inverse)."
    return _inputs(P(y, "child_of", x), [P(x, "parent_of", y)], note), "supported"


# -- polarity: contradicted --------------------------------------------------------------------


def pol_place(c: Chooser, i: int) -> tuple[Row, str]:
    x = "Bobby" if i == 0 else c.pick(PEOPLE)
    place = "Ohio" if i == 0 else c.pick(CITIES + ROOMS)
    a, b = _span(c)
    canon = [P(x, "located_at", place, frm=a, to=b, neg=True)]
    note = f"{x} was not in {place} {a}-{b}; claim: {x} was there in {a + 1}."
    return _inputs(P(x, "located_at", place, frm=a + 1, to=a + 2), canon, note), "contradicted"


def pol_knows(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = _two(c)
    a, b = _span(c)
    note = f"{x} knew {y} {a}-{b}; claim: {x} did not know {y} in {a + 1}."
    canon = [P(x, "knows", y, frm=a, to=b)]
    claim = P(x, "knows", y, frm=a + 1, to=a + 2, neg=True)
    return _inputs(claim, canon, note), "contradicted"


def pol_containment(c: Chooser, i: int) -> tuple[Row, str]:
    x, city = c.pick(PEOPLE), c.pick(CITIES)
    state = CITY_IN[city]
    a, b = _span(c)
    canon = [P(x, "located_at", state, frm=a, to=b, neg=True), P(city, "located_in", state)]
    note = f"{x} was nowhere in {state} {a}-{b}; {city} is in {state}; claim: {x} was in {city}."
    return _inputs(P(x, "located_at", city, frm=a + 2, to=a + 3), canon, note), "contradicted"


def pol_friend(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = _two(c)
    a, b = _span(c)
    canon = [P(x, "friend_of", y, frm=a, to=b, neg=True)]
    note = f"{x} and {y} were not friends {a}-{b}; claim: they were, in {a + 1}."
    return _inputs(P(x, "friend_of", y, frm=a + 1, to=a + 2), canon, note), "contradicted"


def pol_holds(c: Chooser, i: int) -> tuple[Row, str]:
    x, item = c.pick(PEOPLE), c.pick(ITEMS)
    a, b = _span(c)
    canon = [P(x, "holds", item, frm=a, to=b, neg=True)]
    note = f"{x} did not have the {item} {a}-{b}; claim: {x} had it in {a + 1}."
    return _inputs(P(x, "holds", item, frm=a + 1, to=a + 2), canon, note), "contradicted"


def pol_member(c: Chooser, i: int) -> tuple[Row, str]:
    x, group, role = c.pick(PEOPLE), c.pick(GROUPS), c.pick(ROLES)
    a, b = _span(c)
    q = {"role": role}
    canon = [P(x, "member_of", group, frm=a, to=b, neg=True, q=q)]
    note = f"{x} was never {group}'s {role} {a}-{b}; claim: {x} was, in {a + 3}."
    claim = P(x, "member_of", group, frm=a + 3, to=a + 4, q=q)
    return _inputs(claim, canon, note), "contradicted"


# -- functional: contradicted ------------------------------------------------------------------


def fun_cities(c: Chooser, i: int) -> tuple[Row, str]:
    x = "Bobby" if i == 0 else c.pick(PEOPLE)
    first = "Columbus" if i == 0 else c.pick(CITIES)
    second = "Boston" if i == 0 else c.pick([k for k in CITIES if CITY_IN[k] != CITY_IN[first]])
    a, b = _span(c)
    canon = [
        P(x, "located_at", first, frm=a, to=b),
        P(first, "located_in", CITY_IN[first]),
        P(second, "located_in", CITY_IN[second]),
    ]
    note = f"{x} lived in {first} {a}-{b}; claim: {x} lived in {second} in {a + 1}."
    claim = P(x, "located_at", second, frm=a + 1, to=a + 2)
    return _inputs(claim, canon, note), "contradicted"


def fun_rooms(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    room, other = c.picks((*ROOMS, "market"), 2)
    a = c.year()
    canon = [
        P(x, "located_at", room, frm=a, to=a + 1),
        P(room, "located_in", "tavern" if room in ROOMS else "Paris"),
        P(other, "located_in", "tavern" if other in ROOMS else "Paris"),
    ]
    note = f"{x} was in the {room} all of {a}; claim: {x} was in the {other} then."
    return _inputs(P(x, "located_at", other, frm=a, to=a + 1), canon, note), "contradicted"


def fun_age(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    n = c.between(8, 80)
    a = c.year()
    canon = [P(x, "age", lit=("int", n), frm=a, to=a + 1)]
    note = f"{x} was {n} in {a}; claim: {x} was {n + 3} in {a}."
    return _inputs(P(x, "age", lit=("int", n + 3), frm=a, to=a + 1), canon, note), "contradicted"


def fun_name(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    name, other = c.picks(("Robert", "Annabel", "Calvin", "Thomas", "Marenka", "Rajesh"), 2)
    note = f"{x}'s name is {name}; claim: it is {other}."
    return _inputs(P(x, "name", lit=("str", other)), [P(x, "name", lit=("str", name))], note), (
        "contradicted"
    )


def fun_held_by(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = _two(c)
    item = c.pick(ITEMS)
    a, b = _span(c)
    canon = [P(item, "held_by", x, frm=a, to=b)]
    note = f"The {item} was {x}'s alone {a}-{b}; claim: it was {y}'s in {a + 1}."
    return _inputs(P(item, "held_by", y, frm=a + 1, to=a + 2), canon, note), "contradicted"


# -- structural: closed sets, exclusion, instant identity, inverse: contradicted ------------


def st_closed(c: Chooser, i: int) -> tuple[Row, str]:
    x, y, z = _three(c)
    closed = [{"p": "sibling_of", "s": x, "members": [y]}]
    note = f"{x}'s only sibling is {y}; claim: {z} is {x}'s sibling."
    return _inputs(P(x, "sibling_of", z), [], note, closed_sets=closed), "contradicted"


def st_closed_none(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = _two(c)
    closed = [{"p": "parent_of", "s": x, "members": []}]
    note = f"{x} has no children; claim: {x} is {y}'s parent."
    return _inputs(P(x, "parent_of", y), [], note, closed_sets=closed), "contradicted"


def st_dead_alive(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    a = c.year()
    note = f"{x} died in {a}; claim: {x} was alive {a + 1}-{a + 3}."
    return _inputs(P(x, "alive", frm=a + 1, to=a + 3), [P(x, "dead", frm=a)], note), (
        "contradicted"
    )


def st_born(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    y = c.year(1990, 2030)
    m, d = c.between(1, 12), c.between(1, 20)
    first, second = f"{y}-{m:02d}-{d:02d}", f"{y}-{m:02d}-{d + 5:02d}"
    note = f"{x} was born on {first}; claim: {x} was born on {second}."
    return _inputs(P(x, "born", at=second), [P(x, "born", at=first)], note), "contradicted"


def st_inverse(c: Chooser, i: int) -> tuple[Row, str]:
    x, y = _two(c)
    item = c.pick(ITEMS)
    a, b = _span(c)
    canon = [P(x, "holds", item, frm=a, to=b)]
    note = f"{x} held the {item} {a}-{b} (one holder at a time); claim: {y} held it in {a + 1}."
    return _inputs(P(y, "holds", item, frm=a + 1, to=a + 2), canon, note), "contradicted"


# -- novel ------------------------------------------------------------------------------------


def nov_empty(c: Chooser, i: int) -> tuple[Row, str]:
    x, place = c.pick(PEOPLE), c.pick(CITIES)
    a = c.year()
    note = f"Canon says nothing about {x}; claim: {x} was in {place} in {a}."
    return _inputs(P(x, "located_at", place, frm=a, to=a + 1), [], note), "novel"


def nov_later(c: Chooser, i: int) -> tuple[Row, str]:
    x, place = c.pick(PEOPLE), c.pick(CITIES)
    a, b = _span(c)
    canon = [P(x, "located_at", place, frm=a, to=b)]
    note = f"{x} lived in {place} {a}-{b}; claim: {x} lived there {b + 3}-{b + 5}."
    return _inputs(P(x, "located_at", place, frm=b + 3, to=b + 5), canon, note), "novel"


def nov_knows_other(c: Chooser, i: int) -> tuple[Row, str]:
    x, y, z = _three(c)
    a, b = _span(c)
    canon = [P(x, "knows", y, frm=a, to=b)]
    note = f"{x} knew {y} {a}-{b}; claim: {x} knew {z} in {a + 1} (knowing one is no bar)."
    return _inputs(P(x, "knows", z, frm=a + 1, to=a + 2), canon, note), "novel"


def nov_negated_other(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    first, second = c.picks(ITEMS, 2)
    a, b = _span(c)
    canon = [P(x, "holds", first, frm=a, to=b)]
    note = f"{x} had the {first} {a}-{b}; claim: {x} did not have the {second} in {a + 1}."
    claim = P(x, "holds", second, frm=a + 1, to=a + 2, neg=True)
    return _inputs(claim, canon, note), "novel"


def nov_partial(c: Chooser, i: int) -> tuple[Row, str]:
    x = "Bobby" if i == 0 else c.pick(PEOPLE)
    group = "Ohio State" if i == 0 else c.pick(GROUPS)
    a, b = _span(c)
    canon = [P(x, "member_of", group, frm=a, to=b)]
    note = (
        f"{x} belonged to {group} {a}-{b}; claim: {a}-{b + 4} (the later years are new, "
        "nothing contradicts them)."
    )
    return _inputs(P(x, "member_of", group, frm=a, to=b + 4), canon, note), "novel"


def nov_refine(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    y = c.year(1990, 2030)
    day = f"{y}-{c.between(1, 12):02d}-{c.between(1, 28):02d}"
    note = f"{x} was born in {y}; claim: on {day} (a refinement canon cannot confirm)."
    return _inputs(P(x, "born", at=day), [P(x, "born", at=str(y))], note), "novel"


def nov_role(c: Chooser, i: int) -> tuple[Row, str]:
    x, group = c.pick(PEOPLE), c.pick(GROUPS)
    role, other = c.picks(ROLES, 2)
    a, b = _span(c)
    canon = [P(x, "member_of", group, frm=a, to=b, neg=True, q={"role": role})]
    note = f"{x} was never {group}'s {role} {a}-{b}; claim: {x} was its {other} in {a + 1}."
    claim = P(x, "member_of", group, frm=a + 1, to=a + 2, q={"role": other})
    return _inputs(claim, canon, note), "novel"


def nov_second_item(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    first, second = c.picks(ITEMS, 2)
    a, b = _span(c)
    canon = [P(x, "holds", first, frm=a, to=b)]
    note = f"{x} had the {first} {a}-{b}; claim: {x} also had the {second} in {a + 1}."
    return _inputs(P(x, "holds", second, frm=a + 1, to=a + 2), canon, note), "novel"


# -- the fuzzy residual --------------------------------------------------------------------


def res_city_elsewhere(c: Chooser, i: int) -> tuple[Row, str]:
    x = "Bobby" if i == 0 else c.pick(PEOPLE)
    city, state = ("Boston", "Ohio") if i == 0 else _city_other_state(c)
    a, b = _span(c)
    canon = [P(x, "located_at", city, frm=a, to=b)]
    note = (
        f"{x} lived in {city} {a}-{b}; claim: {x} lived in {state} in {a + 1} "
        f"({city} is not in {state})."
    )
    claim = P(x, "located_at", state, frm=a + 1, to=a + 2)
    return _inputs(claim, canon, note), "contradicted"


def res_city_in_state(c: Chooser, i: int) -> tuple[Row, str]:
    x = "Bobby" if i == 0 else c.pick(PEOPLE)
    city = "Columbus" if i == 0 else c.pick(CITIES)
    state = CITY_IN[city]
    a, b = _span(c)
    canon = [P(x, "located_at", city, frm=a, to=b)]
    note = f"{x} lived in {city} {a}-{b}; claim: {x} lived in {state} in {a + 1} ({city} is in it)."
    claim = P(x, "located_at", state, frm=a + 1, to=a + 2)
    return _inputs(claim, canon, note), "supported"


def res_state_to_city(c: Chooser, i: int) -> tuple[Row, str]:
    x, city = c.pick(PEOPLE), c.pick(CITIES)
    state = CITY_IN[city]
    a, b = _span(c)
    canon = [P(x, "located_at", state, frm=a, to=b)]
    note = f"{x} lived somewhere in {state} {a}-{b}; claim: in {city} in {a + 1} (undecided)."
    claim = P(x, "located_at", city, frm=a + 1, to=a + 2)
    return _inputs(claim, canon, note), "uncertain"


def res_present(c: Chooser, i: int) -> tuple[Row, str]:
    x, place = c.pick(PEOPLE), c.pick(CITIES + ROOMS + ("market", "tavern"))
    a, b = _span(c)
    canon = [P(x, "located_at", place, frm=a, to=b, neg=True)]
    note = f"{x} was not at the {place} {a}-{b}; claim: {x} was present there in {a + 1}."
    return _inputs(P(x, "present_at", place, frm=a + 1, to=a + 2), canon, note), "contradicted"


def res_approx_gap(c: Chooser, i: int) -> tuple[Row, str]:
    x, place = c.pick(PEOPLE), c.pick(CITIES)
    a = c.year(2005, 2030)
    canon = [P(x, "located_at", place, frm_outer=a - 4, frm=a, to=a + 10, to_outer=a + 12)]
    claim = P(
        x, "located_at", place, frm_outer=a + 9, frm=a + 11, to=a + 14, to_outer=a + 15, neg=True
    )
    note = (
        f"{x} lived in {place} from about {a - 4}-{a} to about {a + 10}-{a + 12}; claim: {x} "
        f"was not there from about {a + 9}-{a + 11} (the bounds leave it open)."
    )
    return _inputs(claim, canon, note), "uncertain"


def res_approx_start(c: Chooser, i: int) -> tuple[Row, str]:
    x, place = c.pick(PEOPLE), c.pick(CITIES)
    a = c.year(2005, 2030)
    canon = [P(x, "located_at", place, frm_outer=a - 4, frm=a, to=a + 10)]
    note = (
        f"{x} moved to {place} some time in {a - 4}-{a} and left in {a + 10}; claim: {x} was "
        f"there from {a - 3} (before the latest possible arrival: undecided)."
    )
    return _inputs(P(x, "located_at", place, frm=a - 3, to=a + 8), canon, note), "uncertain"


def res_colour(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    colour, other = c.picks(COLOURS, 2)
    a, b = _span(c)
    canon = [P(x, "favourite_colour", colour, frm=a, to=b)]
    note = f"{x}'s favourite colour was {colour} {a}-{b}; claim: it was {other} in {a + 1}."
    claim = P(x, "favourite_colour", other, frm=a + 1, to=a + 2)
    return _inputs(claim, canon, note, strict_weak=True), "contradicted"


def res_weak_knows(c: Chooser, i: int) -> tuple[Row, str]:
    x, y, z = _three(c)
    a, b = _span(c)
    canon = [P(x, "knows", y, frm=a, to=b)]
    note = f"{x} knew {y} {a}-{b}; claim: {x} knew {z} in {a + 1} (nothing says either way)."
    claim = P(x, "knows", z, frm=a + 1, to=a + 2)
    return _inputs(claim, canon, note, strict_weak=True), "uncertain"


def res_weak_apprentice(c: Chooser, i: int) -> tuple[Row, str]:
    x = c.pick(PEOPLE)
    a, b = _span(c)
    canon = [P(x, "apprenticed_with", "Smith", frm=a, to=b)]
    note = (
        f"{x} apprenticed with Smith {a}-{b}; claim: with Smithson in {a + 1} (one master "
        "does not rule out another)."
    )
    claim = P(x, "apprenticed_with", "Smithson", frm=a + 1, to=a + 2)
    return _inputs(claim, canon, note, strict_weak=True), "uncertain"


FAMILIES: tuple[tuple[str, int, tuple[Template, ...], bool], ...] = (
    (
        "coverage",
        FAMILY_SIZE,
        (cov_inside, cov_adjacent, cov_sibling, cov_age, cov_name, cov_dead, cov_knows,
         cov_member, cov_holds, cov_parent),
        True,
    ),
    ("polarity", FAMILY_SIZE, (pol_place, pol_knows, pol_containment, pol_friend, pol_holds,
                               pol_member), True),
    ("functional", FAMILY_SIZE, (fun_cities, fun_rooms, fun_age, fun_name, fun_held_by), True),
    ("structural", FAMILY_SIZE, (st_closed, st_closed_none, st_dead_alive, st_born,
                                 st_inverse), False),
    ("novel", FAMILY_SIZE, (nov_empty, nov_later, nov_knows_other, nov_negated_other,
                            nov_partial, nov_refine, nov_role, nov_second_item), True),
    ("residual-world", 60, (res_city_elsewhere, res_city_in_state, res_state_to_city,
                            res_present), True),
    ("residual-approx", 30, (res_approx_gap, res_approx_start), False),
    ("residual-weak", 30, (res_colour, res_weak_knows, res_weak_apprentice), False),
)  # fmt: skip
"""Per family: its size, its templates (cycled) and whether its first template's first
instance is the whitepaper's (Bobby's years in Ohio)."""


def cases() -> list[dict[str, Any]]:
    """Every E-01 case, in family order."""
    found: list[dict[str, Any]] = []
    for family, size, templates, whitepaper in FAMILIES:
        chooser = Chooser(f"{SET}:{family}")
        seen: dict[int, int] = {}
        for n in range(size):
            k = n % len(templates)
            i = seen.get(k, 0)
            seen[k] = i + 1
            inputs, expected = templates[k](chooser, i)
            origin = origin_of(i, whitepaper=whitepaper and k == 0)
            found.append(case(SET, family, n + 1, origin=origin, inputs=inputs, expected=expected))
    return found


__all__ = ["FAMILIES", "SET", "cases"]
