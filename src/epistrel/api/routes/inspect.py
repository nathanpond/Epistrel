"""Story inspection over REST (docs/02 §9's ``/stories/{id}/inspect/…``; #146).

- ``GET /stories/{id}/inspect/ledger?viewpoint=omniscient`` → the story's thunks (introduced,
  undetermined references) and its promissory ledger (FR-COMMIT-13): a page of thunks by id
  (``limit``, ``after_id``; ``state`` keeps the ``open``, ``resolved`` or ``expired`` ones) with
  the ledger entries on them (``discharged`` keeps the discharged or the open ones).

- ``GET /stories/{id}/inspect/beliefs?character=…&viewpoint=omniscient`` → one character's belief
  projection (FR-EPI-6, FR-ADMIN-5; #159): a page of its ``belief_state`` rows by proposition id
  (``limit``, ``after``; ``proposition_id`` keeps one), each joined with its current belief
  (kind, confidence, provenance, held and valid times) and the proposition it holds.

Both are omniscient reads only (403 ``omniscient_required`` for any other viewpoint): the ledger
shows what no character's partition holds, a partition what no other character may read. 409
``module_off`` for a ledger read of a story that does not run commitment (a Lite story).
FR-ADMIN-5's other inspection reads (divergences, external facts) are later; #178 adds the
inspection reads' own authorization rules. Thin handlers over ``Engine.inspect_ledger`` and
``Engine.inspect_beliefs``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.core.commit.lifecycle import (
    INSPECT_LIMIT_DEFAULT,
    INSPECT_LIMIT_MAX,
    LedgerEntry,
    LedgerPage,
    Thunk,
)
from epistrel.core.enums import ThunkState
from epistrel.core.epistemic.partitions import (
    INSPECT_LIMIT_DEFAULT as BELIEFS_LIMIT_DEFAULT,
)
from epistrel.core.epistemic.partitions import (
    INSPECT_LIMIT_MAX as BELIEFS_LIMIT_MAX,
)
from epistrel.core.epistemic.partitions import Belief, BeliefInspection, BeliefState
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition

router = APIRouter(prefix="/stories", tags=["stories"])


class PositionBody(BaseModel):
    """A narrative position: the turn, its variant and the beat (``null`` when position-less)."""

    turn: int | None
    variant: int | None
    beat: int | None


class RejectedBindingBody(BaseModel):
    """A binding of the thunk that resolution reconciliation rejected (FR-COMMIT-14; #154): the
    claim and beat that made it, the entity it bound and the records it conflicted with (ids)."""

    claim_id: str | None = None
    beat: int | None = None
    bound_entity: str | None = None
    conflicting_records: list[str] = []
    conflicting_lines: list[str] = []
    """#165: the bound character's later sincere lines (event ids) its knowledge contradicts."""


class CouldHaveKnownFlagBody(BaseModel):
    """A could-have-known soft flag on the thunk's resolution (FR-COMMIT-11; #165): the bound
    character, the severity (``mild`` under a story-day, ``moderate`` under 30, ``strong``
    beyond) and the story-time gap in minutes. A warning, never a rejection."""

    character_id: str
    severity: str
    gap_minutes: int


class ThunkBody(BaseModel):
    """One thunk: its proposition sketch (texts, the predicate and the known arguments by id,
    the open slot, the candidates), its effective ``state`` (``resolved`` only while the
    resolving turn is in the timeline), where it was introduced, whether it was made salient,
    the story-minute it is about, the fact that resolved it and its resolution level
    (FR-COMMIT-9: ``consequential`` or ``incidental``), the bindings reconciliation rejected
    (#154) and the could-have-known soft flags its resolution raised (#165)."""

    id: str
    sketch: dict[str, Any]
    description: str
    open_slot: str
    state: str
    position: PositionBody
    salient: bool
    valid_time: int | None
    resolved_fact_id: str | None
    resolution_level: str | None = None
    rejected_bindings: list[RejectedBindingBody] = []
    could_have_known_flags: list[CouldHaveKnownFlagBody] = []

    @classmethod
    def of(cls, found: Thunk, names: dict[str, str], page: LedgerPage | None = None) -> ThunkBody:
        return cls(
            id=found.id,
            sketch=found.sketch.document(),
            description=found.sketch.describe(names, names.get(found.sketch.predicate_id)),
            open_slot=found.open_slot.value,
            state=found.state.value,
            position=PositionBody(
                turn=found.narrative_turn, variant=found.turn_variant, beat=found.beat
            ),
            salient=found.salient,
            valid_time=found.valid_time,
            resolved_fact_id=found.resolved_fact_id if found.state is ThunkState.RESOLVED else None,
            resolution_level=None if page is None else page.levels.get(found.id),
            rejected_bindings=[]
            if page is None
            else [
                RejectedBindingBody.model_validate(
                    {k: v for k, v in item.items() if k in RejectedBindingBody.model_fields}
                )
                for item in page.rejected.get(found.id, ())
            ],
            could_have_known_flags=[]
            if page is None
            else [
                CouldHaveKnownFlagBody.model_validate(
                    {k: v for k, v in item.items() if k in CouldHaveKnownFlagBody.model_fields}
                )
                for item in page.flags.get(found.id, ())
            ],
        )


class LedgerItemBody(BaseModel):
    """One promissory-ledger entry: what it is owed on (a thunk, or a hidden fact's
    proposition), its kind, where it was introduced, and the operation that discharged it and
    how (``null`` while undischarged; reverting that operation makes it undischarged again)."""

    id: str
    thunk_id: str | None
    proposition_id: str | None
    kind: str
    introduced_position: PositionBody
    discharged_by_operation: str | None
    discharged_mode: str | None

    @classmethod
    def of(cls, found: LedgerEntry) -> LedgerItemBody:
        return cls(
            id=found.id,
            thunk_id=found.thunk_id,
            proposition_id=found.proposition_id,
            kind=found.kind.value,
            introduced_position=PositionBody(**found.introduced_position),
            discharged_by_operation=found.discharged_by_operation if found.discharged else None,
            discharged_mode=found.discharged_mode.value
            if found.discharged and found.discharged_mode is not None
            else None,
        )


class LedgerBody(BaseModel):
    """A page of the story's thunks and their ledger entries, the story's revision, and
    ``next_after_id`` (``null`` on the last page)."""

    thunks: list[ThunkBody]
    ledger: list[LedgerItemBody]
    revision: int
    next_after_id: str | None

    @classmethod
    def of(cls, page: LedgerPage) -> LedgerBody:
        names = dict(page.names)
        return cls(
            thunks=[ThunkBody.of(t, names, page) for t in page.inspection.thunks],
            ledger=[LedgerItemBody.of(e) for e in page.inspection.ledger],
            revision=page.revision,
            next_after_id=page.inspection.next_after_id,
        )


@router.get(
    "/{story_id}/inspect/ledger",
    response_model=LedgerBody,
    operation_id="inspect_ledger",
    summary="Inspect the story's thunks and promissory ledger",
    description=(
        "The introduced-but-undetermined references (thunks) and the salient references "
        "awaiting commitment (FR-COMMIT-7,13), `omniscient` only (403 `omniscient_required`). "
        "A page of thunks by id (`limit`, at most 500; `after_id` continues), optionally only "
        "those `open`, `resolved` or `expired`, with the ledger entries on them (`discharged` "
        "keeps the discharged or the undischarged ones); the first page also lists the entries "
        "on hidden facts' propositions. 409 `module_off` for a story that does not run "
        "commitment."
    ),
)
async def inspect_ledger(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    state: ThunkState | None = None,
    discharged: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=INSPECT_LIMIT_MAX)] = INSPECT_LIMIT_DEFAULT,
    after_id: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
) -> LedgerBody:
    return LedgerBody.of(
        await engine.inspect_ledger(
            ctx,
            story_id,
            viewpoint=viewpoint,
            state=state,
            discharged=discharged,
            limit=limit,
            after_id=after_id,
        )
    )


