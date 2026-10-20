"""The structured stores against PostgreSQL (#131): migration 0048's tables and seed, the core
vocabulary's re-sync, the append-only and identity triggers, the store's writes, auto
registration as a configuration change, proposals and their decisions over REST, the author
layer from a snapshot or deferred rows, story deletion, forks and erasure. The milestone parts
are U-15's slices (``tests/test_u_15.py``) and S-PREDICATE-LAYERS
(``tests/scenario/test_s_predicate_layers.py``)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import fork as forking
from epistrel.core.erasure.index import SUBJECT_WRITERS
from epistrel.core.errors import InvalidRequest, StoryArchived, StoryNotFound
from epistrel.core.fork import ForkRequest
from epistrel.core.ids import new_ulid
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.codec import predicate_from_row, proposition_from_row
from epistrel.core.propositions.model import (
    Cardinality,
    Granularity,
    Instant,
    Interval,
    Layer,
    LiteralKind,
    Polarity,
    Predicate,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, CORE_PREDICATES
from epistrel.core.stories import STORY_TABLES, archive_story, delete_story
from epistrel.core.tables import genesis_deferred, predicates, propositions
from tests import erasure_support as e
from tests.event_support import new_story, rows, scalar
from tests.respond_support import new_story as new_rest_story
from tests.respond_support import respond_client
from tests.story_support import WRITE, rest

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[2] / "src" / "epistrel"


async def _entity(engine: AsyncEngine, story: str, name: str, **fields: Any) -> str:
    async with engine.begin() as conn:
        return await store.add_entity(conn, story, fields.pop("kind", "person"), name, **fields)


async def _write(engine: AsyncEngine, story: str, proposition: Proposition) -> str:
    async with engine.begin() as conn:
        reg = await registry.load(conn, story)
        return await store.write_proposition(conn, story, proposition, reg)


async def _auto(engine: AsyncEngine, story: str, relation: str) -> Predicate:
    async with engine.begin() as conn:
        return await registry.auto_register(conn, story, relation)


async def _revision(engine: AsyncEngine, story: str) -> int:
    return int(await scalar(engine, "SELECT config_revision FROM stories WHERE id = :s", s=story))


def _fact(
    predicate: str, subject: str, obj: str | None, start: int, end: int | None
) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=Interval.of(start, end),
    )


# -- the schema and the core vocabulary ------------------------------------------------------------


async def test_the_core_vocabulary_is_seeded_and_resynced(engine: AsyncEngine) -> None:
    """Migration 0048 seeds every core predicate with its fixed id; the start-up re-sync is a
    no-op on a current deployment and restores a row that drifted from the code (its docs and
    semantics), without touching story predicates. Complement: the drift was real."""
    async with engine.connect() as conn:
        found = {
            r.id: predicate_from_row(r)
            for r in await conn.execute(select(predicates).where(predicates.c.story_id.is_(None)))
        }
    assert found == {p.id: p for p in CORE_PREDICATES}
    assert await registry.sync_core(engine) == 0
    member = CORE_BY_NAME["member_of"]
    story = await new_story(engine)
    mine = await _auto(engine, story, "member of")  # a core name: the core predicate
    assert mine == member
    try:
        async with engine.begin() as conn:
            await conn.execute(
                update(predicates)
                .where(predicates.c.id == member.id)
                .values(docs="stale", cardinality="functional", max_cardinality=1)
            )
        drifted = await scalar(engine, "SELECT docs FROM predicates WHERE id = :p", p=member.id)
        assert drifted == "stale"
        assert await registry.sync_core(engine) == 1
    finally:
        await registry.sync_core(engine)
    async with engine.connect() as conn:
        row = (await conn.execute(select(predicates).where(predicates.c.id == member.id))).one()
    assert predicate_from_row(row) == member


async def test_the_schema_refuses_what_the_model_refuses(engine: AsyncEngine) -> None:
    """A functional predicate with a maximum of two, a core-layer row with a story, two story
    predicates of one name, an empty valid-time span and two objects are refused by the
    database itself; a story predicate may share a core predicate's name only through the
    registry, which answers the core one."""
    story = await new_story(engine)
    base: dict[str, Any] = {
        "story_id": story,
        "layer": "author",
        "cardinality": "multi",
        "temporal_kind": "fluent",
    }
    bad_predicates: list[dict[str, Any]] = [
        {**base, "name": "a", "cardinality": "functional", "max_cardinality": 2},
        {**base, "name": "b", "layer": "core"},
        {**base, "name": "c", "story_id": None},
        {**base, "name": "d", "owner": "nobody"},
    ]
    for values in bad_predicates:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(insert(predicates).values(id=new_ulid(), **values))
    async with engine.begin() as conn:
        await conn.execute(insert(predicates).values(id=new_ulid(), **base, name="twice"))
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(insert(predicates).values(id=new_ulid(), **base, name="twice"))
    a = await _entity(engine, story, "Ann")
    good = {
        "story_id": story,
        "subject_id": a,
        "predicate_id": CORE_BY_NAME["knows"].id,
        "polarity": "affirmed",
        "from_exact": True,
        "to_exact": True,
    }
    for values in (
        {**good, "valid_span": text("int8range(5, 5)")},
        {**good, "valid_span": text("int8range(0, 9)"), "object_id": a, "literal": {"x": 1}},
        {**good, "valid_span": text("int8range(0, 9)"), "polarity": "maybe"},
        {
            **good,
            "valid_span": text("int8range(0, 9)"),
            "valid_from_inner": 5,
            "valid_from_outer": 0,
        },
    ):
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(insert(propositions).values(id=new_ulid(), **values))


async def test_propositions_are_append_only_and_predicates_keep_their_identity(
    engine: AsyncEngine,
) -> None:
    """An UPDATE or DELETE of a proposition is refused, and so is a TRUNCATE; a predicate's
    name and scope never change and a core predicate is never deleted. Complement: a story
    predicate's semantics may change (an approval does), and an erasure may rewrite a
    proposition's qualifiers."""
    story = await new_story(engine)
    a, b = await _entity(engine, story, "Ann"), await _entity(engine, story, "Bea")
    pid = await _write(engine, story, _fact(CORE_BY_NAME["knows"].id, a, b, 0, 10))
    for statement in (
        "UPDATE propositions SET polarity = 'negated' WHERE id = :p",
        "DELETE FROM propositions WHERE id = :p",
        "TRUNCATE propositions",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"p": pid})
    auto = await _auto(engine, story, "apprenticed with")
    for statement in (
        "UPDATE predicates SET name = 'renamed' WHERE id = :p",
        "UPDATE predicates SET story_id = NULL, layer = 'core' WHERE id = :p",
        "DELETE FROM predicates WHERE id = :p",
    ):
        with pytest.raises(DBAPIError, match="predicate"):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"p": auto.id})
    with pytest.raises(DBAPIError, match="predicate"):
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM predicates WHERE id = :p"), {"p": CORE_BY_NAME["age"].id}
            )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE predicates SET docs = 'kept' WHERE id = :p"), {"p": auto.id}
        )
        await conn.execute(text("SELECT set_config('epistrel.erasure', 'on', true)"))
        await conn.execute(
            text('UPDATE propositions SET qualifiers = \'{"x": "y"}\' WHERE id = :p'), {"p": pid}
        )
    assert (
        await scalar(engine, "SELECT qualifiers->>'x' FROM propositions WHERE id = :p", p=pid)
        == "y"
    )


