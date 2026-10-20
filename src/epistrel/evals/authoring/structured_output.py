"""E-07's labeled set (``evals/sets/structured_output/cases.jsonl``; #157): 200 structured turns
(a player's line to Maren the innkeeper), 20 of them with garbage injected into the render
(``inject``): ``once`` (the first attempt is unparseable; the retry must recover) and
``always`` (every attempt is unparseable; the turn must fail through the degradation path,
never accept it). ``expected`` is what a robust pipeline ends with: ``valid`` or ``degraded``.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals.authoring.common import Chooser, case
from epistrel.evals.authoring.conformance import LINES, render_answer

SET = "structured_output"
TOTAL = 200
INJECT_ONCE = 14
INJECT_ALWAYS = 6


def cases() -> list[dict[str, Any]]:
    """180 plain turns, then the injected ones."""
    c = Chooser(SET)
    plain = TOTAL - INJECT_ONCE - INJECT_ALWAYS
    found: list[dict[str, Any]] = []
    for family, count, inject, expected in (
        ("plain", plain, "none", "valid"),
        ("garbage-once", INJECT_ONCE, "once", "valid"),
        ("garbage-always", INJECT_ALWAYS, "always", "degraded"),
    ):
        for n in range(1, count + 1):
            found.append(
                case(
                    SET,
                    family,
                    n,
                    origin="authored" if n == 1 else "paraphrase",
                    inputs={"say": c.pick(LINES), "inject": inject},
                    expected=expected,
                    stub_answer=render_answer(c),
                )
            )
    return found


__all__ = ["SET", "cases"]
