"""The attribution lexicon (#167, ``core.verify.attribution``): a detected claim under "<Name>
<verb> [that]" is attributed to that name; "I" is the speaker; "knows" is factive and "said" a
belief about a speech act; anything else is no cue."""

from __future__ import annotations

import pytest

from epistrel.core.verify.attribution import SELF, ClaimAttribution, cue


@pytest.mark.parametrize(
    ("text", "claim", "expected"),
    [
        ("Bobby thinks it was Marcus.", "it was", ClaimAttribution("Bobby")),
        ("Well, Bobby thinks that it was Marcus.", "it was", ClaimAttribution("Bobby")),
        ("I figured it was the delivery guy.", "it was", ClaimAttribution(SELF)),
        ("Jenni knows it was George.", "it was", ClaimAttribution("Jenni", factive=True)),
        ("Bobby said it was Marcus.", "it was", ClaimAttribution("Bobby", mode="said")),
        ("It was Marcus. Bobby thinks so.", "It was", None),
        ("the driver thinks it was Marcus", "it was", None),
        ("Bobby opened the door.", "the door", None),
    ],
)
def test_cue(text: str, claim: str, expected: ClaimAttribution | None) -> None:
    assert cue(text, text.index(claim)) == expected
