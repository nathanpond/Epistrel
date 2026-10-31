"""Shared setup for #212's staged-turn tests (I-01 M6, S-EFFECT-CRASH, S-EFFECT-PROSE,
S-CONDITIONAL-EFFECTS, OUTBOX-SAFETY, the lifecycle tests).

A staged turn is a structured turn played through ``respond`` with the stub adapter whose
render declares an effect on a value a ``synchronous`` write-capable in-process provider owns
(``sync_story``). The outcome step's narrator render is scripted with ``outcome_step`` (its
prompt alone carries ``staged.OUTCOME_STEP``), registered before ``es.narrator`` (the stub's
first matching rule wins).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.orchestrator.structured import staged
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.event_support import rows
from tests.respond_support import body, respond
from tests.story_support import override
from tests.structured_turn_support import beat, structured_story

REASON = "bought a sword"
TAKES = "Mira takes the coins."
COUNTS = "Cal has 300 gold."
WENT = "The purchase went through."
MODEL_CALLS = 40
"""Room for the outcome step's repair (#214 reserves the outcome render itself)."""


async def sync_story(
    client: Any,
    engine: AsyncEngine,
    epistrel: Any,
    *,
    default: Any = 300,
    **spec: Any,
) -> tuple[str, str, str, ps.ScriptedProvider]:
    """A story (Cal, Mira) whose gold a fresh synchronous write-capable in-process provider
    owns (``spec``'s fields over ``write_capable_spec``'s; it reads ``default``): its id, Cal's,
    the provider's name and its fake."""
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name, confirmation="synchronous", **spec))
    fake = ps.ScriptedProvider(name, default=default).attach(epistrel)
    story, ids = await structured_story(client, "Cal", "Mira")
    await override(client, story, {"budgets": {"model_calls": MODEL_CALLS}})
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    return story, ids["Cal"], name, fake


def purchase(cal: str, text: str = TAKES, *more: dict[str, Any]) -> str:
    """Mira's render: ``text``, declaring Cal's ``gold -50`` (and ``more`` effects)."""
    return es.render(
        beat(
            "do",
            text,
            effects=[es.effect("gold", scope_key=cal, delta=-50, reason=REASON), *more],
        )
    )


def outcome_step(stub: Any, *beats: dict[str, Any]) -> None:
    """The outcome step's narrator render answers ``beats``."""
    stub.when(staged.OUTCOME_STEP, es.render(*beats), purpose="generation")


def script_purchase(stub: Any, cal: str, outcome: str = WENT) -> None:
    """A purchase turn: Mira's attempt, the narrator's ``COUNTS``, the outcome step's
    ``outcome``."""
    stub.reset()
    outcome_step(stub, beat("narration", outcome))
    ms.renders_for(stub, "Mira", purchase(cal))
    es.narrator(stub, es.render(beat("narration", COUNTS)))


def buy_body(cal: str) -> dict[str, Any]:
    """The request body of Cal's purchase."""
    return body(cal, None, do="buys the sword")


async def buy(client: Any, story: str, cal: str) -> Any:
    """``respond`` for Cal's purchase (the answer, whatever its status)."""
    return await respond(client, story, buy_body(cal))


async def revision(engine: AsyncEngine, story: str) -> int:
    found = await rows(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)
    return int(found[0].timeline_revision)


async def staged_op(engine: AsyncEngine, story: str) -> Any:
    """The story's latest staged turn's operation row (``status``, ``turn_stage`` as text)."""
    found = await rows(
        engine,
        "SELECT operation_id, status::text AS status, turn_stage::text AS turn_stage, "
        "narrative_turn, timeline_revision, stage_log FROM operations WHERE story_id = :s "
        "AND turn_stage IS NOT NULL ORDER BY narrative_turn DESC LIMIT 1",
        s=story,
    )
    return found[0]


async def stage_names(engine: AsyncEngine, story: str) -> list[str]:
    """The staged turn's logged stages, in order (``stage_log``'s #212 entries)."""
    op = await staged_op(engine, story)
    return [e["stage"] for e in op.stage_log or [] if e.get("stage") in staged.STAGES]


async def event_types(engine: AsyncEngine, operation_id: str) -> list[str]:
    found = await rows(
        engine, "SELECT type FROM events WHERE operation_id = :o ORDER BY seq", o=operation_id
    )
    return [r.type for r in found]


__all__ = [
    "COUNTS",
    "MODEL_CALLS",
    "REASON",
    "TAKES",
    "WENT",
    "buy",
    "buy_body",
    "event_types",
    "outcome_step",
    "purchase",
    "revision",
    "script_purchase",
    "stage_names",
    "staged_op",
    "sync_story",
]