# -- the store ------------------------------------------------------------------------------------


async def test_the_store_writes_canonical_checked_propositions(engine: AsyncEngine) -> None:
    """A symmetric fact is stored lower id first and an inverse one as its twin; a negation is
    stored negated; instants and approximate bounds round-trip; the registry refuses an
    unregistered predicate, an undeclared qualifier, a set where none is allowed and an
    object on an object-less predicate. Every write is indexed for erasure."""
    story = await new_story(engine)
    player = await scalar(engine, "SELECT id FROM characters WHERE story_id = :s", s=story)
    ann = await _entity(engine, story, "Ann", aliases=["Annie", "Annie"], character_id=player)
    bea = await _entity(engine, story, "Bea", kind="person")
    lo, hi = sorted((ann, bea))
    friends = await _write(engine, story, _fact(CORE_BY_NAME["friend_of"].id, hi, lo, 0, 10))
    held = await _write(engine, story, _fact(CORE_BY_NAME["held_by"].id, bea, ann, 0, None))
    never = await _write(
        engine, story, _fact(CORE_BY_NAME["located_at"].id, ann, bea, 0, 5).negation()
    )
    born = await _write(
        engine,
        story,
        Proposition(
            subject_id=ann,
            predicate_id=CORE_BY_NAME["born"].id,
            valid_time=Instant.of(60 * 24 * 40, Granularity.DAY),
        ),
    )
    aged = await _write(
        engine,
        story,
        Proposition(
            subject_id=ann,
            predicate_id=CORE_BY_NAME["age"].id,
            literal=TypedLiteral(kind=LiteralKind.INT, value=40),
            valid_time=Interval.of(0, 100),
        ),
    )
    async with engine.connect() as conn:
        stored = {
            str(r.id): proposition_from_row(r)
            for r in await conn.execute(
                select(propositions).where(propositions.c.story_id == story)
            )
        }
    assert (stored[friends].subject_id, stored[friends].object_id) == (lo, hi)
    assert (stored[held].predicate_id, stored[held].subject_id) == (CORE_BY_NAME["holds"].id, ann)
    assert stored[never].polarity is Polarity.NEGATED
    assert stored[born].valid_time == Instant.of(60 * 24 * 40, Granularity.DAY)
    assert stored[aged].literal == TypedLiteral(kind=LiteralKind.INT, value=40)
    # The player character's entity (#139: written with the character) took "Ann" and "Annie"
    # as aliases, the repeat dropped; every entity's canonical name is an alias row too.
    assert ann == player
    aliases = await rows(
        engine,
        "SELECT alias, source FROM entity_aliases WHERE entity_id = :e ORDER BY alias",
        e=ann,
    )
    assert [(r.alias, r.source) for r in aliases] == [
        ("Ann", "detected"),
        ("Annie", "detected"),
        ("Player", "canonical"),
    ]
    for bad, field in (
        (_fact(new_ulid(), ann, bea, 0, 5), "predicate_id"),
        (
            _fact(CORE_BY_NAME["member_of"].id, ann, bea, 0, 5).model_copy(
                update={"qualifiers": {"rank": "x"}}
            ),
            "qualifiers",
        ),
        (
            Proposition(
                subject_id=ann,
                predicate_id=CORE_BY_NAME["knows"].id,
                object_set=(bea,),
                valid_time=Interval.of(0, 5),
            ),
            "object_set",
        ),
        (_fact(CORE_BY_NAME["alive"].id, ann, bea, 0, 5), "object"),
        (_fact(CORE_BY_NAME["knows"].id, ann, None, 0, 5), "object"),
    ):
        with pytest.raises(InvalidRequest) as refused:
            await _write(engine, story, bad)
        assert refused.value.details["field"] == field
    with pytest.raises(InvalidRequest):
        await _entity(engine, story, "Nowhere", kind="planet")
    indexed = await rows(
        engine,
        "SELECT record_table, record_id, ref_kind FROM subject_references "
        "WHERE story_id = :s AND record_table = 'entities'",
        s=story,
    )
    # The player's entity declares its character and its canonical name mentions it (#139).
    assert sorted((r.record_id, r.ref_kind) for r in indexed) == [
        (ann, "declared"),
        (ann, "mentioned"),
    ]


