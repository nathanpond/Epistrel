"""U-07: directives and the authority hierarchy (docs/04 §4; FR-DIRECT-1,2,6; #226).

- **Ephemeral** ("Make this scene tense.", ``kind`` given): a ``steer_queued`` directive and
  nothing else: no fact, memory, belief or State change, no narrative event, the revision
  unchanged. The next turn's renders carry it (the captured prompt holds the author direction)
  and that turn's commit consumes it: the turn after does not carry it.
- **Durable, now** (``directive_parse`` scripted): "Bobby is in the kitchen" is applied at the
  next inter-turn slot ``N⁺k`` as an author fact superseding the narrative fact that put him in
  the parlor; the revision moves by one and the slot's ``directive_applied`` event is a control
  event.
- **The hierarchy** (``core.authority``): author outranks canon outranks in-world; an external
  value outranks the author only within its own domain. Canon's write refuses a narrative write
  that would supersede the author's locked fact (``AuthorityConflict``); complement, the author
  supersedes narrative canon.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.authority import AuthorityTier, outranks, tier_of
from epistrel.core.enums import FactAuthority, OperationKind
from epistrel.core.errors import AuthorityConflict
from tests import canon_support as c
from tests import multi_support as ms
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import bobby_education
from tests.respond_support import body, played, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat

TENSE = "Make this scene tense."
KITCHEN = json.dumps(
    {
        "classification": "durable",
        "temporal_scope": "now",
        "changes": [
            {
                "kind": "proposition",
                "subject": "Bobby",
                "predicate": "located_at",
                "object": "kitchen",
            }
        ],
    }
)
COUNTED = ("facts", "memories", "beliefs", "state_changes")


def test_u_07() -> None:
    """The ordering, both ways, and the external tier's domain."""
    author, canon_, world, external = (
        AuthorityTier.AUTHOR,
        AuthorityTier.CANON,
        AuthorityTier.IN_WORLD,
        AuthorityTier.EXTERNAL,
    )
    assert outranks(author, canon_)
    assert not outranks(canon_, author)
    assert outranks(canon_, world)
    assert not outranks(world, canon_)
    assert not outranks(author, external)  # within the provider's domain
    assert outranks(external, external)
    assert not outranks(external, author, external_domain=False)
    assert tier_of(FactAuthority.AUTHOR) is author
    assert tier_of(FactAuthority.ENGINE, "render") is canon_
    assert tier_of(FactAuthority.ENGINE, "player") is world
    assert tier_of(FactAuthority.EXTERNAL) is external


async def _directive(client: Any, story: str, **body_: object) -> Any:
    return await client.post(
        f"/stories/{story}/directives", params=rs.OMNISCIENT, json=body_, headers=WRITE
    )


async def _counts(engine: AsyncEngine, story: str) -> dict[str, int]:
    return {t: await rs.count(engine, t, story) for t in COUNTED}


