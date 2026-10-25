"""The purpose registry and its templates: every input tagged, templates use only declared
fields (checked at import), and payloads carry only declared fields."""

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest
from jinja2 import DictLoader, Environment
from pydantic import ConfigDict

from epistrel.config import Settings
from epistrel.orchestrator.models import prompts, registry
from epistrel.orchestrator.models.access import (
    ModelAccessLayer,
    declared_fields,
    get_delivered,
)
from epistrel.orchestrator.models.adapter import UndeclaredInput, UnknownPurpose
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    PORTS,
    RESERVED,
    ContentCategory,
    GenerationInputs,
    InputSchema,
    PlayerInput,
    RegistryError,
)
from tests.auth_support import auth_settings

# docs/03 FR-MODEL-2's purposes and the two separate ports, in registry names.
FR_MODEL_2 = {
    "generation",
    "regeneration",
    "entailment",
    "consolidation",
    "importance_scoring",
    "salience_detection",
    "extraction_backstop",
    "time_estimation",
    "guardrail_check",
    "rating_suggestion",
    "image_prompt",
    "image_check",
    "quality_judge",
    "belief_adjudication",
    "embedding",
    "image_generation",
}


def stub_settings() -> Settings:
    return auth_settings("postgresql+psycopg://u:p@localhost:5432/x", model_adapter="stub")


def bad_template_env(source: str) -> Environment:
    return prompts.make_environment(DictLoader({"bad.j2": source}))


def test_m2_purposes_are_delivered_with_tagged_schemas() -> None:
    # M3 delivers rating_suggestion (#121), which defaults to guardrail_check's row (FR-MODEL-2);
    # M4 the eval harness's quality_judge (#134), memory importance_scoring (#135) and
    # verify's entailment (#142), memory consolidation (#143), which lite_summary now
    # defaults to (#51's note), commitment's salience_detection (#146), claim detection's
    # extraction_backstop (#148) and the repair ladder's regeneration (#151), which shares
    # generation's row (docs/02 §5: usually the same model). M5 delivers testimony's
    # belief_adjudication (#162), which defaults to entailment's row (FR-MODEL-2).
    assert set(DELIVERED) == {
        "generation",
        "guardrail_check",
        "consolidation",
        "lite_summary",
        "rating_suggestion",
        "quality_judge",
        "importance_scoring",
        "entailment",
        "belief_adjudication",
        "salience_detection",
        "extraction_backstop",
        "regeneration",
    }
    for purpose in DELIVERED.values():
        parents = {
            "belief_adjudication": "entailment",
            "rating_suggestion": "guardrail_check",
            "lite_summary": "consolidation",
            "regeneration": "generation",
        }
        expected = parents.get(purpose.name, "default")
        assert purpose.default_model_key == expected
        assert purpose.capability_floor.min_context_tokens == 8_192
        registry.validate_purpose(purpose)
        categories = registry.field_categories(purpose.input_schema)
        assert all(categories.values())
        assert not registry.schema_categories(purpose.input_schema) & purpose.never_receives


def test_registry_enumerates_every_named_purpose() -> None:
    # M4 delivers the embedding port (#138): a port, configured like a purpose, never a chat one.
    assert set(DELIVERED) | set(PORTS) | set(RESERVED) >= FR_MODEL_2
    assert not set(DELIVERED) & set(RESERVED)
    assert not set(PORTS) & (set(DELIVERED) | set(RESERVED))
    assert set(PORTS) == {"embedding"}
    for reserved in RESERVED.values():
        assert reserved.milestone in {"M3", "M4", "M5", "M6", "M7", "M8"}
        assert reserved.defaults_to is None or reserved.defaults_to in FR_MODEL_2
    assert RESERVED["image_check"].defaults_to is None


def test_reserved_and_unknown_purposes_are_refused() -> None:
    with pytest.raises(UnknownPurpose) as reserved:
        get_delivered("time_estimation")
    details = dict(reserved.value.details)
    assert len(details.pop("errors")) == 1
    assert details == {
        "field": "body",
        "purpose": "time_estimation",
        "reason": "reserved",
        "milestone": "M6",
    }
    with pytest.raises(UnknownPurpose) as unknown:
        get_delivered("nope")
    assert unknown.value.details["reason"] == "unknown"


def test_untagged_input_field_fails_the_registry() -> None:
    class Untagged(InputSchema):
        tagged: Annotated[str, ContentCategory.PLAYER_INPUT]
        untagged: str

    purpose = dataclasses.replace(registry.GENERATION, input_schema=Untagged)
    with pytest.raises(RegistryError, match=r"Untagged\.untagged: input field has no content"):
        registry.validate_purpose(purpose)

    class Inner(InputSchema):
        loose: str

    class Outer(InputSchema):
        inner: Inner

    with pytest.raises(RegistryError, match=r"Inner\.loose"):
        registry.field_categories(Outer)

    class TaggedOuter(InputSchema):
        inner: Annotated[Inner, ContentCategory.PLAYER_INPUT]

    # A tag on the enclosing field covers what it holds.
    assert registry.field_categories(TaggedOuter)["inner.loose"] == {ContentCategory.PLAYER_INPUT}


