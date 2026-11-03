"""SPEECH-ACT-INDEPENDENCE: truth, belief and intent move independently (docs/04 §6; FR-TURN-6,
FR-DIRECT-8; #225).

For any sequence of utterances, belief changes and retcons over one key (the objects ``A``,
``B``, ``C``; a retcon makes one of them the truth and supersedes the current one), checked
against an independent model of the three dimensions:

- a retcon changes an utterance's objective relation only (``rewrite.apply.relabelled``):
  ``true`` when it asserted the new truth, ``false`` the superseded one, unchanged otherwise;
- its belief relation changes only when the speaker's own belief at that position follows the
  retcon (``speaker_follows``);
- its intent never changes through a retcon (the explicit intent path is #225's ``set_intent``
  part, which this property never takes);
- a lie stays a lie and a joke a joke, whatever the truth becomes.

The complement on Postgres (``test_s_speech_dimensions``): any part other than ``set_intent``
that names ``intent`` is refused (``IntentNotRetconnable``).
"""

from __future__ import annotations

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from epistrel.orchestrator.rewrite.apply import SpeechAct, relabelled

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
OBJECTS = ("A", "B", "C")
LABELS = ("sincere", "sincere_error", "lie", "joke", "hallucination_repaired")


@st.composite
def acts(draw: st.DrawFn) -> SpeechAct:
    return SpeechAct(
        objective=draw(st.sampled_from(("true", "false", "unresolved"))),
        belief=draw(st.sampled_from(("consistent", "inconsistent", "unknown"))),
        intent=draw(st.sampled_from(("sincere", "deceptive", "joking", "unspecified"))),
        label=draw(st.sampled_from(LABELS)),
    )


steps = st.lists(
    st.tuples(st.sampled_from(OBJECTS), st.booleans()),
    min_size=1,
    max_size=12,
)
"""Each retcon: the object it makes true, and whether the speaker's belief follows it."""


@pytest.mark.part("M7", "FR-TURN-6; FR-DIRECT-8")
@settings(max_examples=1_000 if NIGHTLY else 100, deadline=None)
@given(acts(), st.sampled_from(OBJECTS), steps, st.sampled_from(OBJECTS))
def test_speech_act_independence(
    act: SpeechAct, asserted: str, retcons: list[tuple[str, bool]], truth: str
) -> None:
    current = act
    for new, follows in retcons:
        old = truth
        after = relabelled(
            current, [asserted], new_object=new, old_object=old, speaker_follows=follows
        )
        assert after.intent == current.intent  # never through a retcon
        if asserted not in (new, old):
            assert after.objective == current.objective
            assert after.belief == current.belief
        else:
            expected = "true" if asserted == new else "false"
            assert after.objective == expected
            if follows:
                assert after.belief == ("consistent" if asserted == new else "inconsistent")
            else:
                assert after.belief == current.belief
        if current.label in ("lie", "joke", "hallucination_repaired"):
            assert after.label == current.label
        truth = new
        current = after
