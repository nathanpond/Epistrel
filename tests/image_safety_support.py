"""Helpers for the image prompt check tests (#257): the whisper scene's cast as image subjects,
stories over it, the stub guardrail model scripted for the minors policy and the figure
classification, and the real prompt check installed with scripted output checks.

**The whisper scene.** Bobby (the player, 30) and Jenni (29), Marcus (34), and, in the
``minors`` variant, Lily (17, who looks older) and Wren (16). Each definition carries a marker
(``STORY_TEXT``) that no safety call may ever receive. Under Lite every character is present;
``with_rooms`` puts the story on per-character partitions with perception ``full`` and places the
adults in the cabin and the minors in the barn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.seeds.ratings import EXPLICIT_ID
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.safety_checks import PromptChecks
from epistrel.orchestrator.models.registry import FIGURES_TEMPLATE
from epistrel.orchestrator.models.stub import StubAdapter, StubCall
from epistrel.testing.images import ScriptedChecks
from tests import perception_support as ps
from tests.image_support import story_origin
from tests.memory_support import use_structured
from tests.minors_support import SUBJECTS_HEADING, VIOLATION

BOBBY, JENNI, MARCUS, LILY, WREN = "Bobby", "Jenni", "Marcus", "Lily", "Wren"
MARCUS_AGE, LILY_AGE, WREN_AGE = 34, 17, 16
FIGURES_HEADING = "## Listed subjects (label: how the text names them)"
"""The figure classification's subjects heading (``guardrail_figures.j2``)."""
STORY_TEXT = "QUILLMARK"
"""In every character's definition: story text no safety payload may carry."""
ADULTS = (
    CharacterSpec(name=BOBBY, definition=f"A {STORY_TEXT} carpenter.", true_age=30, is_player=True),
    CharacterSpec(name=JENNI, definition=f"A {STORY_TEXT} singer with a secret.", true_age=29),
    CharacterSpec(
        name=MARCUS, definition=f"A {STORY_TEXT} sailor, Jenni's lover.", true_age=MARCUS_AGE
    ),
)
MINORS = (
    CharacterSpec(
        name=LILY,
        definition=f"Marcus's {STORY_TEXT} niece; she looks older than she is.",
        true_age=LILY_AGE,
    ),
    CharacterSpec(name=WREN, definition=f"The {STORY_TEXT} neighbour's son.", true_age=WREN_AGE),
)
ROOM_ADULTS = (BOBBY, JENNI, MARCUS)
ROOM_MINORS = (LILY, WREN)


async def whisper_story(engine: AsyncEngine, *, minors: bool = True) -> tuple[str, dict[str, str]]:
    """An Explicit story over the whisper scene's cast (with Lily and Wren unless ``minors`` is
    false); its id and its character ids by name."""
    spec = StorySpec(
        name="The whisper scene",
        content_rating_id=EXPLICIT_ID,
        characters=[*ADULTS, *(MINORS if minors else ())],
    )
    created = await create_story_from(engine, spec, principal="p-author")
    return created.story_id, {c.name: c.id for c in created.characters}


async def with_rooms(engine: AsyncEngine, story_id: str, ids: Mapping[str, str]) -> dict[str, str]:
    """The story on the structured profile with perception ``full``: the adults in the cabin and
    the minors in the barn; the place ids by name."""
    await use_structured(engine, story_id)
    await ps.perceive(engine, story_id, "full")
    places = {
        "cabin": await ps.place_entity(engine, story_id, "cabin"),
        "barn": await ps.place_entity(engine, story_id, "barn"),
    }
    for name in ROOM_ADULTS:
        await ps.put(engine, story_id, ids[name], places["cabin"])
    for name in ROOM_MINORS:
        if name in ids:
            await ps.put(engine, story_id, ids[name], places["barn"])
    return places


async def next_turn(engine: AsyncEngine, story_id: str) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT next_turn_index FROM story_heads WHERE story_id = :s"), {"s": story_id}
        )
    return int(found or 1)


async def origin(engine: AsyncEngine, story_id: str, **fields: Any) -> dict[str, Any]:
    """A story image's origin at the story's next turn (its end: every beat's moves)."""
    return story_origin(story_id, turn=await next_turn(engine, story_id), **fields)


def recognise_sexual(stub: StubAdapter, *phrases: str) -> None:
    """The stub guardrail model flags ``phrases`` in the minors policy's checks only (the
    Subjects section appears in no other check)."""
    for phrase in phrases:
        stub.when((phrase, SUBJECTS_HEADING), VIOLATION, purpose="guardrail_check")


