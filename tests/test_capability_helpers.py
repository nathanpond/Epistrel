"""The capability map's module gating helpers and its entries (#129; FR-PROF-12,13).

``module_on`` and ``level_at_least`` are what every module's code path asks of a story's
effective configuration before it runs; the per-module complements (a module off writes
nothing and calls nothing) live with the modules themselves (#150, #143, #148, #142, #146).
The C-16 (M4) parts are in ``tests/integration/test_capability_map.py``.
"""

from __future__ import annotations

import pytest

from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import (
    CAPABILITY,
    CAPABILITY_MAP,
    Needs,
    capability_name,
    capability_names,
    holding,
    level_at_least,
    module_names,
    module_on,
)
from epistrel.core.profiles.validate import parse_settings, validate_profile
from epistrel.core.seeds.profiles import lite_settings, structured_m4_settings
from tests.profile_support import naming, undelivered_modules

LITE = parse_settings(lite_settings())
STRUCTURED = parse_settings(structured_m4_settings())

SLUGS = (
    "structured_pipeline",
    "partitions",
    "partitions_nested",
    "partitions_lazy",
    "partitions_testimony",
    "perception_co_location",
    "perception_full",
    "verification",
    "verification_extraction",
    "verification_entailment",
    "commitment",
    "clock",
    "states_external",
    "per_character_renders",
    "consolidation",
    "required_field_repair",
    "floor",
)


def test_module_on() -> None:
    """A module is on when its switch picks a delivered level; off otherwise. The pipeline is
    always on, at one of its levels. A module the deployment has not delivered is off even when
    a document switches it on. An unknown module is an error, never an off module."""
    assert module_on(LITE, "pipeline")
    assert module_on(STRUCTURED, "pipeline")
    for name in ("episodic", "consolidation", "verification", "contradiction_checking"):
        assert module_on(STRUCTURED, name), name
        assert not module_on(LITE, name), name
    assert module_on(STRUCTURED, "commitment")
    assert not module_on(LITE, "commitment")
    assert module_on(STRUCTURED, "repair")
    assert not module_on(LITE, "repair")
    assert module_on(LITE, "rolling_summary")
    assert not module_on(STRUCTURED, "rolling_summary")
    for module in undelivered_modules():
        switched = parse_settings(naming(module))
        assert schema.level_of(switched, module.name) is not None, module.name
        assert not module_on(switched, module.name), module.name
    with pytest.raises(ValueError, match="not a profile module"):
        module_on(LITE, "embeddings")


def test_level_at_least() -> None:
    """A level and every level above it, in the order the schema declares them: ``lite`` below
    ``structured``; ``declared`` below ``extraction`` below ``entailment``. Off is below every
    level. An unknown level is an error; a level not delivered is never reached."""
    assert level_at_least(LITE, "pipeline", "lite")
    assert not level_at_least(LITE, "pipeline", "structured")
    assert level_at_least(STRUCTURED, "pipeline", "structured")
    assert level_at_least(STRUCTURED, "pipeline", "lite")
    ladder = ("declared", "extraction", "entailment")
    for index, level in enumerate(ladder):
        settings = parse_settings({"pipeline": "structured", "verification": {"level": level}})
        for rank, asked in enumerate(ladder):
            assert level_at_least(settings, "verification", asked) is (index >= rank)
        assert not level_at_least(LITE, "verification", level)
    with pytest.raises(ValueError, match="has no level"):
        level_at_least(STRUCTURED, "verification", "always")
    with pytest.raises(ValueError, match="not a profile module"):
        level_at_least(STRUCTURED, "nonesuch", "on")
    full = parse_settings({"perception": {"level": "full"}})
    assert level_at_least(full, "perception", "co_location")  # delivered (#181)
    clocked = parse_settings({"clock": {"level": "on"}})
    assert not level_at_least(clocked, "clock", "on")  # not delivered (M6)


