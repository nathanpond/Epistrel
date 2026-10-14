"""The pure parts of the Lite turn (#57): settings precedence, the length mapping, viewpoint
resolution, the request model, and the rendered prompt's shape. No database."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epistrel.core.errors import ViewpointUnresolved
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.settings_schema import (
    DEFAULT_LITE_GENERATION,
    GenerationSettings,
    resolve_generation,
)
from epistrel.core.turns import CastEntry, TurnInputs, WindowRow, generation_of
from epistrel.core.viewpoint import OMNISCIENT, resolve_viewpoint
from epistrel.orchestrator.lite.cast import ResolvedCast
from epistrel.orchestrator.lite.prompt import assemble, model_params, prompt_hash
from epistrel.orchestrator.lite.schemas import RespondRequest
from epistrel.orchestrator.models import prompts
from epistrel.orchestrator.models.adapter import Message
from epistrel.orchestrator.models.registry import GENERATION

WREN = CastEntry(id="w", name="Wren", definition="A traveller.", is_player=True)
ADA = CastEntry(id="a", name="Ada", definition="The innkeeper.", is_player=False)
BO = CastEntry(id="b", name="Bo", definition="A farmer.", is_player=False)
ACTOR = "01HZY0000000000000000000AC"


def _inputs(**fields: object) -> TurnInputs:
    values: dict[str, object] = {
        "guidance": "",
        "instructions": "",
        "pins": (),
        "segments": (),
        "cast": (WREN, ADA, BO),
        "window": (),
        "generation": DEFAULT_LITE_GENERATION,
        "window_messages": 30,
    }
    values.update(fields)
    return TurnInputs(**values)  # type: ignore[arg-type]


def test_settings_precedence_and_lengths() -> None:
    deployment = GenerationSettings(temperature=0.2, top_p=0.4, length="long")
    story = GenerationSettings(temperature=0.5)
    request = GenerationSettings(length=77)
    resolved = resolve_generation(deployment, story, request)
    assert (resolved.temperature, resolved.top_p, resolved.max_tokens) == (0.5, 0.4, 77)
    assert resolve_generation(deployment).max_tokens == 800
    assert resolve_generation(GenerationSettings(length="short")).max_tokens == 150
    assert resolve_generation(DEFAULT_LITE_GENERATION).model_dump() == {
        "temperature": 0.8,
        "top_p": 0.95,
        "length": "medium",
        "max_tokens": 400,
    }
    assert resolve_generation().max_tokens == 400
    for bad in ({"temperature": 2.5}, {"top_p": -0.1}, {"length": 0}, {"length": 4001}):
        with pytest.raises(ValidationError):
            GenerationSettings.model_validate(bad)
    assert ProfileSettings().memory.rolling_summary.window_messages == 30
    # A configuration's generation settings are the bottom layer, its response length the
    # turn's length (#113).
    tuned = ProfileSettings.model_validate(
        {"models": {"generation": {"temperature": 0.3, "top_p": None, "response_length": 99}}}
    )
    assert generation_of(tuned) == GenerationSettings(temperature=0.3, top_p=None, length=99)
    assert generation_of(ProfileSettings()) == DEFAULT_LITE_GENERATION


def test_viewpoint_resolution() -> None:
    cast = ("w", "a")
    assert resolve_viewpoint(None, actor_character_id="w", story_default=None, cast_ids=cast) == (
        "character:w"
    )
    assert (
        resolve_viewpoint(None, actor_character_id=None, story_default="character:a", cast_ids=cast)
        == "character:a"
    )
    assert (
        resolve_viewpoint(OMNISCIENT, actor_character_id="w", story_default=None, cast_ids=cast)
        == OMNISCIENT
    )
    for named in ("character:x", "w", "OMNISCIENT"):
        with pytest.raises(ViewpointUnresolved):
            resolve_viewpoint(named, actor_character_id="w", story_default=None, cast_ids=cast)
    with pytest.raises(ViewpointUnresolved):
        resolve_viewpoint(None, actor_character_id=None, story_default=OMNISCIENT, cast_ids=cast)


def test_request_model_refuses_extra_fields_and_blank_input() -> None:
    ok = RespondRequest.model_validate({"actor_character_id": ACTOR, "player_input": {"do": "x"}})
    assert ok.player_input.say_text is None
    assert ok.player_input.do_text == "x"
    for bad in (
        {"actor_character_id": ACTOR, "player_input": {"say": "hi"}, "memories": []},
        {"actor_character_id": ACTOR, "player_input": {"say": "hi", "think": "x"}},
        {"actor_character_id": ACTOR, "player_input": {"say": " ", "do": "\n"}},
        {"actor_character_id": ACTOR, "player_input": {"say": "x" * 8001}},
        {"actor_character_id": ACTOR, "player_input": {"say": "hi"}, "render_character_ids": []},
        {"actor_character_id": "w", "player_input": {"say": "hi"}},
        {"actor_character_id": ACTOR, "player_input": {"say": "hi"}, "render_character_ids": ["a"]},
    ):
        with pytest.raises(ValidationError):
            RespondRequest.model_validate(bad)


def test_prompt_shape_and_empty_sections() -> None:
    """Empty sections are left out; the window alternates roles with same-role rows merged;
    the input is the last ``user`` message; only the actor's and render characters' notes."""
    window = (
        WindowRow("user", "player_input", "w", (), "hi", "hi", None),
        WindowRow("assistant", "message_rendered", None, ("a",), "Ada: hello", None, None),
        WindowRow("assistant", "message_rendered", None, ("a",), "Ada: again", None, None),
    )
    cast = ResolvedCast(actor=WREN, render=(ADA,))
    payload = assemble(_inputs(window=window), cast, say="yo", do=None)
    messages = prompts.render(GENERATION, payload)
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    system = messages[0].content
    assert system.startswith("### Author notes: Wren")
    assert "## Content rating guidance" not in system
    assert "### Author notes: Wren" in system
    assert "### Author notes: Ada" in system
    assert "### Author notes: Bo" not in system
    assert messages[2].content == "Ada: hello\n\nAda: again"
    assert messages[3].content == 'Wren: "yo"'
    params = model_params(resolve_generation(DEFAULT_LITE_GENERATION), cast)
    assert params.stop == ("\nWren:",)
    assert params.max_tokens == 400


def test_prompt_hash_is_canonical() -> None:
    one = [Message(role="system", content="s"), Message(role="user", content="u")]
    assert prompt_hash(one) == prompt_hash(list(one))
    assert prompt_hash(one) != prompt_hash([*one[:1], Message(role="user", content="v")])
    assert len(prompt_hash(one)) == 64


def test_template_without_line_macro_fails() -> None:
    from jinja2 import DictLoader

    source = "{% block system %}x{% endblock %}{% block user %}y{% endblock %}"
    environment = prompts.make_environment(DictLoader({"bad.j2": source}))
    with pytest.raises(prompts.TemplateFieldError, match="line"):
        prompts.check_template(environment, "bad.j2", GENERATION.input_schema, "recent_messages")