async def test_the_writers_are_registered_for_erasure() -> None:
    """Every structured table names the module that writes it, and only the store writes a
    closed set: no module inserts one from observation (the author layer and canon call
    ``declare_closed_set``)."""
    writers = {w.table: w.writers for w in SUBJECT_WRITERS}
    for table in ("propositions", "closed_sets", "entities", "entity_aliases"):
        assert writers[table] == ("epistrel.core.propositions.store",)
    for table in ("predicates", "predicate_proposals"):
        assert writers[table] == ("epistrel.core.propositions.registry",)
    inserting = [
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "insert(closed_sets" in p.read_text(encoding="utf-8")
    ]
    assert inserting == ["core/propositions/store.py"]
    declaring = sorted(
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "declare_closed_set(" in p.read_text(encoding="utf-8")
    )
    assert declaring == ["core/propositions/registry.py", "core/propositions/store.py"]
    assert {t.name for t in STORY_TABLES} >= {
        "entities",
        "entity_aliases",
        "predicates",
        "propositions",
        "closed_sets",
        "predicate_proposals",
    }


# -- auto registration and proposals -------------------------------------------------------------


async def test_auto_registration_is_a_configuration_change(engine: AsyncEngine) -> None:
    """A relation nobody declared becomes an auto predicate with the weakest semantics, one
    revision and one ``config_changes`` row later; the same relation again (in any spelling
    the name normalizes to) is the same predicate with no new change. Two turns racing to
    register it meet at one row."""
    story = await new_story(engine)
    before = await _revision(engine, story)
    first = await _auto(engine, story, "Apprenticed With")
    assert first.layer is Layer.AUTO
    assert first.weak
    assert first.qualifiers is None
    assert (first.name, first.story_id, first.cardinality) == (
        "apprenticed_with",
        story,
        Cardinality.MULTI,
    )
    assert await _revision(engine, story) == before + 1
    again = await _auto(engine, story, "apprenticed  with")
    assert again == first
    assert await _revision(engine, story) == before + 1
    changes = await rows(
        engine,
        "SELECT setting, new, class FROM config_changes WHERE scope = 'story' AND scope_id = :s",
        s=story,
    )
    assert [(c.setting, c.new["layer"], c.__getattribute__("class")) for c in changes] == [
        (f"predicate:{first.id}", "auto", "interpretive")
    ]
    before_race = await _revision(engine, story)
    raced = await asyncio.gather(*(_auto(engine, story, "sails with") for _ in range(4)))
    assert len({p.id for p in raced}) == 1
    assert await _revision(engine, story) == before_race + 1
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM predicates WHERE story_id = :s AND name = 'sails_with'",
            s=story,
        )
        == 1
    )
    with pytest.raises(StoryNotFound):
        await _auto(engine, new_ulid(), "anything")


