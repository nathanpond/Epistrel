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
- ``test_s_gold`` (#207): the external owner. A registered balance provider answers 300; the
  four variants (the narrator's 500; a told, a groundless and a lying 500 from Mira) each read
  the value once, and classify exactly as with a story-owned State (the complement). Later
  drift and frame-bounded reads never change a recorded turn's value; a planted stale fact
  and the pin are no character's knowledge.
- ``test_s_gold__advisory`` (#207): an ``advisory`` provider's value settles nothing either
  way; switched to ``authoritative`` it behaves as above.
- ``test_s_gold__no_leak`` (#207): a character's render inputs carry an external value only as
  that character's belief; the narrator's carry every pinned one.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import states as core_states
from epistrel.core.enums import OperationKind, StateChangeSource
from epistrel.core.epistemic.transitions import Kind
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Implication, Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, STATE_VALUE
from epistrel.core.readset import StructuredReadSet
from epistrel.core.states import external as external_values
from epistrel.core.states import verify as sv
from epistrel.core.verify import rules
from epistrel.core.visibility import Visibility
from tests import canon_support as c
from tests import effect_support as es
from tests import multi_support as ms
from tests import partition_support as parts
from tests import perception_support as ps
from tests import provider_support as pv
from tests import state_support as ss
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
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


# -- #207: the external owner (FR-EXT-2,3, FR-VERIFY-7,10) ------------------------------------

KNOWS = "You have {} gold."
GREETING = "Welcome back to the forge."


async def _forge(
    client: Any, engine: AsyncEngine, *, provider: str | None
) -> tuple[str, dict[str, str], str]:
    """Cal (the player) and Mira the smith at the forge under per-character partitions
    (testimony ``defaulted``, perception ``co_location``), Cal holding 300 gold: owned by
    ``provider`` (an in-process provider answering 300), or story-owned when ``None``. Answers
    the story, the cast and the gold State's id."""
    story, ids = await structured_story(client, "Cal", "Mira")
    await ts.enable_testimony(engine, story, "defaulted", level="co_location")
    await ss.set_level(engine, story, "on")
    forge = await ps.place_entity(engine, story, "forge")
    for character in ids.values():
        await ps.put(engine, story, character, forge)
    if provider is not None:
        gold = await pv.external_gold(engine, story, provider)
    else:
        (gold,) = await ss.define_all(engine, story, ss.gold())
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold",
                300,
                scope_key=ids["Cal"],
                reason="a purse",
                source=StateChangeSource.CONSUMER,
            ),
        )
    assert gold.id is not None
    return story, ids, gold.id


async def _turn(
    client: Any, stub: Any, story: str, cal: str, *beats: dict[str, Any], narration: str
) -> dict[str, Any]:
    """A turn: Cal leans on the counter, the narrator narrates ``narration`` and Mira answers
    ``beats``."""
    stub.reset()
    ms.renders_for(stub, "Mira", es.render(*beats))
    es.narrator(stub, es.render(beat("narration", narration)))
    answer = await respond(client, story, body(cal, None, do="leans on the counter"))
    assert answer.status_code == 200, answer.text
    played: dict[str, Any] = answer.json()
    return played


async def _speech(engine: AsyncEngine, story: str) -> dict[str, Any]:
    """The speech dimensions of the latest turn's one first-person claim, with its kind and
    the turn's repairs."""
    report = await turn_report(engine, story)
    found = [v for v in report["verify"]["verdicts"] if "speech" in v]
    assert len(found) == 1, found
    speech: dict[str, Any] = found[0]["speech"]
    return {**speech, "kind": found[0]["kind"], "repairs": report.get("repairs", [])}


def _objective(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The turn's owner verdicts with no speech dimension (narration, renders): settled and
    citing no fact (an owner's value is cited by its ``owner_ref``, FR-VERIFY-7)."""
    return [
        v
        for v in report["verify"]["verdicts"]
        if "speech" not in v
        and v.get("kind") in ("supported", "contradicted", "uncertain")
        and not v.get("evidence_ids")
    ]


async def _gold_beliefs(engine: AsyncEngine, story: str, holder: str) -> list[Any]:
    """``holder``'s open ``state_value`` beliefs with their value and producing transition."""
    return await rows(
        engine,
        "SELECT b.id, p.literal, t.kind FROM beliefs b "
        "JOIN propositions p ON p.id = b.proposition_id "
        "JOIN belief_transitions t ON t.id = b.provenance "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND p.predicate_id = :v "
        "AND b.held_to_turn IS NULL AND b.tx_to IS NULL",
        s=story,
        h=holder,
        v=STATE_VALUE,
    )


async def _one_read(engine: AsyncEngine, story: str, turn: int, value: int = 300) -> None:
    """The turn read the gold once, at its input: one ``external_read`` of ``value``."""
    found = [r for r in await pv.external_reads(engine, story) if r.narrative_turn == turn]
    assert [(r.payload["value_name"], r.payload["value"], r.beat) for r in found] == [
        ("gold", value, 0)
    ]


async def _variants(
    client: Any, engine: AsyncEngine, stub: Any, provider: str | None
) -> list[tuple[Any, ...]]:
    """S-GOLD's four variants, each over a fresh story (see ``test_s_gold``); answers what each
    showed, comparable across owner kinds."""
    seen: list[tuple[Any, ...]] = []
    external = provider is not None

    # -- (1) The narrator states 500: contradicted against the owner's value, repaired.
    story, ids, gold = await _forge(client, engine, provider=provider)
    cal, mira = ids["Cal"], ids["Mira"]
    played = await _turn(
        client, stub, story, cal, beat("say", GREETING), narration="Cal has 500 gold in all."
    )
    report = await turn_report(engine, story)
    (verdict,) = _objective(report)
    assert verdict["kind"] == "contradicted"
    (repair,) = report["repairs"]
    assert repair["tag"] == ("unpermitted" if external else None)  # #207's external_value
    assert "500 gold" not in played["message"]["text"]
    assert verdict.get("permission") == ("denied:external_value" if external else None)
    seen.append(("narrator", verdict["kind"], len(report["repairs"])))
    if external:
        await _one_read(engine, story, played["turn"])

    # -- (3) No antecedent: Mira's sincere 500 is a hallucination, repaired; nothing believed.
    story, ids, gold = await _forge(client, engine, provider=provider)
    cal, mira = ids["Cal"], ids["Mira"]
    played = await _turn(
        client,
        stub,
        story,
        cal,
        beat("say", f"{KNOWS.format(500)} {GREETING}", intent="sincere"),
        narration="The forge glows.",
    )
    found = await _speech(engine, story)
    assert (found["classification"], found["belief_relation"], found["kind"]) == (
        "hallucination_repaired",
        "unknown",
        "contradicted",
    )
    assert [r["rung"] for r in found["repairs"]] == [1]
    assert "500" not in played["message"]["text"]
    assert await _gold_beliefs(engine, story, mira) == []
    seen.append(("no_antecedent", found["classification"], len(found["repairs"])))
    if external:
        await _one_read(engine, story, played["turn"])

    # -- (2) Told 500 earlier by Cal, whom she trusts (a testimony transition, ADR-170): her
    # sincere 500 is a legitimate false belief, standing and never corrected.
    story, ids, gold = await _forge(client, engine, provider=provider)
    cal, mira = ids["Cal"], ids["Mira"]
    told = await parts.transit(
        engine,
        story,
        mira,
        sv.proposition(gold, cal, sv.number_literal(500)),
        cause=Kind.TESTIMONY,
        source=cal,
        kind=OperationKind.CONTROL,
    )
    assert told.transition.kind is Kind.TESTIMONY
    played = await _turn(
        client,
        stub,
        story,
        cal,
        beat("say", f"{KNOWS.format(500)} {GREETING}", intent="sincere"),
        narration="The forge glows.",
    )
    found = await _speech(engine, story)
    assert (found["classification"], found["belief_relation"], found["kind"]) == (
        "sincere_error",
        "consistent",
        "contradicted",
    )
    assert found["repairs"] == []
    assert KNOWS.format(500) in played["message"]["text"]
    seen.append(("told", found["classification"], len(found["repairs"])))
    if external:
        await _one_read(engine, story, played["turn"])

    # -- (4) She watched the count (an observation transition, #160): a deceptive 500 against
    # her belief of 300 is a lie, standing, its divergence recorded.
    story, ids, gold = await _forge(client, engine, provider=provider)
    cal, mira = ids["Cal"], ids["Mira"]
    watched = await ts.observed(
        engine, story, mira, sv.proposition(gold, cal, sv.number_literal(300))
    )
    played = await _turn(
        client,
        stub,
        story,
        cal,
        beat("think", "Cal has 300 gold."),
        beat("say", KNOWS.format(500), intent="deceptive"),
        narration="The forge glows.",
    )
    found = await _speech(engine, story)
    assert (found["classification"], found["belief_relation"]) == ("lie", "inconsistent")
    assert found["contrary_belief_ids"] == [watched.belief.id]
    assert found["repairs"] == []
    (divergence,) = await rows(
        engine, "SELECT speaker_character_id FROM divergences WHERE story_id = :s", s=story
    )
    assert str(divergence.speaker_character_id) == mira
    seen.append(("lie", found["classification"], len(found["repairs"])))
    if external:
        await _one_read(engine, story, played["turn"])

    # -- A think alone is self-attestation and forms no belief: the same lie with no watched
    # count is a hallucination (no contrary belief, and canon settles the value).
    story, ids, gold = await _forge(client, engine, provider=provider)
    cal, mira = ids["Cal"], ids["Mira"]
    await _turn(
        client,
        stub,
        story,
        cal,
        beat("think", "Cal has 300 gold."),
        beat("say", KNOWS.format(500), intent="deceptive"),
        narration="The forge glows.",
    )
    found = await _speech(engine, story)
    assert found["classification"] == "hallucination_repaired"
    assert await _gold_beliefs(engine, story, mira) == []
    seen.append(("think_only", found["classification"], len(found["repairs"])))
    return seen


@pytest.mark.part("M6", "FR-EXT-2,3; FR-VERIFY-7,10")
async def test_s_gold(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """S-GOLD (#207): a registered balance provider answers 300 for Cal's gold. (1) The
    narrator's "500" is contradicted against the pinned value, ``denied:external_value`` and
    repaired. (2) Mira, told 500 by Cal (a testimony transition), sincerely says so: a
    legitimate false belief, standing. (3) With no antecedent: a hallucination, repaired, no
    belief written. (4) Mira watched the count (an observation of 300) and declares a
    deceptive 500: a lie, its divergence recorded; a ``think`` alone forms no belief (the same
    lie without the observation is a hallucination). Every turn reads the gold once (one
    ``external_read``). Later drift changes nothing: verified afterwards, the recorded turn's
    value is still 300 (no new read), and a frame-bounded read inside a later turn (#165's
    replay) takes the version pinned at or before its bound, never the later pin.
    FR-VERIFY-7: a planted stale canon fact of 500 supports nothing, and a belief holder's
    canon fallback (ADR-167) never answers an external value: Mira's unbelieved "500" stays a
    hallucination. Complement: a story-owned State with the same variants classifies
    identically."""
    name = pv.unique_name("ledger")
    await pv.register(engine, pv.in_process_spec(name, {"gold": "number"}))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = pv.ScriptedProvider(name).attach(epistrel)
        fake.script("gold", 300)
        external = await _variants(client, engine, stub, name)
        owned = await _variants(client, engine, stub, None)
        assert external == owned  # the owner kind is invisible to the classification

        # -- Later drift changes nothing; a frame-bounded read takes the pin at or before it.
        story, ids, gold = await _forge(client, engine, provider=name)
        cal = ids["Cal"]
        played = await _turn(
            client, stub, story, cal, beat("say", GREETING), narration="Cal has 300 gold in all."
        )
        first = played["turn"]
        fake.script("gold", 250)
        reads = len(fake.reads)
        for amount, kind in ((300, "supported"), (500, "contradicted"), (250, "contradicted")):
            claim = sv.proposition(gold, cal, sv.number_literal(amount))
            async with engine.connect() as conn:
                found = await rules.verify(
                    conn, StructuredReadSet(), story, claim, through_position=(first, 0)
                )
            assert found is not None
            assert found.kind.value == kind, amount
        assert len(fake.reads) == reads  # nothing reads live outside a turn
        later = await _turn(
            client, stub, story, cal, beat("say", GREETING), narration="Wind rattles."
        )
        await _one_read(engine, story, later["turn"], 250)
        async with engine.connect() as conn:
            defined = await core_states.definitions(conn, story)
            pins = _Pins(later["turn"], 250)
            with external_values.pinning(pins):
                bounded = await external_values.pinned_values(
                    conn, story, defined, [(gold, cal)], through=Position.end_of(first)
                )
                current = await external_values.pinned_values(
                    conn, story, defined, [(gold, cal)], through=Position.end_of(later["turn"])
                )
        assert [p.value for p in bounded.values()] == [300]
        assert [p.value for p in current.values()] == [250]

        # -- FR-VERIFY-7: a planted stale canon fact (public: every viewpoint may read it)
        # never answers, for canon or for Mira; nor does the pin answer for her (ADR-167's
        # canon fallback is not consulted for an external value).
        fake.script("gold", 300)
        story, ids, gold = await _forge(client, engine, provider=name)
        cal = ids["Cal"]
        stale = await c.commit(
            engine,
            story,
            _gold(cal, gold, 500),
            kind=OperationKind.CONTROL,
            provenance="author",
            visibility=Visibility(scope="public"),
        )
        await _turn(
            client, stub, story, cal, beat("say", GREETING), narration="Cal has 500 gold in all."
        )
        (verdict,) = _objective(await turn_report(engine, story))
        assert verdict["kind"] == "contradicted"
        assert stale.id not in json.dumps(verdict)
        mira = ids["Mira"]
        for amount, canon_kind in ((500, "contradicted"), (300, "supported")):
            claim = sv.proposition(gold, cal, sv.number_literal(amount))
            async with engine.connect() as conn:
                held = await rules.verify(
                    conn,
                    StructuredReadSet(),
                    story,
                    claim,
                    holder=rules.Holder.belief(mira),
                    viewpoint=f"character:{mira}",
                )
                objective = await rules.verify(conn, StructuredReadSet(), story, claim)
            assert held is not None
            assert objective is not None
            # Neither the public stale fact nor the pin is Mira's knowledge: novel to her.
            assert (held.kind.value, objective.kind.value) == ("novel", canon_kind), amount
        await _turn(
            client,
            stub,
            story,
            cal,
            beat("say", f"{KNOWS.format(300)} {GREETING}", intent="sincere"),
            narration="The forge glows.",
        )
        found_speech = await _speech(engine, story)
        assert (found_speech["classification"], found_speech["belief_relation"]) == (
            "hallucination_repaired",
            "unknown",
        )


async def _tier(engine: AsyncEngine, provider: str, tier: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE external_providers SET trust_tier = :t WHERE name = :n"),
            {"t": tier, "n": provider},
        )


@pytest.mark.part("M6", "FR-EXT-3")
async def test_s_gold__advisory(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """The ``advisory`` trust tier (#207, the owner's decision 2026-10-30): the provider
    answers 300, and its value settles nothing either way. The narrator's "300" is
    ``uncertain(advisory)``, not ``supported`` (so no external fact is committed for it); its
    "500" is ``uncertain(advisory)`` too, never ``contradicted``: it takes the profile's safe
    disposition (#151's undecided-claim handling), not a contradiction's repair, and is no
    ``external_value`` denial; no model is asked (the reason never escalates). Complement: the
    same provider switched to ``authoritative`` gives ``supported`` and ``contradicted``, the
    latter repaired."""
    name = pv.unique_name("ledger")
    await pv.register(engine, pv.in_process_spec(name, {"gold": "number"}, trust_tier="advisory"))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = pv.ScriptedProvider(name).attach(epistrel)
        fake.script("gold", 300)
        story, ids, _gold_id = await _forge(client, engine, provider=name)
        cal = ids["Cal"]
        for tier, said, kind in (
            ("advisory", 300, "uncertain"),
            ("advisory", 500, "uncertain"),
            ("authoritative", 300, "supported"),
            ("authoritative", 500, "contradicted"),
        ):
            await _tier(engine, name, tier)
            played = await _turn(
                client,
                stub,
                story,
                cal,
                beat("say", GREETING),
                narration=f"Cal has {said} gold in all.",
            )
            report = await turn_report(engine, story)
            (verdict,) = _objective(report)
            assert verdict["kind"] == kind, (tier, said)
            assert not verdict["escalated"]
            assert "entailment" not in report["budget"]["used_by_kind"]
            repairs = report.get("repairs", [])
            if tier == "advisory":
                assert verdict["reason"] == "advisory"
                assert verdict["disposition"] == "repair"  # the profile's safe disposition
                assert verdict.get("permission") is None  # no external_value denial
                assert [r["verdict"] for r in repairs] == ["uncertain"]  # hedged, not corrected
                assert "300 gold" not in played["message"]["text"]  # nothing asserts the value
            elif kind == "supported":
                assert repairs == []
                assert f"{said} gold in all" in played["message"]["text"]
            else:
                assert verdict["permission"] == "denied:external_value"
                assert [r["verdict"] for r in repairs] == ["denied"]
                assert f"{said} gold" not in played["message"]["text"]
        # Only the authoritative "300" committed an external fact (FR-EXT-4: a supported
        # reference); the advisory turns committed none.
        assert len(await pv.external_facts(engine, story)) == 1


def _shows(prompt: str, value: int) -> bool:
    """Whether ``prompt`` states ``value`` as a number of its own (not inside an id)."""
    return re.search(rf"(?<![\w.]){value}(?![\w])", prompt) is not None


@pytest.mark.part("M6", "FR-EXT-3")
async def test_s_gold__no_leak(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """S-GOLD's no-leak clause (#207, NFR-PRIV-4), a property over random external States:
    for each seed, a provider owns several player-visible values (story-scoped or Cal's, each a
    random number) pinned at the turn's input. The narrator's inputs carry every pinned value
    (``narrator_inputs`` may); Mira's render inputs (``character_inputs`` over
    ``build_context``, the only source of a character's knowledge) carry none of them until she
    holds a belief in one, and then that one alone, as her belief."""
    for seed in (207, 2071, 20710):
        rng = random.Random(seed)  # noqa: S311 - test data, not a secret
        count = rng.randint(2, 4)
        names = [f"tally{i}" for i in range(count)]
        values = rng.sample(range(10_000, 99_999), count)
        scopes = [rng.choice(("story", "character")) for _ in names]
        provider = pv.unique_name("ledger")
        await pv.register(engine, pv.in_process_spec(provider, dict.fromkeys(names, "number")))
        async with respond_client(stub_settings, engine) as (client, epistrel):
            stub = epistrel.models.stub
            fake = pv.ScriptedProvider(provider).attach(epistrel)
            for value_name, value in zip(names, values, strict=True):
                fake.script(value_name, value)
            story, ids = await structured_story(client, "Cal", "Mira")
            await ts.enable_testimony(engine, story, "defaulted", level="co_location")
            await ss.set_level(engine, story, "on")
            forge = await ps.place_entity(engine, story, "forge")
            for character in ids.values():
                await ps.put(engine, story, character, forge)
            cal, mira = ids["Cal"], ids["Mira"]
            await pv.bind(engine, story, provider, {"player": "p-1"})
            defined = await ss.define_all(
                engine,
                story,
                *(
                    ss.gold(
                        name=value_name,
                        scope=scope,
                        aliases=(),
                        unit=None,
                        owner="external",
                        provider_id=provider,
                    )
                    for value_name, scope in zip(names, scopes, strict=True)
                ),
            )

            await _turn(client, stub, story, cal, beat("say", GREETING), narration="Rain taps.")
            (narrator,) = es.prompts(stub, "NarratorRender")
            (character,) = ms.render_prompts(stub, "Mira")
            assert all(_shows(narrator, v) for v in values), seed
            assert not [v for v in values if _shows(character, v)], seed

            pick = rng.randrange(count)
            state = defined[pick]
            assert state.id is not None
            subject = state.id if scopes[pick] == "story" else cal
            await parts.believe(
                engine,
                story,
                mira,
                sv.proposition(state.id, subject, sv.number_literal(values[pick])),
                kind=OperationKind.CONTROL,
            )
            await _turn(client, stub, story, cal, beat("say", GREETING), narration="Rain taps.")
            (narrator,) = es.prompts(stub, "NarratorRender")
            (character,) = ms.render_prompts(stub, "Mira")
            assert all(_shows(narrator, v) for v in values), seed
            assert [v for v in values if _shows(character, v)] == [values[pick]], seed


@dataclass
class _Pins:
    """A turn's pins as Core reads them (``core.states.external.PinSource``): one value of
    every key, read at the turn's input."""

    turn: int
    value: int

    def pinned(self, state_id: str, scope_key: str | None, through: Position | None) -> Any:
        at = Position(self.turn)
        if through is not None and not at.at_or_before(through):
            return None
        return external_values.ExternalPin(state_id, scope_key, Decimal(self.value), at)
