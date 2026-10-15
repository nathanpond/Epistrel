"""Trimmed request strings are length-checked as sent (#366; FR-API-1, FR-API-4).

C-01's negative-data check found that ``" " + 80 * "*"`` passed an 80-character name: the
published ``maxLength`` held the raw string, the validator the stripped one. It also sent
``"\x1f"`` to promote: pydantic's strip kept it, the catalog's ``str.strip`` blank check did not,
and the request was a 500. Every trimmed type
and every field of a stripping model now refuses a raw string over its ``maxLength``, keeps
accepting one at it, and still strips. A source check keeps a new trimmed type from missing it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from epistrel.core import feedback, ratings, stories
from epistrel.core.catalog import models as catalog_models
from epistrel.core.catalog import personas
from epistrel.core.policies import model as policy_model
from epistrel.core.profiles import store as profiles
from epistrel.core.ratings import change as rating_change
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.policies import schemas as policy_schemas

SRC = Path(__file__).resolve().parents[1] / "src" / "epistrel"

TYPES: dict[str, Any] = {
    "catalog.Name": catalog_models.Name,
    "personas.PersonaName": personas.PersonaName,
    "personas.Gender": personas.Gender,
    "personas.Description": personas.Description,
    "stories.CharacterName": stories.CharacterName,
    "profiles.Name": profiles.Name,
    "ratings.Name": ratings.Name,
    "ratings.Reference": ratings.Reference,
    "ratings.change.Reference": rating_change.Reference,
    "feedback.RatingTag": feedback.RatingTag,
    "policies.Word": policy_model.Word,
    "policy_schemas.Name": policy_schemas.Name,
    "policy_schemas.Message": policy_schemas.Message,
}


def _max_length(adapter: TypeAdapter[Any]) -> int:
    found = adapter.json_schema()["maxLength"]
    assert isinstance(found, int)
    return found


@pytest.mark.parametrize("name", sorted(TYPES))
def test_a_trimmed_type_checks_the_length_as_sent(name: str) -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(TYPES[name])
    limit = _max_length(adapter)
    with pytest.raises(ValidationError) as caught:
        adapter.validate_python(" " + "x" * limit)
    assert caught.value.errors()[0]["type"] == "string_too_long"
    assert adapter.validate_python("x" * limit) == "x" * limit
    assert adapter.validate_python(" xy ") == "xy"
    # Blank as the server judges blank (``str.strip``): C-01 sent "\x1f" to promote, a 500.
    with pytest.raises(ValidationError) as caught:
        adapter.validate_python("\x1f")
    assert caught.value.errors()[0]["type"] == "string_too_short"


@pytest.mark.parametrize(
    ("model", "field", "valid"),
    [
        (model_config.ConfigReplacement, "model", "m"),
        (model_config.ConfigReplacement, "base_url", "http://localhost:11434/v1"),
        (model_config.ApiKeyBody, "api_key", "k"),
    ],
)
def test_a_stripping_model_checks_the_length_as_sent(
    model: type[BaseModel], field: str, valid: str
) -> None:
    limit = model.model_json_schema()["properties"][field]
    limit = limit.get("maxLength") or next(
        a["maxLength"] for a in limit["anyOf"] if "maxLength" in a
    )
    with pytest.raises(ValidationError) as caught:
        model.model_validate({field: valid + " " * (limit - len(valid) + 1)})
    assert caught.value.errors()[0]["type"] == "string_too_long"
    model.model_validate({field: valid + " " * (limit - len(valid))})


def test_every_trimmed_type_carries_the_raw_check() -> None:
    """Each ``StringConstraints(strip_whitespace=True, ...)`` in the source is followed, in its
    ``Annotated``, by ``BeforeValidator(trim_as_sent(...))``; each model stripping with
    ``str_strip_whitespace`` is one this file tests."""
    missing = []
    stripping = []
    for path in sorted(SRC.rglob("*.py")):
        if path == SRC / "core" / "trimmed.py":  # its docstring shows the old pattern
            continue
        source = path.read_text()
        for match in re.finditer(r"StringConstraints\((?:[^()]|\([^()]*\))*\)", source):
            if "strip_whitespace=True" not in match.group(0):
                continue
            after = source[match.end() : match.end() + 120]
            if not re.match(r"\s*,\s*BeforeValidator\(trim_as_sent\(", after):
                missing.append(
                    f"{path.relative_to(SRC)}:{source[: match.start()].count(chr(10)) + 1}"
                )
        if "str_strip_whitespace=True" in source:
            stripping.append(str(path.relative_to(SRC)))
    assert missing == []
    assert stripping == ["orchestrator/models/config.py"]