async def test_conflict_arbiters_hold_on_a_generic_plan(engine: AsyncEngine) -> None:
    """#466: auto registration and proposal filing name their ON CONFLICT arbiters exactly as
    the indexes spell them. A bound value in the arbiter matched the index only while Postgres
    folded it on a custom plan; once psycopg prepared the statement and the generic plan took
    over (about the tenth registration on one connection in a live run), every insert raised
    ``InvalidColumnReference``. Forcing the generic plan makes that happen on the first one."""
    story = await new_story(engine)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        registered = [
            await registry.auto_register(conn, story, f"is not actually his {n}") for n in range(12)
        ]
        for predicate in registered:
            first = await registry.file_proposal(
                conn, story, predicate.id, registry.ProposedChanges(symmetric=True)
            )
            again = await registry.file_proposal(
                conn, story, predicate.id, registry.ProposedChanges(symmetric=True)
            )
            assert again == first  # the pending proposal is the arbiter's conflicting row
    assert len({p.id for p in registered}) == 12
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM predicate_proposals WHERE story_id = :s AND status = 'pending'",
            s=story,
        )
        == 12
    )


async def test_proposals_are_inert_until_decided(settings: Settings, engine: AsyncEngine) -> None:
    """The engine proposes ``functional`` after five single-valued observations; the
    proposal changes nothing (the predicate stays weak, the revision stays) until approved
    over REST, which applies it as one configuration change and reports the propositions it
    would flag without changing them. A rejection closes a later proposal unapplied. Unknown
    predicates, core ones, missing proposals and fields the proposal does not hold are
    refused; an archived story's registry is read-only."""
    story = await new_story(engine)
    teacher = await _auto(engine, story, "taught by")
    smiths = [await _entity(engine, story, f"Smith {n}") for n in range(6)]
    pupils = [await _entity(engine, story, f"Pupil {n}") for n in range(5)]
    async with engine.begin() as conn:
        assert await registry.consider_proposal(conn, story, teacher.id) is None
    for n, pupil in enumerate(pupils[:4]):
        await _write(engine, story, _fact(teacher.id, pupil, smiths[n], 0, 10))
    async with engine.begin() as conn:
        assert await registry.consider_proposal(conn, story, teacher.id) is None  # four
    await _write(engine, story, _fact(teacher.id, pupils[4], smiths[4], 0, 10))
    revision = await _revision(engine, story)
    async with engine.begin() as conn:
        proposal = await registry.consider_proposal(conn, story, teacher.id)
        assert proposal is not None
        assert await registry.consider_proposal(conn, story, teacher.id) == proposal
    # Inert: semantics and revision unchanged.
    reg = await registry.load(engine, story)
    held = reg.get(teacher.id)
    assert held == teacher
    assert held is not None
    assert held.weak
    assert await _revision(engine, story) == revision
    # A sixth observation contradicting the proposal, written before the approval.
    await _write(engine, story, _fact(teacher.id, pupils[0], smiths[5], 5, 15))
    async with rest(settings, engine) as client:
        listed = (await client.get(f"/stories/{story}/predicates", headers=WRITE)).json()
        mine = {p["name"]: p for p in listed["predicates"]}
        assert mine["taught_by"]["pending_proposal"]["changes"] == {"cardinality": "functional"}
        assert mine["taught_by"]["weak"] is True
        assert mine["member_of"]["layer"] == "core"
        assert listed["predicates"][0]["id"] == CORE_PREDICATES[0].id
        path = f"/stories/{story}/predicates/{teacher.id}"
        refused = await client.post(
            f"{path}/approve", json={"changes": ["symmetric"]}, headers=WRITE
        )
        assert refused.status_code == 422, refused.text
        approved = await client.post(f"{path}/approve", json={}, headers=WRITE)
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "approved"
        assert body["applied"] == {"cardinality": "functional"}
        assert body["predicate"]["cardinality"] == "functional"
        assert body["predicate"]["max_cardinality"] == 1
        assert body["predicate"]["weak"] is False
        assert body["config_revision"] == revision + 1
        (violation,) = body["violations"]
        assert violation["subject_id"] == pupils[0]
        assert len(violation["proposition_ids"]) == 2
        again = await client.post(f"{path}/approve", headers=WRITE)
        assert again.status_code == 404
        assert again.json()["error"]["details"]["reason"] == ("no_pending_proposal")
        core = await client.post(
            f"/stories/{story}/predicates/{CORE_BY_NAME['knows'].id}/reject", headers=WRITE
        )
        assert core.status_code == 404
        unread = await client.get(f"/stories/{new_ulid()}/predicates", headers=WRITE)
        assert unread.status_code == 404
    assert await _revision(engine, story) == revision + 1
    # The violating propositions are unchanged.
    assert (
        await scalar(
            engine, "SELECT count(*) FROM propositions WHERE predicate_id = :p", p=teacher.id
        )
        == 6
    )
    # A rejected proposal changes nothing.
    other = await _auto(engine, story, "rides with")
    async with engine.begin() as conn:
        with pytest.raises(InvalidRequest):
            await registry.file_proposal(conn, story, other.id, registry.ProposedChanges())
        with pytest.raises(registry.PredicateNotFound):
            await registry.file_proposal(
                conn, story, CORE_BY_NAME["knows"].id, registry.ProposedChanges(symmetric=True)
            )
        await registry.file_proposal(
            conn, story, other.id, registry.ProposedChanges(symmetric=True)
        )
    revision = await _revision(engine, story)
    rejected = await registry.reject(engine, story, other.id, principal="p-author")
    assert rejected.status == "rejected"
    assert rejected.predicate == other
    assert await _revision(engine, story) == revision
    assert (await registry.load(engine, story)).get(other.id) == other
    async with engine.begin() as conn:
        await registry.file_proposal(
            conn, story, other.id, registry.ProposedChanges(symmetric=True)
        )
    await archive_story(engine, story, principal="p-author")
    with pytest.raises(StoryArchived):
        await registry.approve(engine, story, other.id, None, principal="p-author")