def test_capability_names() -> None:
    """Reported capabilities: a module simply on by its name, a module on a ladder as
    ``<module>:<level>``, and the pipeline by its level; the bare names drop the level."""
    assert capability_name("pipeline", "structured") == "structured"
    assert capability_name("commitment", schema.ON) == "commitment"
    assert capability_name("verification", "entailment") == "verification:entailment"
    assert capability_names(LITE) == (
        "budgets",
        "lite",
        "models",
        "pins",
        "polish",
        "rolling_summary",
    )
    assert capability_names(STRUCTURED) == (
        "budgets",
        "commitment",
        "consolidation",
        "contradiction_checking",
        "episodic",
        "models",
        "pins",
        "polish",
        "repair",
        "structured",
        "verification:entailment",
    )
    assert "verification" in module_names(STRUCTURED)
    assert not any(":" in name for name in module_names(STRUCTURED))


def test_the_map_entries() -> None:
    """One entry per bullet and sub-clause of the requirement, in its order, each with a slug;
    one floor, requiring nothing; every module and level an entry needs is a schema module and
    level. Under Lite only the floor and the required-field repair clause hold; under the
    seeded structured profile the structured pipeline, verification at every level, commitment
    and consolidation hold too, and nothing that needs an undelivered module."""
    assert tuple(entry.slug for entry in CAPABILITY_MAP) == SLUGS
    assert [entry.slug for entry in CAPABILITY_MAP if entry.floor] == ["floor"]
    assert CAPABILITY["floor"].requires == ()
    assert CAPABILITY["floor"].requirements == ()
    for entry in CAPABILITY_MAP:
        for need in entry.requires:
            module = schema.MODULE[need.module]
            assert need.level is None or need.level in module.level_names, entry.slug
    assert holding(LITE) == ("required_field_repair", "floor")
    assert holding(STRUCTURED) == (
        "structured_pipeline",
        "verification",
        "verification_extraction",
        "verification_entailment",
        "commitment",
        "consolidation",
        "required_field_repair",
        "floor",
    )
    assert {e.slug for e in CAPABILITY_MAP if not e.delivered} == {"clock", "states_external"}
    alternatives = Needs("testimony", "model", only=True)
    assert alternatives.describe() == "`testimony` at `model`"
    assert Needs("verification", "declared").describe() == "`verification` at `declared` or above"


def test_the_seed_runs_every_delivered_module() -> None:
    """The seeded structured profile validates, and every module M4 delivers is on under it but
    the rolling summary, which ``structured`` allows and the seed leaves off."""
    assert validate_profile(structured_m4_settings()).valid
    m4 = {"episodic", "consolidation", "verification", "contradiction_checking", "commitment"}
    assert {name for name in m4 if module_on(STRUCTURED, name)} == m4
    assert level_at_least(STRUCTURED, "verification", "entailment")
    assert STRUCTURED.verification.extraction_mode == "always"
    assert STRUCTURED.verification.entailment_gate == 1.0
    assert STRUCTURED.repair.max_attempts == 1
    assert STRUCTURED.memory.episodic.k == 40
    assert STRUCTURED.commitment.salience == "rules_and_model"
    assert STRUCTURED.budgets.model_dump() == {"model_calls": None, "deadline_ms": 45_000}
    assert STRUCTURED.models.purposes == {}
    assert STRUCTURED.models.generation == LITE.models.generation


def test_reranking_is_not_delivered() -> None:
    """#141: retrieval has no reranker in M4. A profile setting ``memory.episodic.reranking``
    to ``true`` is invalid, naming it as undelivered; ``false`` (the seeds' value) and the new
    ranking dials are valid."""
    document = structured_m4_settings()
    document["memory"]["episodic"].update(
        weight_similarity=1.0, weight_recency=0.0, recency_half_life_turns=50
    )
    assert validate_profile(document).valid
    document["memory"]["episodic"]["reranking"] = True
    refused = validate_profile(document)
    assert not refused.valid
    assert [u.setting for u in refused.undelivered] == ["memory.episodic.reranking"]
    assert refused.undelivered[0].module == "episodic"
