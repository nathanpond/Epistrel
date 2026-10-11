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
    "MINOR_AGE",
    "MINOR_NAME",
    "SUBJECTS_HEADING",
    "VIOLATION",
    "explicit_story",
    "guarded_client",
    "recognise",
    "subject_checks",
]
