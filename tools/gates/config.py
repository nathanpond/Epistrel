"""`[tool.epistrel.gates]` in pyproject.toml: the settings every gate check reads."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KNOWN_KEYS = frozenset({"current_milestone"})
SUBTABLES_PASSED_THROUGH = frozenset({"deny", "licenses", "lexicon", "traceability"})


class ConfigError(Exception):
    """The gates section is missing or malformed (a structural problem, exit 2)."""


@dataclass(frozen=True)
class GatesConfig:
    current_milestone: int
    """Highest *verified* milestone. Checks that say "at or before the current milestone" use it."""

    subtables: dict[str, Any] = field(default_factory=dict)
    """Per-check sub-tables (`deny`, `licenses`, ...) handed to their owners unvalidated."""


def load_config(pyproject: Path) -> GatesConfig:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {pyproject}: {exc}") from exc
    section = data.get("tool", {}).get("epistrel", {}).get("gates")
    if not isinstance(section, dict):
        raise ConfigError(f"{pyproject}: missing [tool.epistrel.gates]")
    unknown = {
        k
        for k, v in section.items()
        if k not in KNOWN_KEYS and not (isinstance(v, dict) and k in SUBTABLES_PASSED_THROUGH)
    }
    if unknown:
        raise ConfigError(f"{pyproject}: unknown [tool.epistrel.gates] keys: {sorted(unknown)}")
    if "current_milestone" not in section:
        raise ConfigError(f"{pyproject}: [tool.epistrel.gates] lacks current_milestone")
    current = section["current_milestone"]
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise ConfigError(
            f"{pyproject}: current_milestone must be an integer >= 0, got {current!r}"
        )
    subtables = {k: v for k, v in section.items() if isinstance(v, dict)}
    return GatesConfig(current_milestone=current, subtables=subtables)
