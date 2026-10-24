"""E-01 M5's labeled set (``evals/sets/entailment_viewpoint/cases.jsonl``; #179, completing
#166's AC5): (claim, canon, viewpoint → verdict) cases where a false contradiction would
"correct" an intentional lie or a false belief.

Each case is a character's first-person ``say`` claim checked by #142's ``Verifier`` with the
speaker's belief partition inline (``e01_viewpoint``): canon (``canon``, the objective
relation), the speaker's beliefs (``beliefs``, each optionally kept ``secret``), the declared
``intent``, and the label: the classification #166 owes it (``lie``, ``sincere_error`` or
``sincere``), its belief relation and its objective verdict. None of them is repaired: a lie and
a sincere error stand as said (FR-VERIFY-5,10), and a belief-consistent truth is simply true.

One third each (100 per family, 300 cases):

- ``lie``: the speaker believes the truth and says otherwise, deceptively (a contrary belief;
  canon contradicts the claim, or settles nothing, or, an attempted lie that happens to be true,
  supports it), and the deceptive intent inferred from a ``secret`` contrary belief (#167);
- ``false_belief``: the speaker sincerely says what it wrongly believes (canon contradicts it,
  the speaker's belief supports it; declared or unspecified intent);
- ``consistent``: the speaker says what it rightly believes (and a declared lie the speaker
  actually believes is relabelled sincere, #166's intent check).

Canon and beliefs use E-01's vocabulary (``suites.e01_entailment``): names, real geography,
years. No narrator cases (a narrator has no belief partition).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from epistrel.evals.authoring.common import Chooser, case, origin_of
from epistrel.evals.authoring.entailment import CITIES, CITY_IN, ITEMS, PEOPLE, P

SET = "entailment_viewpoint"
FAMILY_SIZE = 100

Row = dict[str, Any]
Expected = dict[str, str]
Template = Callable[[Chooser, int], tuple[Row, Expected]]


def _expected(category: str, objective: str, belief: str) -> Expected:
    return {"category": category, "objective": objective, "belief_relation": belief}


def _inputs(
    speaker: str,
    claim: Row,
    canon: list[Row],
    beliefs: list[Row],
    intent: str,
    note: str,
) -> Row:
    return {
        "speaker": speaker,
        "claim": claim,
        "canon": canon,
        "beliefs": beliefs,
        "intent": intent,
        "note": note,
    }


def _belief(prop: Row, *, secret: bool = False) -> Row:
    return {"prop": prop, "secret": secret} if secret else {"prop": prop}


def _cast(c: Chooser, i: int) -> tuple[str, str]:
    """The speaker and the person the claim is about (the speaker itself at times)."""
    speaker, subject = c.picks(PEOPLE, 2)
    return speaker, (speaker if i % 3 == 0 else subject)


def _span(c: Chooser) -> tuple[int, int]:
    a = c.year(2001, 2040)
    return a, a + c.between(4, 12)


def _two_cities(c: Chooser) -> tuple[str, str]:
    first = c.pick(CITIES)
    second = c.pick([k for k in CITIES if CITY_IN[k] != CITY_IN[first]])
    return first, second


def _where(x: str, city: str, a: int, b: int) -> Row:
    return P(x, "located_at", city, frm=a, to=b)


def _geo(*cities: str) -> list[Row]:
    return [P(city, "located_in", CITY_IN[city]) for city in cities]


# -- lies: the speaker believes the truth and says otherwise -----------------------------------


def lie_place(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    true, said = _two_cities(c)
    a, b = _span(c)
    truth = [_where(x, true, a, b), *_geo(true, said)]
    claim = _where(x, said, a + 1, a + 2)
    note = f"{x} lived in {true} {a}-{b}; {speaker} knows it and says {x} was in {said}."
    inputs = _inputs(speaker, claim, truth, [_belief(t) for t in truth], "deceptive", note)
    return inputs, _expected("lie", "contradicted", "inconsistent")


def lie_holds(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    item = c.pick(ITEMS)
    a, b = _span(c)
    truth = P(x, "holds", item, frm=a, to=b)
    claim = P(x, "holds", item, frm=a + 1, to=a + 2, neg=True)
    note = f"{x} had the {item} {a}-{b}; {speaker} knows it and denies it."
    inputs = _inputs(speaker, claim, [truth], [_belief(truth)], "deceptive", note)
    return inputs, _expected("lie", "contradicted", "inconsistent")


def lie_knows(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    y = c.pick([p for p in PEOPLE if p not in (speaker, x)])
    a, b = _span(c)
    truth = P(x, "knows", y, frm=a, to=b)
    claim = P(x, "knows", y, frm=a + 1, to=a + 2, neg=True)
    note = f"{x} knew {y} {a}-{b}; {speaker} knows it and says they never met."
    inputs = _inputs(speaker, claim, [truth], [_belief(truth)], "deceptive", note)
    return inputs, _expected("lie", "contradicted", "inconsistent")


def lie_secret(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    item = c.pick(ITEMS)
    a, b = _span(c)
    truth = P(x, "holds", item, frm=a, to=b)
    claim = P(x, "holds", item, frm=a + 1, to=a + 2, neg=True)
    note = (
        f"{x} had the {item} {a}-{b}; {speaker} keeps that secret and denies it (no declared "
        "intent: the deception is inferred from the secret)."
    )
    beliefs = [_belief(truth, secret=True)]
    inputs = _inputs(speaker, claim, [truth], beliefs, "unspecified", note)
    return inputs, _expected("lie", "contradicted", "inconsistent")


def lie_unsettled(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    item = c.pick(ITEMS)
    a, b = _span(c)
    belief = P(x, "holds", item, frm=a, to=b)
    claim = P(x, "holds", item, frm=a + 1, to=a + 2, neg=True)
    note = (
        f"{speaker} believes {x} had the {item} {a}-{b} and says otherwise; canon says "
        "nothing about it."
    )
    inputs = _inputs(speaker, claim, [], [_belief(belief)], "deceptive", note)
    return inputs, _expected("lie", "novel", "inconsistent")


def lie_true_by_accident(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    true, believed = _two_cities(c)
    a, b = _span(c)
    canon = [_where(x, true, a, b), *_geo(true, believed)]
    beliefs = [_where(x, believed, a, b), *_geo(true, believed)]
    claim = _where(x, true, a + 1, a + 2)
    note = (
        f"{speaker} wrongly believes {x} lived in {believed} {a}-{b} and, to deceive, says "
        f"{true}: an attempted lie that is true."
    )
    inputs = _inputs(speaker, claim, canon, [_belief(r) for r in beliefs], "deceptive", note)
    return inputs, _expected("lie", "supported", "inconsistent")


# -- false beliefs: the speaker sincerely says what it wrongly believes -----------------------


def fb_place(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    true, believed = _two_cities(c)
    a, b = _span(c)
    canon = [_where(x, true, a, b), *_geo(true, believed)]
    beliefs = [_where(x, believed, a, b), *_geo(true, believed)]
    claim = _where(x, believed, a + 1, a + 2)
    note = f"{x} lived in {true} {a}-{b}; {speaker} believes it was {believed} and says so."
    inputs = _inputs(speaker, claim, canon, [_belief(r) for r in beliefs], "sincere", note)
    return inputs, _expected("sincere_error", "contradicted", "consistent")


def fb_holds(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    item = c.pick(ITEMS)
    a, b = _span(c)
    truth = P(x, "holds", item, frm=a, to=b)
    wrong = P(x, "holds", item, frm=a, to=b, neg=True)
    claim = P(x, "holds", item, frm=a + 1, to=a + 2, neg=True)
    note = f"{x} had the {item} {a}-{b}; {speaker} believes not, and says so."
    inputs = _inputs(speaker, claim, [truth], [_belief(wrong)], "sincere", note)
    return inputs, _expected("sincere_error", "contradicted", "consistent")


def fb_knows(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    y = c.pick([p for p in PEOPLE if p not in (speaker, x)])
    a, b = _span(c)
    truth = P(x, "knows", y, frm=a, to=b, neg=True)
    wrong = P(x, "knows", y, frm=a, to=b)
    claim = P(x, "knows", y, frm=a + 1, to=a + 2)
    note = f"{x} and {y} never met {a}-{b}; {speaker} believes they did, and says so."
    inputs = _inputs(speaker, claim, [truth], [_belief(wrong)], "sincere", note)
    return inputs, _expected("sincere_error", "contradicted", "consistent")


def fb_unspecified(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    true, believed = _two_cities(c)
    a, b = _span(c)
    canon = [_where(x, true, a, b), *_geo(true, believed)]
    beliefs = [_where(x, believed, a, b), *_geo(true, believed)]
    claim = _where(x, believed, a + 1, a + 2)
    note = (
        f"{x} lived in {true} {a}-{b}; {speaker} believes {believed} and says so, declaring "
        "no intent."
    )
    inputs = _inputs(speaker, claim, canon, [_belief(r) for r in beliefs], "unspecified", note)
    return inputs, _expected("sincere_error", "contradicted", "consistent")


def fb_age(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = c.picks(PEOPLE, 2)
    n = c.between(8, 80)
    a = c.year()
    truth = P(x, "age", lit=("int", n), frm=a, to=a + 1)
    wrong = P(x, "age", lit=("int", n + 4), frm=a, to=a + 1)
    claim = P(x, "age", lit=("int", n + 4), frm=a, to=a + 1)
    note = f"{x} was {n} in {a}; {speaker} believes {n + 4} and says so."
    inputs = _inputs(speaker, claim, [truth], [_belief(wrong)], "sincere", note)
    return inputs, _expected("sincere_error", "contradicted", "consistent")


# -- consistent: the speaker says what it rightly believes -----------------------------------


def con_place(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    city = c.pick(CITIES)
    a, b = _span(c)
    truth = _where(x, city, a, b)
    claim = _where(x, city, a + 1, a + 2)
    note = f"{x} lived in {city} {a}-{b}; {speaker} knows it and says so."
    inputs = _inputs(speaker, claim, [truth], [_belief(truth)], "sincere", note)
    return inputs, _expected("sincere", "supported", "consistent")


def con_holds(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    item = c.pick(ITEMS)
    a, b = _span(c)
    truth = P(x, "holds", item, frm=a, to=b)
    claim = P(x, "holds", item, frm=a + 2, to=a + 3)
    note = f"{x} had the {item} {a}-{b}; {speaker} knows it and says so."
    inputs = _inputs(speaker, claim, [truth], [_belief(truth)], "sincere", note)
    return inputs, _expected("sincere", "supported", "consistent")


def con_knows(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    y = c.pick([p for p in PEOPLE if p not in (speaker, x)])
    a, b = _span(c)
    truth = P(x, "knows", y, frm=a, to=b)
    claim = P(x, "knows", y, frm=a + 1, to=a + 2)
    note = f"{x} knew {y} {a}-{b}; {speaker} knows it and says so."
    inputs = _inputs(speaker, claim, [truth], [_belief(truth)], "unspecified", note)
    return inputs, _expected("sincere", "supported", "consistent")


def con_corrected(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    item = c.pick(ITEMS)
    a, b = _span(c)
    truth = P(x, "holds", item, frm=a, to=b)
    claim = P(x, "holds", item, frm=a + 1, to=a + 2)
    note = (
        f"{x} had the {item} {a}-{b}; {speaker} believes it and says so, though the render "
        "declared a lie: no lie, relabelled sincere."
    )
    inputs = _inputs(speaker, claim, [truth], [_belief(truth)], "deceptive", note)
    return inputs, _expected("sincere", "supported", "consistent")


def con_unsettled(c: Chooser, i: int) -> tuple[Row, Expected]:
    speaker, x = _cast(c, i)
    city = c.pick(CITIES)
    a, b = _span(c)
    truth = _where(x, city, a, b)
    claim = _where(x, city, a + 1, a + 2)
    note = (
        f"{speaker} believes {x} lived in {city} {a}-{b} and says so; canon says nothing about it."
    )
    inputs = _inputs(speaker, claim, [], [_belief(truth)], "sincere", note)
    return inputs, _expected("sincere", "novel", "consistent")


FAMILIES: dict[str, tuple[Template, ...]] = {
    "lie": (lie_place, lie_holds, lie_knows, lie_secret, lie_unsettled, lie_true_by_accident),
    "false_belief": (fb_place, fb_holds, fb_knows, fb_unspecified, fb_age),
    "consistent": (con_place, con_holds, con_knows, con_corrected, con_unsettled),
}


def cases() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for family, templates in FAMILIES.items():
        chooser = Chooser(f"{SET}:{family}")
        for i in range(FAMILY_SIZE):
            template = templates[i % len(templates)]
            inputs, expected = template(chooser, i // len(templates))
            found.append(
                case(
                    SET,
                    family,
                    i + 1,
                    origin=origin_of(i // len(templates)),
                    inputs={**inputs, "template": template.__name__},
                    expected=expected,
                )
            )
    return found


__all__ = ["FAMILIES", "FAMILY_SIZE", "SET", "cases"]
