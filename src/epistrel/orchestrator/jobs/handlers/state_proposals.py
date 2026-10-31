"""``state_proposal_analysis``: the on-demand history analysis that proposes States
(FR-STATE-3; #192).

``POST /stories/{id}/state-proposals/analyze`` enqueues it (``core.states.proposals``). The
handler scans the story's active beats in a read-only snapshot (``proposals.scan``: no model
call, nothing in the respond path) and writes the proposals in a commit step of its
``maintenance`` operation (no revision moves), under the story lock, where definitions are
matched again (``proposals.write``). With ``states.level`` off it writes nothing. It runs with
the job's ``story_id``, so an erasure block holds it at claim (#125).
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import states
from epistrel.core.enums import OperationKind
from epistrel.core.events.operation import Operation
from epistrel.core.states import proposals
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

STATE_PROPOSAL_KIND = proposals.ANALYSIS_KIND


async def _run(ctx: JobContext) -> JobOutput:
    payload = cast(proposals.AnalysisPayload, ctx.payload)
    story_id = payload.story_id
    async with ctx.snapshot() as conn:
        if await states.state_level(conn, story_id) is None:
            return JobOutput(result={"skipped": "module_off", "proposed": 0})
        candidates = await proposals.scan(conn, story_id)
    written: list[str] = []

    async def store(conn: AsyncConnection) -> None:
        written.extend(
            await proposals.write(conn, op_of[0], story_id, candidates, payload.operation_id)
        )

    op_of: list[Operation] = []

    def stage(op: Operation) -> None:
        op_of.append(op)
        op.add_commit_step(store)

    return JobOutput(
        stage=stage,
        result_of=lambda: {
            "candidates": len(candidates),
            "proposed": len(written),
            "request_operation_id": payload.operation_id,
        },
    )


STATE_PROPOSAL_HANDLER = JobHandler(
    kind=STATE_PROPOSAL_KIND,
    payload=proposals.AnalysisPayload,
    run=_run,
    operation_kind=OperationKind.MAINTENANCE,
)

__all__ = ["STATE_PROPOSAL_HANDLER", "STATE_PROPOSAL_KIND"]
