"""U-30 (M4): resolution levels (FR-COMMIT-9, FR-STORE-29; docs/04 §3; #154). One file per test
ID and milestone: ``test_u_30`` drives Core's ``boundary.depend_on_detail`` directly;
``test_u_30__structured`` plays the same story through structured turns on the stub, where the
commit's detail rule (#494) routes a dependent ``think``/``do`` claim to it."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import timeline
from epistrel.core.canon import facts as canon
from epistrel.core.commit import boundary, lifecycle
from epistrel.core.commit.reconcile import Ok
from epistrel.core.enums import Membership, OperationKind, RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from tests import commit_support as c
from tests.auth_support import auth_settings
from tests.canon_support import PRINCIPAL, commit, fact_rows
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.structured_turn_support import beat, claim, render, structured_story, turn_report

pytestmark = pytest.mark.integration


def _handwriting(letter: str, predicate: str, value: str) -> Proposition:
    return Proposition(
        subject_id=letter,
        predicate_id=predicate,
        literal=TypedLiteral(kind=LiteralKind.STR, value=value),
        valid_time=Interval.of(0, None),
    )


@pytest.mark.part("M4", "FR-COMMIT-9,14; FR-STORE-29")
async def test_u_30(engine: AsyncEngine) -> None:
    """Turn 1 commits, at ``consequential``, that the drawer holds a letter and that Marcus
    wrote it: the letter's presence and author are fixed, its handwriting undetermined (no fact,
    no thunk). Turn 2 only mentions the handwriting (a ``say``): nothing is introduced or
    written. Turn 3's ``think`` depends on it: the detail becomes a thunk linked to the letter's
    fact and resolves once, through reconciliation, at ``incidental`` ("spidery"). Turn 4's
    ``do`` claiming "neat" is caught (``contradicted``: the detail has one value). Rewinding to
    turn 2 (past the resolving turn) makes the handwriting undetermined again (its fact
    ``revised`` and out of the timeline, its thunk gone with turn 3) while the letter and its
    author stay fixed: their rows are byte-identical, and a new turn 3 resolves the handwriting
    afresh ("neat")."""
    k = await c.knock_story(engine)
    story = k.story_id
    async with engine.begin() as conn:
        drawer = await store.add_entity(conn, story, "item", "drawer")
        letter = await store.add_entity(conn, story, "item", "letter")
        marcus = await store.add_entity(conn, story, "person", "Marcus")
        holds = (await registry.auto_register(conn, story, "holds_item")).id
        wrote = (await registry.auto_register(conn, story, "written_by")).id
        hand = (await registry.auto_register(conn, story, "handwriting_of", object_type="str")).id

    async def letter_facts(conn: AsyncConnection, op: Operation) -> list[canon.Fact]:
        return [
            await canon.commit_fact(
                conn,
                story,
                Proposition(
                    subject_id=s, predicate_id=p, object_id=o, valid_time=Interval.of(0, None)
                ),
                provenance=RecordProvenance.RENDER,
                operation=op,
                beat=2,
                resolution_level=boundary.CONSEQUENTIAL,
            )
            for s, p, o in ((drawer, holds, letter), (letter, wrote, marcus))
        ]

    there, author = await c.in_turn(engine, story, letter_facts)
    assert there.resolution_level == author.resolution_level == boundary.CONSEQUENTIAL

    async def detail(channel: str, value: str) -> boundary.Detail:
        async def step(conn: AsyncConnection, op: Operation) -> boundary.Detail:
            return await boundary.depend_on_detail(
                conn, op, there.id, _handwriting(letter, hand, value), beat=2, channel=channel
            )

        found = await c.in_turn(engine, story, step)
        assert isinstance(found, boundary.Detail)
        return found

    # Turn 2: a mention resolves nothing.
    mentioned = await detail("say", "spidery")
    assert mentioned.thunk_id is None
    assert await c.rows(engine, "thunks", story) == []
    assert [f for f in await c.rows(engine, "facts", story) if f.predicate_id == hand] == []

    # Turn 3: the first dependent think resolves it once, through reconciliation.
    resolved = await detail("think", "spidery")
    dependence = resolved.dependence
    assert isinstance(dependence, lifecycle.Dependence)
    assert dependence.kind == "committed"
    assert isinstance(dependence.reconciliation, Ok)
    assert dependence.fact is not None
    assert dependence.fact.resolution_level == boundary.INCIDENTAL
    assert dependence.fact.provenance is RecordProvenance.RESOLUTION
    (thunk_row,) = await c.rows(engine, "thunks", story)
    assert thunk_row.proposition_sketch["parent_fact_id"] == there.id
    assert thunk_row.narrative_turn == 3

    # Turn 4: a contradicting claim about the same detail is caught.
    caught = await detail("do", "neat")
    assert caught.thunk_id == resolved.thunk_id
    assert caught.dependence.kind == "contradicted"
    assert len([f for f in await c.rows(engine, "facts", story) if f.predicate_id == hand]) == 1

    # Rewind past the resolving turn: the detail is undetermined again; the letter stays.
    before = await fact_rows(engine, story)
    await timeline.truncate(engine, story, timeline.TruncateRequest(to_turn=2), principal=PRINCIPAL)
    after = await fact_rows(engine, story)
    for fact in (there, author):
        assert dict(after[fact.id]._mapping) == dict(before[fact.id]._mapping)
        assert after[fact.id].commit_state == "committed"
    spidery = after[dependence.fact.id]
    assert spidery.commit_state == "revised"
    assert spidery.membership == Membership.DISCARDED.value
    async with engine.connect() as conn:
        open_thunk = await lifecycle.read_thunk(conn, story, str(resolved.thunk_id))
        current: list[Any] = await canon.canon(conn, story, predicate=hand, subject=letter)
    assert open_thunk is not None
    assert open_thunk.state.value == "open"  # its row: discarded with turn 3
    assert current == []
    afresh = await detail("think", "neat")
    assert afresh.dependence.kind == "committed"
    assert afresh.thunk_id != resolved.thunk_id
    assert afresh.dependence.fact.proposition.literal.value == "neat"


# -- through structured turns -------------------------------------------------------------------


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _handwritings(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.id, f.resolution_level, f.provenance, f.commit_state, f.membership, "
        "f.thunk_id, p.literal FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "JOIN predicates q ON q.id = f.predicate_id "
        "WHERE f.story_id = :s AND q.name = 'handwriting_of' ORDER BY f.id",
        s=story,
    )


async def _thunks(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(engine, "SELECT * FROM thunks WHERE story_id = :s ORDER BY id", s=story)


def _ada(*beats: dict[str, Any]) -> str:
    """Ada's render: ``beats`` then a ``say`` (a render needs a ``say`` or a ``do``)."""
    return render(*beats, beat("say", "Hm.", intent="sincere"))


