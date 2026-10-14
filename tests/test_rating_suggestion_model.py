"""The ``rating_suggestion`` purpose's registry and configuration rules (FR-MODEL-2; #121),
database-free: its model resolves through ``guardrail_check``'s row, it is no story purpose, and
a profile cannot route it. The C-21 parts are in ``tests/conformance/test_c_21.py``."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from epistrel.core.errors import InvalidRequest
from epistrel.core.profiles.validate import validate_profile
from epistrel.core.seeds.profiles import lite_settings
from epistrel.engine import Engine
from epistrel.orchestrator.models import registry
from epistrel.orchestrator.models.config import ConfigRow, KeyBox, describe, parent_of
from epistrel.orchestrator.models.facts import TEMPLATE_HASHES, identities
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    STORY_PURPOSES,
    RegistryError,
)
from tests.auth_support import auth_settings


def test_the_inheriting_purpose_resolves_through_guardrail_check() -> None:
    """#121: ``rating_suggestion`` defaults to the guardrail-check model. Per field
    and per param key: its own row, else ``guardrail_check``'s (source ``inherited``), else the
    ``default`` row's. Other purposes skip the middle step."""

    now = datetime.now(UTC)
    rows = {
        "default": ConfigRow("default", "http://d", "base", None, {"temperature": 0.1}, now),
        "guardrail_check": ConfigRow(
            "guardrail_check", None, "judge", None, {"max_tokens": 64}, now
        ),
        "rating_suggestion": ConfigRow("rating_suggestion", None, None, None, {"top_p": 0.5}, now),
    }
    assert parent_of("rating_suggestion") == "guardrail_check"
    assert parent_of("generation") is None
    entry = describe("rating_suggestion", rows, KeyBox(None))
    assert (entry.base_url, entry.model) == ("http://d", "judge")
    assert entry.params == {"temperature": 0.1, "max_tokens": 64, "top_p": 0.5}
    assert entry.sources == {
        "base_url": "default",
        "model": "inherited",
        "api_key": "default",
        "params": {"temperature": "default", "max_tokens": "inherited", "top_p": "own"},
    }
    generation = describe("generation", rows, KeyBox(None))
    assert (generation.model, generation.sources["model"]) == ("base", "default")
    del rows["guardrail_check"]
    assert describe("rating_suggestion", rows, KeyBox(None)).model == "base"


def test_a_purpose_defaulting_to_an_undelivered_row_fails_the_registry() -> None:
    broken = dataclasses.replace(registry.RATING_SUGGESTION, name="x", default_model_key="nope")
    with pytest.raises(RegistryError, match="defaults to 'nope'"):
        registry._build((broken,))


def test_it_is_no_story_purpose_and_no_profile_routes_it(dummy_dsn: str) -> None:
    """It runs outside any story: the story purposes, a story's runtime facts and template
    hashes leave it out, and a profile naming it is refused at its field."""
    assert set(DELIVERED) - set(STORY_PURPOSES) == {"rating_suggestion"}
    assert "rating_suggestion" not in identities({})
    assert "rating_suggestion" not in TEMPLATE_HASHES
    settings = lite_settings()
    settings["models"]["purposes"]["rating_suggestion"] = {"model": None}
    purposes = Engine(auth_settings(dummy_dsn, model_adapter="stub")).purposes
    assert purposes.deployment == {"rating_suggestion"}
    assert "rating_suggestion" not in purposes.delivered | purposes.reserved
    with pytest.raises(InvalidRequest) as refused:
        validate_profile(settings, purposes=purposes)
    assert refused.value.details["field"] == "body.settings.models.purposes.rating_suggestion"
    assert "outside any story" in refused.value.message