@pytest.mark.integration
@pytest.mark.part("M7", "FR-DIRECT-1,2,6")
async def test_u_07__directives(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
        assert ed.parlor is not None
        assert ed.kitchen is not None
        story = ed.story_id
        stub = epistrel.models.stub

        # Ephemeral: queued, and nothing else.
        counts, revision = await _counts(engine, story), await rs.revision(engine, story)
        events = await rs.count(engine, "events", story)
        queued = await _directive(client, story, text=TENSE, kind="ephemeral")
        assert queued.status_code == 200, queued.text
        assert (queued.json()["classification"], queued.json()["disposition"]) == (
            "ephemeral",
            "steer_queued",
        )
        assert await _counts(engine, story) == counts
        assert await rs.revision(engine, story) == revision
        assert await rs.count(engine, "events", story) == events
        assert [c_ for c_ in stub.calls if c_.purpose == "directive_parse"] == []

        def prompts_of_last_turn() -> str:
            return "\n".join(
                m.content
                for call in stub.calls
                if call.purpose == "generation"
                for m in call.messages
            )

        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("do", "Bobby nods.")))
        await played(client, story, body(ed.cal, "Go on.", render_character_ids=[ed.bobby]))
        assert TENSE in prompts_of_last_turn()
        (row,) = await rows(
            engine,
            "SELECT consumed_by_operation_id FROM directives WHERE id = :d",
            d=queued.json()["directive_id"],
        )
        assert row.consumed_by_operation_id is not None
        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("do", "Bobby nods again.")))
        await played(client, story, body(ed.cal, "And?", render_character_ids=[ed.bobby]))
        assert TENSE not in prompts_of_last_turn()

        # Durable, now: the author fact supersedes narrative canon at the next slot.
        narrative = await c.commit(engine, story, c.at(ed.bobby, ed.parlor, 0, None))
        assert narrative.authority is FactAuthority.ENGINE
        revision = await rs.revision(engine, story)
        stub.reset()
        stub.when("", KITCHEN, purpose="directive_parse")
        applied = await _directive(client, story, text="Bobby is in the kitchen.")
        assert applied.status_code == 200, applied.text
        answer = applied.json()
        assert (answer["classification"], answer["disposition"]) == ("durable", "applied")
        assert "⁺" in answer["position"]
        assert answer["timeline_revision"] == revision + 1
        (fact_id,) = answer["applied"]["facts"]
        (fact,) = await rows(
            engine,
            "SELECT f.authority, f.provenance, p.object_id FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id WHERE f.id = :f",
            f=fact_id,
        )
        assert (fact.authority, fact.provenance, fact.object_id) == (
            "author",
            "author",
            ed.kitchen,
        )
        (closed,) = await rows(
            engine, "SELECT superseded_by FROM facts WHERE id = :f", f=narrative.id
        )
        assert closed.superseded_by is not None
        (event,) = await rows(
            engine,
            "SELECT e.category FROM events e JOIN directives d ON d.operation_id = e.operation_id "
            "WHERE d.id = :d AND e.type = 'directive_applied'",
            d=answer["directive_id"],
        )
        assert event.category == "control"

        # Canon never lets a narrative write supersede the author's locked fact.
        await c.commit(
            engine,
            story,
            c.at(ed.marcus or "", ed.kitchen, 0, None),
            provenance="author",
            authority=FactAuthority.AUTHOR,
            author_locked=True,
            kind=OperationKind.CONTROL,
        )
        with pytest.raises(AuthorityConflict):
            await c.commit(engine, story, c.at(ed.marcus or "", ed.parlor, 0, None))
        # #696: verification sees it first. Marcus walking back to the parlor would close the
        # locked fact (a fluent change): contradicted by rule ``authority``, so #142 repairs
        # the claim; the author's own claim is a plain change (``novel``).
        from epistrel.core.readset import KeyedLookup, StructuredReadSet
        from epistrel.core.verify.rules import Rule, VerdictKind, check_with

        moved = c.at(ed.marcus or "", ed.parlor, 30, None)
        async with engine.connect() as conn:
            lookup = KeyedLookup(conn, StructuredReadSet())
            narrative_verdict = await check_with(lookup, story, moved, mover=ed.marcus)
            author_verdict = await check_with(lookup, story, moved, mover=ed.marcus, author=True)
        assert narrative_verdict.kind is VerdictKind.CONTRADICTED
        assert narrative_verdict.rules == {Rule.AUTHORITY}
        assert author_verdict.kind is VerdictKind.NOVEL


UCLA_AS_A_KID = json.dumps(
    {
        "classification": "durable",
        "temporal_scope": "past",
        "changes": [
            {"kind": "proposition", "subject": "Bobby", "predicate": "attended", "object": "UCLA"}
        ],
    }
)
ASKS = json.dumps(
    {
        "changes": [],
        "ambiguities": [
            {"about": "x", "question": "When?", "options": ["a", "b"], "recommended": 0}
        ],
    }
)


