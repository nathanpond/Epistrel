"""Helpers for the nested-belief tests (#167): a story switched to nested beliefs (with
partitions, perception and testimony) at a depth, the knock world of S-NESTED-BELIEF (who
knocked at the door: the functional story predicate ``knocker``), and nested writes in an
operation's commit step."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Proposition
from tests import canon_support as c
from tests import perception_support as ps
from tests import takeback_support
from tests import testimony_support as ts
from tests.event_support import rows


async def enable_nested(
    engine: AsyncEngine,
    story_id: str,
    *,
    nested: bool = True,
    depth: int = 2,
    testimony: str = "defaulted",
    level: str = "co_location",
    lazy: bool = False,
) -> None:
    """The story's overrides: partitions with ``testimony`` at perception ``level``, and nested
    beliefs on at ``depth`` (off when ``nested`` is false)."""
    takeback_support.overridden(story_id, ("character_knowledge", "perception"))
    await set_overrides(
        engine,
        story_id,
        ProfileOverridesPatch(
            settings={
                "character_knowledge": {
                    "partitions": True,
                    "testimony": testimony,
                    "lazy_resolution": lazy,
                    "nested_beliefs": {"enabled": nested, "depth": depth},
                },
                "perception": {"level": level},
            },
            ignore_warnings=True,
        ),
        principal="p-author",
        facts=ps.NO_FACTS,
    )


async def set_depth(engine: AsyncEngine, story_id: str, depth: int) -> None:
    """The story's nested-belief depth set in its stored overrides directly (a deeper dial is a
    data increase, which the two-stage change would stage behind a backfill)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE story_profile_overrides SET settings = jsonb_set(settings, "
                "'{character_knowledge,nested_beliefs,depth}', to_jsonb(CAST(:d AS int))) "
                "WHERE story_id = :s"
            ),
            {"d": depth, "s": story_id},
        )


async def nested_settings(
    engine: AsyncEngine, story_id: str, *, nested: bool = True, depth: int = 2
) -> ProfileSettings:
    """The story's effective settings with partitions on and nested beliefs at ``depth``."""
    async with engine.connect() as conn:
        found = (await effective_configuration(conn, story_id, validate=False)).settings
    document = found.model_dump(mode="json")
    document["character_knowledge"]["partitions"] = True
    document["character_knowledge"]["nested_beliefs"] = {"enabled": nested, "depth": depth}
    return ProfileSettings.model_validate(document)


@dataclass(frozen=True)
class Knock:
    """The knock world: the story, its characters by name, the knock (an event entity), the
    people who might have knocked and the functional predicate ``knocker``."""

    story_id: str
    ids: dict[str, str]
    knock: str
    george: str
    marcus: str
    driver: str
    knocker: str

    def knocked(self, who: str) -> Proposition:
        """``knocker(the knock, who)``."""
        return Proposition(
            subject_id=self.knock,
            predicate_id=self.knocker,
            object_id=who,
            valid_time=Interval.of(None, None),
        )


async def knock_world(client: Any, engine: AsyncEngine, *names: str) -> Knock:
    """A room (``ts.room``: the first name the player, the second the rendered character, all in
    the parlor) with the knock, George, Marcus and the delivery driver, and the author predicate
    ``knocker`` (functional: one person knocked)."""
    story, ids, _ = await ts.room(client, engine, *names)
    async with engine.begin() as conn:
        knock = await store.add_entity(conn, story, "event", "the knock")
        george = await store.add_entity(conn, story, "person", "George")
        marcus = await store.add_entity(conn, story, "person", "Marcus")
        driver = await store.add_entity(
            conn, story, "person", "the delivery driver", aliases=("the delivery guy",)
        )
        rules = registry.ScenarioRules.parse(
            {
                "predicates": [
                    {
                        "name": "knocker",
                        "subject_type": "event",
                        "object_type": "person",
                        "cardinality": "functional",
                        "temporal_kind": "permanent",
                        "docs": "Who knocked.",
                    }
                ]
            }
        )
        reg = await registry.register_author(conn, story, rules, resolve=lambda name: name)
    found = reg.named("knocker")
    assert found is not None
    return Knock(story, ids, knock, george, marcus, driver, found.id)


Step = Callable[[AsyncConnection, Operation, str], Awaitable[Any]]


async def in_operation(
    engine: AsyncEngine,
    story_id: str,
    step: Step,
    *,
    kind: OperationKind = OperationKind.TURN,
    say: tuple[str, str, str] | None = None,
) -> Any:
    """``step(conn, op, event_id)`` in a new operation's commit (a turn by default), with
    nested beliefs delivered; ``say`` = ``(speaker, text, intent)`` stages a ``beat_say`` at beat
    2 whose event id the step gets (else the operation's own author act's). Answers the step's
    answer."""
    found: dict[str, Any] = {}
    async with Operation(
        engine,
        story_id=story_id,
        kind=kind,
        principal=c.PRINCIPAL,
        bumps_revision=True if kind is not OperationKind.TURN else None,
    ) as op:
        if kind is OperationKind.TURN:
            op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER, beat=1)
        if say is not None:
            speaker, words, intent = say
            op.append("beat_say", {"text": words, "intent": intent}, character_id=speaker, beat=2)
        else:
            op.append("belief_authored", {"holder_character_id": c.PRINCIPAL})
        if kind is OperationKind.TURN:
            op.append("message_rendered", {"text": "reply"})

        async def run(conn: AsyncConnection) -> None:
            kind_ = "beat_say" if say is not None else "belief_authored"
            event_id = await conn.scalar(
                text("SELECT id FROM events WHERE operation_id = :o AND type = :t"),
                {"o": op.operation_id, "t": kind_},
            )
            found["value"] = await step(conn, op, str(event_id))

        op.add_commit_step(run)
        await op.commit()
    return found.get("value")


async def nested_rows(engine: AsyncEngine, story_id: str, holder: str) -> list[Any]:
    """``holder``'s nested-shape rows (every kind but first-order) with their producing
    transition's kind, source and antecedent."""
    return await rows(
        engine,
        "SELECT b.*, p.object_id, t.kind AS cause, t.source_character_id, t.antecedent_ref "
        "FROM beliefs b JOIN propositions p ON p.id = b.proposition_id "
        "JOIN belief_transitions t ON t.id = b.provenance "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND b.kind <> 'first_order' "
        "AND b.membership = 'active' ORDER BY b.id",
        s=story_id,
        h=holder,
    )


__all__ = [
    "Knock",
    "enable_nested",
    "in_operation",
    "knock_world",
    "nested_rows",
    "nested_settings",
    "set_depth",
]
