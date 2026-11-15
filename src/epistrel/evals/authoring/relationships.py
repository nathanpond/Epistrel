"""Q-06's labeled set (``evals/sets/relationships/cases.jsonl``; #913): questions about a
relationship, asked of the innkeeper in ``evals.relationship_world``.

Four kinds (the world's module docstring has the depths):

- **undescribed**: a question about one end of a relationship between two townspeople that
  points at the other end by a verb or a plural ("Maren, who is Bobby married to?", "who are
  Bobby's parents?"), which retrieval's resolve stage does not resolve to a person: the answer
  is not among the message's people, so a ``message``-scoped profile leaves the relationship
  out and a ``full`` one carries it. 11 questions, at both depths (22 cases).
- **described**: the same relationships asked with a relation noun of the resolution lexicon
  ("who is Bobby's spouse?", "who is Odile's child?"): the resolve stage resolves the
  description to the answer, which joins the message's people, so both settings keep it. A
  description with more than one answer (Jory's two children) is ambiguous, and the resolve
  stage leaves it unresolved, so only single-answer descriptions are asked (13 cases, deep).
- **named**: a question naming both ends and a distractor ("Maren, is Bobby married to Carol
  or to Tamsin?"): both settings keep it (8 cases, deep).
- **speaker**: a question whose answer is Maren herself ("Maren, who is Jory's friend?"): she
  is the render's speaker, kept in scope by both settings (3 cases, deep).

46 cases, ids ``relationships-<kind>-<depth>-<nnnn>``. Each case names the expected answer
forms (the answer's names; for ``speaker``, also ``me`` and ``myself``) and the stub's clean
answer, a render that gives the expected answer. The generator refuses a question that holds
one of its own expected forms (a named case's own names aside), a world that gives a question
no answer, and a speaker question whose answer is anyone but Maren.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals.authoring.asymmetry import render
from epistrel.evals.authoring.common import case, origin_of
from epistrel.evals.fixtures import disclosures
from epistrel.evals.relationship_world import RELATIONSHIPS, SPEAKER, Relationship, names, related

SET = "relationships"
SELF_FORMS = ("me", "myself")
ADDRESS = f"{SPEAKER}, "

UNDESCRIBED: dict[tuple[str, bool], str] = {
    ("married_to", False): "who is {x} married to?",
    ("parent_of", True): "who are {x}'s parents?",
    ("parent_of", False): "who are {x}'s children?",
    ("enemy_of", True): "who has a grudge against {x}?",
    ("enemy_of", False): "who does {x} have a grudge against?",
}
"""A question about ``{x}``'s relationship with no relation noun, by predicate and whether
``{x}`` is the fact's object (a symmetric predicate's question is the same either way)."""

DESCRIBED: dict[tuple[str, bool], str] = {
    ("married_to", False): "who is {x}'s spouse?",
    ("parent_of", True): "who is {x}'s parent?",
    ("parent_of", False): "who is {x}'s child?",
    ("sibling_of", False): "who is {x}'s sibling?",
    ("friend_of", False): "who is {x}'s friend?",
    ("enemy_of", False): "who is {x}'s enemy?",
}
"""The same with a relation noun of ``core/resolution/lexicon.json``'s ``relations`` (its
``enemy`` names the fact's object: Silas's enemy is the one Silas has a grudge against)."""

BETWEEN: dict[str, str] = {
    "married_to": "is {x} married to {a} or to {b}?",
    "sibling_of": "is {x}'s sibling {a} or {b}?",
    "friend_of": "who is {x}'s friend, {a} or {b}?",
    "parent_of": "who is a parent of {x}, {a} or {b}?",
    "enemy_of": "who has a grudge against {x}, {a} or {b}?",
}
"""A named question: ``{x}``, the right answer and a distractor, in either order. A directed
predicate asks about its object."""

ANSWER = "That'd be {name}. Everyone in Gullhaven knows that."
SELF_ANSWER = "Why, that's me. Has been for years."
NOD = "wipes down the bar"


def _between(r: Relationship) -> tuple[str, str]:
    """The named question's subject and its right answer: a directed predicate asks about its
    object; a symmetric one about its stored subject."""
    if r.symmetric:
        return r.subject, r.object
    return r.object, r.subject


def _distractor(x: str, answer: str) -> str:
    """The first townsperson (cast order) with no relationship to ``x`` and not ``answer``."""
    tied = {r.other(x) for r in RELATIONSHIPS if r.touches(x)}
    return next(n for n in names() if n not in {SPEAKER, x, answer, *tied})


def _inputs(
    kind: str, depth: str, x: str, predicate: str, question: str, expected: list[str], **extra: Any
) -> dict[str, Any]:
    return {
        "kind": kind,
        "depth": depth,
        "about": x,
        "predicate": predicate,
        "question": question,
        "expected": expected,
        **extra,
    }


def _check(question: str, expected: list[str], answer: str, *, named: tuple[str, ...]) -> None:
    hits = disclosures(question, [e for e in expected if e not in named])
    if hits:
        raise ValueError(f"{question!r} names its own answer {hits}")
    if not disclosures(answer, expected):
        raise ValueError(f"{answer!r} does not give the expected answer {expected}")


def questions(
    templates: dict[tuple[str, bool], str], *, speaker: bool = False, single: bool = False
) -> list[tuple[str, str, str, list[str]]]:
    """``(about, predicate, question, expected)`` for each relationship end ``templates`` can
    ask about, in world order, once each: the relationships among the townspeople, or (with
    ``speaker``) Maren's own, asked about her counterpart; with ``single``, only the questions
    with one answer."""
    found: list[tuple[str, str, str, list[str]]] = []
    seen: set[tuple[str, str, bool]] = set()
    for r in RELATIONSHIPS:
        if (SPEAKER in (r.subject, r.object)) is not speaker:
            continue
        ends = (r.other(SPEAKER),) if speaker else (r.subject, r.object)
        for x in ends:
            as_object = False if r.symmetric else x == r.object
            template = templates.get((r.predicate, as_object))
            if template is None or (x, r.predicate, as_object) in seen:
                continue
            seen.add((x, r.predicate, as_object))
            expected = related(x, r.predicate, as_object=None if r.symmetric else as_object)
            if not expected:
                raise ValueError(f"{x}'s {r.predicate} has no answer")
            if speaker and expected != [SPEAKER]:
                raise ValueError(f"{x}'s {r.predicate} is not {SPEAKER} alone: {expected}")
            if (not speaker and SPEAKER in expected) or (single and len(expected) > 1):
                continue
            found.append((x, r.predicate, ADDRESS + template.format(x=x), expected))
    return found


def _asked(
    kind: str, depth: str, asked: list[tuple[str, str, str, list[str]]]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for n, (x, predicate, question, expected) in enumerate(asked, start=1):
        if kind == "speaker":
            forms = [SPEAKER, *SELF_FORMS]
            answer = render(SELF_ANSWER, NOD)
            _check(question, forms, answer, named=(x, SPEAKER))
        else:
            forms = expected
            answer = render(ANSWER.format(name=expected[0]), NOD)
            _check(question, forms, answer, named=(x,))
        found.append(
            case(
                SET,
                f"{kind}-{depth}",
                n,
                origin=origin_of(n - 1),
                inputs=_inputs(kind, depth, x, predicate, question, forms),
                expected="correct",
                stub_answer=answer,
            )
        )
    return found


def _named(depth: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    among = [r for r in RELATIONSHIPS if SPEAKER not in (r.subject, r.object)]
    for n, r in enumerate(among, start=1):
        x, right = _between(r)
        wrong = _distractor(x, right)
        a, b = (right, wrong) if n % 2 else (wrong, right)
        question = ADDRESS + BETWEEN[r.predicate].format(x=x, a=a, b=b)
        answer = render(ANSWER.format(name=right), NOD)
        _check(question, [right], answer, named=(x, right))
        found.append(
            case(
                SET,
                f"named-{depth}",
                n,
                origin=origin_of(n - 1),
                inputs=_inputs("named", depth, x, r.predicate, question, [right], distractor=wrong),
                expected="correct",
                stub_answer=answer,
            )
        )
    return found


def cases() -> list[dict[str, Any]]:
    return [
        *_asked("undescribed", "shallow", questions(UNDESCRIBED)),
        *_asked("undescribed", "deep", questions(UNDESCRIBED)),
        *_asked("described", "deep", questions(DESCRIBED, single=True)),
        *_named("deep"),
        *_asked("speaker", "deep", questions(DESCRIBED, speaker=True)),
    ]


__all__ = ["BETWEEN", "DESCRIBED", "SELF_FORMS", "SET", "UNDESCRIBED", "cases", "questions"]
