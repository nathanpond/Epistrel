"""Prompt templates bound to the purpose registry (FR-MODEL-13).

Each delivered purpose names a Jinja2 template in ``templates/`` with a ``system`` and a
``user`` block, rendered separately into one system and one user ``Message``. Templates render
with ``StrictUndefined`` and only Jinja's built-in filters, and are checked when this module is
imported: a variable that is not a field of the purpose's ``input_schema`` (found by
``meta.find_undeclared_variables``), or an attribute a nested model does not have (found by
rendering against an example instance built from the schema), raises ``TemplateFieldError`` —
so a template that references an undeclared field fails the build.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from jinja2 import Environment, PackageLoader, StrictUndefined, Template, TemplateError, meta
from pydantic import BaseModel

from epistrel.orchestrator.models.adapter import Message
from epistrel.orchestrator.models.registry import DELIVERED, InputSchema, Purpose

BLOCKS = ("system", "user")


class TemplateFieldError(Exception):
    """A template uses a field its purpose does not declare, or lacks a role block."""


def make_environment(loader: Any = None) -> Environment:
    return Environment(
        loader=loader or PackageLoader("epistrel.orchestrator.models", "templates"),
        undefined=StrictUndefined,
        # Prompts are plain text sent to a model, never HTML.
        autoescape=False,  # noqa: S701
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )


def _example_value(annotation: Any) -> Any:
    """A non-empty example value for ``annotation`` so every template branch is exercised."""
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _example_value(typing.get_args(annotation)[0])
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return example_instance(annotation)
    if origin is Literal:
        return typing.get_args(annotation)[0]
    if origin in (list, tuple, set, frozenset):
        return [_example_value(typing.get_args(annotation)[0])]
    if origin is types.UnionType or origin is typing.Union:
        options = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _example_value(options[0])
    if annotation is bool:
        return True
    if annotation is int:
        return 1
    if annotation is float:
        return 0.5
    return "example"


def example_instance[T: BaseModel](schema: type[T]) -> T:
    """An instance of ``schema`` with every field set, optional ones included."""
    values = {name: _example_value(info.annotation) for name, info in schema.model_fields.items()}
    return schema.model_validate(values)


def template_context(inputs: BaseModel) -> dict[str, Any]:
    """Top-level fields as attributes (nested models stay models, so attribute access works)."""
    return {name: getattr(inputs, name) for name in type(inputs).model_fields}


def render_blocks(template: Template, inputs: BaseModel) -> list[Message]:
    context = template.new_context(template_context(inputs))
    return [
        Message(role=role, content="".join(template.blocks[role](context)).strip())
        for role in BLOCKS
    ]


def check_template(environment: Environment, name: str, schema: type[InputSchema]) -> Template:
    """Load ``name`` and prove it uses only ``schema``'s fields; raise ``TemplateFieldError``."""
    assert environment.loader is not None  # noqa: S101 - make_environment always sets one
    source, _, _ = environment.loader.get_source(environment, name)
    parsed = environment.parse(source)
    declared = set(schema.model_fields)
    undeclared = meta.find_undeclared_variables(parsed) - declared
    if undeclared:
        raise TemplateFieldError(
            f"{name}: references undeclared input field(s): {', '.join(sorted(undeclared))}"
        )
    template = environment.get_template(name)
    missing = [role for role in BLOCKS if role not in template.blocks]
    if missing:
        raise TemplateFieldError(f"{name}: missing block(s): {', '.join(missing)}")
    try:
        render_blocks(template, example_instance(schema))
    except TemplateError as exc:
        raise TemplateFieldError(f"{name}: {exc}") from exc
    return template


def load_templates(
    purposes: Mapping[str, Purpose], environment: Environment | None = None
) -> dict[str, Template]:
    environment = environment or make_environment()
    return {
        name: check_template(environment, purpose.template, purpose.input_schema)
        for name, purpose in purposes.items()
    }


TEMPLATES: Mapping[str, Template] = types.MappingProxyType(load_templates(DELIVERED))
"""Every delivered purpose's checked template, by purpose name (checked at import)."""


def _paths(value: Any, prefix: str = "") -> list[str]:
    """The field paths actually present in a payload (list indexes dropped)."""
    found: list[str] = []
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            path = f"{prefix}{name}"
            found.append(path)
            found.extend(_paths(getattr(value, name), f"{path}."))
    elif isinstance(value, list | tuple):
        for item in value:
            found.extend(p for p in _paths(item, prefix) if p not in found)
    return found


def payload_fields(inputs: InputSchema) -> tuple[str, ...]:
    """Sorted, de-duplicated field paths of a validated payload."""
    return tuple(sorted(set(_paths(inputs))))


def render(purpose: Purpose, inputs: InputSchema) -> list[Message]:
    """The system and user messages of ``purpose`` for ``inputs`` (already validated)."""
    return render_blocks(TEMPLATES[purpose.name], inputs)


__all__ = [
    "TEMPLATES",
    "TemplateFieldError",
    "check_template",
    "example_instance",
    "load_templates",
    "make_environment",
    "payload_fields",
    "render",
]
