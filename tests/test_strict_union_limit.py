"""#941: api.anthropic.com's strict mode refuses a schema with more than 16 union-typed parameters.

Live on 2026-11-24, every strict ``RewriteParseResult`` call was a 400: "Schemas contains too many
parameters with union types (17 parameters with type arrays or anyOf) ... (limit: 16 parameters
with unions)". The wire form inlines ``$ref``s (#1009), so a union inside a shared definition
counts once per use. Such a schema has no wire form: it goes as parse-validate-retry, and the
warm-up sends nothing for it. The 400's phrase is still mapped to ``SchemaTooLarge`` as a
safety net (a per-schema fallback, not an endpoint-wide one).

Not covered: the count is this module's reading of the provider's rule (a node with a ``type``
list or an ``anyOf``). It reproduces the live count for ``RewriteParseResult`` (17), and every
other schema sent strictly is at 8 or fewer.
"""

from __future__ import annotations

import openai

from epistrel.orchestrator.models import provider_schema
from epistrel.orchestrator.models.errors import SchemaTooLarge, map_provider_error
from epistrel.orchestrator.models.provider_schema import (
    UNION_LIMIT,
    json_schema_mode,
    rejects_grammar_size,
    strict_wire_schema,
    union_parameters,
)
from epistrel.orchestrator.models.registry import RewriteParseResult
from epistrel.tools.schema_compile_probe import schemas
from tests.test_structured_output import _status_error

ANTHROPIC = "https://api.anthropic.com/v1"
LIVE_400 = (
    "Schemas contains too many parameters with union types (17 parameters with type arrays or "
    "anyOf). This causes exponential compilation cost. Reduce the number of nullable or "
    "union-typed parameters (limit: 16 parameters with unions)."
)


def test_union_parameters__counts_type_lists_and_any_of() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": ["string", "null"]},
            "b": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "c": {"type": "string"},
            "d": {"type": "array", "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]}},
        },
    }
    assert union_parameters(schema) == 3


def test_rewrite_parse_result__over_the_limit_so_no_wire_form() -> None:
    lifted = provider_schema._strip(RewriteParseResult.model_json_schema(), _defs())
    assert union_parameters(lifted) == 17 > UNION_LIMIT == 16
    assert strict_wire_schema(RewriteParseResult.model_json_schema()) is None
    assert not json_schema_mode(ANTHROPIC, RewriteParseResult)
    # Other endpoints are unaffected: they get the Pydantic schema with no strict key.
    assert json_schema_mode("https://openrouter.ai/api/v1", RewriteParseResult)


def test_every_other_strict_schema__within_the_limit() -> None:
    """The probe's list is every schema a structured call may send strictly."""
    found = dict(schemas())
    assert "RewriteParseResult" not in found
    assert found  # the rest still go strictly
    assert all(union_parameters(wire) <= UNION_LIMIT for wire in found.values())


def test_union_400__maps_to_schema_too_large() -> None:
    assert rejects_grammar_size(LIVE_400)
    exc = _status_error(openai.BadRequestError, 400, LIVE_400)
    assert isinstance(map_provider_error("rewrite_parse", exc), SchemaTooLarge)


def test_unrelated_400__not_too_large() -> None:
    assert not rejects_grammar_size("max_tokens: field required")


def _defs() -> dict[str, dict[str, object]]:
    schema = RewriteParseResult.model_json_schema()
    return {**schema.get("$defs", {}), **schema.get("definitions", {})}
