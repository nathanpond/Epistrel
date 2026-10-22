"""A structured turn's own predicate registration is not a configuration change made under it
(FR-CONC-16; ``core.own_predicates``).

P-08's structured row (#153): a turn that auto-registered a relation while it generated compared
the registry at commit with its capture, counted its own registration as an interpretive change,
re-checked itself, and on the minors row conflicted when the re-check failed and its budget could
not pay to generate again."""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.concurrency import run_with_revalidation
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.propositions.registry import auto_register
from epistrel.core.readset import StructuredReadSet
from tests import novelty_support as ns
from tests.auth_support import auth_settings
from tests.concurrency_support import TURN_PRINCIPAL

pytestmark = pytest.mark.asyncio


async def _nothing(conn: AsyncConnection) -> None:
    del conn


async def test_a_turn_commits_past_its_own_registration_and_rechecks_anothers(
    pg_url: str,
) -> None:
    """A turn that registers ``hums_at`` while it generates commits on its first attempt, with
    the predicate in ``own_predicates``.

    Complement asserted: a registration by someone else while the turn generates (another
    context, as another operation's) still re-checks it, naming ``predicate_registry``."""
    engine = make_engine(auth_settings(pg_url))
    try:
        cast = await ns.bobby_and_jenni(engine)
        story = cast.story_id

        def turn(relation: str, *, by_other: bool) -> object:
            async def register() -> str:
                async with engine.begin() as conn:
                    return (await auto_register(conn, story, relation)).id

            async def generate(op: Operation, inputs: object) -> None:
                del inputs
                if not op.attempts:
                    if by_other:
                        await asyncio.create_task(register(), context=contextvars.Context())
                    else:
                        await register()
                op.append("player_input", {"text": "I listen."}, provenance=Provenance.PLAYER)
                op.append("message_rendered", {"text": "The kettle hums."})

            return generate

        own = Operation(engine, story_id=story, kind=OperationKind.TURN, principal=TURN_PRINCIPAL)
        await run_with_revalidation(
            own,
            begin=_nothing,
            generate=turn("hums_at", by_other=False),  # type: ignore[arg-type]
            read_set=StructuredReadSet(),
        )
        assert len(own.own_predicates) == 1
        assert all(a.get("outcome") != "recheck" for a in own.attempts), own.attempts

        other = Operation(engine, story_id=story, kind=OperationKind.TURN, principal=TURN_PRINCIPAL)
        await run_with_revalidation(
            other,
            begin=_nothing,
            generate=turn("whistles_at", by_other=True),  # type: ignore[arg-type]
            read_set=StructuredReadSet(),
        )
        assert other.own_predicates == set()
        first = other.attempts[0]
        assert first["outcome"] == "recheck", other.attempts
        assert first["config_changed"] == ["predicate_registry"], first
    finally:
        await engine.dispose()
