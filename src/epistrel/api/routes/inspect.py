"""Story inspection over REST (docs/02 §9's ``/stories/{id}/inspect/…``; #146).

- ``GET /stories/{id}/inspect/ledger?viewpoint=omniscient`` → the story's thunks (introduced,
  undetermined references) and its promissory ledger (FR-COMMIT-13): a page of thunks by id
  (``limit``, ``after_id``; ``state`` keeps the ``open``, ``resolved`` or ``expired`` ones) with
  the ledger entries on them (``discharged`` keeps the discharged or the open ones), and the
  story's hidden facts (``hidden_facts``, #169).

- ``GET /stories/{id}/inspect/beliefs?character=…&viewpoint=omniscient`` → one character's belief
  projection (FR-EPI-6, FR-ADMIN-5; #159): a page of its ``belief_state`` rows by proposition id
  (``limit``, ``after``; ``proposition_id`` keeps one), each joined with its current belief
  (kind, confidence, provenance, held and valid times) and the proposition it holds.

Both are omniscient reads only (403 ``omniscient_required`` for any other viewpoint): the ledger
shows what no character's partition holds, a partition what no other character may read. 409
``module_off`` for a ledger read of a story that does not run commitment (a Lite story).
- ``GET /stories/{id}/inspect/divergences?viewpoint=omniscient`` → the story's divergences (#166:
  what a lying speaker asserted against what it believes; ``speaker``, ``subject``, ``as_of``).

- ``GET /stories/{id}/inspect/exposures?viewpoint=omniscient`` → the story's exposures (#164:
  what a witness heard and has or has not decided; ``character``, ``state``, ``as_of``).

#178: every inspection is an omniscient read and the viewpoint is its only gate
(``core.epistemic.inspection.omniscient_read``): no viewpoint is 422 ``viewpoint_unresolved``, a
character viewpoint 403 ``omniscient_required``; each answer carries ``output_viewpoint:
"omniscient"``. Service authentication is the only other check (C-28, S-TRUST-BOUNDARY). The
belief, divergence and exposure reads are 409 ``module_off`` for a story that keeps no character
partitions.

- ``GET /stories/{id}/inspect/external-facts?viewpoint=omniscient`` → the story's snapshotted
  external facts (#207, FR-ADMIN-5: each ``external_read`` with the facts citing it; ``as_of``),
  with no partitions gate: a story with no reads answers an empty page.

Thin handlers over ``Engine.inspect_*``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.core.canon.facts import Fact
from epistrel.core.commit.lifecycle import (
    INSPECT_LIMIT_DEFAULT,
    INSPECT_LIMIT_MAX,
    LedgerEntry,
    LedgerPage,
    Thunk,
)
from epistrel.core.enums import ExposureState, ThunkState
from epistrel.core.epistemic.exposure import Exposure
from epistrel.core.epistemic.inspection import (
    INSPECT_LIMIT_DEFAULT as KNOWLEDGE_LIMIT_DEFAULT,
)
from epistrel.core.epistemic.inspection import (
    INSPECT_LIMIT_MAX as KNOWLEDGE_LIMIT_MAX,
)
from epistrel.core.epistemic.inspection import DivergenceItem, DivergencePage, ExposurePage
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
from epistrel.core.states.external import ExternalFactItem, ExternalFactPage

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


class HiddenFactBody(BaseModel):
    """A hidden fact (#169, FR-COMMIT-4): a current canon version scoped away from the shared
    view (``characters``: the ones who know it; ``engine``: nobody does), its proposition, its
    valid-time (story-minutes) and the turn that committed it."""

    fact_id: str
    proposition: Proposition
    scope: str
    characters: list[str]
    valid_from: int | None
    valid_to: int | None
    narrative_turn: int | None

    @classmethod
    def of(cls, fact: Fact) -> HiddenFactBody:
        return cls(
            fact_id=fact.id,
            proposition=fact.proposition,
            scope=fact.visibility.scope,
            characters=list(fact.visibility.characters or ()),
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            narrative_turn=fact.narrative_turn,
        )


class LedgerBody(BaseModel):
    """A page of the story's thunks and their ledger entries, the hidden facts (the first page
    only, #169), the story's revision, and ``next_after_id`` (``null`` on the last page)."""

    thunks: list[ThunkBody]
    ledger: list[LedgerItemBody]
    hidden_facts: list[HiddenFactBody] = []
    revision: int
    next_after_id: str | None
    output_viewpoint: Literal["omniscient"] = "omniscient"
    """An inspection is an omniscient read, labelled so (C-28; #178)."""

    @classmethod
    def of(cls, page: LedgerPage) -> LedgerBody:
        names = dict(page.names)
        return cls(
            thunks=[ThunkBody.of(t, names, page) for t in page.inspection.thunks],
            ledger=[LedgerItemBody.of(e) for e in page.inspection.ledger],
            hidden_facts=[HiddenFactBody.of(f) for f in page.hidden],
            revision=page.revision,
            next_after_id=page.inspection.next_after_id,
            output_viewpoint="omniscient",
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
        "on hidden facts' propositions and the hidden facts themselves (`hidden_facts`: current "
        "canon scoped to characters or to the engine, #169). 422 `viewpoint_unresolved` when "
        "no viewpoint is named; 409 `module_off` for a story that does not run commitment."
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


class IdentityItemBody(BaseModel):
    """One ``same_as`` the character holds (#171, FR-PROP-7): the two entities (id order),
    whether it is a canon fact the character may read (``canon``) or its own belief
    (``belief``), and that record's id."""

    entities: list[str]
    source: Literal["canon", "belief"]
    record_id: str


class BeliefsBody(BaseModel):
    """A page of one character's belief projection, the story's revision, ``next_after`` (the
    proposition id to continue after; ``null`` on the last page) and ``identities``: who the
    character holds to be one referent (#171; its ``same_as`` beliefs are also ordinary
    ``beliefs`` rows)."""

    character: str
    beliefs: list[BeliefItemBody]
    revision: int
    next_after: str | None
    identities: list[IdentityItemBody] = []
    output_viewpoint: Literal["omniscient"] = "omniscient"
    """An inspection is an omniscient read, labelled so (C-28; #178)."""

    @classmethod
    def of(cls, page: BeliefInspection) -> BeliefsBody:
        return cls(
            character=page.holder_character_id,
            beliefs=[BeliefItemBody.of(state, belief) for state, belief in page.items],
            revision=page.revision,
            next_after=page.next_after,
            identities=[
                IdentityItemBody(entities=list(i.entities), source=i.source, record_id=i.record_id)
                for i in sorted(page.identities, key=lambda i: (i.entities, i.record_id))
            ],
            output_viewpoint="omniscient",
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
        "not canon agrees (FR-EPI-3). `identities` lists who the character holds to be one "
        "referent (FR-PROP-7): the canon `same_as` facts it may read and its own `same_as` "
        "beliefs. `as_of` reads the projection as the corrected record stood through that "
        "turn. 422 `viewpoint_unresolved` when no viewpoint is named; 409 `module_off` when "
        "the story keeps no character partitions; 422 for a character outside the story's "
        "cast; the player's character has no partition (an empty page)."
    ),
)
async def inspect_beliefs(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    character: Annotated[str, Query(pattern=ULID_PATTERN)],
    viewpoint: str | None = None,
    proposition_id: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    as_of: Annotated[int | None, Query(ge=0, le=2_147_483_647)] = None,
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
            as_of=as_of,
            limit=limit,
            after=after,
        )
    )