def test_a_category_the_purpose_never_receives_fails_the_registry() -> None:
    class Leaky(InputSchema):
        player_input: Annotated[str, ContentCategory.PLAYER_INPUT]
        age: Annotated[int, ContentCategory.SUBJECT_TRUE_AGE]

    purpose = dataclasses.replace(registry.GENERATION, input_schema=Leaky)
    with pytest.raises(RegistryError, match="subject_true_age"):
        registry.validate_purpose(purpose)


@pytest.mark.parametrize(
    ("source", "complaint"),
    [
        (
            "{% block system %}{{ secret_notes }}{% endblock %}{% block user %}{% endblock %}",
            "undeclared input field(s): secret_notes",
        ),
        (
            "{% block system %}{{ player_input.true_age }}{% endblock %}"
            "{% block user %}{% endblock %}",
            "true_age",
        ),
        (
            "{% block system %}{% for line in recent_messages %}{{ line.thought }}{% endfor %}"
            "{% endblock %}{% block user %}{% endblock %}",
            "thought",
        ),
        ("{% block system %}{{ rating_guidance }}{% endblock %}", "missing block(s): user"),
    ],
)
def test_template_with_undeclared_field_fails(source: str, complaint: str) -> None:
    """The fixture templates fail the same check ``prompts`` runs on every real template at
    import; the complement is that the shipped templates load."""
    with pytest.raises(
        prompts.TemplateFieldError, match=complaint.replace("(", r"\(").replace(")", r"\)")
    ):
        prompts.check_template(bad_template_env(source), "bad.j2", GenerationInputs)
    bad = {"generation": dataclasses.replace(registry.GENERATION, template="bad.j2")}
    with pytest.raises(prompts.TemplateFieldError):
        prompts.load_templates(bad, bad_template_env(source))


def test_shipped_templates_were_checked_at_import() -> None:
    """Every delivered purpose's template and each of its variants (#150) was checked."""
    assert set(prompts.TEMPLATES) == set(DELIVERED) | {
        prompts.variant_key(name, variant)
        for name, purpose in DELIVERED.items()
        for variant in purpose.variants
    }
    for purpose in DELIVERED.values():
        for variant in (None, *purpose.variants):
            messages = prompts.render(
                purpose, prompts.example_instance(purpose.input_schema), variant
            )
            assert [m.role for m in messages] == ["system", "user"]
            assert all(m.content for m in messages)


def test_an_unknown_template_variant_is_a_registry_error() -> None:
    """A call naming a variant its purpose does not have is refused (#150)."""
    with pytest.raises(registry.RegistryError):
        prompts.render(
            registry.GENERATION, prompts.example_instance(GenerationInputs), "no_such_variant"
        )


@pytest.mark.parametrize("purpose", sorted(DELIVERED))
async def test_captured_request_carries_only_declared_fields(purpose: str) -> None:
    mal = ModelAccessLayer.from_settings(stub_settings())
    declared = DELIVERED[purpose]
    await mal.call(purpose, prompts.example_instance(declared.input_schema))
    (call,) = mal.stub.calls
    assert call.purpose == purpose
    assert call.fields is not None
    assert set(call.fields) <= declared_fields(declared)
    assert set(call.fields) == declared_fields(declared)  # the example sets every field


async def test_extra_field_from_a_payload_builder_is_refused() -> None:
    """The complement of the test above: a builder that adds a field is rejected, at the top
    level, in a model it holds, or by smuggling it through a subclass."""
    mal = ModelAccessLayer.from_settings(stub_settings())
    player = {"name": "Ada", "say": "hi"}

    with pytest.raises(UndeclaredInput) as top:
        await mal.call("generation", {"player_input": player, "true_ages": {"Ada": 17}})
    assert top.value.details["fields"] == ["true_ages"]

    with pytest.raises(UndeclaredInput) as inner:
        await mal.call("generation", {"player_input": {**player, "secret": "x"}})
    assert inner.value.details["fields"] == ["player_input.secret"]

    class Smuggling(GenerationInputs):
        model_config = ConfigDict(extra="allow", frozen=True)

    smuggled = Smuggling(player_input=PlayerInput(name="Ada"), hidden_memory="x")
    with pytest.raises(UndeclaredInput) as sub:
        await mal.call("generation", smuggled)
    assert sub.value.details["fields"] == ["hidden_memory"]
    assert mal.stub.calls == []
