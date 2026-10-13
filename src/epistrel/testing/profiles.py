"""A test-only dependency rule for engine profiles (#106).

No two modules delivered in M3 depend on each other, so the dependency graph's delivered path
(``epistrel.core.profiles.validate.missing_dependencies``) has no real rule to fire. While a
test holds ``extra_rule(rule)``, validation reads ``rule`` as one more entry of
``schema.DEPENDENCIES``; leaving the block restores the declared graph. Nothing in ``Engine`` or
``create_app`` imports this module, and Core never does.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import DependencyRule

TEST_CLAUSE = "test"
"""The ``clause`` of a test-only rule: FR-PROF-4 states no such rule."""


@contextmanager
def extra_rule(rule: DependencyRule) -> Iterator[DependencyRule]:
    """Validate with ``rule`` added to the dependency graph for the block."""
    declared = schema.DEPENDENCIES
    schema.DEPENDENCIES = (*declared, rule)
    try:
        yield rule
    finally:
        schema.DEPENDENCIES = declared


__all__ = ["TEST_CLAUSE", "extra_rule"]
