"""Operational telemetry: structured logs, Prometheus metrics and OpenTelemetry traces that
carry identifiers and counts, never story content (FR-OBS-1, NFR-SEC-4, invariant 3; #74).

``content`` holds the one content rule; ``logging``, ``metrics`` and ``tracing`` the three
sinks; ``instrument`` the wrappers that give every ``Engine`` call, job and request its line,
span and count; ``context`` the request-scoped ``operation_id``. ``configure_process`` is what
``epistrel serve`` runs once per process. Submodules are imported on use: ``epistrel.core``
imports ``context`` and ``tracing`` only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from epistrel.config import Settings


def configure_process(settings: Settings) -> None:
    """Install the stdout JSON log handler (content debug logging as configured) and, when
    ``EPISTREL_OTLP_ENDPOINT`` is set, the OTLP trace exporter."""
    from importlib.metadata import PackageNotFoundError, version

    from epistrel.observability.logging import configure_logging
    from epistrel.observability.tracing import configure_tracing

    configure_logging(
        level="DEBUG" if settings.debug else settings.log_level,
        content_debug=settings.content_debug_logging,
        debug=settings.debug,
    )
    try:
        release = version("epistrel")
    except PackageNotFoundError:  # pragma: no cover - a source tree that was never installed
        release = "unknown"
    configure_tracing(settings.otlp_endpoint, version=release, debug=settings.debug)


__all__ = ["configure_process"]
