"""Epistrel: a viewpoint-aware memory engine for character/actor GenAI."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

try:
    __version__ = version("epistrel")
except PackageNotFoundError:  # pragma: no cover - only outside an installed environment
    __version__ = "0.0.0+unknown"

if TYPE_CHECKING:
    from epistrel.engine import Engine

__all__ = ["Engine", "__version__"]


def __getattr__(name: str) -> Any:
    """``epistrel.Engine`` is imported on first use, so ``import epistrel`` (and importing any
    subpackage) does not pull in the database stack. Coverage's start-up resolves
    ``epistrel.core`` by importing this package, and SQLAlchemy's compiled cache-key module
    breaks when it is first imported at that moment."""
    if name == "Engine":
        from epistrel.engine import Engine

        return Engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
