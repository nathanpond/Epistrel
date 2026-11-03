"""Game-owned values an author cannot override (FR-EXT-8, FR-EXT-10; #242).

A game's provider is the authority for the values it owns (a gold balance, an inventory it
keeps): an author's directive or amendment must not override such a value inside Epistrel, or
the two systems would disagree. ``classify`` names, for each State change of a directive or a
rewrite, whether its State is externally owned (``owner = external``, #204) and what to do:

- ``refuse``: the provider is read-only, or the story has no binding for it while its manifest
  needs one, or it registered no write for the value. A directive with any refused change is
  refused whole (422 ``external_value_not_overridable``, nothing written); a rewrite drops the
  change into its preview's ``refused_changes`` (a rewrite whose only changes are refused is
  422 and opens no session).
- ``route``: the provider accepts writes. A durable directive about now asks the game to make
  the change: an ``intended`` outbox row (#210) in the directive's slot operation instead of a
  ``state_changes`` row, dispatched, confirmed and listed like any effect; Epistrel's value
  changes only through the provider's confirmation or its next pinned read.
- A **past** change (a rewrite, an undo or a redo) to a game-owned value is always refused,
  even on a write-capable provider: a write changes only the present.

#697: a fact on a predicate whose registry ``owner = external`` (#131) is guarded too, and
always refused (``classify_facts``): a predicate-owned value has no outbox write shape.

Story-owned values are not guarded: they are the story's own, and retcon like any canon.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import providers as core_providers
from epistrel.core import states
from epistrel.core.enums import StateOwner
from epistrel.core.errors import ExternalValueNotOverridable

EXPLANATION = (
    "{value} is managed by {provider}; Epistrel can't change it here: change it in the game."
)
PAST = "a provider's past value cannot be rewritten"
CODE = "external_value_not_overridable"


@dataclass(frozen=True, slots=True)
class ExternalVerdict:
    """What to do with one State change: its State, the owning provider, the provider's name
    for the value, the scope key, ``refuse`` or ``route``, and why."""

    state: str
    provider: str
    value_name: str
    scope_key: str | None
    action: Literal["refuse", "route"]
    explanation: str

    def document(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "value_name": self.value_name,
            "scope_key": self.scope_key,
        }


async def verdict(
    conn: AsyncConnection, story_id: str, state: str, scope_key: str | None, *, past: bool
) -> ExternalVerdict | None:
    """The verdict on a change of ``state`` (``None``: story-owned, not guarded)."""
    definition = await states.definition(conn, story_id, state)
    if definition.owner is not StateOwner.EXTERNAL or definition.provider_id is None:
        return None
    provider_name = str(definition.provider_id)
    value_name = definition.external_value_name or definition.name
    provider = await core_providers.find_provider(conn, provider_name)
    writable = provider is not None and provider.write_capable and not past
    if writable and provider is not None and core_providers.binding_keys(provider.manifest):
        writable = await core_providers.find_binding(conn, story_id, provider_name) is not None
    explanation = PAST if past else EXPLANATION.format(value=value_name, provider=provider_name)
    return ExternalVerdict(
        state=definition.name,
        provider=provider_name,
        value_name=value_name,
        scope_key=scope_key,
        action="route" if writable else "refuse",
        explanation=explanation,
    )


async def classify(
    conn: AsyncConnection, story_id: str, changes: Sequence[Any], *, past: bool = False
) -> list[tuple[Any, ExternalVerdict]]:
    """The externally owned changes among ``changes`` (each with a ``state`` and a
    ``scope_key``), with their verdicts; before anything is written."""
    found: list[tuple[Any, ExternalVerdict]] = []
    for change in changes:
        name = getattr(change, "state", None)
        if not name:
            continue
        judged = await verdict(
            conn, story_id, str(name), getattr(change, "scope_key", None), past=past
        )
        if judged is not None:
            found.append((change, judged))
    return found


async def classify_facts(
    conn: AsyncConnection, story_id: str, propositions: Sequence[Any], *, past: bool = False
) -> list[tuple[Any, ExternalVerdict]]:
    """#697 (FR-EXT-8): the propositions among ``propositions`` whose predicate the registry
    gives an external owner (#131), each refused: a predicate-owned value has no outbox write
    shape (#204's writes name State values), so the author changes it in the game. The verdict
    names the predicate as the value."""
    from epistrel.core.propositions.registry import load

    registry = await load(conn, story_id)
    found: list[tuple[Any, ExternalVerdict]] = []
    for proposition in propositions:
        predicate = registry.get(str(proposition.predicate_id))
        if predicate is None or str(getattr(predicate.owner, "value", predicate.owner)) != (
            "external"
        ):
            continue
        provider = "the game"
        explanation = PAST if past else EXPLANATION.format(value=predicate.name, provider=provider)
        found.append(
            (
                proposition,
                ExternalVerdict(
                    state=predicate.name,
                    provider=provider,
                    value_name=predicate.name,
                    scope_key=str(proposition.subject_id),
                    action="refuse",
                    explanation=explanation,
                ),
            )
        )
    return found


def refusal(verdicts: Sequence[ExternalVerdict]) -> ExternalValueNotOverridable:
    """The 422 for refused changes (one explanation per change, the first one's as the
    message)."""
    return ExternalValueNotOverridable(
        verdicts[0].explanation,
        {
            "changes": [v.document() for v in verdicts],
            "explanation": verdicts[0].explanation,
        },
    )


__all__ = [
    "CODE",
    "EXPLANATION",
    "PAST",
    "ExternalValueNotOverridable",
    "ExternalVerdict",
    "classify",
    "classify_facts",
    "refusal",
    "verdict",
]
