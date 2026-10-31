"""S-GOLD: objective truth vs character belief (docs/04 §7; FR-EXT-2,3, FR-VERIFY-7,10).

FR-VERIFY-7's M6 test. #191 adds the story-State part (``test_s_gold__states``); #207 adds the
external-provider variants (a registered balance provider's pinned value) to this file.

- ``test_s_gold__states``: Cal holds 300 gold, a player-visible State (tolerance 2), and canon
  holds a planted stale fact that his gold is 500. An objective claim of his gold verifies
  against the State's fold at its position, never the fact: "500" is ``contradicted`` citing
  the State (no fact), "300" and "301" are ``supported``. Every read is a query key of the
  turn's read set, and a value changed before the commit fails #140's rebase re-check, so the
  verdict is revalidated at commit. ADR-160: under a declared rule ``holds ⇒ state_value`` (the
  purse the State tracks), an affirmed "Cal holds 500 gold in his purse" is ``contradicted``
  through the rule and the fold; "300" is not. Played through ``respond`` (a response in Cal's
  own viewpoint): the narrator's "Cal has 500 gold left" is contradicted and repaired, while
  "300 gold left" stands; neither is canonized.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind, StateChangeSource
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Implication, Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, STATE_VALUE
from epistrel.core.readset import StructuredReadSet
from epistrel.core.states import verify as sv
from epistrel.core.verify import rules
from tests import canon_support as c
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import scalar
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

HOLDS = CORE_BY_NAME["holds"].id


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _gold(cal: str, gold: str, amount: int) -> Proposition:
    return sv.proposition(gold, cal, sv.number_literal(amount))


def _purse(cal: str, purse: str, amount: int) -> Proposition:
    return Proposition(
        subject_id=cal,
        predicate_id=HOLDS,
        object_id=purse,
        qualifiers={"quantity": f"{amount} gold"},
        valid_time=Interval.of(0, None),
    )


async def _verify(
    engine: AsyncEngine, story: str, claim: Proposition, read_set: StructuredReadSet
) -> rules.Verdict:
    async with engine.connect() as conn:
        found = await rules.verify(conn, read_set, story, claim)
    assert found is not None
    return found


@pytest.mark.part("M6", "FR-VERIFY-7")
async def test_s_gold__states(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        purse = await ps.place_entity(engine, story, "purse")  # any entity the State tracks
        (gold,) = await ss.define_all(
            engine,
            story,
            es.gold(constraints={"non_negative": True, "tolerance": 2}, entity_id=purse),
        )
        assert gold.id is not None
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold", 300, scope_key=cal, reason="a purse", source=StateChangeSource.CONSUMER
            ),
        )
        # The planted stale fact: canon says his gold is 500.
        stale = await c.commit(
            engine, story, _gold(cal, gold.id, 500), kind=OperationKind.CONTROL, provenance="author"
        )

        # -- An objective claim verifies against the fold, never the stale fact.
        read_set = StructuredReadSet()
        read_set.start_attempt()
        wrong = await _verify(engine, story, _gold(cal, gold.id, 500), read_set)
        assert wrong.kind is rules.VerdictKind.CONTRADICTED
        assert [(e.owner_ref, e.fact_id) for e in wrong.evidence] == [(f"state:{gold.id}", None)]
        assert stale.id not in {e.fact_id for e in wrong.evidence}
        for amount in (300, 301):
            right = await _verify(engine, story, _gold(cal, gold.id, amount), read_set)
            assert right.kind is rules.VerdictKind.SUPPORTED, amount
        assert (await _verify(engine, story, _gold(cal, gold.id, 303), read_set)).kind is (
            rules.VerdictKind.CONTRADICTED
        )
        keys = [k for k in read_set.query_keys if k.predicate_id == STATE_VALUE]
        assert keys
        assert {k.reading for k in keys} == {"owner"}

        # -- Revalidated at commit: the rebase re-check re-reads the value.
        operation = SimpleNamespace(read_set_source=read_set)
        async with engine.connect() as conn:
            assert await rules.recheck(conn, operation) is True  # type: ignore[arg-type]
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold",
                500,
                scope_key=cal,
                reason="a windfall",
                source=StateChangeSource.CONSUMER,
                position=Position(await _head(engine, story), 0, 1),
            ),
            kind=OperationKind.CONTROL,
        )
        async with engine.connect() as conn:
            assert await rules.recheck(conn, operation) is False  # type: ignore[arg-type]
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold",
                300,
                scope_key=cal,
                reason="the windfall spent",
                source=StateChangeSource.CONSUMER,
                position=Position(await _head(engine, story), 0, 2),
            ),
            kind=OperationKind.CONTROL,
        )
        assert await ss.value(engine, story, "gold", cal) == 300

        # -- ADR-160: a declared rule from holds to state_value checks the implied value.
        no_rule = await _verify(engine, story, _purse(cal, purse, 500), StructuredReadSet())
        assert no_rule.kind is not rules.VerdictKind.CONTRADICTED
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET predicate_rules = CAST(:r AS jsonb) WHERE id = :s"),
                {
                    "s": story,
                    "r": json.dumps([Implication(when=HOLDS, then=STATE_VALUE).model_dump()]),
                },
            )
        implied = await _verify(engine, story, _purse(cal, purse, 500), StructuredReadSet())
        assert implied.kind is rules.VerdictKind.CONTRADICTED
        assert [(e.rule, e.owner_ref) for e in implied.evidence] == [
            (rules.Rule.IMPLICATION, None),
            (rules.Rule.FUNCTIONAL_KEY, f"state:{gold.id}"),
        ]
        same = await _verify(engine, story, _purse(cal, purse, 300), StructuredReadSet())
        assert same.kind is not rules.VerdictKind.CONTRADICTED
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET predicate_rules = '[]'::jsonb WHERE id = :s"),
                {"s": story},
            )

        # -- Through respond, in Cal's own viewpoint: the narrator's claims take the objective
        # path; the wrong one is repaired, the right one stands, and neither is canonized.
        facts_before = await _state_facts(engine, story)
        for said, kind in (("500", "contradicted"), ("300", "supported")):
            stub.reset()
            narration = f"Rain taps the shutters. Cal has {said} gold left."
            es.narrator(stub, es.render(beat("narration", narration)))
            ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
            answer = await respond(client, story, body(cal, None, do="checks his purse"))
            assert answer.status_code == 200, answer.text
            report = await turn_report(engine, story)
            (verdict,) = [
                v
                for v in report["verify"]["verdicts"]
                if v.get("kind") in ("supported", "contradicted")
            ]
            assert verdict["kind"] == kind, said
            text_out = answer.json()["message"]["text"]
            if kind == "contradicted":
                assert [r["rung"] for r in report["repairs"]] == [1]
                assert f"{said} gold" not in text_out
            else:
                assert "repairs" not in report
                assert f"{said} gold left" in text_out
        assert await _state_facts(engine, story) == facts_before
        assert await ss.value(engine, story, "gold", cal) == 300


async def _head(engine: AsyncEngine, story: str) -> int:
    found = await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
    return int(found) - 1


async def _state_facts(engine: AsyncEngine, story: str) -> int:
    """How many canon facts of ``state_value`` the story holds."""
    found = await scalar(
        engine,
        "SELECT count(*) FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND p.predicate_id = :v",
        s=story,
        v=STATE_VALUE,
    )
    return int(found or 0)