@pytest.mark.integration
@pytest.mark.part("M7", "FR-DIRECT-1")
async def test_u_07__routing(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A durable directive about the past ("Bobby went to UCLA as a kid") is routed whole into a
    rewrite session of kind ``directive``: 202 ``routed_to_rewrite`` with the session, the
    ``directives`` row naming it. Complement: no slot operation, no ``directive_applied``
    event, and the revision unchanged."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
        story = ed.story_id
        stub = epistrel.models.stub
        stub.reset()
        stub.when("", UCLA_AS_A_KID, purpose="directive_parse")
        stub.when("", ASKS, purpose="rewrite_parse")
        revision = await rs.revision(engine, story)
        slots = await rs.count(engine, "operations", story)
        routed = await _directive(client, story, text="Bobby went to UCLA as a kid.")
        assert routed.status_code == 202, routed.text
        answer = routed.json()
        assert answer["disposition"] == "routed_to_rewrite"
        session = answer["rewrite_session"]
        assert session["kind"] == "directive"
        (row,) = await rows(
            engine,
            "SELECT disposition, rewrite_session_id, operation_id FROM directives WHERE id = :d",
            d=answer["directive_id"],
        )
        assert (row.disposition, row.rewrite_session_id, row.operation_id) == (
            "routed_to_rewrite",
            session["id"],
            None,
        )
        assert await rs.revision(engine, story) == revision
        applied = await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'directive_applied'",
            s=story,
        )
        assert applied == []
        inter_turn = await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'inter_turn'",
            s=story,
        )
        assert inter_turn == []
        assert await rs.count(engine, "operations", story) >= slots


@pytest.mark.integration
@pytest.mark.part("M7", "FR-DIRECT-2")
async def test_u_07__lite(pg_url: str, engine: AsyncEngine) -> None:
    """#244 (FR-LITE-8): a Lite story's ephemeral directive is a nudge, never ``module_off``:
    a ``directives`` row ``steer_queued``, no ``directive_parse`` call (the request named its
    kind), and the revision unmoved."""
    from tests.story_support import create, story_body

    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        revision = await rs.revision(engine, story)
        epistrel.models.stub.calls.clear()
        nudged = await _directive(client, story, text="Ada is tired.", kind="ephemeral")
        assert nudged.status_code == 200, nudged.text
        assert nudged.json()["disposition"] == "steer_queued"
        assert [c for c in epistrel.models.stub.calls if c.purpose == "directive_parse"] == []
        assert await rs.count(engine, "directives", story) == 1
        assert await rs.revision(engine, story) == revision


def _parsed(**fields: Any) -> str:
    return json.dumps({"classification": "durable", "temporal_scope": "now", **fields})


