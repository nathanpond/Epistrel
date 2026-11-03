"""#707, #711: #140's check under a planned change's hypothetical canon
(``orchestrator.rewrite.hypothetical``): an inferred belief follows a change only when its
premises, re-run with the change in place, support the new value; a fact stays consistent under
a restored canon only when the check does not contradict it."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import edges
from epistrel.core.enums import DependencyEdgeType, OperationKind
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.registry import load
from epistrel.core.verify.rules import Record
from epistrel.orchestrator.rewrite import hypothetical
from tests.auth_support import auth_settings
from tests.fixtures.bobby_education import ENROLLED_AT, bobby_education
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


def _change(subject: str, new: str, old: str) -> dict[str, object]:
    return {
        "kind": "proposition",
        "subject_id": subject,
        "predicate_id": ENROLLED_AT,
        "object_id": new,
        "superseded_object_id": old,
        "polarity": "affirmed",
        "valid_from_minutes": None,
        "valid_to_minutes": None,
    }


async def _inferred(engine: AsyncEngine, story: str, premises: list[str]) -> str:
    """A belief id with ``derives`` (``inference``) edges onto ``premises``."""
    belief = new_ulid()
    async with Operation(engine, story_id=story, kind=OperationKind.CONTROL, principal="p") as op:

        async def step(conn: AsyncConnection) -> None:
            specs = await edges.fact_edges(
                conn, story, DependencyEdgeType.DERIVES, "beliefs", belief, premises
            )
            await edges.record_edges(
                conn,
                op,
                [dataclasses.replace(e, acquisition_kind="inference") for e in specs],
            )

        op.add_commit_step(step)
        await op.commit()
    return belief


async def test_inference_follows_its_premises(pg_url: str, engine: AsyncEngine) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, _):
        ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
        assert ed.ucla is not None
        change = _change(ed.bobby, ed.ucla, ed.osu)
        changed = {ed.enrollment.id}
        only = await _inferred(engine, ed.story_id, [ed.enrollment.id])
        async with engine.connect() as conn:
            assert await hypothetical.inference_follows(conn, ed.story_id, only, change, changed)
            registry = await load(conn, ed.story_id)
        # Complement: a premise that denies the new value (Bobby never enrolled at UCLA).
        new_value = hypothetical.proposition_of(change)
        assert new_value is not None
        denial = Proposition(
            subject_id=ed.bobby,
            predicate_id=ENROLLED_AT,
            object_id=ed.ucla,
            polarity=Polarity.NEGATED,
            valid_time=Interval.of(None, None),
        )
        assert hypothetical.premises_support(new_value, [], change, registry)
        assert not hypothetical.premises_support(
            new_value, [Record(denial, "denial")], change, registry
        )


async def test_a_fact_stays_consistent_unless_contradicted(
    pg_url: str, engine: AsyncEngine
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, _):
        ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
        assert ed.ucla is not None
        async with engine.connect() as conn:
            # The degree is another key: consistent whatever the enrollment becomes.
            assert await hypothetical.stays_consistent(
                conn, ed.story_id, "facts", ed.degree.id, [_change(ed.bobby, ed.ucla, ed.osu)]
            )
            # Text has no proposition the check can read: never shown consistent.
            assert not await hypothetical.stays_consistent(
                conn, ed.story_id, "events", new_ulid(), [_change(ed.bobby, ed.ucla, ed.osu)]
            )