# -- the author layer ------------------------------------------------------------------------------


AUTHOR_RULES: dict[str, list[dict[str, Any]]] = {
    "predicates": [
        {"name": "part_of", "temporal_kind": "permanent", "docs": "A team belongs to a school."},
        {
            "name": "rank",
            "cardinality": "functional",
            "qualifiers": {"service": "str"},
            "key_qualifiers": ["service"],
            "exclusions": ["dead"],
        },
    ],
    "closed_sets": [
        {"predicate": "enrolled_at", "subject": "bobby", "members": ["osu"], "valid_from": 0}
    ],
    "rules": [{"when": "member_of", "then": "enrolled_at", "via": "part_of"}],
}


async def test_the_author_layer_registers_scenario_rules(engine: AsyncEngine) -> None:
    """The scenario's predicates enter the story's registry with their declared semantics, its
    rules the story's ``predicate_rules`` and its closed set a ``closed_sets`` row, read from
    the snapshot or from ``genesis_deferred`` rows of a story created earlier; a core name, an
    unknown predicate, a closed set keyed wrongly and a rule cycle are refused. No
    configuration change is recorded (it is part of the story's creation)."""
    story = await new_story(engine)
    bobby, osu = (
        await _entity(engine, story, "Bobby"),
        await _entity(engine, story, "OSU", kind="organization"),
    )
    refs = {"bobby": bobby, "osu": osu}
    async with engine.begin() as conn:
        deferred_op = new_ulid()
        await conn.execute(
            insert(genesis_deferred),
            [
                {
                    "id": new_ulid(),
                    "story_id": story,
                    "operation_id": deferred_op,
                    "field_ref": f"starting_state.{name}[{index}]",
                    "field_class": "story_rule",
                    "location": {},
                    "capability_needed": "structured",
                    "payload": {"value": value, "characters": {}},
                }
                for name in ("predicates", "closed_sets", "rules")
                for index, value in reversed(list(enumerate(AUTHOR_RULES[name])))
            ],
        )
        rules = await registry.rules_from_deferred(conn, story)
    assert rules == registry.ScenarioRules.parse(AUTHOR_RULES)
    assert rules == registry.rules_from_snapshot({"starting_state": AUTHOR_RULES})
    revision = await _revision(engine, story)
    async with engine.begin() as conn:
        reg = await registry.register_author(conn, story, rules, resolve=refs.__getitem__)
    assert await _revision(engine, story) == revision
    part_of, rank = reg.named("part_of"), reg.named("rank")
    assert part_of is not None
    assert rank is not None
    assert part_of.layer is Layer.AUTHOR
    assert part_of.docs == "A team belongs to a school."
    assert rank.cardinality is Cardinality.FUNCTIONAL
    assert rank.key_qualifiers == ("service",)
    assert rank.exclusions == (CORE_BY_NAME["dead"].id,)
    (rule,) = reg.story_rules
    assert (rule.when, rule.then, rule.via) == (
        CORE_BY_NAME["member_of"].id,
        CORE_BY_NAME["enrolled_at"].id,
        part_of.id,
    )
    assert reg.rules() == (rule,)
    (closed,) = await rows(engine, "SELECT * FROM closed_sets WHERE story_id = :s", s=story)
    assert closed.key == {"subject_id": bobby}
    assert closed.members == [osu]
    assert (closed.asserted_by, closed.valid_from, closed.valid_to) == ("author", 0, None)
    for document, field in (
        ({"predicates": [{"name": "knows"}]}, "starting_state.predicates[0].name"),
        ({"predicates": [{"name": "rank"}]}, "starting_state.predicates[0].name"),
        ({"rules": [{"when": "nothing_here", "then": "knows"}]}, "starting_state.rules[0].when"),
        (
            {"predicates": [{"name": "x", "exclusions": ["nope"]}]},
            "starting_state.predicates[0].exclusions",
        ),
        (
            {"closed_sets": [{"predicate": "rank", "subject": "bobby", "members": ["osu"]}]},
            "starting_state.closed_sets[0].key",
        ),
        (
            {"predicates": [{"name": "y", "key_qualifiers": ["k"]}]},
            "starting_state.predicates[0]",
        ),
        (
            {"rules": [{"when": "enrolled_at", "then": "member_of"}]},
            "starting_state.rules",
        ),
    ):
        with pytest.raises(InvalidRequest) as refused:
            async with engine.begin() as conn:
                await registry.register_author(
                    conn, story, registry.ScenarioRules.parse(document), resolve=refs.__getitem__
                )
        assert refused.value.details["field"].startswith(field), (document, refused.value.details)
    async with engine.begin() as conn:
        with pytest.raises(InvalidRequest):
            await store.declare_closed_set(
                conn,
                story,
                store.ClosedSet(CORE_BY_NAME["knows"].id, bobby, (osu,), valid_from=5, valid_to=5),
                reg,
                asserted_by="canon",
            )
        with pytest.raises(InvalidRequest):
            await store.declare_closed_set(
                conn, story, store.ClosedSet(new_ulid(), bobby, (osu,)), reg, asserted_by="canon"
            )
        with pytest.raises(InvalidRequest):
            await store.declare_closed_set(
                conn,
                story,
                store.ClosedSet(CORE_BY_NAME["knows"].id, bobby, ()),
                reg,
                asserted_by="canon",
            )
    assert (
        await scalar(engine, "SELECT count(*) FROM closed_sets WHERE story_id = :s", s=story) == 1
    )