# -- beliefs (#159) ------------------------------------------------------------------------------


class NarrativePositionBody(BaseModel):
    """A narrative position in its five parts (#159): the turn, its variant, the inter-turn
    slot (0: the turn itself), the beat (0: the player input) and the step."""

    turn: int
    variant: int
    slot_k: int
    beat: int
    step: int

    @classmethod
    def of(cls, position: Position) -> NarrativePositionBody:
        return cls(**position.document())


class BeliefItemBody(BaseModel):
    """One belief the character holds now: the ``belief_state`` row (``current_level``,
    ``current_scalar``, ``polarity``, ``since``) joined with its belief record (``kind``,
    ``confidence_scalar``, ``provenance``: #160's acquisition, ``held_from``/``held_to``,
    ``valid_from``/``valid_to`` in story-minutes) and the proposition it holds."""

    belief_id: str
    proposition_id: str
    proposition: Proposition
    kind: str
    polarity: str
    current_level: str
    current_scalar: float | None
    since: NarrativePositionBody
    provenance: str | None
    held_from: NarrativePositionBody
    held_to: NarrativePositionBody | None
    valid_from: int | None
    valid_to: int | None
    tx_from: str

    @classmethod
    def of(cls, state: BeliefState, belief: Belief) -> BeliefItemBody:
        return cls(
            belief_id=belief.id,
            proposition_id=state.proposition_id,
            proposition=belief.proposition,
            kind=belief.kind.value,
            polarity=state.polarity,
            current_level=state.current_level.value,
            current_scalar=state.current_scalar,
            since=NarrativePositionBody.of(state.since),
            provenance=belief.provenance,
            held_from=NarrativePositionBody.of(belief.held_from),
            held_to=None if belief.held_to is None else NarrativePositionBody.of(belief.held_to),
            valid_from=belief.valid_from,
            valid_to=belief.valid_to,
            tx_from=belief.tx_from.isoformat(),
        )


