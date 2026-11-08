"""Unit tests of automatic illustration's hints (#265, FR-IMG-6): the Lite tag grammar, the
structured ``illustrate`` field, the per-turn cap and the fixed instructions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epistrel.core.settings_schema import StorySettings
from epistrel.observability.metrics import IMAGE_AUTO_HINTS_DROPPED
from epistrel.orchestrator.images import auto
from epistrel.orchestrator.images.auto import IllustrationPoint, strip_illustrate_tag
from epistrel.orchestrator.structured.render import render_schema


def _dropped() -> float:
    return float(IMAGE_AUTO_HINTS_DROPPED._value.get())


@pytest.mark.parametrize(
    ("reply", "text", "reason"),
    [
        ("Hello.\n[[illustrate: new_location]]", "Hello.", "new_location"),
        ("Hello.\n[[ ILLUSTRATE : Turning_Point ]]\n\n", "Hello.", "turning_point"),
        ("Hello.\n[[illustrate:first_appearance]]  ", "Hello.", "first_appearance"),
        (
            "Hello.\n[[illustrate: turning_point]]\n[[illustrate: new_location]]",
            "Hello.",
            "new_location",
        ),
        ("Hello. [[illustrate: new_location]]", "Hello. [[illustrate: new_location]]", None),
        ("[[illustrate: new_location]]\nHello.", "[[illustrate: new_location]]\nHello.", None),
        ("Hello.", "Hello.", None),
    ],
)
def test_lite_tag_grammar(reply: str, text: str, reason: str | None) -> None:
    """Only trailing ``[[illustrate…]]`` lines are stripped, every one of them; the reply's
    final tag is the one parsed, trimmed and lowercased. A tag inside a line, or above the
    reply's text, is text."""
    found = strip_illustrate_tag(reply)
    assert (found.text, found.reason, found.dropped) == (text, reason, False)


@pytest.mark.parametrize(
    "reply",
    ["Hello.\n[[illustrate: sunset]]", "Hello.\n[[illustrate]]", "Hello.\n[[illustrate: ]]"],
)
def test_lite_invalid_tag_dropped(reply: str) -> None:
    """An invalid tag is stripped all the same, gives no point and is counted."""
    before = _dropped()
    found = strip_illustrate_tag(reply)
    assert (found.text, found.reason, found.dropped) == ("Hello.", None, True)
    assert _dropped() == before + 1


def test_lite_tag_only_reply_is_empty() -> None:
    """A reply that is only a tag is empty once stripped (the pipeline's empty-reply path)."""
    assert strip_illustrate_tag("[[illustrate: new_location]]").text == ""


def test_structured_illustrate_field() -> None:
    """Under ``auto`` the render schema has an optional ``illustrate`` list (at most four
    ``{beat_index, reason}``): invalid entries are dropped and counted, never a structural
    failure, and the first hint naming a beat is its point. Without it the field is not in
    the schema, and an emitted one is ignored."""
    schema = render_schema(
        actor_id="c1", player_id=None, cast_ids=["c1"], max_beats=6, illustrate=True
    )
    field = schema.model_json_schema()["properties"]["illustrate"]
    assert (field["type"], field["maxItems"]) == ("array", auto.MAX_HINTS)
    before = _dropped()
    render = schema.model_validate(
        {
            "beats": [{"kind": "say", "text": "Here."}, {"kind": "do", "text": "Ada turns."}],
            "illustrate": [
                {"beat_index": 1, "reason": "turning_point"},
                {"beat_index": 1, "reason": "new_location"},
                {"beat_index": "0", "reason": "new_location"},
                {"beat_index": 0, "reason": "sunset"},
                {"beat_index": 0},
                *[{"beat_index": 0, "reason": "new_location"}] * 4,
            ],
        }
    )
    assert len(render.hints()) == auto.MAX_HINTS
    assert auto.first_hints(render.hints()) == {1: "turning_point", 0: "new_location"}
    assert _dropped() == before + 3 + 2

    plain = render_schema(actor_id="c1", player_id=None, cast_ids=["c1"], max_beats=6)
    assert "illustrate" not in plain.model_json_schema()["properties"]
    ignored = plain.model_validate(
        {"beats": [{"kind": "say", "text": "Here."}], "illustrate": "anything"}
    )
    assert ignored.hints() == []


def test_cap_keeps_the_first_points_in_beat_order() -> None:
    """The first ``cap`` points in beat order, one per beat (the first marked); the rest
    dropped."""
    points = [
        IllustrationPoint(4, "turning_point"),
        IllustrationPoint(2, "new_location"),
        IllustrationPoint(2, "first_appearance"),
        IllustrationPoint(3, "first_appearance"),
    ]
    found = auto.accept(points, 2)
    assert found.points == (
        IllustrationPoint(2, "new_location"),
        IllustrationPoint(3, "first_appearance"),
    )
    assert found.dropped == 1
    assert auto.accept(points, 0).points == ()


def test_illustration_settings() -> None:
    """``illustration_mode`` (``on_request`` by default, or ``auto``) and
    ``max_auto_images_per_turn`` (0 to 4, default 1, integers only)."""
    default = StorySettings()
    assert (default.illustration_mode, default.max_auto_images_per_turn) == ("on_request", 1)
    assert StorySettings(illustration_mode="auto", max_auto_images_per_turn=4)
    for bad in (
        {"illustration_mode": "always"},
        {"max_auto_images_per_turn": 5},
        {"max_auto_images_per_turn": -1},
        {"max_auto_images_per_turn": True},
        {"max_auto_images_per_turn": 1.0},
        {"max_auto_images_per_turn": "1"},
    ):
        with pytest.raises(ValidationError):
            StorySettings.model_validate(bad)


def test_instructions_snapshot() -> None:
    """The fixed instruction lines (one per pipeline): they name the three reasons, ask for a
    point at most rarely, only when one clearly applies, and never name the cap."""
    assert auto.LITE_ILLUSTRATE_INSTRUCTION == (
        "Illustration, at most rarely and only when one clearly applies: when this reply reaches "
        "a new location, a character's first appearance, or a moment the scene turns on, end it "
        "with one final line `[[illustrate: new_location]]`, `[[illustrate: first_appearance]]` or "
        "`[[illustrate: turning_point]]`. Otherwise add nothing."
    )
    assert auto.STRUCTURED_ILLUSTRATE_INSTRUCTION == (
        "Illustration, at most rarely and only when one clearly applies: when a beat reaches a new "
        "location, a character's first appearance, or a moment the scene turns on, list it in "
        "`illustrate` as `{beat_index, reason}`: the beat's 0-based index in your `beats`, and "
        "`new_location`, `first_appearance` or `turning_point`. Otherwise leave `illustrate` out."
    )
    for line in (auto.LITE_ILLUSTRATE_INSTRUCTION, auto.STRUCTURED_ILLUSTRATE_INSTRUCTION):
        assert all(reason in line for reason in auto.REASONS)
        assert "max_auto_images_per_turn" not in line
