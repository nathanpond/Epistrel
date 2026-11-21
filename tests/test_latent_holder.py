"""#750 (FR-EPI-8, FR-TURN-12): a render names its latent's knowers by cast name as often as by
id, and records "who knows" as the latent's ``holder``. Names in ``visibility.characters`` and
``hidden.noticers`` resolve to cast ids (exact name case-folded, or a first name no one else
shares); a latent ``holder`` naming a cast member is repaired to ``canon`` with that member in
``visibility``. The live failure this guards: Haiku wrote ``holder: "Bobby"`` and
``visibility.characters: ["Bobby"]`` and failed every retry with the same two errors."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from epistrel.orchestrator.structured.render import (
    LATENT_HOLDER_REPAIRED,
    RenderedBeats,
    render_schema,
)

BOBBY, CLARA, CLAIRE, WREN = "c-bobby", "c-clara", "c-claire", "c-wren"
NAMES = {
    BOBBY: "Bobby Hale",
    CLARA: "Clara Voss",
    CLAIRE: "Clara Lind",  # shares Clara's first name: "Clara" alone is ambiguous
    WREN: "Wren",
}
SECRET = "A small silver brooch is hidden in Bobby's coat pocket."


def _schema() -> type[RenderedBeats]:
    return render_schema(
        actor_id=BOBBY,
        player_id=WREN,
        cast_ids=list(NAMES),
        cast_names=NAMES,
        max_beats=6,
    )


def _render(**latent: Any) -> RenderedBeats:
    return _schema().model_validate(
        {
            "beats": [
                {"kind": "do", "text": "Bobby shrugs."},
                {"kind": "latent", "text": SECRET, **latent},
            ]
        }
    )


def _latent(render: RenderedBeats) -> Any:
    return render.beats[1]


def test_cast_names_resolve_to_ids_in_visibility_and_noticers() -> None:
    """Full names case-folded, an unshared first name and ids, mixed and with duplicates: each
    resolves to its id once, in order."""
    render = _render(
        visibility={"scope": "characters", "characters": ["bobby hale", "Wren", BOBBY]},
        hidden={"noticers": ["Bobby", "Clara Voss"]},
    )
    latent = _latent(render)
    assert latent.visibility.characters == [BOBBY, WREN]
    assert latent.hidden.noticers == [BOBBY, CLARA]
    assert render.latent_holder_repairs == 0


@pytest.mark.parametrize("who", ["Nobody", "Clara", "he"])
def test_an_unknown_or_ambiguous_name_is_still_refused_naming_the_cast(who: str) -> None:
    """Complement: a name no cast member has, a first name two share, and a pronoun are refused,
    and the refusal names the cast so the retry can correct it."""
    with pytest.raises(ValidationError) as found:
        _render(visibility={"scope": "characters", "characters": [who]})
    message = str(found.value)
    assert "beats.1.visibility: an unknown character" in message
    assert "Bobby Hale" in message
    assert BOBBY in message
    with pytest.raises(ValidationError, match=r"hidden\.noticers: an unknown character"):
        _render(hidden={"noticers": [who]})


@pytest.mark.parametrize(
    ("visibility", "expected"),
    [
        (None, {"scope": "characters", "characters": [BOBBY]}),
        ({"scope": "objective"}, {"scope": "characters", "characters": [BOBBY]}),
        (
            {"scope": "characters", "characters": ["Wren"]},
            {"scope": "characters", "characters": [WREN, BOBBY]},
        ),
        (
            {"scope": "characters", "characters": ["Bobby"]},
            {"scope": "characters", "characters": [BOBBY]},
        ),
        ({"scope": "public"}, {"scope": "public", "characters": []}),
    ],
)
def test_a_latent_holder_naming_a_cast_member_is_repaired_to_canon(
    visibility: dict[str, Any] | None, expected: dict[str, Any]
) -> None:
    """The live shape (``holder`` a name, ``visibility`` that name) validates: the holder is
    ``canon`` (the truth stays canon's) and the named member is among those who may know it."""
    fields: dict[str, Any] = {"holder": "Bobby"}
    if visibility is not None:
        fields["visibility"] = visibility
    render = _render(**fields)
    latent = _latent(render)
    assert latent.holder == "canon"
    assert latent.visibility.model_dump() == expected
    assert render.latent_holder_repairs == 1
    assert LATENT_HOLDER_REPAIRED == "latent_holder_repaired"


def test_a_holder_naming_no_one_is_refused_as_before() -> None:
    """Complement: a holder that is no cast member is ``viewpoint_cannot_assign_truth``; so is
    a holder on a beat that is not a latent (nothing is repaired there)."""
    with pytest.raises(ValidationError, match="viewpoint_cannot_assign_truth"):
        _render(holder="the mailman")
    with pytest.raises(ValidationError, match="only a latent commits a hidden fact"):
        _schema().model_validate(
            {"beats": [{"kind": "do", "text": "Bobby shrugs.", "holder": "Bobby"}]}
        )
    assert _render(holder="canon").latent_holder_repairs == 0


def test_without_names_ids_alone_resolve() -> None:
    """A schema built without ``cast_names`` (the probe, older callers) keeps the id-only
    check."""
    bare = render_schema(actor_id=BOBBY, player_id=None, cast_ids=list(NAMES), max_beats=6)
    beats: list[dict[str, Any]] = [
        {"kind": "do", "text": "x"},
        {
            "kind": "latent",
            "text": SECRET,
            "visibility": {"scope": "characters", "characters": [BOBBY]},
        },
    ]
    visibility = bare.model_validate({"beats": beats}).beats[1].visibility
    assert visibility is not None
    assert visibility.characters == [BOBBY]
    beats[1]["visibility"] = {"scope": "characters", "characters": ["Bobby Hale"]}
    with pytest.raises(ValidationError, match="unknown character"):
        bare.model_validate({"beats": beats})
