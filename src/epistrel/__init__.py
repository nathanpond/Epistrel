"""Epistrel: a viewpoint-aware memory engine for character/actor GenAI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("epistrel")
except PackageNotFoundError:  # pragma: no cover - only outside an installed environment
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
