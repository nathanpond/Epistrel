"""Helpers for the time-belief tests (#200): a story running the clock with per-character
partitions and ``clock.time_claims`` on (``Scene``), a character's own claim checked as
``verify_detected`` checks it (``Scene.say``), a narration checked objectively
(``Scene.narrate``), a timepiece read (``Scene.observe``), a played turn (``play``) and what a
character was given of a turn (``perceived``)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.epistemic import transitions
from epistrel.core.membership import Position
from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    LiteralKind,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.vocabulary import (
    CURRENT_TIME,
    ELAPSED_SINCE,
    SCHEDULED_STATUS,
    TIME_IS,
    TIME_OF_DAY_BAND,
)
from epistrel.core.readset import StructuredReadSet
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT
from epistrel.core.verify.permission import Asserter
from epistrel.core.world_clock import time_belief
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.verify.verifier import (
    ClaimRef,
    Verifier,
    VerifyResult,
    story_verifier,
)
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import partition_support as pt
from tests import perception_support as ps
from tests.event_support import rows, scalar
from tests.respond_support import body, respond
from tests.story_support import WRITE
from tests.structured_turn_support import beat, structured_story


def hhmm_minute(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass
class Scene:
    """A story running the clock (``manual``: only the test moves it) anchored to the minute at
    ``start`` (``None``: unanchored), with per-character partitions, every character at one
    place and the profile's ``clock.time_claims`` on (#200)."""

    client: httpx.AsyncClient
    engine: AsyncEngine
    story: str
    ids: dict[str, str]
    place: str
    start: int
    zero: int
    clock: str

    @classmethod
    async def open(
        cls,
        client: httpx.AsyncClient,
        engine: AsyncEngine,
        start: str | None,
        *names: str,
        awareness: str = "exact",
        testimony: str = "off",
        mode: str = "manual",
        stops: bool = False,
        time_claims: bool = True,
    ) -> Scene:
        """The scene at ``start`` (``mode`` ``manual``: only the test moves the clock), its
        characters' ``time_awareness`` ``awareness``; ``testimony`` the profile's #162 level;
        ``time_claims`` the profile's ``clock.time_claims``."""
        story, ids = await structured_story(client, "Cal", *names)
        place = await ps.place_entity(engine, story, "the diner")
        for who in ids.values():
            await ps.put(engine, story, who, place)
        await ck.set_clock(engine, story, "on", stops=stops, time_claims=time_claims)
        # ``time_awareness`` through #193's PATCH (an interpretive change).
        manual = await client.patch(
            f"/stories/{story}/clock",
            json={"mode": mode, "time_awareness": awareness},
            headers=WRITE,
        )
        assert manual.status_code == 200, manual.text
        if start is not None:
            await ck.anchor_at(engine, story, hhmm_minute(start))
        await ms.per_character(
            engine,
            story,
            character_knowledge={"partitions": True, "testimony": testimony},
            perception={"level": "co_location"},
        )
        (row,) = await rows(
            engine,
            "SELECT s.story_minute_zero AS zero, d.id AS clock FROM stories s JOIN "
            "state_definitions d ON d.story_id = s.id AND d.name = 'world_clock' "
            "WHERE s.id = :s",
            s=story,
        )
        begin = 0 if start is None else hhmm_minute(start)
        return cls(client, engine, story, ids, place, begin, int(row.zero), str(row.clock))

    async def at(self, hhmm: str) -> None:
        """Move the clock forward to ``hhmm`` (a consumer's advance between turns)."""
        offset = hhmm_minute(hhmm) - self.start
        if offset > await ck.offset(self.engine, self.story):
            await ck.advance(self.engine, self.story, to=offset)

    async def now(self) -> int:
        return self.zero + await ck.offset(self.engine, self.story)

    async def time(self, said: str) -> Proposition:
        return time_claim(self.place, said, await self.now())

    async def event(self, name: str, hhmm: str, who: str) -> str:
        """A canon scheduled event ``who`` holds: its entity (what a status names)."""
        created = await self.client.post(
            f"/stories/{self.story}/scheduled-events",
            json={
                "proposition": name,
                "planned": hhmm_minute(hhmm) - self.start,
                "participants": [self.ids[who]],
                "owner": "world",
            },
            headers=WRITE,
        )
        assert created.status_code == 201, created.text
        event = created.json()["event"]["event_id"]
        (row,) = await rows(
            self.engine, "SELECT object_id FROM propositions WHERE id = :p", p=event
        )
        return str(row.object_id)

    async def status(self, entity: str, status: str) -> Proposition:
        return Proposition(
            subject_id=entity,
            predicate_id=SCHEDULED_STATUS,
            literal=TypedLiteral(kind=LiteralKind.STR, value=status),
            valid_time=Instant.of(await self.now(), Granularity.MINUTE),
        )

    async def elapsed(self, since: str, minutes: int) -> Proposition:
        return Proposition(
            subject_id=self.place,
            predicate_id=ELAPSED_SINCE,
            qualifiers={"since": self.zero + hhmm_minute(since) - self.start},
            literal=TypedLiteral(kind=LiteralKind.DURATION, value=minutes),
            valid_time=Instant.of(await self.now(), Granularity.MINUTE),
        )

    async def observe(self, who: str, read: str) -> None:
        """``who`` reads ``read`` off a timepiece now: an ``observation`` of ``current_time``
        through ``transition()``, citing its act."""
        await pt.transit(
            self.engine,
            self.story,
            self.ids[who],
            Proposition(
                subject_id=self.clock,
                predicate_id=CURRENT_TIME,
                literal=TypedLiteral(kind=LiteralKind.STR, value=read),
                valid_time=Instant.of(await self.now(), Granularity.MINUTE),
            ),
            cause=transitions.Kind.OBSERVATION,
        )

    async def verifier(self, conn: AsyncConnection) -> Verifier:
        return await story_verifier(
            conn, StructuredReadSet(), self.story, models=None, budget=TurnBudget(deadline_s=30)
        )

    async def turn(self) -> int:
        found = await scalar(
            self.engine, "SELECT next_turn_index FROM story_heads WHERE story_id = :s", s=self.story
        )
        return int(found)

    async def say(
        self, who: str, claim: Proposition, *, channel: str = "say", intent: str | None = None
    ) -> tuple[VerifyResult, Verifier]:
        """``who``'s own claim with its belief dimension (as ``verify_detected`` checks a
        character's time claim with ``time_claims`` on)."""
        speaker = self.ids[who]
        async with self.engine.connect() as conn:
            verifier = await self.verifier(conn)
            found = await verifier.verify(
                claim,
                f"{CHARACTER_VIEWPOINT_PREFIX}{speaker}",
                (await self.turn(), 1),
                ref=ClaimRef(claim_id="c:1", beat=1, channel=channel),
                asserter=Asserter("character", speaker),
                believer=speaker,
                intent=intent,
            )
        assert found is not None
        return found, verifier

    async def narrate(self, claim: Proposition) -> tuple[VerifyResult, Verifier]:
        async with self.engine.connect() as conn:
            verifier = await self.verifier(conn)
            found = await verifier.verify(
                claim,
                OMNISCIENT,
                (await self.turn(), 1),
                ref=ClaimRef(claim_id="c:1", beat=1, channel="narration"),
                asserter=Asserter("director"),
            )
        assert found is not None
        return found, verifier

    async def belief(self, who: str) -> time_belief.TimeBelief:
        async with self.engine.connect() as conn:
            found = await time_belief.time_belief(
                conn, self.story, self.ids[who], Position(await self.turn())
            )
        assert found is not None
        return found


