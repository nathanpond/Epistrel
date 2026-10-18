"""Helpers for the structured-turn tests (#150): a story on the seeded ``Structured (M4)``
profile, scripted beat renders for the stub, and reads of what a structured turn stored."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.orchestrator.models.stub import StubAdapter, StubCall
from epistrel.testing.profiles import structured_m4
from tests.canon_support import commit
from tests.event_support import rows, scalar
from tests.story_support import READ, WRITE, create, story_body

AGE = CORE_BY_NAME["age"].id
LOCATED_AT = CORE_BY_NAME["located_at"].id


async def structured_story(
    client: httpx.AsyncClient, *names: str, **fields: Any
) -> tuple[str, dict[str, str]]:
    """A new story (the first name is the player) assigned ``Structured (M4)`` before any turn;
    its id and its character ids by name."""
    created = await create(client, story_body(*(names or ("Wren", "Ada")), **fields))
    story = created["story_id"]
    response = await client.put(
        f"/stories/{story}/engine-profile",
        json={"profile_id": structured_m4(), "ignore_warnings": True},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    return story, {c["name"]: c["id"] for c in created["characters"]}


def beat(kind: str, text: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "text": text, **fields}


def claim(subject: str, relation: str, obj: str | None = None, **fields: Any) -> dict[str, Any]:
    return {"subject": subject, "relation": relation, "object": obj, **fields}


def render(*beats: dict[str, Any], introduces: Sequence[dict[str, Any]] = ()) -> str:
    """A scripted beat render (the ``generation`` answer of one render)."""
    found: dict[str, Any] = {"beats": list(beats)}
    if introduces:
        found["introduces"] = list(introduces)
    return json.dumps(found)


def ages(**by_name: int) -> str:
    """A scripted age-repair answer."""
    return json.dumps({"ages": [{"name": n, "age": a} for n, a in by_name.items()]})


def renders_of(stub: StubAdapter, variant_marker: str = "## Your part") -> list[StubCall]:
    """The stub's structured ``generation`` calls (beat renders and age repairs)."""
    return [
        c
        for c in stub.calls
        if c.purpose == "generation"
        and c.response_schema is not None
        and any(variant_marker in m.content for m in c.messages)
    ]


def schema_calls(stub: StubAdapter, name: str) -> list[StubCall]:
    """The ``generation`` calls whose ``response_schema`` was ``name``."""
    return [c for c in stub.calls if c.purpose == "generation" and c.response_schema == name]


def text_of(call: StubCall) -> str:
    return "\n".join(m.content for m in call.messages)


async def place(engine: AsyncEngine, story_id: str, name: str) -> str:
    """A place entity of the story."""
    async with engine.begin() as conn:
        return await store.add_entity(conn, story_id, "place", name)


async def age_fact(engine: AsyncEngine, story_id: str, character_id: str, years: int) -> None:
    """The author's canon: ``character_id`` is ``years`` old, for all story-time."""
    await commit(
        engine,
        story_id,
        Proposition(
            subject_id=character_id,
            predicate_id=AGE,
            literal=TypedLiteral(kind=LiteralKind.INT, value=years),
            valid_time=Interval.of(None, None),
        ),
        kind=OperationKind.CONTROL,
        provenance="author",
    )


LABEL_KEYS = ("objective_relation", "belief_relation", "intent")
"""An utterance's three labels (FR-TURN-6), in order. Named here: the M4 parts read them through
``labels_of`` (the second label's name is a later milestone's term in docs/04 §13's lexicon)."""
UNKNOWN = "unknown"
"""The second label in M4: no per-character partitions yet."""


def labels_of(item: dict[str, Any]) -> tuple[str, str, str]:
    """A ``say`` item's three labels, in ``LABEL_KEYS`` order."""
    labels = item["labels"]
    first, second, third = (labels[k] for k in LABEL_KEYS)
    return first, second, third


def row_labels(row: Any) -> tuple[str, str, str]:
    """An ``utterances`` row's three labels, in ``LABEL_KEYS`` order."""
    first, second, third = (getattr(row, k) for k in LABEL_KEYS)
    return first, second, third


async def history(
    client: httpx.AsyncClient, story_id: str, viewpoint: str, **params: Any
) -> list[dict[str, Any]]:
    response = await client.get(
        f"/stories/{story_id}/history", params={"viewpoint": viewpoint, **params}, headers=READ
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def utterances(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT u.*, e.beat AS event_beat, e.type AS event_type FROM utterances u "
        "JOIN events e ON e.id = u.event_id WHERE u.story_id = :s ORDER BY e.seq",
        s=story_id,
    )


async def turn_report(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The latest committed turn's ``turn_report``."""
    found = await scalar(
        engine,
        "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY narrative_turn DESC, turn_variant DESC LIMIT 1",
        s=story_id,
    )
    assert isinstance(found, dict)
    return found


__all__ = [
    "AGE",
    "LABEL_KEYS",
    "LOCATED_AT",
    "UNKNOWN",
    "age_fact",
    "ages",
    "beat",
    "claim",
    "history",
    "labels_of",
    "place",
    "render",
    "renders_of",
    "row_labels",
    "schema_calls",
    "structured_story",
    "text_of",
    "turn_report",
    "utterances",
]
