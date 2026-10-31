"""A staged turn played by an engine in a child process (#213's crash tests: S-EFFECT-CRASH,
X-03 M6).

The child (``epistrel.testing.faults.EngineProcess``) runs on its own scratch database with
the stub adapter, scripted with ``StubRule``s (``purchase_rules``: Mira's render declares Cal's
``gold -50``, the narrator's line, the outcome step's narration) and the ``states`` module
delivered. Its provider is an ``HttpManifestProvider`` pointed at a ``provider_support.
GameServer`` in the test process (the loopback is allowed through the deployment's
``provider_allowed_hosts``, read per request from the shared database), so the purchases a
crash interrupts are counted where the kill cannot reach them. The setup writes through the
child's REST surface and straight through Core on the same database (``db``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.orchestrator.structured import staged
from epistrel.testing.faults import EngineProcess, StubRule, StubRules
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as pc
from tests import provider_support as ps
from tests import staged_support as sp
from tests import state_support as ss
from tests.event_support import rows
from tests.idempotency_support import KEY
from tests.respond_support import body
from tests.story_support import WRITE, override
from tests.structured_turn_support import beat, structured_story

CONFIRM_WAIT_S = 30
"""The crash tests' providers' ``confirm_wait_max_seconds``. The wait ends as soon as every
synchronous row has an answer, so it costs nothing when the send is prompt; it only has to
outlast a resumed send on a loaded host. A wait that lapses first completes the turn with the
purchase still pending (FR-EXT-15; the lease timer re-sends it a minute later), which the
tests would misread as the resume never sending (seen once at 1 s: #213's ``staged.after_claim``
case under a 23-minute sharded run)."""
GARBAGE = "<<not a render>>"
"""An outcome-step render answer no attempt can parse (the render fails every attempt)."""


@dataclass(frozen=True)
class Staged:
    """A story set up for a staged purchase: its id, Cal's, the provider's name and the turn
    operations its setup committed (``staged_ops`` leaves them out)."""

    story: str
    cal: str
    provider: str
    setup: frozenset[str] = frozenset()


PERCEIVED = {"character_knowledge": {"partitions": True}, "perception": {"level": "full"}}
"""Overrides routing every beat per perceiver (#161; ``http_story(perceived=True)``)."""


async def http_story(
    client: httpx.AsyncClient,
    db: AsyncEngine,
    server: ps.GameServer,
    *,
    wait_s: int = CONFIRM_WAIT_S,
    perceived: bool = False,
) -> Staged:
    """A story (Cal, Mira) whose gold ``server`` owns through a synchronous HTTP provider
    (deduplicating as the server does, ``confirm_wait_max_seconds`` ``wait_s``). ``perceived``:
    routed per perceiver at perception ``full``, Cal and Mira both at the forge."""
    name = ps.unique_name("game")
    await ps.register(db, server.spec(name, confirm_wait_max_seconds=wait_s))
    story, ids = await structured_story(client, "Cal", "Mira")
    settings: dict[str, Any] = {"budgets": {"model_calls": sp.MODEL_CALLS}}
    await override(client, story, {**settings, **(PERCEIVED if perceived else {})})
    if perceived:
        forge = await pc.place_entity(db, story, "the forge")
        await pc.put(db, story, ids["Cal"], forge)
        await pc.put(db, story, ids["Mira"], forge)
    await ss.set_level(db, story, "on")
    await ps.external_gold(db, story, name, authorization=es.ANY_CHARACTER)
    return await settled(db, Staged(story, ids["Cal"], name))


async def settled(db: AsyncEngine, story: Staged) -> Staged:
    """``story`` with the turn operations committed so far as its setup."""
    found = await rows(
        db,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn'",
        s=story.story,
    )
    return Staged(story.story, story.cal, story.provider, frozenset(r.operation_id for r in found))


def purchase_rules(cal: str, *, outcome: str = sp.WENT, failing: bool = False) -> list[StubRule]:
    """The child's stub for a purchase turn: Mira's attempt (Cal's ``gold -50``), the narrator's
    ``COUNTS``, and the outcome step's ``outcome`` (``failing``: an answer no attempt parses)."""
    rules = StubRules()
    if failing:
        rules.when(staged.OUTCOME_STEP, GARBAGE, purpose="generation")
    else:
        sp.outcome_step(rules, beat("narration", outcome))
    ms.renders_for(rules, "Mira", sp.purchase(cal))  # type: ignore[arg-type]
    es.narrator(rules, es.render(beat("narration", sp.COUNTS)))  # type: ignore[arg-type]
    return rules.rules


def plain_rules() -> list[StubRule]:
    """The child's stub for an ordinary structured turn (no effect)."""
    rules = StubRules()
    ms.renders_for(rules, "Mira", es.render(beat("say", "Good evening.")))  # type: ignore[arg-type]
    es.narrator(rules, es.render(beat("narration", "The fire crackles.")))  # type: ignore[arg-type]
    return rules.rules


async def buy(child: EngineProcess, story: Staged, key: str) -> httpx.Response | None:
    """Cal's purchase under ``key``; ``None`` when the child died of its crash point."""
    try:
        async with httpx.AsyncClient(base_url=child.base_url, timeout=90) as client:
            return await client.post(
                f"/stories/{story.story}/respond",
                json=body(story.cal, None, do="buys the sword"),
                headers={**WRITE, KEY: key},
            )
    except httpx.HTTPError:
        return None


async def staged_ops(db: AsyncEngine, story: Staged) -> list[Any]:
    """The story's turn operations since its setup: status, ``turn_stage`` and ``stage_log``."""
    found = await rows(
        db,
        "SELECT operation_id, status::text AS status, turn_stage::text AS turn_stage, "
        "stage_log, timeline_revision FROM operations WHERE story_id = :s AND kind = 'turn' "
        "ORDER BY started_at",
        s=story.story,
    )
    return [r for r in found if r.operation_id not in story.setup]


def logged(op: Any) -> list[str]:
    """The stages a staged turn's ``stage_log`` recorded, in order."""
    return [e["stage"] for e in op.stage_log or [] if e.get("stage") in staged.STAGES]


__all__ = [
    "CONFIRM_WAIT_S",
    "GARBAGE",
    "PERCEIVED",
    "Staged",
    "buy",
    "http_story",
    "logged",
    "plain_rules",
    "purchase_rules",
    "settled",
    "staged_ops",
]
