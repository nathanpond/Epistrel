"""Helpers for #155's escalation tests: the camp turn of S-TENNIS's variant 4 without its rule
(``uncertain(world_knowledge)``, so the model residual decides it), reads of the
``escalations`` rows, scripted rows for a shadow window, and gate cleanup."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx2 as httpx
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.tables.escalations import escalations, gate_thresholds
from epistrel.core.verify.yield_queries import path_of_shape
from tests.event_support import rows, scalar
from tests.fixtures.bobby_education import Education, bobby_education
from tests.respond_support import body
from tests.story_support import READ, WRITE
from tests.structured_turn_support import beat, claim, render

CAMP_LINE = "I learned at a summer tennis camp at Florida."
ELSEWHERE = "I learned at a summer tennis camp back in Ohio."
TEST_SHAPE = "eval|e02|test|affirmed"
"""A dedicated shape no real predicate has (its layer is not one of the registry's)."""


def camp_render() -> str:
    return render(
        beat(
            "say",
            CAMP_LINE,
            intent="sincere",
            propositions=[claim("I", "attended_program", "Florida")],
        )
    )


def entailment(verdict: str, confidence: float = 0.9) -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence})


def regenerated(text_: str = ELSEWHERE) -> str:
    return json.dumps({"text": text_, "propositions": []})


async def camp_story(client: httpx.AsyncClient, engine: AsyncEngine) -> Education:
    """Variant 4's world without the presence rule: the camp claim is ``uncertain``."""
    return await bobby_education(client, engine, never_in_florida=True)


async def play_camp(
    client: httpx.AsyncClient, edu: Education, viewpoint: str | None = None
) -> dict[str, Any]:
    """Cal asks where Bobby learned tennis (``viewpoint``: the turn's output viewpoint)."""
    response = await client.post(
        f"/stories/{edu.story_id}/respond",
        json=body(edu.cal, "Where did you learn tennis?"),
        params={} if viewpoint is None else {"viewpoint": viewpoint},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    played: dict[str, Any] = response.json()
    return played


async def escalation_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT *, pass AS pass_ FROM escalations WHERE story_id = :s "
        "ORDER BY narrative_turn, pass, claim_id",
        s=story_id,
    )


async def turn_operation(engine: AsyncEngine, story_id: str) -> str:
    """The latest committed turn's operation id."""
    found = await scalar(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY narrative_turn DESC, turn_variant DESC LIMIT 1",
        s=story_id,
    )
    return str(found)


async def any_operation(engine: AsyncEngine, story_id: str) -> str:
    """Any committed operation of the story (its creation will do), for scripted rows."""
    found = await scalar(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND status = 'committed' "
        "ORDER BY started_at LIMIT 1",
        s=story_id,
    )
    return str(found)


async def window_rows(
    engine: AsyncEngine,
    story_id: str,
    operation_id: str,
    shape: str,
    *,
    cases: int,
    agreed: int,
    proposed: float = 0.25,
) -> None:
    """``cases`` escalations of ``shape`` the proposal would have skipped, ``agreed`` of them
    agreeing with the model (the model's ``novel`` left them ``uncertain``), the rest not (the
    model's ``contradicted`` was taken)."""
    made = []
    for n in range(cases):
        agrees = n < agreed
        made.append(
            {
                "id": new_ulid(),
                "story_id": story_id,
                "operation_id": operation_id,
                "claim_id": f"{operation_id}:{n}",
                "pass": "render",
                "gate_reason": "world_knowledge",
                "predicate_shape": shape,
                "deterministic_confidence": 0.5,
                "threshold": 1.0,
                "called": True,
                "model_verdict": "novel" if agrees else "contradicted",
                "model_confidence": 0.9,
                "flipped": not agrees,
                "final_kind": "uncertain" if agrees else "contradicted",
                "final_disposition": "repair" if agrees else None,
                "consequence": "none",
                "shadow_threshold": proposed,
                "shadow_decision": False,
                "shadow_agreed": agrees,
            }
        )
    async with engine.begin() as conn:
        await insert_derived(conn, escalations, made)


async def clear_gates(engine: AsyncEngine, shapes: Sequence[str] | None = None) -> None:
    """Remove gate thresholds (every one, or ``shapes``) so no test leaves a tightening."""
    async with engine.begin() as conn:
        query = delete(gate_thresholds)
        if shapes is not None:
            query = query.where(gate_thresholds.c.shape.in_(list(shapes)))
        await conn.execute(query)


async def put_gate(
    client: httpx.AsyncClient,
    shape: str,
    threshold: float,
    mode: str = "shadow",
    **extra: Any,
) -> httpx.Response:
    return await client.put(
        "/admin/verify/gate",
        json={"shape": shape, "threshold": threshold, "mode": mode, **extra},
        headers=WRITE,
    )


async def promote_gate(client: httpx.AsyncClient, shape: str) -> httpx.Response:
    return await client.post(f"/admin/verify/gate/{path_of_shape(shape)}/promote", headers=WRITE)


async def remove_gate(client: httpx.AsyncClient, shape: str) -> httpx.Response:
    return await client.delete(f"/admin/verify/gate/{path_of_shape(shape)}", headers=WRITE)


async def yield_report(client: httpx.AsyncClient, **params: str) -> dict[str, Any]:
    response = await client.get("/admin/evals/escalation-yield", params=params, headers=READ)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def database_now(engine: AsyncEngine) -> Any:
    """The database's current time, which stamps ``committed_at`` (a window from it holds no
    earlier test's turn)."""
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()


async def count_rows(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


__all__ = [
    "CAMP_LINE",
    "ELSEWHERE",
    "TEST_SHAPE",
    "any_operation",
    "camp_render",
    "camp_story",
    "clear_gates",
    "count_rows",
    "database_now",
    "entailment",
    "escalation_rows",
    "play_camp",
    "promote_gate",
    "put_gate",
    "regenerated",
    "remove_gate",
    "turn_operation",
    "window_rows",
    "yield_report",
]