# -- divergences (#166, #178) ---------------------------------------------------------------------


class DivergenceItemBody(BaseModel):
    """One divergence (FR-TURN-7): the utterance and its speaker, the proposition asserted and
    the contrary belief it deviates from (``null`` for a lie no record settles), the say beat's
    position, the event of a reveal that settled it (#169) and the writing operation."""

    id: str
    utterance_id: str
    speaker: str
    asserted_proposition_id: str
    asserted: Proposition
    believed_belief_id: str | None
    believed_proposition_id: str | None
    believed: Proposition | None
    position: NarrativePositionBody
    revealed_by_event_id: str | None
    operation_id: str

    @classmethod
    def of(cls, item: DivergenceItem) -> DivergenceItemBody:
        r = item.row
        return cls(
            id=r.id,
            utterance_id=r.utterance_id,
            speaker=r.speaker,
            asserted_proposition_id=r.asserted_proposition_id,
            asserted=item.asserted,
            believed_belief_id=r.believed_belief_id,
            believed_proposition_id=r.believed_proposition_id,
            believed=item.believed,
            position=NarrativePositionBody.of(r.position),
            revealed_by_event_id=r.revealed_by_event_id,
            operation_id=r.operation_id,
        )


class DivergencesBody(BaseModel):
    """A page of the story's divergences by id, the story's revision and ``next_after_id``
    (``null`` on the last page), labelled omniscient."""

    items: list[DivergenceItemBody]
    revision: int
    next_after_id: str | None
    output_viewpoint: Literal["omniscient"] = "omniscient"

    @classmethod
    def of(cls, page: DivergencePage) -> DivergencesBody:
        return cls(
            items=[DivergenceItemBody.of(i) for i in page.items],
            revision=page.revision,
            next_after_id=page.next_after_id,
            output_viewpoint="omniscient",
        )


