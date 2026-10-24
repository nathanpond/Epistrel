"""Helpers for the testimony tests (#162): a structured story switched to partitions with
testimony at ``model`` or ``defaulted``, a room of characters, the claim the says carry, and
the witnesses' first-order and speech-act beliefs read back."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import BeliefConfidenceLevel, OperationKind
from epistrel.core.epistemic import transitions
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from tests import partition_support as parts
from tests import perception_support as ps
from tests.canon_support import auto_predicate
from tests.event_support import rows
from tests.structured_turn_support import structured_story

TESTIMONY_LEVELS = frozenset({"model", "defaulted"})
CLAIM_LINE = "Olivia sent the text."


async def enable_testimony(
    engine: AsyncEngine,
    story_id: str,
    testimony: str = "model",
    *,
    level: str = "full",
    partitions: bool = True,
    salience: str | None = None,
    model_calls: int | None = None,
    lazy: bool = True,
) -> None:
    """The story's overrides: partitions with ``testimony`` at perception ``level`` and lazy
    resolution (#164) on unless ``lazy`` is false (and the commitment's salience, the call
    budget, when given)."""
    settings: dict[str, Any] = {
        "character_knowledge": {
            "partitions": partitions,
            "testimony": testimony,
            "lazy_resolution": lazy,
        }
        if partitions
        else {"partitions": False},
        "perception": {"level": level if partitions else "none"},
    }
    if salience is not None:
        settings["commitment"] = {"enabled": True, "salience": salience}
    if model_calls is not None:
        settings["budgets"] = {"model_calls": model_calls}
    await set_overrides(
        engine,
        story_id,
        ProfileOverridesPatch(settings=settings, ignore_warnings=True),
        principal="p-author",
        facts=ps.NO_FACTS,
    )


FORGET = text(
    "UPDATE story_profile_overrides SET settings = settings - 'perception' "
    "- 'character_knowledge' - 'commitment' "
    "WHERE settings ? 'perception' OR settings ? 'character_knowledge'"
)


async def forget(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(FORGET)


async def room(
    client: Any, engine: AsyncEngine, *names: str, away: tuple[str, ...] = ()
) -> tuple[str, dict[str, str], dict[str, str]]:
    """A structured story (the first name the player, the second the rendered character) with
    everyone in the parlor except ``away`` (in the kitchen, not adjoining), plus the claim's
    entities: Olivia, the ex and the text, and the story predicate ``sent``."""
    story, ids = await structured_story(client, *names)
    parlor = await ps.place_entity(engine, story, "parlor")
    kitchen = await ps.place_entity(engine, story, "kitchen")
    for name in names:
        await ps.put(engine, story, ids[name], kitchen if name in away else parlor)
    async with engine.begin() as conn:
        things = {
            "Olivia": await store.add_entity(conn, story, "person", "Olivia"),
            "the text": await store.add_entity(conn, story, "item", "the text"),
        }
    things["sent"] = await auto_predicate(engine, story, "sent")
    return story, ids, things


def sent(things: dict[str, str], polarity: Polarity = Polarity.AFFIRMED) -> Proposition:
    """Olivia sent the text (or did not)."""
    return Proposition(
        subject_id=things["Olivia"],
        predicate_id=things["sent"],
        object_id=things["the text"],
        polarity=polarity,
        valid_time=Interval.of(None, None),
    )


async def observed(
    engine: AsyncEngine, story_id: str, holder: str, proposition: Proposition
) -> transitions.TransitionResult:
    """``holder`` saw for itself that ``proposition`` holds (an observation, ``known``), in an
    author operation before the turn."""
    return await parts.transit(
        engine,
        story_id,
        holder,
        proposition,
        cause=transitions.Kind.OBSERVATION,
        confidence=transitions.Confidence(BeliefConfidenceLevel.KNOWN),
        kind=OperationKind.CONTROL,
    )


async def make_minor(engine: AsyncEngine, character_id: str, age: int = 12) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE characters SET true_age = :a WHERE id = :c"), {"a": age, "c": character_id}
        )


def salient_say(beat: int, words: str = CLAIM_LINE) -> str:
    """A scripted ``salience_detection`` answer marking the say at ``beat`` salient (a plain
    claim: nothing is owed)."""
    return json.dumps(
        {
            "salient": [
                {
                    "beat": beat,
                    "span": words,
                    "proposition_sketch": {
                        "subject": "Olivia",
                        "relation": "sent",
                        "object": "the text",
                    },
                }
            ],
            "texture": [],
        }
    )


def adjudicated(decision: str, confidence: float = 0.9) -> str:
    return json.dumps({"decision": decision, "confidence": confidence, "reasons": ["scripted"]})


async def beliefs_of(engine: AsyncEngine, story_id: str, holder: str) -> list[Any]:
    """``holder``'s active beliefs with their proposition and producing transition: kind,
    polarity, level, state, the transition's kind, source and outcome, and its antecedent."""
    return await rows(
        engine,
        "SELECT b.id, b.kind, b.polarity, b.confidence_level, b.state, b.tx_to, b.held_to_turn, "
        "p.subject_id, p.predicate_id, p.object_id, t.kind AS cause, t.source_character_id, "
        "t.outcome, t.antecedent_kind, t.antecedent_ref, t.position_beat, t.position_step "
        "FROM beliefs b JOIN propositions p ON p.id = b.proposition_id "
        "JOIN belief_transitions t ON t.id = b.provenance "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND b.membership = 'active' "
        "ORDER BY b.id",
        s=story_id,
        h=holder,
    )


async def transitions_of(engine: AsyncEngine, story_id: str, holder: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT t.kind, t.outcome, t.source_character_id, t.antecedent_ref, b.kind AS belief_kind "
        "FROM belief_transitions t JOIN beliefs b ON b.id = t.belief_id "
        "WHERE t.story_id = :s AND t.holder_character_id = :h AND t.membership = 'active' "
        "ORDER BY t.id",
        s=story_id,
        h=holder,
    )


def first_order(found: list[Any]) -> list[Any]:
    return [b for b in found if b.kind == "first_order" and b.tx_to is None]


def said(found: list[Any]) -> list[Any]:
    return [b for b in found if b.kind == "speech_act"]


__all__ = [
    "CLAIM_LINE",
    "TESTIMONY_LEVELS",
    "adjudicated",
    "beliefs_of",
    "enable_testimony",
    "first_order",
    "forget",
    "make_minor",
    "observed",
    "room",
    "said",
    "salient_say",
    "sent",
    "transitions_of",
]
