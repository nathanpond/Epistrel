# ruff: noqa: E501 - the hand-written case tables read best one case a line
"""E-05's labeled set (``evals/sets/salience/cases.jsonl``; #157): 300 renders of one to three
observable beats (``say``, ``do``, ``narration``), 180 holding one salient span — content placed
to be noticed, which the story then owes (FR-COMMIT-3) — and 120 holding texture only.

Salient families follow the whitepaper's (``the-knock``, ``the-coin``, ``sunday-dinner``) and
add others of the same kind (a stranger, a letter, a threat, a clue, a disappearance, an object
out of place); texture families are weather, the room, food, small talk and routine. A
template marks its salient span with ``[[…]]``; the span's character offsets are computed from
the marker, and its proposition sketch (subject, relation, object) comes with the template.
Texture beats are drawn from the texture families and shuffled around the salient beat.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals.authoring.common import Chooser, case

SET = "salience"
SALIENT_PER_FAMILY = 20
TEXTURE_PER_FAMILY = 24
NAMES = ("Wren", "Maren", "Linda", "Marcus", "Bobby", "Ann", "Cal", "Tom", "Ada", "Felix")

# (kind, speaker or None, text with [[span]], subject, relation, object)
Salient = tuple[str, bool, str, str, str, str | None]
SALIENT: dict[str, tuple[Salient, ...]] = {
    "the-knock": (
        ("narration", False, "[[Someone knocks at the door]] three times, then nothing.", "someone", "knocks at", "the door"),
        ("narration", False, "Halfway through supper [[there is a knock at the back door]].", "someone", "knocks at", "the back door"),
        ("do", True, "{a} freezes as [[a slow knock comes from the cellar hatch]].", "someone", "knocks at", "the cellar hatch"),
        ("narration", False, "[[A heavy knock shakes the shutters]], and the dogs fall silent.", "someone", "knocks at", "the shutters"),
    ),
    "the-coin": (
        ("do", True, "{a} turns over [[a coin stamped with a crown no one recognises]].", "{a}", "holds", "a strange coin"),
        ("narration", False, "[[A gold coin lies on the doorstep]], bright as if new.", "a gold coin", "lies on", "the doorstep"),
        ("do", True, "{a} finds [[an old silver coin sewn into the hem of the coat]].", "{a}", "finds", "a silver coin"),
        ("do", True, "{a} pockets [[the foreign coin the stranger left behind]].", "{a}", "holds", "a foreign coin"),
    ),
    "sunday-dinner": (
        ("say", True, "[[You will all be at Sunday dinner]], and I mean all of you.", "everyone", "attends", "Sunday dinner"),
        ("say", True, "[[Bobby is bringing someone to Sunday dinner]], so set another place.", "Bobby", "brings a guest to", "Sunday dinner"),
        ("say", True, "Don't forget: [[your father wants to talk to you after Sunday dinner]].", "your father", "wants to talk to", "you"),
        ("say", True, "[[Sunday dinner is at Aunt Ruth's this week]], not here.", "Sunday dinner", "is held at", "Aunt Ruth's"),
    ),
    "stranger": (
        ("narration", False, "[[A rider in a grey cloak watches the inn from the hill]].", "a rider", "watches", "the inn"),
        ("narration", False, "[[A woman nobody knows takes the corner table]] and orders nothing.", "a stranger", "sits at", "the corner table"),
        ("do", True, "{a} glances at [[the man by the fire who has not taken off his gloves]].", "a man", "waits by", "the fire"),
        ("narration", False, "[[Footprints that are not anyone's lead to the well]].", "someone", "walked to", "the well"),
    ),
    "letter": (
        ("do", True, "{a} slips [[a sealed letter addressed to no one]] under the door.", "{a}", "leaves", "a sealed letter"),
        ("narration", False, "[[A letter with a black seal waits on the counter]].", "a letter", "waits on", "the counter"),
        ("say", True, "[[A letter came for you while you were out]], and I didn't open it.", "a letter", "came for", "you"),
        ("do", True, "{a} burns [[a letter before anyone can read it]].", "{a}", "burns", "a letter"),
    ),
    "threat": (
        ("say", True, "[[I'll be back for the ledger before the moon is full]].", "the speaker", "will return for", "the ledger"),
        ("say", True, "[[If the rent isn't paid by Friday, you're out]].", "the landlord", "will evict", "you"),
        ("say", True, "[[Tell your brother I know what he did]].", "the speaker", "knows a secret of", "your brother"),
        ("say", True, "[[Someone at this table is lying to me]], and I'll find out who.", "someone at the table", "is lying to", "the speaker"),
    ),
    "clue": (
        ("narration", False, "[[There is fresh blood on the cellar steps]].", "blood", "is on", "the cellar steps"),
        ("narration", False, "[[The lock on the strongbox has been forced]].", "someone", "forced", "the strongbox"),
        ("do", True, "{a} notices [[the missing page torn from the ledger]].", "a page", "is missing from", "the ledger"),
        ("narration", False, "[[A muddy boot print sits just inside the window]].", "someone", "came in through", "the window"),
    ),
    "disappearance": (
        ("say", True, "[[Has anyone seen Tom since the market?]]", "Tom", "is missing since", "the market"),
        ("say", True, "[[The horse was gone from the stable this morning]].", "the horse", "is missing from", "the stable"),
        ("narration", False, "[[Ada's bed has not been slept in]].", "Ada", "did not sleep in", "her bed"),
        ("say", True, "[[My ring was on the sill an hour ago]], and now it isn't.", "the ring", "is missing from", "the sill"),
    ),
    "out-of-place": (
        ("narration", False, "[[A rusted key hangs on a nail above the hearth]], where none hung yesterday.", "a rusted key", "hangs above", "the hearth"),
        ("narration", False, "[[The portrait in the hall has been turned to face the wall]].", "someone", "turned", "the portrait"),
        ("do", True, "{a} stares at [[a child's shoe on the top shelf of the pantry]].", "a child's shoe", "is on", "the pantry shelf"),
        ("narration", False, "[[The church bell rings once at midnight]], though no one is in the tower.", "the church bell", "rings at", "midnight"),
    ),
}  # fmt: skip
TEXTURE: dict[str, tuple[tuple[str, bool, str], ...]] = {
    "weather": (
        ("narration", False, "Rain taps steadily on the roof."),
        ("narration", False, "The wind worries at the shutters."),
        ("say", True, "Looks like another wet one tomorrow."),
        ("narration", False, "Grey light fills the window."),
    ),
    "room": (
        ("narration", False, "The fire pops and settles."),
        ("narration", False, "The kitchen smells of onions and smoke."),
        ("do", True, "{a} leans back in the creaking chair."),
        ("narration", False, "A kettle hums on the stove."),
    ),
    "food": (
        ("do", True, "{a} tears off a piece of bread."),
        ("say", True, "This stew could use more salt."),
        ("do", True, "{a} pours a second cup of tea."),
        ("say", True, "Pass the butter, would you?"),
    ),
    "small-talk": (
        ("say", True, "How was the walk over?"),
        ("say", True, "The market was busy today."),
        ("say", True, "I always forget how long the road is."),
        ("say", True, "Did you sleep at all last night?"),
    ),
    "routine": (
        ("do", True, "{a} wipes down the counter."),
        ("do", True, "{a} stacks the plates by the sink."),
        ("do", True, "{a} hangs the coat on its peg."),
        ("do", True, "{a} trims the lamp wick."),
    ),
}


def _span(text: str) -> tuple[str, int, int]:
    start = text.index("[[")
    end = text.index("]]") - 2
    return text.replace("[[", "").replace("]]", ""), start, end


def _texture_beat(c: Chooser, speaker: str) -> dict[str, Any]:
    kind, voiced, text = c.pick(TEXTURE[c.pick(sorted(TEXTURE))])
    return {"kind": kind, "speaker": speaker if voiced else None, "text": text.format(a=speaker)}


def _scene(c: Chooser, speaker: str) -> dict[str, Any]:
    others = c.picks([n for n in NAMES if n != speaker], 2)
    return {"present": [speaker, *others], "last_beats": [f"{others[0]} says: Evening."]}


def _salient_cases() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for family, templates in SALIENT.items():
        c = Chooser(f"{SET}:{family}")
        for n in range(SALIENT_PER_FAMILY):
            kind, voiced, raw, subject, relation, obj = templates[n % len(templates)]
            speaker = c.pick(NAMES)
            text, start, end = _span(raw.format(a=speaker))
            beat = {"kind": kind, "speaker": speaker if voiced else None, "text": text}
            others = [_texture_beat(c, speaker) for _ in range(c.between(0, 2))]
            position = c.between(0, len(others))
            beats = [*others[:position], beat, *others[position:]]
            expected = [
                {
                    "beat": position,
                    "start": start,
                    "end": end,
                    "subject": subject.format(a=speaker),
                    "relation": relation,
                    "object": obj,
                }
            ]
            found.append(
                case(
                    SET,
                    family,
                    n + 1,
                    origin=(
                        (
                            "whitepaper"
                            if family in {"the-knock", "the-coin", "sunday-dinner"}
                            else "authored"
                        )
                        if n < len(templates)
                        else "paraphrase"
                    ),
                    inputs={"beats": beats, **_scene(c, speaker)},
                    expected=expected,
                )
            )
    return found


def _texture_cases() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for family, templates in TEXTURE.items():
        c = Chooser(f"{SET}:texture-{family}")
        for n in range(TEXTURE_PER_FAMILY):
            speaker = c.pick(NAMES)
            kind, voiced, text = templates[n % len(templates)]
            first = {
                "kind": kind,
                "speaker": speaker if voiced else None,
                "text": text.format(a=speaker),
            }
            beats = [first, *(_texture_beat(c, speaker) for _ in range(c.between(0, 2)))]
            found.append(
                case(
                    SET,
                    f"texture-{family}",
                    n + 1,
                    origin="authored" if n < len(templates) else "paraphrase",
                    inputs={"beats": beats, **_scene(c, speaker)},
                    expected=[],
                )
            )
    return found


def cases() -> list[dict[str, Any]]:
    """The salient cases, then the texture-only ones."""
    return [*_salient_cases(), *_texture_cases()]


__all__ = ["SET", "cases"]