@router.get(
    "/{story_id}/inspect/divergences",
    response_model=DivergencesBody,
    operation_id="inspect_divergences",
    summary="Inspect the story's divergences",
    description=(
        "What each lying speaker asserted against what it believes (FR-TURN-7, FR-ADMIN-5), "
        "`omniscient` only (422 `viewpoint_unresolved` when no viewpoint is named, 403 "
        "`omniscient_required` for a character). A page by id (`limit`, at most 500; "
        "`after_id` continues); `speaker` keeps one speaker's, `subject` those whose asserted "
        "proposition is about an entity, `as_of` those positioned through a turn. 409 "
        "`module_off` when the story keeps no character partitions."
    ),
)
async def inspect_divergences(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    speaker: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    subject: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    as_of: Annotated[int | None, Query(ge=0, le=2_147_483_647)] = None,
    limit: Annotated[int, Query(ge=1, le=KNOWLEDGE_LIMIT_MAX)] = KNOWLEDGE_LIMIT_DEFAULT,
    after_id: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
) -> DivergencesBody:
    return DivergencesBody.of(
        await engine.inspect_divergences(
            ctx,
            story_id,
            viewpoint=viewpoint,
            speaker=speaker,
            subject=subject,
            as_of=as_of,
            limit=limit,
            after_id=after_id,
        )
    )


# -- exposures (#164, #178) -----------------------------------------------------------------------


class ExposureItemBody(BaseModel):
    """One exposure (FR-BELIEF-5): the witness, the proposition it heard and from whom, the
    beat's event, how much it caught (``fidelity``), the exposure position, its effective
    ``state`` and, once resolved, the transition and the operation that resolved it."""

    id: str
    witness: str
    proposition_id: str
    proposition: Proposition
    source: str | None
    source_event_id: str
    kind: str
    fidelity: str
    position: NarrativePositionBody
    state: str
    resolved_by_transition: str | None
    resolution_operation_id: str | None
    operation_id: str

    @classmethod
    def of(cls, found: Exposure) -> ExposureItemBody:
        return cls(
            id=found.id,
            witness=found.witness,
            proposition_id=found.proposition_id,
            proposition=found.proposition,
            source=found.source,
            source_event_id=found.source_event_id,
            kind=found.kind.value,
            fidelity=found.fidelity.value,
            position=NarrativePositionBody.of(found.position),
            state=found.state.value,
            resolved_by_transition=found.resolved_by_transition,
            resolution_operation_id=found.resolution_operation_id,
            operation_id=found.operation_id,
        )


class ExposuresBody(BaseModel):
    """A page of the story's exposures by id, the story's revision and ``next_after_id``
    (``null`` on the last page), labelled omniscient."""

    items: list[ExposureItemBody]
    revision: int
    next_after_id: str | None
    output_viewpoint: Literal["omniscient"] = "omniscient"

    @classmethod
    def of(cls, page: ExposurePage) -> ExposuresBody:
        return cls(
            items=[ExposureItemBody.of(x) for x in page.items],
            revision=page.revision,
            next_after_id=page.next_after_id,
            output_viewpoint="omniscient",
        )


@router.get(
    "/{story_id}/inspect/exposures",
    response_model=ExposuresBody,
    operation_id="inspect_exposures",
    summary="Inspect the story's exposures",
    description=(
        "What each witness heard and has (`resolved`) or has not yet (`unresolved`) decided "
        "(FR-BELIEF-5,14, FR-ADMIN-5), `omniscient` only (422 `viewpoint_unresolved` when no "
        "viewpoint is named, 403 `omniscient_required` for a character). A raw read: it never "
        "resolves an exposure. A page by id (`limit`, at most 500; `after_id` continues); "
        "`character` keeps one witness's, `state` one effective state as the record stood "
        "through `as_of`. 409 `module_off` when the story keeps no character partitions."
    ),
)
async def inspect_exposures(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    character: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    state: ExposureState | None = None,
    as_of: Annotated[int | None, Query(ge=0, le=2_147_483_647)] = None,
    limit: Annotated[int, Query(ge=1, le=KNOWLEDGE_LIMIT_MAX)] = KNOWLEDGE_LIMIT_DEFAULT,
    after_id: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
) -> ExposuresBody:
    return ExposuresBody.of(
        await engine.inspect_exposures(
            ctx,
            story_id,
            viewpoint=viewpoint,
            character=character,
            state=state,
            as_of=as_of,
            limit=limit,
            after_id=after_id,
        )
    )


