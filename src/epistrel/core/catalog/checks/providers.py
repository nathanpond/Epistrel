"""The provider check (FR-SCN-2,5; #111): every external provider the scenario's content names is
declared in ``story_settings_defaults.required_providers``, the list a game must register.

The check is by name only: the starting values a scenario tracks (``starting_state.states``)
and the items it schedules (``starting_state.scheduled_events``) name a provider by a
``provider`` or ``provider_id`` key holding a string, at any depth of their stored form (#202
types them: an externally owned value's ``provider_id``). Each name not declared is a
``provider_undeclared`` error at that key, quoting the name. When nothing names a provider, the
check finds nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.report import Outcome

PROVIDER_KEY = "provider"
PROVIDER_KEYS = frozenset({PROVIDER_KEY, "provider_id"})
RULE = "every provider the scenario names is declared in story_settings_defaults.required_providers"


def provider_names(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """``(field, name)`` for every ``provider`` (or ``provider_id``) key holding a string
    inside ``value`` (an object as stored, at ``path``), in document order."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            at = f"{path}.{key}"
            if key in PROVIDER_KEYS and isinstance(item, str):
                yield at, item
            else:
                yield from provider_names(item, at)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from provider_names(item, f"{path}[{index}]")


class ProvidersCheck(Check):
    """See the module docstring."""

    name = "providers"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        draft = draft_of(subject)
        declared = set(draft.story_settings_defaults.required_providers)
        start = draft.starting_state
        named = [
            *provider_names(start.stored("states"), "starting_state.states"),
            *provider_names(start.stored("scheduled_events"), "starting_state.scheduled_events"),
        ]
        return Findings(
            entries=[
                subject.entry(
                    "provider_undeclared", Outcome.rejected, field=field, text=name, rule=RULE
                )
                for field, name in named
                if name not in declared
            ]
        )


CHECK = ProvidersCheck()

__all__ = ["CHECK", "PROVIDER_KEY", "PROVIDER_KEYS", "ProvidersCheck", "provider_names"]