@pytest.mark.integration
@pytest.mark.part("M7", "FR-DIRECT-1,2")
async def test_u_07__follow_ups(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#696 (#226's not-built clauses):

    - **scheduled events**: a durable directive creates an event (owner ``author``), a second
      reschedules it by name and a third cancels it, each at its own slot; a name that is no
      single open event is 422 ``directive_ambiguous`` with the candidates;
    - **split**: a clock advance together with a change to the past is 422
      ``directive_split_required``, nothing written;
    - **standing guidance**: a steer the parse reads as standing answers
      ``hints: [use_custom_instructions]``;
    - **steer budget**: steers past 500 queued tokens are 409 ``steer_budget_exceeded``
      ``{tokens, max: 500}``, nothing written;
    - **TTL**: the sweep expires an unconsumed steer older than
      ``directive_steer_ttl_minutes`` (disposition ``expired``) and keeps a fresh one."""
    from epistrel.orchestrator.directives import expire_steers
    from tests import clock_support as ck

    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        ed = await bobby_education(client, engine, witnesses=True, closed_enrollment=False)
        story = ed.story_id
        stub = epistrel.models.stub
        await ck.set_clock(engine, story, "on")

        async def directed(text: str, parsed: str, **extra: object) -> Any:
            stub.reset()
            stub.when("", parsed, purpose="directive_parse")
            return await _directive(client, story, text=text, **extra)

        async def events() -> dict[str, Any]:
            found = await client.get(
                f"/stories/{story}/scheduled-events",
                params={"viewpoint": "omniscient"},
                headers=WRITE,
            )
            assert found.status_code == 200, found.text
            return {e["name"]: e for e in found.json()["events"]}

        made = await directed(
            "Bobby has a dentist appointment at minute 600.",
            _parsed(
                changes=[
                    {
                        "kind": "event",
                        "name": "the dentist",
                        "planned": 600,
                        "participants": ["Bobby"],
                    }
                ]
            ),
        )
        assert made.status_code == 200, made.text
        (event_id,) = made.json()["applied"]["scheduled_events"]
        event = (await events())["the dentist"]
        assert (event["owner"], event["planned"], event["participants"]) == (
            "author",
            600,
            [ed.bobby],
        )
        moved = await directed(
            "Move the dentist to minute 900.",
            _parsed(changes=[{"kind": "reschedule", "event": "the dentist", "planned": 900}]),
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["applied"]["scheduled_events"] == [event_id]
        assert (await events())["the dentist"]["planned"] == 900
        unknown = await directed(
            "Cancel the opera.", _parsed(changes=[{"kind": "cancel_event", "event": "the opera"}])
        )
        assert unknown.status_code == 422, unknown.text
        assert unknown.json()["error"]["code"] == "directive_ambiguous"
        assert unknown.json()["error"]["details"]["candidates"] == ["the dentist"]
        gone = await directed(
            "Cancel the dentist.",
            _parsed(changes=[{"kind": "cancel_event", "event": "the dentist"}]),
        )
        assert gone.status_code == 200, gone.text
        assert (await events())["the dentist"]["status"] == "cancelled"

        # A clock advance with a change to the past: split it.
        revision = await rs.revision(engine, story)
        mixed = await directed(
            "It is the next morning, and Bobby went to UCLA as a kid.",
            _parsed(
                temporal_scope="past",
                changes=[
                    {"kind": "clock", "minutes": 600},
                    {
                        "kind": "proposition",
                        "subject": "Bobby",
                        "predicate": "attended",
                        "object": "UCLA",
                    },
                ],
            ),
        )
        assert mixed.status_code == 422, mixed.text
        assert mixed.json()["error"]["code"] == "directive_split_required"
        assert await rs.revision(engine, story) == revision

        # Standing guidance: a steer, with the hint.
        standing = await directed(
            "Always describe the weather.",
            json.dumps(
                {
                    "classification": "ephemeral",
                    "steer_text": "Describe the weather.",
                    "standing": True,
                }
            ),
        )
        assert standing.status_code == 200, standing.text
        assert standing.json()["hints"] == ["use_custom_instructions"]

        # The steer budget: 500 tokens of queued steers.
        long = "Keep every reply hushed and slow, with the rain loud on the roof. " * 50
        refused = await _directive(client, story, text=long, kind="ephemeral")
        assert refused.status_code == 409, refused.text
        details = refused.json()["error"]["details"]
        assert details["max"] == 500
        assert details["tokens"] > 500
        (queued,) = await rows(
            engine,
            "SELECT count(*) AS n FROM directives WHERE story_id = :s "
            "AND disposition = 'steer_queued'",
            s=story,
        )
        assert queued.n == 1

        # The TTL: an old unconsumed steer expires; the fresh one stays.
        fresh = await _directive(client, story, text="Make it tense.", kind="ephemeral")
        assert fresh.status_code == 200, fresh.text
        await engine.dispose()
        async with engine.begin() as conn:
            from sqlalchemy import text

            await conn.execute(
                text("UPDATE directives SET created_at = now() - interval '2 days' WHERE id = :d"),
                {"d": standing.json()["directive_id"]},
            )
        assert await expire_steers(engine) >= 1
        found = {
            r.id: r.disposition
            for r in await rows(
                engine, "SELECT id, disposition FROM directives WHERE story_id = :s", s=story
            )
        }
        assert found[standing.json()["directive_id"]] == "expired"
        assert found[fresh.json()["directive_id"]] == "steer_queued"
