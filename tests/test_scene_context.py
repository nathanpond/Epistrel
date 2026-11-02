"""#629: the shared scene context's last beats (the extraction backstop's and salience's input)
never carry a character's thought or latent beat from an earlier turn (FR-MODEL-13)."""

from __future__ import annotations

from types import SimpleNamespace

from epistrel.orchestrator.structured.pipeline import QUERY_BEATS, _scene_last_beats


def test_scene_last_beats_leave_out_thoughts() -> None:
    """Complement asserted: the character's says and dos stay, in order, at most QUERY_BEATS."""
    beats = [
        SimpleNamespace(character_id="mira", kind="say", text="Evening."),
        SimpleNamespace(character_id="mira", kind="think", text="He looks tired."),
        SimpleNamespace(character_id="mira", kind="latent", text="The cellar is flooded."),
        SimpleNamespace(character_id="cal", kind="say", text="Hello."),
        SimpleNamespace(character_id="mira", kind="do", text="She pours tea."),
    ]
    assert _scene_last_beats(beats, "mira") == ("Evening.", "She pours tea.")
    many = [SimpleNamespace(character_id="mira", kind="say", text=str(n)) for n in range(9)]
    assert len(_scene_last_beats(many, "mira")) == QUERY_BEATS
