"""Configuration changes against a staged turn, by stage (FR-CONC-16, FR-EXT-16; docs/04
S-CONFIG-RACE; #214).

A configuration change (FR-CONC-14) that lands while a structured turn is in flight is
revalidated by what the turn has already committed, keyed on ``operations.turn_stage`` (#212).
No committed stage is ever regenerated or rolled back (``stage_of`` names the check that
applies):

- **Before intent** (``turn_stage IS NULL``, ``PRE_INTENT``): the turn has committed nothing,
  so it rebases in the #47/#64 sense: ``core.concurrency.run_with_revalidation`` re-checks it
  at its commit (``core.readset.REBASE_CHECKS``) and a failure regenerates it. This module adds
  one such check, ``recheck_routes``: an effect the attempt routed through the outbox (its
  provider write-capable) or asked of the consumer (read-only), or staged on a
  ``synchronous`` provider, whose provider's write capability or confirmation mode changed
  since (an interpretive provider change, ``config_dependencies``' ``provider_routes``),
  regenerates the turn, whose next attempt reads the registration as it now is: an operator
  who makes the provider read-only before intent gets the effect back as a consumer-applied
  state effect (#209's ``requested_effect``) instead of an outbox row.
- **After intent, before the claim** (``intent_committed``, ``CLAIM``): the committed attempt
  stands. #210's ``claim`` re-reads the registration and #188's authorization under the
  current configuration (``revalidate_claim``): a provider that is gone or no longer
  write-capable, or a declared effect #188 no longer admits (the row's effect document names
  the State and source it authorized at intent, ``effects.Requested.authorized``: that State's
  ``authorization.sources``, a transfer's counterparties, or the row's State removed or no
  longer owned by that provider), cancels the row, never sent, listed
  ``reconciliation_required``; the outcome step narrates the attempt as not carried out.
- **After the claim** (``awaiting_outcome``, ``SETTLEMENT``): the outcome is preserved
  whatever it is. #212's outcome transaction settles the turn's conditional effects and
  reservations (#216's ``contingency.settle``) under the current configuration: its State
  declarations as they are now (a changed schema validates the grant under the new one, its
  bounds included), and ``revalidate_settlement``: a grant #188 no longer admits (its
  declared State's sources) is not committed (``conflict``), stays ``pending`` or ``held``,
  and its prerequisite effect is listed ``settlement_conflict`` (#210's reason); the outcome
  step narrates the payment as done (``staged.all_confirmed``: a sentence naming no State then
  speaks of the exchange) and the grant as held pending (``staged.lines``).
- **After the outcome transaction** (``outcome_recorded`` or later, ``OUTCOME_STEP``): its
  settled effects are durable (a grant is retained; removing it is a separate explicit
  operation); only the outcome step's own render is checked, under the configuration current
  when it runs.

These post-intent checks are not ``REBASE_CHECKS`` entries: those answer pass or regenerate
for a turn still generating, and a committed stage is never regenerated.

Codes and ids only; this module logs nothing of a turn's content (invariant 3).
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import providers as core_providers
from epistrel.core import states
from epistrel.core.enums import (
    Provenance,
    StagedTurnStage,
    StateChangeKind,
    StateChangeSource,
    StateOwner,
)
from epistrel.core.events.operation import Operation
from epistrel.core.readset import StructuredReadSet, register_rebase_check
from epistrel.core.states.authorize import Source, source_permitted, transfer_of
from epistrel.core.states.model import StateDefinition
from epistrel.core.tables import events
from epistrel.core.tables.outbox import external_effect_outbox
from epistrel.core.turns.beats import TurnBeat

log = logging.getLogger(__name__)

PRE_INTENT: Final = "pre_intent"
CLAIM: Final = "claim"
SETTLEMENT: Final = "settlement"
OUTCOME_STEP: Final = "outcome_step"

NOT_WRITE_CAPABLE: Final = "not_write_capable"
"""``revalidate_claim``: the provider is gone or no longer write-capable (FR-CONC-16)."""
UNPERMITTED: Final = "unpermitted"
"""``revalidate_claim``: #188 no longer admits the declared effect (its State's sources, a
transfer's counterparties), or its State no longer exists as that provider's."""


def stage_of(turn_stage: StagedTurnStage | str | None) -> str:
    """Which revalidation a configuration change gets in a turn at ``turn_stage`` (see the
    module docstring)."""
    if turn_stage is None:
        return PRE_INTENT
    stage = StagedTurnStage(turn_stage)
    if stage is StagedTurnStage.INTENT_COMMITTED:
        return CLAIM
    if stage is StagedTurnStage.AWAITING_OUTCOME:
        return SETTLEMENT
    return OUTCOME_STEP


# -- who asserted a committed effect -------------------------------------------------------------


async def asserter_at(conn: AsyncConnection, operation_id: str, beat: int) -> Source | None:
    """Who asserted the effects of ``operation_id``'s beat ``beat`` (#188's ``Source``): the
    character whose beat it is, else the director (a narrator's beat); ``None`` when the
    operation has no beat there (an effect no render declared)."""
    e = events.c
    row = (
        await conn.execute(
            select(e.character_id, e.provenance)
            .where(e.operation_id == operation_id, e.beat == beat, e.type.like("beat\\_%"))
            .order_by(e.seq)
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    provenance = str(getattr(row.provenance, "value", row.provenance))
    if row.character_id is None or provenance == Provenance.SYSTEM.value:
        return Source("director")
    return Source("character", str(row.character_id))


async def _definition(conn: AsyncConnection, story_id: str, name: object) -> StateDefinition | None:
    if not isinstance(name, str):
        return None
    for found in await states.definitions(conn, story_id):
        if found.name == name:
            return found
    return None


def _authorized(effect: Mapping[str, Any]) -> tuple[str, str] | None:
    """The ``(State, source)`` #188 authorized at intent for an outbox row's effect document
    (``effects.outbox_rows``), else ``None`` (a row no render declared)."""
    found = effect.get("authorized")
    if not isinstance(found, Mapping):
        return None
    state, source = found.get("state"), found.get("source")
    if not isinstance(state, str) or not isinstance(source, str):
        return None
    return state, source


async def _permits(
    conn: AsyncConnection, story_id: str, side: StateDefinition, authorized: tuple[str, str]
) -> bool:
    """Whether the declared effect ``authorized`` (its State and source), of which ``side`` is
    a side, is still admitted under the current declarations (#188's source and counterparty
    rules)."""
    name, source = authorized
    declared = side if name == side.name else await _definition(conn, story_id, name)
    if declared is None or not source_permitted(declared, source):
        return False
    return declared is side or side.name in transfer_of(declared).counterparties


# -- after intent: the claim ---------------------------------------------------------------------


async def revalidate_claim(conn: AsyncConnection, row: Any) -> str | None:
    """#210's ``claim`` of outbox row ``row`` (locked), under the current configuration
    (FR-CONC-16): ``NOT_WRITE_CAPABLE`` when its provider is gone or no longer write-capable;
    for a row a render declared (its effect document names what #188 authorized at intent),
    ``UNPERMITTED`` when its State is gone or no longer that provider's, or the declared effect
    is no longer admitted (its State's ``authorization.sources``, or a transfer's
    counterparties); ``None`` when it may be sent."""
    registered = await core_providers.find_provider(conn, str(row.provider_name))
    if registered is None or not registered.write_capable:
        return NOT_WRITE_CAPABLE
    effect = dict(row.effect or {})
    authorized = _authorized(effect)
    if authorized is None:
        return None
    story_id = str(row.story_id)
    own = await _definition(conn, story_id, effect.get("state"))
    if (
        own is None
        or own.owner is not StateOwner.EXTERNAL
        or own.provider_id != str(row.provider_name)
        or not await _permits(conn, story_id, own, authorized)
    ):
        return UNPERMITTED
    return None


# -- after the claim: the outcome transaction's settlement ---------------------------------------

TRANSFER_KINDS: Final = frozenset(
    {StateChangeKind.TRANSFER_IN.value, StateChangeKind.TRANSFER_OUT.value}
)


async def revalidate_settlement(
    conn: AsyncConnection, row: Any, definition: StateDefinition
) -> bool:
    """Whether intent row ``row`` (a ``held`` or ``pending`` ``state_changes`` row whose
    prerequisites confirmed) may be settled under ``definition`` as it is now (#216's
    ``contingency.settle`` asks it): a render's effect that #188 no longer admits (the declared
    State's ``authorization.sources``, or a transfer's counterparties: a transfer's story side
    is checked as the declared effect its prerequisite outbox row names) may not, so it is not
    committed (``conflict``). Its bounds and schema are ``apply_change``'s to check."""
    source = str(getattr(row.source, "value", row.source))
    if source != StateChangeSource.TURN.value:
        return True
    story_id = str(row.story_id)
    kind = str(getattr(row.kind, "value", row.kind))
    if kind in TRANSFER_KINDS:
        o = external_effect_outbox.c
        prerequisites = [str(p) for p in row.prerequisite_effect_ids]
        found = [
            a
            for a in (
                _authorized(dict(r.effect or {}))
                for r in await conn.execute(select(o.effect).where(o.id.in_(prerequisites)))
            )
            if a is not None
        ]
        mine = [a for a in found if a[0] == definition.name] or found
        if mine:
            return await _permits(conn, story_id, definition, mine[0])
    asserter = await asserter_at(conn, str(row.operation_id), int(row.beat))
    return asserter is None or source_permitted(definition, asserter.name)


# -- before intent: the provider routes ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Route:
    """How an attempt routed the effects on one external State: its provider, whether it was
    write-capable (dispatched through the outbox) and whether it confirms ``synchronous``."""

    provider: str
    write_capable: bool
    synchronous: bool


_ROUTES: dict[int, tuple[weakref.ref[StructuredReadSet], int, str, dict[str, Route]]] = {}


def remember(read_set: StructuredReadSet, book: Any, beats: Sequence[TurnBeat]) -> None:
    """Record how the attempt routed its effects on external States (``book``: the attempt's
    ``effects.Book``), for ``recheck_routes``."""
    from epistrel.orchestrator.structured import effects as state_effects

    touched = {r.state for r in state_effects.requested_of(book, beats)}
    found: dict[str, Route] = {}
    for name in sorted(touched):
        definition = book.definitions.get(name)
        if definition is None or definition.provider_id is None:
            continue
        found[name] = Route(
            str(definition.provider_id), name in book.outbox, name in book.synchronous
        )
    key = id(read_set)
    _ROUTES[key] = (
        weakref.ref(read_set, lambda _: _ROUTES.pop(key, None)),
        read_set.attempt,
        str(book.story_id),
        found,
    )


def routes_of(read_set: StructuredReadSet) -> Mapping[str, Route] | None:
    entry = _ROUTES.get(id(read_set))
    if entry is None or entry[0]() is not read_set or entry[1] != read_set.attempt:
        return None
    return entry[3]


async def recheck_routes(conn: AsyncConnection, op: Operation) -> bool:
    """The pre-intent rebase re-check of provider routing (``REBASE_CHECKS``; see the module
    docstring): ``False`` (regenerate) when an external State the attempt's effects touch is
    now routed otherwise (its provider, write capability or confirmation mode changed)."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet):
        return True
    entry = _ROUTES.get(id(source))
    if entry is None or entry[0]() is not source or entry[1] != source.attempt or not entry[3]:
        return True
    now = {
        name: Route(provider, writable, confirmation == core_providers.SYNCHRONOUS)
        for name, provider, writable, confirmation in await core_providers.routes(conn, entry[2])
    }
    for name, route in entry[3].items():
        if now.get(name) != route:
            log.debug("rebase re-check: an external State's provider route changed")
            return False
    return True


register_rebase_check(recheck_routes)


__all__ = [
    "CLAIM",
    "NOT_WRITE_CAPABLE",
    "OUTCOME_STEP",
    "PRE_INTENT",
    "SETTLEMENT",
    "TRANSFER_KINDS",
    "UNPERMITTED",
    "Route",
    "asserter_at",
    "recheck_routes",
    "remember",
    "revalidate_claim",
    "revalidate_settlement",
    "routes_of",
    "stage_of",
]