def figures(stub: StubAdapter, phrase: str, *found: tuple[str, str], **descriptors: Any) -> None:
    """The figure classification answers ``found`` (``(descriptor, classification)``) for a
    request containing ``phrase``, and ``descriptors`` (label → list) for its subjects."""
    answer = {
        "figures": [{"descriptor": d, "classification": c} for d, c in found],
        "subjects": [
            {"label": label, "visual_descriptors": list(value)}
            for label, value in descriptors.items()
        ],
    }
    stub.when((phrase,), json.dumps(answer), purpose=FIGURES_TEMPLATE)
    # Once the endpoint is on parse-validate-retry (ADR-180's memo, after a fault or garbage)
    # the call carries no response schema, so the stub keys it by its purpose: match the
    # figures template's own heading there too.
    stub.when((phrase, FIGURES_HEADING), json.dumps(answer), purpose="guardrail_check")


def safety_calls(stub: StubAdapter) -> list[StubCall]:
    """Every ``guardrail_check`` call: the figure classification and the policy checks."""
    return [call for call in stub.calls if call.purpose == "guardrail_check"]


def payload(call: StubCall) -> str:
    """Everything a call sent the model, as one text."""
    return "\n".join(message.content for message in call.messages)


@contextmanager
def prompt_checked(output: ScriptedChecks | None = None) -> Iterator[ScriptedChecks]:
    """#257's real prompt check, with scripted output checks (passing, unless given)."""
    scripted = output or ScriptedChecks(fail_closed=False)
    image_jobs.set_image_checks(PromptChecks(scripted))
    try:
        yield scripted
    finally:
        image_jobs.set_image_checks(None)


async def jobs_of(engine: AsyncEngine, story_id: str) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(
                text("SELECT count(*) FROM image_jobs WHERE story_id = :s"), {"s": story_id}
            )
            or 0
        )


async def refusals(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's ``image_prompt_rejected`` audit rows."""
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT principal, policy_ids, details FROM audit_log "
                        "WHERE kind = 'image_prompt_rejected' AND story_id = :s ORDER BY at"
                    ),
                    {"s": story_id},
                )
            ).all()
        )


def subject(character_id: str, *descriptors: str, depicted: bool = True) -> dict[str, Any]:
    """A depicted subject as the prompt builder declares one (#259)."""
    return {
        "character_id": character_id,
        "visual_descriptors": list(descriptors),
        "depicted": depicted,
    }


def labels_with_ages(context: Mapping[str, Any]) -> list[tuple[str, int | None, str | None]]:
    return [(s["label"], s["true_age"], s["classification"]) for s in context.get("subjects") or ()]


def names_in(value: str, names: Sequence[str]) -> list[str]:
    return [name for name in names if name in value]


_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def assert_no_age(value: Any, age: int, path: str = "$") -> None:
    """``value`` (a dumped view: dicts, lists, scalars) carries ``age`` nowhere. No key names
    an age (``age`` as an ``_``-separated token), no number equals it, and no string contains
    its digits. ULIDs and ``datetime`` values are skipped: they are random, so they can hold
    the digits by chance, and they carry no age (#791)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            assert "age" not in str(key).lower().split("_"), f"{path}: key {key!r}"
            assert_no_age(item, age, f"{path}.{key}")
    elif isinstance(value, list | tuple | set | frozenset):
        for n, item in enumerate(value):
            assert_no_age(item, age, f"{path}[{n}]")
    elif isinstance(value, datetime):
        return
    elif isinstance(value, str):
        if not _ULID.fullmatch(value):
            assert str(age) not in value, f"{path}: {value!r}"
    elif isinstance(value, int | float) and not isinstance(value, bool):
        assert value != age, f"{path}: {value!r}"
    else:
        assert str(age) not in str(value), f"{path}: {value!r}"


__all__ = [
    "ADULTS",
    "BOBBY",
    "JENNI",
    "LILY",
    "LILY_AGE",
    "MARCUS",
    "MARCUS_AGE",
    "MINORS",
    "STORY_TEXT",
    "WREN",
    "WREN_AGE",
    "assert_no_age",
    "figures",
    "jobs_of",
    "labels_with_ages",
    "names_in",
    "next_turn",
    "origin",
    "payload",
    "prompt_checked",
    "recognise_sexual",
    "refusals",
    "safety_calls",
    "subject",
    "whisper_story",
    "with_rooms",
]
