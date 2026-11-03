"""``docs/OPERATIONS.md``'s story snapshots and restore section (#251, FR-ADMIN-4): its limits
table states the deployment settings' defaults and bounds as the code holds them."""

from __future__ import annotations

import re

from epistrel.core.settings_schema import RetentionSettings
from epistrel.tools.generated import REPO_ROOT

LIMITS = ("bundle_max_uncompressed_bytes", "bundle_max_entries", "bundle_max_entry_bytes")


def _section() -> str:
    text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    return text.split("## Story snapshots and restore", 1)[1].split("\n## ", 1)[0]


def test_operations_import_limits() -> None:
    rows = {
        m.group(1): (int(m.group(2)), int(m.group(3)), int(m.group(4)))
        for m in re.finditer(r"\| `(\w+)` \| (\d+) \| (\d+)\u2013(\d+) \|", _section())
    }
    assert set(rows) == set(LIMITS)
    for name in LIMITS:
        field = RetentionSettings.model_fields[name]
        bounds = {type(m).__name__: m for m in field.metadata}
        assert rows[name] == (field.default, bounds["Ge"].ge, bounds["Le"].le), name


def test_operations_import_gates_in_order() -> None:
    section = _section()
    order = [
        section.index(word) for word in ("erasure ledger", "guardrails", "parked", "profile is")
    ]
    assert order == sorted(order)
