"""Several primaries in one structured turn: ``sequential`` mode (FR-MULTI-1,2,5,6,11, FR-EPI-14
(b); docs/02 §4.4; #174).

**Who renders.** Under ``multi_character.renders = per_character`` a request may name up to
``MAX_PRIMARIES`` characters in ``render_character_ids`` when the turn's interaction mode is
``sequential``: the request's ``interaction_mode``, else the story's setting, else the scenario's
setting genesis kept inactive (``stories.settings.inactive``, capability ``multi_character``,
#122), else ``sequential`` (``mode_of``). Omitted, the turn keeps #150's single first-by-ordinal
primary (the roster never widens a default). Under ``combined`` (every M4 profile) two or more
named are still 422 ``validation_error`` (``one_render_character``), and a request that
overrides ``interaction_mode`` or ``sequence_order`` is 409 ``module_off``. The player's own
character is never a primary of a multi-primary turn (422 ``player_cannot_render``);
``simultaneous`` with several primaries is #175's (422 ``simultaneous_not_delivered`` until
then).

**Order** (FR-MULTI-6, ``order_of``): request order, or under ``sequence_order = director`` the
narrator's one ordering call (``director_order``: ``generation`` with ``director_order.j2``, its
answer ``{order: [ids]}``), optional work after salience and before entailment (#156's order,
``budget.DIRECTOR_ORDER``) and never for one primary. Its answer is validated: every id a
primary, none twice; a subset is completed with the missing primaries in request order; an
unknown or repeated id, an unaffordable call or a failed model falls back to request order with
``director_order_skipped`` (never a failed turn).

**The loop** (``orchestrator.structured.pipeline``): for each primary in order its context
(#170's ``build_context``, with the verified beats the earlier primaries rendered as it perceived
them, ``core.epistemic.stage.TurnStage``) → its gated render → detection and verification
against its own beliefs and canon (#166) → its own repair (#151) → staged. A later primary sees
only staged beats, never a rejected or regenerated-away version, and never another character's
``think`` or ``latent``. Everything commits as one turn (one ``message_rendered`` naming every
primary in order); a later render that fails fails the whole turn with nothing recorded.

``turn_report.multi`` = ``{mode, order, order_source, staged, accepted, regenerated_beats,
deferred, not_present}``; ``epistrel_multi_turns_total{mode}`` and
``epistrel_director_order_total{outcome}`` count them. Log lines carry ids and counts only
(invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.errors import InvalidRequest, ModuleOff
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.settings_schema import DEFAULT_INTERACTION_MODE, StorySettings
from epistrel.core.turns import CastEntry
from epistrel.observability.metrics import DIRECTOR_ORDER as DIRECTOR_ORDER_METRIC
from epistrel.observability.metrics import MULTI_TURNS
from epistrel.orchestrator.budget import DIRECTOR_ORDER, TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    GENERATION,
    DirectorCandidate,
    GenerationInputs,
    PlayerInput,
)
from epistrel.orchestrator.models.structured import Uncertain

log = logging.getLogger(__name__)

MODULE = "multi_character"
SEQUENTIAL, SIMULTANEOUS = "sequential", "simultaneous"
REQUEST, DIRECTOR = "request", "director"
DIRECTOR_FALLBACK = "director_fallback"
MAX_PRIMARIES = 6
"""The most primaries one structured turn renders."""
DIRECTOR_RECENT_BEATS = 5
"""How many of the latest beats the director sees."""
DIRECTOR_VARIANT = "director_order"
INACTIVE = "inactive"

DIRECTOR_ORDER_SKIPPED = "director_order_skipped"
"""Degradation: the director's ordering call could not be made or answered nothing usable, so
the request's order stood."""
PRIMARY_NOT_PRESENT = "primary_not_present"
"""Warning: a named primary is not where the scene is; it rendered with nothing perceived."""
PRIMARIES_DEFERRED = "primaries_deferred"
"""Degradation (NFR-PERF-7, before beat replacement): the budget could not plan every named
primary, so the later ones were deferred to a later turn and only the first rendered."""