# -- deletion, forks and erasure ----------------------------------------------------------------


async def _structured_story(engine: AsyncEngine) -> tuple[str, dict[str, str]]:
    story = await new_story(engine)
    ann = await _entity(engine, story, "Ann", aliases=["Annie"])
    bea = await _entity(engine, story, "Bea")
    auto = await _auto(engine, story, "apprenticed with")
    await _write(engine, story, _fact(auto.id, ann, bea, 0, 10))
    async with engine.begin() as conn:
        reg = await registry.register_author(
            conn,
            story,
            registry.ScenarioRules.parse(
                {"closed_sets": [{"predicate": "knows", "subject": "a", "members": ["b"]}]}
            ),
            resolve={"a": ann, "b": bea}.__getitem__,
        )
        await registry.file_proposal(
            conn, story, auto.id, registry.ProposedChanges(cardinality=Cardinality.FUNCTIONAL)
        )
    assert reg.get(auto.id) is not None
    return story, {"ann": ann, "bea": bea, "auto": auto.id}


STRUCTURED = (
    "entities",
    "entity_aliases",
    "predicates",
    "propositions",
    "closed_sets",
    "predicate_proposals",
)


async def _counts(engine: AsyncEngine, story: str) -> dict[str, int]:
    return {
        table: int(
            await scalar(engine, f"SELECT count(*) FROM {table} WHERE story_id = :s", s=story)  # noqa: S608
        )
        for table in STRUCTURED
    }