def time_claim(place: str, said: str, minute: int, *, band: bool = False) -> Proposition:
    return Proposition(
        subject_id=place,
        predicate_id=TIME_OF_DAY_BAND if band else TIME_IS,
        literal=TypedLiteral(kind=LiteralKind.STR, value=said),
        valid_time=Instant.of(minute, Granularity.MINUTE),
    )


def classified(result: VerifyResult) -> tuple[str, str, bool]:
    """``(classification, belief relation, repaired)``."""
    assert result.speech is not None
    decision = result.speech.decision
    return decision.classification, decision.belief_relation, result.needs_repair


async def play(
    scene: Scene,
    stub: Any,
    narration: str | None,
    who: str,
    *beats: dict[str, Any],
    say: str | None = None,
    do: str | None = "looks around",
    regenerated: str | None = None,
) -> dict[str, Any]:
    """One turn: the player (Cal) says ``say`` and ``do``es ``do``, the narrator narrates
    ``narration`` (when given) and ``who``'s render answers ``beats`` (a regeneration answers
    ``regenerated``); the response."""
    stub.reset()
    if narration is not None:
        es.narrator(stub, es.render(beat("narration", narration)))
    ms.renders_for(stub, who, es.render(*beats))
    if regenerated is not None:
        stub.script("regeneration", [json.dumps({"text": regenerated})])
    fields: dict[str, Any] = {"render_character_ids": [scene.ids[who]]}
    if do is not None:
        fields["do"] = do
    answer = await respond(scene.client, scene.story, body(scene.ids["Cal"], say, **fields))
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


def prompt_of(stub: Any, marker: str) -> str:
    """The latest ``generation`` prompt holding ``marker`` (``"## You are Bobby\\n"``, the
    narrator's ``es.NARRATOR_MARKER``)."""
    found = [
        "\n".join(m.content for m in c.messages)
        for c in stub.calls
        if c.purpose == "generation" and any(marker in m.content for m in c.messages)
    ]
    assert found, marker
    return found[-1]


async def perceived(scene: Scene, who: str, turn: int) -> dict[str, int]:
    """What ``who`` was given of ``turn``: its memories, belief transitions and exposures."""
    holder = scene.ids[who]
    found = {}
    for table, column, at in (
        ("memories", "holder_character_id", "narrative_turn"),
        ("belief_transitions", "holder_character_id", "position_turn"),
        ("exposures", "witness_character_id", "position_turn"),
    ):
        found[table] = int(
            await scalar(
                scene.engine,
                f"SELECT count(*) FROM {table} WHERE story_id = :s AND {column} = :h "  # noqa: S608
                f"AND {at} = :t",
                s=scene.story,
                h=holder,
                t=turn,
            )
        )
    return found
