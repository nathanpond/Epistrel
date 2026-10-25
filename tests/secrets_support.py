"""Helpers for the private-content tests (#172): a structured story under per-character
partitions with everyone on one street, a secret only one character's partition holds, and one
scripted render played through ``respond`` with what it repaired."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.orchestrator.models.stub import StubAdapter
from tests import modeling_support as ms
from tests import partition_support as parts
from tests.respond_support import body, respond
from tests.structured_turn_support import render, turn_report


async def secret_story(
    client: httpx.AsyncClient, engine: AsyncEngine, *names: str
) -> tuple[str, dict[str, str]]:
    """A structured story (the first name the player) under partitions at perception ``full``,
    everyone on the street; its id and the character ids by name."""
    story, ids, _ = await ms.street(client, engine, *names, salience=None)
    return story, ids


async def secret(
    engine: AsyncEngine, story_id: str, holder: str, subject: str, relation: str, obj: str
) -> tuple[str, str]:
    """``holder`` alone believes ``subject <relation> <obj>``, ``obj`` a new person entity
    named nowhere else; the entity's id and the belief's."""
    async with engine.begin() as conn:
        entity = await store.add_entity(conn, story_id, "person", obj)
    proposition = Proposition(
        subject_id=subject,
        predicate_id=CORE_BY_NAME[relation].id,
        object_id=entity,
        valid_time=Interval.of(None, None),
    )
    belief = await parts.believe(engine, story_id, holder, proposition)
    return entity, belief.id


@dataclass(frozen=True, slots=True)
class Played:
    """One scripted turn: the response and the turn report."""

    answer: dict[str, Any]
    report: dict[str, Any]

    @property
    def message(self) -> str:
        return str(self.answer["message"]["text"])

    @property
    def tags(self) -> list[str]:
        return [r["tag"] for r in self.report.get("repairs", [])]

    def protected_refs(self) -> list[str]:
        """The protected references the detection of the turn's renders hit
        (``turn_report.detection``; the composed message's, as the viewpoint reads it, aside)."""
        claims = self.report.get("detection", {}).get("claims", [])
        return [
            c["protected_ref"]
            for c in claims
            if c.get("protected_ref") and c["span"][0] is not None
        ]

    @property
    def denied(self) -> list[str]:
        return [d["reason"] for d in self.report.get("verify", {}).get("denied", [])]


async def play(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    stub: StubAdapter,
    story_id: str,
    actor: str,
    character: str,
    *beats: dict[str, Any],
    say: str = "Hi.",
) -> Played:
    """``actor`` says ``say`` and ``character`` renders ``beats`` (the stub's scripted render)."""
    stub.reset()
    stub.script("generation", [render(*beats)])
    found = await respond(client, story_id, body(actor, say, render_character_ids=[character]))
    assert found.status_code == 200, found.text
    return Played(found.json(), await turn_report(engine, story_id))


__all__ = ["Played", "play", "secret", "secret_story"]