def _about_handwriting(kind: str, text: str, value: str) -> dict[str, Any]:
    return beat(kind, text, propositions=[claim("letter", "handwriting_of", value)])


@pytest.mark.part("M4", "FR-COMMIT-9,14; FR-STORE-29")
async def test_u_30__structured(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """U-30 through structured turns on the stub (#494: the owner's detail rule). The author
    commits, at ``consequential``, that the drawer holds a letter and that Marcus wrote it.
    Turn 1: Ada's ``say`` mentions its handwriting (spidery) and her ``do`` claims the drawer is
    painted blue (a novel claim about no attribute the letter's fact leaves open): nothing is
    introduced for either, and the paint is ordinary canon. Turn 2: her ``think`` depends on the
    handwriting: the structured commit routes it to ``boundary.depend_on_detail``, which resolves
    it once, reconciled, at ``incidental`` (a thunk linked to the letter's fact). Turn 3: her
    ``do`` claiming "neat" is caught (the verifier's ``contradicted``; still one value).
    Rewinding to turn 1 makes the handwriting undetermined again while the letter's facts stay
    byte-identical, and a new turn 2 resolves it afresh ("neat")."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")
        wren = ids["Wren"]
        async with engine.begin() as conn:
            drawer = await store.add_entity(conn, story, "item", "drawer")
            letter = await store.add_entity(conn, story, "item", "letter")
            marcus = await store.add_entity(conn, story, "person", "Marcus")
            holds = (await registry.auto_register(conn, story, "holds_item")).id
            wrote = (await registry.auto_register(conn, story, "written_by")).id
            await registry.auto_register(conn, story, "painted", object_type="str")
            functional = {"name": "handwriting_of", "object_type": "str"}
            await registry.register_author(
                conn,
                story,
                registry.ScenarioRules.parse(
                    {"predicates": [{**functional, "cardinality": "functional"}]}
                ),
            )
        there, author = [
            await commit(
                engine,
                story,
                Proposition(
                    subject_id=s, predicate_id=p, object_id=o, valid_time=Interval.of(None, None)
                ),
                kind=OperationKind.CONTROL,
                provenance="author",
                resolution_level=boundary.CONSEQUENTIAL,
            )
            for s, p, o in ((drawer, holds, letter), (letter, wrote, marcus))
        ]

        # Turn 1: a mention resolves nothing; an unrelated novel claim is not routed.
        stub.script(
            "generation",
            [
                render(
                    _about_handwriting("say", "Its handwriting is spidery.", "spidery")
                    | {"intent": "sincere"},
                    beat(
                        "do",
                        "Ada runs a finger along the blue drawer.",
                        propositions=[claim("drawer", "painted", "blue")],
                    ),
                )
            ],
        )
        await played(client, story, body(wren, "Ada, look at this letter."))
        assert await _thunks(engine, story) == []
        assert await _handwritings(engine, story) == []
        (paint,) = await rows(
            engine,
            "SELECT f.resolution_level, f.thunk_id FROM facts f JOIN predicates q "
            "ON q.id = f.predicate_id WHERE f.story_id = :s AND q.name = 'painted'",
            s=story,
        )
        assert (paint.resolution_level, paint.thunk_id) == (None, None)

        # Turn 2: the first dependent think resolves it once, through reconciliation.
        stub.script(
            "generation",
            [_ada(_about_handwriting("think", "Such spidery handwriting.", "spidery"))],
        )
        await played(client, story, body(wren, "Ada, who wrote it?"))
        (thunk,) = await _thunks(engine, story)
        assert thunk.proposition_sketch["parent_fact_id"] == there.id
        assert (thunk.narrative_turn, str(thunk.state)) == (2, "resolved")
        (spidery,) = await _handwritings(engine, story)
        assert spidery.resolution_level == boundary.INCIDENTAL
        assert str(spidery.provenance) == "resolution"
        assert spidery.thunk_id == thunk.id
        assert spidery.literal["value"] == "spidery"
        assert (await turn_report(engine, story))["commit"]["reconciliations"] == 1

        # Turn 3: a contradicting claim about the same detail is caught; it keeps one value.
        stub.script(
            "generation",
            [_ada(_about_handwriting("do", "Ada traces the neat handwriting.", "neat"))],
        )
        await played(client, story, body(wren, "Ada, read it to me."))
        verdicts = (await turn_report(engine, story))["verify"]["verdicts"]
        assert [(v["kind"], v["rule"]) for v in verdicts] == [("contradicted", "functional_key")]
        assert [h.id for h in await _handwritings(engine, story)] == [spidery.id]
        assert len(await _thunks(engine, story)) == 1

        # Rewind past the resolving turn: the detail is undetermined again; the letter stays.
        before = await fact_rows(engine, story)
        await timeline.truncate(
            engine, story, timeline.TruncateRequest(to_turn=1), principal=PRINCIPAL
        )
        after = await fact_rows(engine, story)
        for fact in (there, author):
            assert dict(after[fact.id]._mapping) == dict(before[fact.id]._mapping)
            assert after[fact.id].commit_state == "committed"
        assert after[spidery.id].commit_state == "revised"
        async with engine.connect() as conn:
            reopened = await lifecycle.read_thunk(conn, story, str(thunk.id))
        assert reopened is not None
        assert reopened.state.value == "open"
        stub.script(
            "generation",
            [_ada(_about_handwriting("think", "Neat handwriting, after all.", "neat"))],
        )
        await played(client, story, body(wren, "Ada, who wrote it?"))
        current = [
            (h.literal["value"], h.resolution_level)
            for h in await _handwritings(engine, story)
            if h.membership == "active" and h.commit_state == "committed"
        ]
        assert current == [("neat", boundary.INCIDENTAL)]