def per_character(settings: ProfileSettings) -> bool:
    """Whether the profile renders characters one by one (``multi_character = per_character``)."""
    return module_on(settings, MODULE)


def _inactive(story: StorySettings, name: str) -> str | None:
    found = (story.model_extra or {}).get(INACTIVE)
    if not isinstance(found, Mapping):
        return None
    entry = found.get(name)
    value = entry.get("value") if isinstance(entry, Mapping) else None
    return value if isinstance(value, str) else None


def mode_of(story: StorySettings, request: Any = None) -> str:
    """The turn's interaction mode (see the module docstring)."""
    override = getattr(request, "interaction_mode", None)
    if override is not None:
        return str(override)
    if story.interaction_mode is not None:
        return str(story.interaction_mode)
    inactive = _inactive(story, "interaction_mode")
    if inactive in (SEQUENTIAL, SIMULTANEOUS):
        return inactive
    return DEFAULT_INTERACTION_MODE


def order_of(story: StorySettings, request: Any = None) -> str:
    """Who orders the turn's primaries: ``request`` or ``director``."""
    override = getattr(request, "sequence_order", None)
    if override is not None:
        return str(override)
    return str(story.sequence_order or REQUEST)


def _invalid(message: str, reason: str) -> InvalidRequest:
    return InvalidRequest(
        message,
        {
            "field": "body.render_character_ids",
            "reason": reason,
            "errors": [
                {"field": "body.render_character_ids", "message": message, "type": "value_error"}
            ],
        },
    )


def refuse_override(story_id: str, request: Any) -> None:
    """409 ``module_off``: a request overriding the interaction mode or the order of a story
    whose profile renders one combined reply."""
    if (
        getattr(request, "interaction_mode", None) is not None
        or getattr(request, "sequence_order", None) is not None
    ):
        raise ModuleOff(
            "interaction_mode and sequence_order need per-character renders "
            "(multi_character = per_character)",
            {"story_id": story_id, "module": MODULE},
        )


def check(
    settings: ProfileSettings,
    story_id: str,
    story: StorySettings,
    request: Any,
    named: Sequence[CastEntry],
) -> None:
    """The request's primaries under the story's profile (see the module docstring); raises."""
    if not per_character(settings):
        refuse_override(story_id, request)
        if len(named) > 1:
            raise _invalid("a structured turn renders one character", "one_render_character")
        return
    if len(named) <= 1:
        return
    if len(named) > MAX_PRIMARIES:
        raise _invalid(
            f"a structured turn renders at most {MAX_PRIMARIES} characters", "too_many_primaries"
        )
    if mode_of(story, request) != SEQUENTIAL:
        raise _invalid(
            "simultaneous turns with several primaries are not delivered yet",
            "simultaneous_not_delivered",
        )
    if any(c.is_player for c in named):
        raise _invalid("the player's character is never rendered", "player_cannot_render")


# -- the director's order ---------------------------------------------------------------------


class DirectorOrder(BaseModel):
    """The director's answer (``response_schema``): the primaries' ids in acting order."""

    model_config = ConfigDict(extra="forbid")

    stub_script: ClassVar[str] = DIRECTOR_VARIANT
    """The stub adapter answers this schema's calls from scripts keyed ``director_order``
    (``stub.script("director_order", …)``), never from the ``generation`` queue."""

    order: list[str] = Field(default_factory=list, max_length=MAX_PRIMARIES * 2)

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Any]) -> dict[str, Any]:
        """The stub's default: no order (the request's stands)."""
        del digest, messages
        return {"order": []}


def validated_order(answer: Sequence[str], primaries: Sequence[str]) -> list[str] | None:
    """The director's ``answer`` as an order of ``primaries``: a subset completed with the
    missing ones in request order; ``None`` for an unknown or repeated id, or nothing."""
    wanted = set(primaries)
    if not answer or any(a not in wanted for a in answer) or len(set(answer)) != len(answer):
        return None
    return [*answer, *(p for p in primaries if p not in answer)]


