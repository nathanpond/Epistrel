"""The engine's length rules for a structured turn (#464; FR-RESP-3, NFR-QUAL-7), the
extraction call's token cap, and E-06's render-only repeat key (the owner's decision on #464)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from epistrel.evals.suites.structured_turns import render_key
from epistrel.orchestrator.compose.compose import (
    LENGTH_FACTOR,
    MIN_POLISH_TOKENS,
    longest_polish,
    polish_params,
)
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import EXTRACTION_MAX_CLAIMS, ExtractedClaim
from epistrel.orchestrator.structured.render import (
    RenderedBeats,
    hold_length,
    length_chars,
    length_line,
    render_schema,
)
from epistrel.orchestrator.verify.extraction import BUILT_IN_PARAMS

CHARACTER = render_schema(actor_id="c1", player_id=None, cast_ids=["c1"], max_beats=6)
NARRATOR = render_schema(actor_id=None, player_id=None, cast_ids=["c1"], max_beats=6)
PRIVATE = frozenset({"think", "latent"})


def _render(*beats: tuple[str, str], introduces: tuple[str, ...] = ()) -> RenderedBeats:
    schema = NARRATOR if beats and beats[0][0] == "narration" else CHARACTER
    return schema.model_validate(
        {
            "beats": [{"kind": k, "text": t} for k, t in beats],
            "introduces": [{"name": n, "age": 30} for n in introduces],
        }
    )


def _kinds(render: RenderedBeats) -> list[str]:
    return [b.kind for b in render.beats]


def test_beats_past_the_length_are_dropped_in_order() -> None:
    render = _render(("say", "a" * 50), ("do", "b" * 60), ("say", "c" * 10))
    held, used, dropped = hold_length(render, 100)
    assert (_kinds(held), used, dropped) == (["say"], 50, 2)  # never a later, smaller beat


def test_the_first_say_or_do_is_always_kept() -> None:
    render = _render(("think", "t" * 300), ("do", "d" * 300), ("say", "s" * 5))
    held, _, dropped = hold_length(render, 50)
    assert (_kinds(held), dropped) == (["think", "do"], 1)


def test_hidden_kinds_do_not_count() -> None:
    render = _render(("think", "t" * 300), ("say", "s" * 80), ("latent", "l" * 300))
    held, used, dropped = hold_length(render, 100, hidden=PRIVATE)
    assert (_kinds(held), used, dropped) == (["think", "say", "latent"], 80, 0)
    assert held is render


def test_an_earlier_renders_share_counts_and_a_narrator_keeps_its_first_beat() -> None:
    narr = _render(("narration", "n" * 150), ("narration", "m" * 10))
    held, used, _ = hold_length(narr, 100)
    assert (_kinds(held), used) == (["narration"], 150)
    char = _render(("say", "s" * 20), ("do", "d" * 20))
    held, _, dropped = hold_length(char, 100, used=used)
    assert (_kinds(held), dropped) == (["say"], 1)


def test_an_introduction_only_in_a_dropped_beat_goes_with_it() -> None:
    render = _render(
        ("say", "Ask Tobias at the mill."),
        ("do", "She waves at Gerda across the room, " + "x" * 200),
        introduces=("Tobias", "Gerda", "Someone"),
    )
    held, _, _ = hold_length(render, 60)
    names = [d.name for d in held.introduces]
    assert names == ["Tobias", "Someone"]


def test_length_line_and_chars() -> None:
    assert length_chars(120) == 480
    assert "about 90 words" in length_line(120)
    assert "about 10 words" in length_line(1)


def test_a_polish_may_not_pass_the_requested_length() -> None:
    draft = "x" * 100
    assert longest_polish(draft) == LENGTH_FACTOR * 100
    assert longest_polish(draft, 120) == 120
    assert longest_polish(draft, 80) == 100  # never shorter than the draft itself
    assert longest_polish(draft, 1_000) == LENGTH_FACTOR * 100


def test_the_polish_cap_holds_the_longest_polish_in_its_json_answer() -> None:
    """#956: on hosted Haiku a 107-134-character draft's polish was cut off at the old cap
    (twice the plain draft at four characters a token, so the 64-token minimum). The cap now
    holds the longest polish accepted, JSON-escaped, at two characters a token."""
    draft = 'Bobby: "That sounds like a chore."\n\n*Bobby picks at a thread.*\n\nBobby: "No idea."'
    longest = draft + " " * int(longest_polish(draft) - len(draft))
    answer = json.dumps({"text": longest}, ensure_ascii=False)
    found = polish_params(ModelParams(temperature=0.7, top_p=0.9), draft)
    assert found.max_tokens is not None
    assert found.max_tokens * 2 >= len(answer)
    assert found.max_tokens > MIN_POLISH_TOKENS
    assert (found.temperature, found.top_p) == (0.7, 0.9)
    assert polish_params(ModelParams(), "Hi.").max_tokens == MIN_POLISH_TOKENS


def test_extraction_cap_fits_its_schema() -> None:
    """A full answer (``EXTRACTION_MAX_CLAIMS`` claims of an ordinary size, pretty-printed at
    four characters a token) is not cut at ``length`` (#464's ``extraction_degraded``)."""
    claim = ExtractedClaim(
        beat=0,
        span="Marcus used to sail with the harbour crews",
        subject="Marcus",
        predicate="worked_as",
        object="harbour crew sailor",
        time="years ago",
    )
    answer = json.dumps({"claims": [claim.model_dump()] * EXTRACTION_MAX_CLAIMS}, indent=2)
    assert BUILT_IN_PARAMS.max_tokens is not None
    assert BUILT_IN_PARAMS.max_tokens >= len(answer) / 4


def _beat(n: int, channel: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(beat=n, channel=channel, text=text)


def test_render_key_is_the_render_not_the_message() -> None:
    """E-06's repeat key: the rendered beats in order, a repaired beat by its original text;
    the composed message plays no part (two turns with different messages and the same render
    repeat)."""
    first = render_key([_beat(3, "do", "She sits."), _beat(2, "say", "Hello.")], {})
    assert first == ((2, "say", "Hello."), (3, "do", "She sits."))
    repaired = render_key([_beat(2, "say", "I am 64.")], {2: "I am 70."})
    assert repaired == ((2, "say", "I am 70."),)
    dropped = render_key([_beat(2, "say", "Hello.")], {3: "A dropped beat."})
    assert dropped == ((2, "say", "Hello."), (3, "", "A dropped beat."))
