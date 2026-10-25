"""Test-only switches for engine profiles (#106, #109, #113).

No two modules delivered in M3 depend on each other, so the dependency graph's delivered path
(``epistrel.core.profiles.validate.missing_dependencies``) has no real rule to fire. While a
test holds ``extra_rule(rule)``, validation reads ``rule`` as one more entry of
``schema.DEPENDENCIES``; leaving the block restores the declared graph.

No module delivered in M3 is data-producing either, so FR-PROF-7's ``upgrade_unavailable`` path
(``epistrel.core.profiles.impact``) has nothing real to report. While a test holds
``data_producing(*modules)``, the impact report reads those delivered modules as data-producing
(``schema.DATA_PRODUCING_MODULES``): switching one on, or raising its level, adds data a story's
history lacks. Leaving the block restores the schema's own tags.

Every setting delivered in M3 is prospective, so a configuration change's class per setting
(FR-CONC-14) has nothing real to tell apart. While a test holds ``interpretive(*paths)``, the
schema reads those delivered settings as interpretive (``schema.INTERPRETIVE_SETTINGS``):
``schema.setting_tags`` gives them the interpretive class, and every ``config_changes`` row that
takes its class from there records it. Leaving the block restores the schema's own tags.

``structured_m4()`` names the seeded profile whose version 1 is ``Structured (M4)`` (#129), the
fixture profile of every M4 integration test: a story pinned to its version 1 (``version: 1`` on
``PUT /stories/{id}/engine-profile``, ``engine_profile_version: 1`` on a scenario creation, or
``engine_profile_version_pin = 1``) runs under every module M4 delivers. ``structured()`` names
the same row at its latest version, ``Structured`` (#181: version 2, every M5 module on).
Migrations ``0047`` and ``0080`` seed it in every database at head, the test databases
included.

Nothing in ``Engine`` or ``create_app`` imports this module, and Core never does.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import DependencyRule
from epistrel.core.seeds.profiles import (
    STRUCTURED_M4_PROFILE_ID,
    STRUCTURED_M4_VERSION,
    STRUCTURED_PROFILE_ID,
    structured_m4_settings,
    structured_settings,
)

TEST_CLAUSE = "test"
"""The ``clause`` of a test-only rule: FR-PROF-4 states no such rule."""


@contextmanager
def data_producing(*modules: str) -> Iterator[frozenset[str]]:
    """Read ``modules`` as data-producing for the block (each must be a schema module)."""
    unknown = [name for name in modules if name not in schema.MODULE]
    if unknown:
        raise ValueError(f"not modules: {unknown}")
    declared = schema.DATA_PRODUCING_MODULES
    schema.DATA_PRODUCING_MODULES = declared | frozenset(modules)
    try:
        yield schema.DATA_PRODUCING_MODULES
    finally:
        schema.DATA_PRODUCING_MODULES = declared


@contextmanager
def interpretive(*paths: str) -> Iterator[frozenset[str]]:
    """Read ``paths`` as interpretive settings for the block (each must be a delivered
    setting)."""
    unknown = [path for path in paths if path not in schema.delivered_tags()]
    if unknown:
        raise ValueError(f"not delivered settings: {unknown}")
    declared = schema.INTERPRETIVE_SETTINGS
    schema.INTERPRETIVE_SETTINGS = declared | frozenset(paths)
    try:
        yield schema.INTERPRETIVE_SETTINGS
    finally:
        schema.INTERPRETIVE_SETTINGS = declared


@contextmanager
def extra_rule(rule: DependencyRule) -> Iterator[DependencyRule]:
    """Validate with ``rule`` added to the dependency graph for the block."""
    declared = schema.DEPENDENCIES
    schema.DEPENDENCIES = (*declared, rule)
    try:
        yield rule
    finally:
        schema.DEPENDENCIES = declared


def structured_m4() -> str:
    """The id of the seeded profile whose version ``STRUCTURED_M4_VERSION`` is ``Structured
    (M4)`` (its settings: ``structured_m4_settings()``); a story pins that version to run it."""
    return STRUCTURED_M4_PROFILE_ID


def structured() -> str:
    """The id of the seeded ``Structured`` profile, whose latest version a story follows (its
    settings: ``structured_settings()``)."""
    return STRUCTURED_PROFILE_ID


__all__ = [
    "STRUCTURED_M4_VERSION",
    "TEST_CLAUSE",
    "data_producing",
    "extra_rule",
    "interpretive",
    "structured",
    "structured_m4",
    "structured_m4_settings",
    "structured_settings",
]
