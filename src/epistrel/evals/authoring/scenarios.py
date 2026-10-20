"""The intervention scenarios E-12 and E-13 replay (``evals/sets/scenarios/cases.jsonl``; #158).

Each case is one scratch story: Wren (the player) and Bobby (who renders), in a house whose
canon puts Bobby's coat on the hallway hook (the author's facts, as S-COAT plants them), played
for ``TURNS`` turns through ``Engine.respond`` on ``Structured (M4)`` (``config: structured``)
or its ``gated`` variant (``verification.extraction_mode = gated``). The story's custom
instructions carry its drivers (hooks, objectives, planned reveals: E-13's story-driver
adherence), and some scenarios request a response length (E-13's length accuracy).

The **families** are built so that, on the stub, every turn triggers its intervention type, as
docs/04's E-12 asks ("the suites include scenarios built to trigger each repair rung"):

- ``rung-1``: a ``do`` beat whose second sentence puts the coat on the armchair; narrowing drops
  that sentence;
- ``rung-2``: one sentence with the coat across the armchair; substitution puts it on the hook;
- ``rung-3``: the claim is declared but not in the prose; the scripted regeneration corrects it;
- ``rung-4``: the scripted regeneration still contradicts canon; the safe fallback replaces the
  beat;
- ``polished``: no claim; the scripted polish rewrites the two beats as one paragraph that
  passes #152's check;
- ``fallback``: a ``rung-1`` render whose scripted polish omits a beat, so the composition falls
  back to the deterministic assembly;
- ``plain``: no claim and the polish returns its draft: nothing changes (E-12's control; E-13's
  baseline).

On a real model the scripted renders do not apply: the scenarios' canon and lines are the
temptations, and the interventions are whatever the model's own claims set off. ``expected`` per
turn is the stub's intervention, checked by the suite test, never by the suite itself.

``stub_judge`` is the scripted judge's preference on the turn's pair (``final``, ``draft`` or
``tie``), ``stub_voice`` its voice score (1-5) and the case's ``stub_driver`` its driver score:
the stub run's plumbing only, never a label of quality.
"""

from __future__ import annotations

import json
from typing import Any

from epistrel.evals.authoring.common import Chooser

SET = "scenarios"
TURNS = 4
PER_FAMILY = 3
CONFIGS = ("structured", "gated")
FAMILIES: dict[str, list[str]] = {
    "rung-1": ["repair_rung_1"],
    "rung-2": ["repair_rung_2"],
    "rung-3": ["repair_rung_3"],
    "rung-4": ["repair_rung_4"],
    "polished": ["composition_polished"],
    "fallback": ["repair_rung_1", "composition_fallback"],
    "plain": [],
}
"""Each family and the intervention types every one of its turns triggers on the stub."""

PLAYER_LINES = (
    "What are you doing?",
    "Is it cold in here?",
    "Where did you leave your coat?",
    "Are you staying for supper?",
    "Did you hear the rain start?",
    "Who left the hallway door open?",
    "Fidgeting again?",
    "Have you seen the post today?",
    "Can you help me with the lamp?",
    "Why are you smiling?",
    "Is your brother coming by later?",
    "Shall we sit in the parlor?",
)
SAYS = (
    "Evening.",
    "Not much, just thinking.",
    "Bit draughty, isn't it?",
    "I was about to ask you the same.",
    "Give me a moment.",
    "It's been a long day.",
    "I'd rather not say.",
    "Sure, why not.",
    "You worry too much.",
    "Funny you should ask.",
    "Maybe later.",
    "Ask me tomorrow.",
)
GESTURES = (
    "Bobby grins at you.",
    "Bobby rubs his hands together.",
    "Bobby leans against the doorframe.",
    "Bobby glances at the window.",
    "Bobby taps his foot.",
    "Bobby yawns.",
    "Bobby straightens his collar.",
    "Bobby scratches his chin.",
)
ARMCHAIR = (
    "The coat lies across the armchair.",
    "His coat is draped over the armchair.",
    "The coat sits on the armchair.",
)
SUBSTITUTED = (
    "The coat lies across the armchair; Bobby plays with its hem.",
    "The coat hangs off the armchair; Bobby tugs at its sleeve.",
    "The coat rests on the armchair; Bobby picks at a loose thread.",
)
SILENT_CLAIM = (
    "Bobby pulls the coat down and plays with its hem.",
    "Bobby fiddles with the coat's buttons.",
    "Bobby brushes lint from the coat.",
)
CORRECTED = (
    "Bobby glances at his coat on the hook and plays with his sleeve.",
    "Bobby looks at the coat hanging on the hook and smiles.",
    "Bobby nods toward his coat on the hook.",
)
LOOKS = (
    (
        "I like the rain.",
        "Bobby looks out of the window.",
        '"I like the rain," Bobby says. He looks out of the window.',
    ),
    (
        "The fire needs a log.",
        "Bobby kneels by the hearth.",
        '"The fire needs a log," Bobby says. He kneels by the hearth.',
    ),
    (
        "Supper smells good.",
        "Bobby sniffs the air.",
        '"Supper smells good," Bobby says. He sniffs the air.',
    ),
)
HOOKS = (
    "A stranger knocks asking about Bobby's coat.",
    "A letter arrives addressed to Bobby's brother.",
    "The lamp in the hallway keeps flickering.",
)
OBJECTIVES = (
    "Find out who left the hallway door open.",
    "Learn why Bobby stays so late.",
    "Discover where the missing key went.",
)
REVEALS = (
    "Bobby borrowed the coat from his brother without asking.",
    "Bobby has been sleeping in the parlor for a week.",
    "Bobby found the key and kept it.",
)
LENGTHS: tuple[str | None, ...] = ("short", None, "medium")


