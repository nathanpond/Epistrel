"""Unit tests of the story image builder's pure parts (#263): perceived age bands, the
unperceived-beat index, the beat cap, the framing text, the consumer prompt's sections, the
deterministic parameters and the prompt request's shape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epistrel.core.images.story_perceived import UnperceivedIndex, _mid, believed_band
from epistrel.orchestrator.images.prompt_builder import (
    FRAMING,
    ImagePromptRequest,
    PerceivedSubject,
    PerceivedView,
    capped_beats,
    consumer_prompt,
    framing,
    story_parameters,
)

WHISPER = "The key is under the blue mat."


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (34, "mid thirties"),
        ("31", "early thirties"),
        ("thirties", "mid thirties"),
        ("a man in his thirties", "mid thirties"),
        ("late twenties", "late twenties"),
        ("tall", None),
        (True, None),
    ],
)
def test_believed_band(value: object, expected: str | None) -> None:
    assert believed_band(value) == expected


def test_unknown_third_is_mid() -> None:
    assert _mid("early thirties") == "mid thirties"
    assert _mid("late forties") == "mid forties"
    # Complement: bands without a third are kept.
    assert _mid("teens") == "teens"
    assert _mid("elderly") == "elderly"


def test_unperceived_index() -> None:
    """A whispered sentence is indexed by its 4-word shingles, less those of what the viewpoint
    perceived; a two- or three-word utterance is one phrase; a single word is not indexed."""
    index = UnperceivedIndex.of(
        [WHISPER, "Run, now!", "Dana", "Jenni leans in close"],
        ["Jenni leans in close and lowers her voice."],
    )
    assert index.hit("A note says: under the blue mat")
    assert index.hit("THE KEY IS UNDER the doormat")
    assert index.hit("she told him to run now")
    # Complement: a perceived 4-gram, a single unperceived word and three whisper words alone
    # are no hit.
    assert not index.hit("Jenni leans in close to him.")
    assert not index.hit("Dana was there.")
    assert not index.hit("the blue mat")
    assert not index.hit(None)


def test_capped_beats_keep_the_nearest() -> None:
    beats = ("a" * 10, "b" * 10, "c" * 10)
    assert capped_beats(beats, 25) == ("b" * 10, "c" * 10)
    assert capped_beats(beats, 30) == beats
    assert capped_beats(beats, 5) == ()


def test_framing() -> None:
    assert framing({"kind": "third_person"}, None) == FRAMING["third_person"]
    assert framing({}, None) == FRAMING["third_person"]
    assert framing({"kind": "establishing"}, None) == "wide establishing shot of the location"
    assert framing({"kind": "over_the_shoulder"}, "person 2") == "over the shoulder of person 2"
    assert framing({"kind": "first_person"}, "person 1") == (
        "first-person view through the eyes of person 1, who is not shown"
    )


def test_consumer_prompt_sections() -> None:
    assert consumer_prompt("  A parlor.  ", "woodcut", "All ages. ") == (
        "A parlor.\n\nVisual style: woodcut\n\nImage guidance: All ages."
    )
    assert consumer_prompt("A parlor.", None, "") == "A parlor."


def _subject(n: int, *, seed: int | None = None, refs: int = 0) -> PerceivedSubject:
    return PerceivedSubject(
        description="",
        age_band="mid thirties",
        base_appearance_prompt=None,
        reference_image_ids=tuple(f"r{n}-{i}" for i in range(refs)),
        seed=seed,
        label=f"person {n}",
        negative_prompt="blurry, Text",
        subject_ids=(f"c{n}",),
    )


def _view(*subjects: PerceivedSubject, camera: str | None = None) -> PerceivedView:
    return PerceivedView(
        subjects=subjects,
        scene="",
        visual_style=None,
        rating_image_guidance="",
        purpose="story",
        style_negative="text, neon",
        camera_subject=camera,
    )


def test_story_parameters() -> None:
    """The camera subject's references first, at most four, the rest recorded as dropped; its
    seed, else the one seeded subject's; negatives merged without duplicates."""
    view = _view(_subject(1, refs=3), _subject(2, seed=7, refs=2), camera="c2")
    negative, seed, refs, dropped = story_parameters(view)
    assert refs == ("r2-0", "r2-1", "r1-0", "r1-1")
    assert dropped == ("r1-2",)
    assert seed == 7
    assert negative == "blurry, Text, neon"
    # Two seeded subjects and no camera subject: no seed.
    assert story_parameters(_view(_subject(1, seed=1), _subject(2, seed=2)))[1] is None
    assert story_parameters(_view(_subject(1), _subject(2, seed=2)))[1] == 2


def test_prompt_request_purpose() -> None:
    story = {
        "kind": "story",
        "story_id": "01J8ZQ6T7Y3K5V9W2X4N6P8R0V",
        "position": {"turn": 1, "beat": 2},
        "audience_viewpoint": "omniscient",
    }
    assert ImagePromptRequest.model_validate({"target": story}).purpose is None
    with pytest.raises(ValidationError):
        ImagePromptRequest.model_validate({"target": story, "purpose": "cover"})
    with pytest.raises(ValidationError):
        ImagePromptRequest.model_validate(
            {"target": {"kind": "character", "id": "01J8ZQ6T7Y3K5V9W2X4N6P8R0S"}}
        )