class BeliefsBody(BaseModel):
    """A page of one character's belief projection, the story's revision, and ``next_after``
    (the proposition id to continue after; ``null`` on the last page)."""

    character: str
    beliefs: list[BeliefItemBody]
    revision: int
    next_after: str | None

    @classmethod
    def of(cls, page: BeliefInspection) -> BeliefsBody:
        return cls(
            character=page.holder_character_id,
            beliefs=[BeliefItemBody.of(state, belief) for state, belief in page.items],
            revision=page.revision,
            next_after=page.next_after,
        )


@router.get(
    "/{story_id}/inspect/beliefs",
    response_model=BeliefsBody,
    operation_id="inspect_beliefs",
    summary="Inspect a character's belief projection",
    description=(
        "What `character` believes now (FR-EPI-6): its `belief_state` rows, each joined with "
        "the belief record it rests on and the proposition held, `omniscient` only (403 "
        "`omniscient_required`). A page by proposition id (`limit`, at most 500; `after` "
        "continues); `proposition_id` keeps one. A belief is its holder's truth, whether or "
        "not canon agrees (FR-EPI-3). 422 for a character outside the story's cast."
    ),
)
async def inspect_beliefs(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    character: Annotated[str, Query(pattern=ULID_PATTERN)],
    viewpoint: str | None = None,
    proposition_id: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=BELIEFS_LIMIT_MAX)] = BELIEFS_LIMIT_DEFAULT,
    after: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
) -> BeliefsBody:
    return BeliefsBody.of(
        await engine.inspect_beliefs(
            ctx,
            story_id,
            character=character,
            viewpoint=viewpoint,
            proposition_id=proposition_id,
            limit=limit,
            after=after,
        )
    )