def claim(subject: str, relation: str, obj: str) -> dict[str, Any]:
    return {"subject": subject, "relation": relation, "object": obj}


def render(*beats: dict[str, Any]) -> str:
    return json.dumps({"beats": list(beats)})


def say(text: str) -> dict[str, Any]:
    return {"kind": "say", "text": text, "intent": "sincere"}


def do(text: str, *claims: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {"kind": "do", "text": text}
    if claims:
        found["propositions"] = list(claims)
    return found


ON_ARMCHAIR = claim("coat", "located_at", "armchair")
ON_HOOK = claim("coat", "located_at", "hook")


def regeneration(text: str, *claims: dict[str, Any]) -> str:
    return json.dumps({"text": text, "propositions": list(claims)})


def _turn(c: Chooser, family: str, line: str, said: str) -> dict[str, Any]:
    """One turn of ``family``: the player's line and the stub's scripted answers."""
    gesture = c.pick(GESTURES)
    stub: dict[str, Any] = {}
    if family in ("rung-1", "fallback"):
        stub["render"] = render(say(said), do(f"{gesture} {c.pick(ARMCHAIR)}", ON_ARMCHAIR))
        if family == "fallback":
            # Omits the do beat: #152's completeness check fails, the assembly is released.
            stub["composition"] = json.dumps({"text": f'"{said}" Bobby says.'})
    elif family == "rung-2":
        stub["render"] = render(say(said), do(c.pick(SUBSTITUTED), ON_ARMCHAIR))
    elif family in ("rung-3", "rung-4"):
        stub["render"] = render(say(said), do(c.pick(SILENT_CLAIM), ON_ARMCHAIR))
        stub["regeneration"] = (
            regeneration(c.pick(CORRECTED), ON_HOOK)
            if family == "rung-3"
            else regeneration("Bobby drapes the coat over the armchair again.", ON_ARMCHAIR)
        )
    elif family == "polished":
        spoken, action, polished = c.pick(LOOKS)
        stub["render"] = render(say(spoken), do(action))
        stub["composition"] = json.dumps({"text": polished})
    else:
        stub["render"] = render(say(said), do(gesture))
    judged = c.pick(("final", "final", "final", "tie", "draft"))
    return {
        "say": line,
        "expected": FAMILIES[family],
        "stub": stub,
        "stub_judge": judged,
        "stub_voice": c.between(3, 5),
    }


def cases() -> list[dict[str, Any]]:
    """``PER_FAMILY`` scenarios per family, alternating the two configurations."""
    found: list[dict[str, Any]] = []
    for family in FAMILIES:
        c = Chooser(f"{SET}:{family}")
        for n in range(1, PER_FAMILY + 1):
            lines = c.picks(PLAYER_LINES, TURNS)
            spoken = c.picks(SAYS, TURNS)
            turns = [_turn(c, family, line, said) for line, said in zip(lines, spoken, strict=True)]
            found.append(
                {
                    "id": f"{SET}-{family}-{n:04d}",
                    "family": family,
                    "origin": "authored" if n == 1 else "paraphrase",
                    "config": CONFIGS[(n - 1) % len(CONFIGS)],
                    "length": c.pick(LENGTHS),
                    "drivers": {
                        "hooks": [c.pick(HOOKS)],
                        "objectives": [c.pick(OBJECTIVES)],
                        "planned_reveals": [c.pick(REVEALS)],
                    },
                    "turns": turns,
                    "stub_driver": c.between(2, 5),
                }
            )
    return found


__all__ = ["FAMILIES", "SET", "TURNS", "cases"]
