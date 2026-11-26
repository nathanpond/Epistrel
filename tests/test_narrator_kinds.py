"""#1059 (FR-MULTI-9): a narrator's beat of a kind its render may not return is corrected once,
then dropped (``render.drop_narrator_kinds``), on the schema alone.

- Before the correction was sent, the answer is refused with ``NARRATOR_KIND`` and the
  correction text.
- After it, the ``say`` is dropped, the narration stands, the count is one, and an
  ``illustrate`` hint follows its beat to the new index (the dropped beat's hint goes).
- An answer with only character beats is still refused after the correction (nothing to keep).
- A character's render keeps the plain refusal (never ``NARRATOR_KIND``), and an allowed answer
  drops nothing.
"""

from __future__ import annotations

import json

from epistrel.orchestrator.models.structured import parse
from epistrel.orchestrator.structured.render import (
    NARRATOR_KIND,
    NARRATOR_KIND_CORRECTION,
    RenderedBeats,
    render_schema,
)

CAST = ["c1", "c2"]


def _narrator() -> type[RenderedBeats]:
    return render_schema(actor_id=None, player_id=None, cast_ids=CAST, max_beats=6, illustrate=True)


def _answer(*kinds: str, hints: list[dict[str, object]] | None = None) -> str:
    found: dict[str, object] = {
        "beats": [{"kind": k, "text": f"Beat {n} ({k})."} for n, k in enumerate(kinds)]
    }
    if hints is not None:
        found["illustrate"] = hints
    return json.dumps(found)


def test_refused_with_the_correction_before_it_was_sent() -> None:
    failure = parse(_answer("narration", "say"), _narrator())
    assert not isinstance(failure, RenderedBeats)
    assert NARRATOR_KIND in failure.codes
    assert NARRATOR_KIND_CORRECTION in failure.problem
    assert "beats.1.kind: not a kind this render may return" in failure.problem


def test_dropped_after_the_correction() -> None:
    hints: list[dict[str, object]] = [
        {"beat_index": 1, "reason": "turning_point"},
        {"beat_index": 2, "reason": "new_location"},
    ]
    found = parse(
        _answer("narration", "say", "narration", hints=hints),
        _narrator(),
        frozenset({NARRATOR_KIND}),
    )
    assert isinstance(found, RenderedBeats)
    assert [(b.kind, b.text) for b in found.beats] == [
        ("narration", "Beat 0 (narration)."),
        ("narration", "Beat 2 (narration)."),
    ]
    assert found.narrator_kinds_dropped == 1
    assert [(h.beat_index, h.reason) for h in found.hints()] == [(1, "new_location")]


def test_nothing_left_is_still_refused() -> None:
    failure = parse(_answer("say", "do"), _narrator(), frozenset({NARRATOR_KIND}))
    assert not isinstance(failure, RenderedBeats)
    assert "beats.0.kind: not a kind this render may return" in failure.problem


def test_a_character_render_keeps_the_plain_refusal() -> None:
    character = render_schema(actor_id="c1", player_id=None, cast_ids=CAST, max_beats=6)
    failure = parse(_answer("say", "narration"), character, frozenset({NARRATOR_KIND}))
    assert not isinstance(failure, RenderedBeats)
    assert NARRATOR_KIND not in failure.codes
    assert NARRATOR_KIND_CORRECTION not in failure.problem
    allowed = parse(_answer("narration", "latent"), _narrator(), frozenset({NARRATOR_KIND}))
    assert isinstance(allowed, RenderedBeats)
    assert allowed.narrator_kinds_dropped == 0
