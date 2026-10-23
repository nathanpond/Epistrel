"""Author-directed beliefs: ``POST /admin/stories/{id}/beliefs`` (FR-EPI-7's author direction,
FR-BELIEF-4; #160).

An operator sets what one modeled character believes: a position-less ``control`` operation
that bumps the story's revision, stages a ``belief_authored`` author act and, in its commit,
records an ``author`` transition (``core.epistemic.transitions``) citing that act as its
``record`` antecedent. The belief is positioned after everything on the timeline (the head
turn's end, one step per author write there), so it holds from now on and a correction of an
earlier belief closes it there. Its confidence is dual: a level, a scalar, both (they must
agree: 422 ``confidence_inconsistent``) or neither (``known``, an author's default).

409 ``module_off`` when the story's configuration keeps no partitions; 422
``holder_has_no_partition`` for the player's character or a non-character. ``Idempotency-Key``
and ``X-Epistrel-Expected-Revision`` are honoured by the Engine method.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.concurrency import check_expected_revision
from epistrel.core.enums import BeliefConfidenceLevel, OperationKind, Provenance
from epistrel.core.epistemic import transitions
from epistrel.core.epistemic.partitions import Belief
from epistrel.core.epistemic.transitions import Antecedent, Confidence, Transition
from epistrel.core.errors import ModuleOff
from epistrel.core.events import Operation
from epistrel.core.events.types import BeliefAuthored
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.stories import require_writable
from epistrel.core.tables import events, stories
from epistrel.core.tables.beliefs import belief_transitions

log = logging.getLogger(__name__)

BELIEF_AUTHORED = "belief_authored"


class BeliefRequest(BaseModel):
    """An author belief: the holder (a modeled character), #131's normalized proposition (JSON,
    never prose), an optional polarity replacing the proposition's, the confidence (a level, a
    scalar in ``[0, 1]`` with at most three decimals, both or neither) and how the holder
    treats it (``disclosure``, FR-SCN-8's ``open``/``guarded``/``secret``; recorded on the
    author act)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "holder": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
                    "proposition": {
                        "subject_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C6",
                        "predicate_id": "01JPRED0000000000000000015",
                        "object_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C4",
                        "valid_time": {
                            "kind": "interval",
                            "start": {"inner": 0, "outer": 0},
                            "end": None,
                        },
                    },
                    "confidence_scalar": 0.6,
                    "disclosure": "secret",
                }
            ]
        },
    )

    holder: Annotated[str, Field(pattern=ULID_PATTERN)]
    proposition: Proposition
    polarity: Polarity | None = None
    confidence_level: BeliefConfidenceLevel | None = None
    confidence_scalar: StrictFloat | None = Field(default=None, ge=0, le=1)
    disclosure: Literal["open", "guarded", "secret"] | None = None


@dataclass(frozen=True, slots=True)
class BeliefWrite:
    """An author belief's answer: the belief after it, the transition that recorded it, the
    beliefs it closed, the story's revision and the ``control`` operation."""

    belief: Belief
    transition: Transition
    timeline_revision: int
    operation_id: str
    closed: tuple[str, ...] = ()
    replayed: bool = False


async def require_partitions(db: AsyncEngine, story_id: str) -> ProfileSettings:
    """404 for an unknown story; 409 ``module_off`` unless the story keeps partitions."""
    await check_expected_revision(db, story_id, None)
    async with db.connect() as conn:
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(settings, transitions.PARTITIONS_MODULE):
        raise ModuleOff(
            "the story's configuration keeps no character partitions",
            {"story_id": story_id, "module": transitions.PARTITIONS_MODULE},
        )
    return settings


async def _position(conn: AsyncConnection, story_id: str) -> Position:
    """After everything on the timeline: the head turn's end, one step per author write."""
    head = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    turn = max(0, int(head or 1) - 1)
    t = belief_transitions.c
    taken = await conn.scalar(
        select(func.count())
        .select_from(belief_transitions)
        .where(
            t.story_id == story_id,
            t.position_turn == turn,
            t.position_slot_k == 0,
            t.position_beat == POSITION_MAX,
        )
    )
    return Position(turn, 0, 0, POSITION_MAX, min(POSITION_MAX, int(taken or 0) + 1))


async def write_belief(
    db: AsyncEngine,
    story_id: str,
    request: BeliefRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> BeliefWrite:
    """Write an author belief (see the module docstring)."""
    await require_writable(db, story_id)
    settings = await require_partitions(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    found: dict[str, Any] = {}
    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    ) as op:
        op.append(
            BELIEF_AUTHORED,
            BeliefAuthored(disclosure=request.disclosure, holder_character_id=request.holder),
        )

        async def step(conn: AsyncConnection) -> None:
            act = await conn.scalar(
                select(events.c.id).where(
                    events.c.story_id == story_id,
                    events.c.operation_id == op.operation_id,
                    events.c.type == BELIEF_AUTHORED,
                )
            )
            found["result"] = await transitions.transition(
                conn,
                op,
                holder=request.holder,
                kind=transitions.Kind.AUTHOR,
                proposition=request.proposition,
                polarity=request.polarity,
                antecedent=Antecedent.record("events", str(act)),
                position=await _position(conn, story_id),
                confidence=Confidence(request.confidence_level, request.confidence_scalar),
                settings=settings,
            )

        op.add_commit_step(step)
        await op.commit()
    result: transitions.TransitionResult = found["result"]
    assert op.timeline_revision is not None  # noqa: S101 - a committed story operation
    log.info(
        "author belief story=%s belief=%s closed=%d",
        story_id,
        result.belief.id,
        len(result.closed),
    )
    return BeliefWrite(
        belief=result.belief,
        transition=result.transition,
        timeline_revision=int(op.timeline_revision),
        operation_id=op.operation_id,
        closed=result.closed,
    )


__all__ = ["BELIEF_AUTHORED", "BeliefRequest", "BeliefWrite", "require_partitions", "write_belief"]