# -- external facts (#207) ------------------------------------------------------------------------


class ExternalFactBody(BaseModel):
    """A fact version citing an external read (#205's reference to it, FR-EXT-4): its id, its
    proposition, its scope, valid-time (story-minutes), provenance, authority, and when it was
    recorded and closed (``tx_to``: ``null`` while current)."""

    fact_id: str
    proposition: Proposition
    scope: str
    valid_from: int | None
    valid_to: int | None
    provenance: str
    authority: str
    tx_from: datetime
    tx_to: datetime | None

    @classmethod
    def of(cls, fact: Fact) -> ExternalFactBody:
        return cls(
            fact_id=fact.id,
            proposition=fact.proposition,
            scope=fact.visibility.scope,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            provenance=fact.provenance.value,
            authority=fact.authority.value,
            tx_from=fact.tx_from,
            tx_to=fact.tx_to,
        )


class ExternalReadItemBody(BaseModel):
    """One snapshotted external value (FR-EXT-2, FR-ADMIN-5): the ``external_read`` event, the
    provider and the value's name, the State and its scope key (``null``: story-scoped), the
    value read (``null``: unknown), when it was read, the turn and variant that read it, its
    source (``live``, ``last_known``, ``unknown``) and the facts that cite it."""

    event_id: str
    provider: str
    value_name: str
    state_id: str | None
    scope_key: str | None
    value: Any
    read_at: str | None
    turn: int | None
    variant: int | None
    source: str
    facts: list[ExternalFactBody]

    @classmethod
    def of(cls, item: ExternalFactItem) -> ExternalReadItemBody:
        return cls(
            event_id=item.event_id,
            provider=item.provider,
            value_name=item.value_name,
            state_id=item.state_id,
            scope_key=item.scope_key,
            value=item.value,
            read_at=item.read_at,
            turn=item.turn,
            variant=item.variant,
            source=item.source,
            facts=[ExternalFactBody.of(f) for f in item.facts],
        )


class ExternalFactsBody(BaseModel):
    """A page of the story's external reads by id, the story's revision and ``next_after_id``
    (``null`` on the last page), labelled omniscient."""

    items: list[ExternalReadItemBody]
    revision: int
    next_after_id: str | None
    output_viewpoint: Literal["omniscient"] = "omniscient"

    @classmethod
    def of(cls, page: ExternalFactPage) -> ExternalFactsBody:
        return cls(
            items=[ExternalReadItemBody.of(i) for i in page.items],
            revision=page.revision,
            next_after_id=page.next_after_id,
            output_viewpoint="omniscient",
        )


@router.get(
    "/{story_id}/inspect/external-facts",
    response_model=ExternalFactsBody,
    operation_id="inspect_external_facts",
    summary="Inspect the story's snapshotted external facts",
    description=(
        "Each value an external provider answered for the story, as pinned and recorded "
        "(`external_read`), with the facts that cite it (FR-EXT-2,4, FR-ADMIN-5), `omniscient` "
        "only (422 `viewpoint_unresolved` when no viewpoint is named, 403 "
        "`omniscient_required` for a character). A raw read: no provider is called. A page by "
        "id (`limit`, at most 500; `after_id` continues); `as_of` keeps the reads positioned "
        "through a turn. A story with no reads answers an empty page."
    ),
)
async def inspect_external_facts(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    as_of: Annotated[int | None, Query(ge=0, le=2_147_483_647)] = None,
    limit: Annotated[int, Query(ge=1, le=KNOWLEDGE_LIMIT_MAX)] = KNOWLEDGE_LIMIT_DEFAULT,
    after_id: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
) -> ExternalFactsBody:
    return ExternalFactsBody.of(
        await engine.inspect_external_facts(
            ctx, story_id, viewpoint=viewpoint, as_of=as_of, limit=limit, after_id=after_id
        )
    )