async def test_story_deletion_and_forks_carry_the_structured_stores(engine: AsyncEngine) -> None:
    """A fork copies every structured row under new ids, remapped consistently (its
    proposition names its own copy of the auto predicate and entities); deleting the source
    removes its rows past the triggers and leaves the fork's and the core vocabulary's."""
    story, ids = await _structured_story(engine)
    source = await _counts(engine, story)
    assert all(source.values()), source
    forked = await forking.fork(engine, story, ForkRequest(at_turn=0), principal="p-test")
    copy = await _counts(engine, forked.story_id)
    assert copy == source
    reg = await registry.load(engine, forked.story_id)
    (auto,) = [
        p
        for p in reg.predicates.values()
        if p.story_id == forked.story_id and p.layer is Layer.AUTO
    ]
    assert auto.id != ids["auto"]
    assert auto.name == "apprenticed_with"
    (fact,) = await rows(
        engine, "SELECT * FROM propositions WHERE story_id = :s", s=forked.story_id
    )
    assert fact.predicate_id == auto.id
    entity_ids = {
        r.id
        for r in await rows(
            engine, "SELECT id FROM entities WHERE story_id = :s", s=forked.story_id
        )
    }
    assert {fact.subject_id, fact.object_id} <= entity_ids
    assert ids["ann"] not in entity_ids
    pending = await scalar(
        engine,
        "SELECT predicate_id FROM predicate_proposals WHERE story_id = :s",
        s=forked.story_id,
    )
    assert pending == auto.id
    setting = await scalar(
        engine,
        "SELECT setting FROM config_changes WHERE scope = 'story' AND scope_id = :s "
        "AND setting LIKE 'predicate:%'",
        s=forked.story_id,
    )
    assert setting == f"predicate:{auto.id}"
    await delete_story(engine, story, principal="p-test")
    assert await _counts(engine, story) == dict.fromkeys(STRUCTURED, 0)
    assert await _counts(engine, forked.story_id) == copy
    core = await scalar(engine, "SELECT count(*) FROM predicates WHERE story_id IS NULL")
    assert core == len(CORE_PREDICATES)


