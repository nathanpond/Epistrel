"""Helpers for the minors-policy tests (#55): a REST client with the test-only guarded reply,
stories with and without a minor in the cast, and a stub guardrail model scripted to recognise
fixture phrases — only in checks of the age-conditioned policy (the subjects section is in no
other check), so another live global policy in the shared database never trips on them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.config import Settings
from epistrel.core.seeds.ratings import EXPLICIT_ID
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.engine import Engine
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.testing.ops import make_router

MINOR_NAME = "Maribelle Quist"
ADULT_NAME = "Jonah Brack"
MINOR_AGE = 16
SUBJECTS_HEADING = "## Subjects (label: true age)"
VIOLATION = '{"violation": true, "confidence": 0.97}'


@asynccontextmanager
async def guarded_client(
    settings: Settings, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, Engine]]:
    app = create_app(settings)
    include_protected(app, make_router())
    epistrel: Engine = app.state.epistrel
    epistrel.bind(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, epistrel


async def explicit_story(engine: AsyncEngine, *, minor: bool) -> str:
    """An Explicit story: an adult player character and a second character who is 16 — or,
    for the complement, the same cast with the second character an adult."""
    spec = StorySpec(
        name="R-07",
        content_rating_id=EXPLICIT_ID,
        characters=[
            CharacterSpec(name=ADULT_NAME, definition="A traveller.", true_age=34, is_player=True),
            CharacterSpec(
                name=MINOR_NAME,
                definition="A traveller's companion.",
                true_age=MINOR_AGE if minor else 31,
            ),
        ],
    )
    created = await create_story_from(engine, spec, principal="p-author")
    return created.story_id


PARTNER_NAME = "Ilse Varga"
ELSEWHERE = f"age {MINOR_AGE}, not present"
"""How a subjects section marks the minor authoritatively elsewhere (#161)."""
NO_VIOLATION = '{"violation": false, "confidence": 0.95}'


async def explicit_trio(engine: AsyncEngine) -> tuple[str, dict[str, str]]:
    """An Explicit story on the seeded ``Structured (M4)`` profile: the adult player
    character, an adult partner, and a 16-year-old (in that order, so the partner is the
    rendered character); its id and its character ids by name."""
    from tests.memory_support import use_structured

    spec = StorySpec(
        name="R-07 M5",
        content_rating_id=EXPLICIT_ID,
        characters=[
            CharacterSpec(name=ADULT_NAME, definition="A traveller.", true_age=34, is_player=True),
            CharacterSpec(name=PARTNER_NAME, definition="The traveller's wife.", true_age=31),
            CharacterSpec(name=MINOR_NAME, definition="Their neighbour's child.", true_age=16),
        ],
    )
    created = await create_story_from(engine, spec, principal="p-author")
    await use_structured(engine, created.story_id)
    return created.story_id, {c.name: c.id for c in created.characters}


def recognise_near(stub: StubAdapter, *phrases: str) -> None:
    """The stub guardrail model flags ``phrases`` as sexual content near the minor: a
    violation while the subjects show the minor present, none while they show the minor
    authoritatively elsewhere (#161)."""
    for phrase in phrases:
        stub.when((phrase, ELSEWHERE), NO_VIOLATION, purpose="guardrail_check")
        stub.when((phrase, SUBJECTS_HEADING), VIOLATION, purpose="guardrail_check")


def recognise(stub: StubAdapter, *phrases: str) -> None:
    """The stub guardrail model flags a text containing any of ``phrases`` — in checks of
    the age-conditioned policy only."""
    for phrase in phrases:
        stub.when((phrase, SUBJECTS_HEADING), VIOLATION, purpose="guardrail_check")


def subject_checks(stub: StubAdapter) -> list[str]:
    """The system message of every ``guardrail_check`` call that carried subjects."""
    return [
        call.messages[0].content
        for call in stub.calls
        if call.purpose == "guardrail_check" and SUBJECTS_HEADING in call.messages[0].content
    ]


__all__ = [
    "ADULT_NAME",
    "ELSEWHERE",
    "MINOR_AGE",
    "MINOR_NAME",
    "NO_VIOLATION",
    "PARTNER_NAME",
    "SUBJECTS_HEADING",
    "VIOLATION",
    "explicit_story",
    "explicit_trio",
    "guarded_client",
    "recognise",
    "recognise_near",
    "subject_checks",
]
