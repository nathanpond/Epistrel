"""Unit tests of the image prompt builder (#259): ``PerceivedView``'s shape (no age and no
restricted field anywhere in it), the authored-age redaction, the negative prompt's merge, the
size and ratio rule, the deterministic completion of the model's prompt, and the
restricted-term check of a drafted base appearance prompt. Unit level: no part marker."""

from __future__ import annotations

import dataclasses
import re
import typing
from typing import Any, get_args, get_origin

import pytest

from epistrel.core.images import perceived
from epistrel.orchestrator.images import port
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.images.prompt_builder import (
    PerceivedSubject,
    PerceivedView,
    completed,
    merge_negative,
    prompt_inputs,
    resolve_dimensions,
)

FORBIDDEN_NAME = re.compile(r"(^|_)ages?($|_)|restricted|true_")


def _dataclass_fields(cls: type, seen: set[type]) -> list[tuple[str, str]]:
    """``(owner, field name)`` of every field of ``cls`` and of every dataclass its fields
    hold, at any depth."""
    if cls in seen:
        return []
    seen.add(cls)
    hints = typing.get_type_hints(cls)
    found: list[tuple[str, str]] = []
    for field in dataclasses.fields(cls):
        found.append((cls.__name__, field.name))
        stack: list[Any] = [hints[field.name]]
        while stack:
            hint = stack.pop()
            if dataclasses.is_dataclass(hint) and isinstance(hint, type):
                found += _dataclass_fields(hint, seen)
            stack.extend(get_args(hint))
            origin = get_origin(hint)
            if origin is not None and dataclasses.is_dataclass(origin):
                stack.append(origin)
    return found


def test_perceived_view_has_no_age_or_restricted_field() -> None:
    """Every field of ``PerceivedView`` and the dataclasses it nests: none is named for an age
    (other than ``age_band``), a restricted element or a true value."""
    fields = _dataclass_fields(PerceivedView, set())
    assert ("PerceivedSubject", "age_band") in fields
    assert ("PerceivedView", "subjects") in fields
    offending = [
        (owner, name)
        for owner, name in fields
        if name != "age_band" and FORBIDDEN_NAME.search(name)
    ]
    assert offending == []
    # Complement: the check bites on the names it guards against.
    assert all(
        FORBIDDEN_NAME.search(name)
        for name in ("true_age", "age", "restricted_details_prompt", "apparent_age")
    )
    assert not FORBIDDEN_NAME.search("reference_image_ids")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A 34-year-old woman with a braid.", "A woman with a braid."),
        ("She is thirty-four years old, and tall.", "She is, and tall."),
        ("Tall, aged 34, copper braid", "Tall, copper braid"),
        ("Tall (34 yo), copper braid", "Tall, copper braid"),
        ("A woman of 34 with a braid", "A woman of with a braid"),
        ("Twenty-two freckles on her nose", "Twenty-two freckles on her nose"),
        ("A tall woman with a braid.", "A tall woman with a braid."),
    ],
)
def test_redact_ages(text: str, expected: str) -> None:
    assert perceived.redact_ages(text, 34) == expected


def test_size_patterns_agree_with_the_port() -> None:
    assert perceived.SIZE_PATTERN.pattern == port.SIZE_PATTERN
    assert perceived.ASPECT_RATIO_PATTERN.pattern == port.ASPECT_RATIO_PATTERN


def test_merge_negative() -> None:
    assert merge_negative("text, watermark, Blurry", "blurry, , lens flare") == (
        "text, watermark, Blurry, lens flare"
    )
    assert merge_negative(None, "  ") is None
    assert merge_negative(None, None) is None


def test_resolve_dimensions() -> None:
    configured = ImageGenerationParams(adapter="stub", default_size="512x512")
    ratio_only = ImageGenerationParams(adapter="stub", default_aspect_ratio="4:3")
    unset = ImageGenerationParams(adapter="stub")
    assert resolve_dimensions("primary", "768x768", "3:2", configured) == ("768x768", None)
    assert resolve_dimensions("primary", None, "3:2", configured) == (None, "3:2")
    assert resolve_dimensions("primary", None, None, configured) == ("512x512", None)
    assert resolve_dimensions("cover", None, None, ratio_only) == (None, "4:3")
    # #254's built-in 1:1 is not a configured default: the purpose's ratio applies.
    assert resolve_dimensions("cover", None, None, unset) == (None, "16:9")
    assert resolve_dimensions("gallery", None, None, None) == (None, "1:1")


def _view(**fields: Any) -> PerceivedView:
    subject = PerceivedSubject(
        description="a tall woman",
        age_band="mid thirties",
        base_appearance_prompt=None,
        reference_image_ids=("r1",),
        seed=7,
        adapter_addons=({"name": "seen", "audience": "perceivable"},),
    )
    values: dict[str, Any] = {
        "subjects": (subject,),
        "scene": "single-subject portrait, plain background",
        "visual_style": "Gilded Umber brushwork",
        "rating_image_guidance": "All-ages imagery only.",
        "purpose": "primary",
        "identity_negative": "text",
        "size": "512x512",
        **fields,
    }
    return PerceivedView(**values)


def test_completed_appends_the_style_and_the_band() -> None:
    view = _view()
    assert completed("a woman on a pier", view) == (
        "a woman on a pier, Gilded Umber brushwork, apparent age: mid thirties"
    )
    # Present already (ignoring case): nothing is appended.
    assert completed("a woman in her MID THIRTIES, gilded umber brushwork", view) == (
        "a woman in her MID THIRTIES, gilded umber brushwork"
    )


def test_prompt_inputs_carry_no_parameter() -> None:
    """The ``image_prompt`` payload holds the subjects, scene, style, guidance and purpose:
    never a reference, a seed, an add-on, a size or a negative prompt."""
    payload = prompt_inputs(_view()).model_dump()
    assert payload["subjects"] == [
        {
            "description": "a tall woman",
            "age_band": "mid thirties",
            "base_appearance_prompt": "",
            "label": "",
        }
    ]
    # A catalog image has no story sections (#263's beats, location and framing).
    assert (payload["beats"], payload["location"], payload["camera"]) == ([], "", "")
    dumped = repr(payload)
    for absent in ("r1", "seen", "512x512", "'text'"):
        assert absent not in dumped


def test_restricted_terms_and_leaks() -> None:
    terms = perceived.restricted_terms(
        "a hidden kraken tattoo across her shoulders, with scars", "her shoulders are broad"
    )
    assert terms == {"hidden", "kraken", "tattoo", "across", "scars"}
    assert perceived.leaks("tall woman, kraken tattoo", None, terms)
    assert perceived.leaks("a 34 year old woman", None, terms)
    assert perceived.leaks("tall woman, 34", 34, terms)
    assert not perceived.leaks("tall woman, broad shoulders, copper braid", 34, terms)
    assert perceived.restricted_terms(None, "anything") == frozenset()