async def test_erasure_reaches_the_structured_stores(pg_url: str, engine: AsyncEngine) -> None:
    """Erasing a character deletes its entity's aliases (each with a marker) and redacts every
    structured record naming it, a predicate's name kept unique; nothing naming it is left.
    Complement: the other character's entity and alias are untouched."""
    settings = e.erasure_settings(pg_url)
    async with respond_client(settings, engine) as (client, _):
        story, cast = await new_rest_story(client, "Wren", "Marisol Quenby", "Ty")
        marisol = await _entity(
            engine,
            story,
            "Marisol Quenby",
            aliases=["the stranger"],
            character_id=cast["Marisol Quenby"],
        )
        ty = await _entity(engine, story, "Ty", aliases=["the boy"], character_id=cast["Ty"])
        visits = await _auto(engine, story, "visits Marisol Quenby")
        await _write(
            engine,
            story,
            _fact(visits.id, ty, marisol, 0, 10).model_copy(
                update={"qualifiers": {"note": "met Marisol Quenby at the well"}}
            ),
        )
        async with engine.begin() as conn:
            await registry.file_proposal(
                conn,
                story,
                visits.id,
                registry.ProposedChanges(cardinality=Cardinality.FUNCTIONAL),
                evidence={"why": "Marisol Quenby is visited once"},
            )
        done = await e.erase(client, "character", cast["Marisol Quenby"], story)
        assert done["residue"] == 0
    assert sum((await e.residue(engine, story, ["Marisol Quenby", "Marisol"])).values()) == 0
    aliases = await rows(
        engine, "SELECT entity_id, alias FROM entity_aliases WHERE story_id = :s", s=story
    )
    # The erased character's entity keeps no alias; the others keep theirs (#139: each
    # character's entity has its name as an alias).
    assert marisol not in {a.entity_id for a in aliases}
    assert sorted((a.entity_id, a.alias) for a in aliases) == sorted(
        [(cast["Wren"], "Wren"), (ty, "Ty"), (ty, "the boy")]
    )
    markers = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'erasure_marker'",
        s=story,
    )
    assert "entity_aliases" in {m.payload.get("record_table") for m in markers}
    name = await scalar(engine, "SELECT name FROM predicates WHERE id = :p", p=visits.id)
    assert "marisol" not in name.casefold()
    assert name.endswith(visits.id.lower())
    assert await scalar(engine, "SELECT canonical_name FROM entities WHERE id = :e", e=ty) == "Ty"