@dataclass(frozen=True, slots=True)
class Ordered:
    """The turn's order: the primaries' ids, who chose it, and its degradation."""

    order: tuple[str, ...]
    source: str
    degradation: tuple[tuple[str, str], ...] = ()


async def director_order(
    models: ModelAccessLayer,
    primaries: Sequence[CastEntry],
    *,
    actor: CastEntry,
    say: str | None,
    do: str | None,
    guidance: str,
    instructions: str,
    scenario_text: str,
    director_notes: str,
    scene: str,
    recent: Sequence[str],
    budget: TurnBudget,
    params: ModelParams,
) -> Ordered:
    """Ask the director who acts first (see the module docstring); never raises for the
    model."""
    ids = [p.id for p in primaries]

    def skipped(cause: str) -> Ordered:
        DIRECTOR_ORDER_METRIC.labels(outcome="skipped").inc()
        return Ordered(tuple(ids), DIRECTOR_FALLBACK, ((DIRECTOR_ORDER_SKIPPED, cause),))

    if len(ids) < 2:
        return Ordered(tuple(ids), REQUEST)
    if not budget.try_optional(DIRECTOR_ORDER, 1):
        return skipped("budget")
    held = budget.held
    inputs = GenerationInputs(
        rating_guidance=guidance,
        custom_instructions=instructions,
        scenario_text=scenario_text,
        director_notes=director_notes,
        scene=scene,
        working_memory=list(recent[-DIRECTOR_RECENT_BEATS:]),
        player_input=PlayerInput(name=actor.name, say=say, do=do),
        director_candidates=[
            DirectorCandidate(id=p.id, name=p.name, card=(p.definition or "")[:2_000])
            for p in primaries
        ],
    )
    try:
        with budget.charging(DIRECTOR_ORDER):
            found = await models.structured_call(
                GENERATION.name,
                DirectorOrder,
                inputs,
                params=params.merged(ModelParams(temperature=0.0)),
                budget=budget,
                variant=DIRECTOR_VARIANT,
            )
    except ModelError:
        return skipped("model")
    finally:
        budget.release_to(min(budget.held, max(0, held - 1)))
    if isinstance(found, Uncertain):
        return skipped("model")
    order = validated_order(found.order, ids)
    if order is None:
        return skipped("invalid_order")
    DIRECTOR_ORDER_METRIC.labels(outcome="director").inc()
    log.debug("director order: %d primaries", len(order))
    return Ordered(tuple(order), DIRECTOR)


# -- the report -------------------------------------------------------------------------------


@dataclass(slots=True)
class MultiReport:
    """``turn_report.multi``."""

    mode: str
    order: list[str]
    order_source: str = REQUEST
    staged: int = 0
    accepted: int = 0
    """The later primaries' acceptances of earlier staged beats, formed at staging."""
    regenerated_beats: list[int] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    not_present: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "order": list(self.order),
            "order_source": self.order_source,
            "staged": self.staged,
            "accepted": self.accepted,
            "regenerated_beats": sorted(set(self.regenerated_beats)),
            "deferred": list(self.deferred),
            "not_present": list(self.not_present),
        }


def count_turn(mode: str) -> None:
    MULTI_TURNS.labels(mode=mode).inc()


__all__ = [
    "DIRECTOR",
    "DIRECTOR_FALLBACK",
    "DIRECTOR_ORDER_SKIPPED",
    "MAX_PRIMARIES",
    "MODULE",
    "PRIMARIES_DEFERRED",
    "PRIMARY_NOT_PRESENT",
    "REQUEST",
    "SEQUENTIAL",
    "SIMULTANEOUS",
    "DirectorOrder",
    "MultiReport",
    "Ordered",
    "check",
    "count_turn",
    "director_order",
    "mode_of",
    "order_of",
    "per_character",
    "refuse_override",
    "validated_order",
]
