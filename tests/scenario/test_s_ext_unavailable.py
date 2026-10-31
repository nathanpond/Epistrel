"""S-EXT-UNAVAILABLE: the provider is down (docs/04 §7; FR-EXT-7, FR-EXT-11; #206).

A registered balance provider owns the player Cal's gold; Bobby, the smith, shares the forge
with him, under per-character partitions. The provider goes offline (#204's scripted
error and timeout injection) and each unavailability policy is applied:

- ``fail_turn``: the turn fails cleanly (503 ``provider_unavailable {provider, value}``) and
  nothing is committed (no event, no ``external_read``).
- ``last_known`` within the freshness window (60 minutes by default): the recorded value is
  pinned, an ``external_read`` with ``source = last_known`` and the live read's
  ``origin_event_id`` is written (its ``external_values`` row ``last_known``), the narration of
  it is ``supported`` and the report lists the policy with the value's age.
- ``last_known`` past the window, or ``unknown``: the value is pinned ``Unknown``
  (``external_read`` ``source = unknown``, ``value = null``). Narration may not state the
  balance (``uncertain(provider_unknown)``, the safe disposition repairs it), and no character
  newly learns it: under ``surface`` the narration stands but no observation of it is written.
  Bobby, who counted 500 earlier (a #160 observation), may say "You have 500 gold": sincere,
  belief-consistent, with an ``unresolved`` objective relation; a deliberate lie about it is
  verified against his beliefs and its divergence recorded.
- Complement: a story-owned State is unaffected by the outage; with the adapter back the next
  turn reads live and carries no policy entry; a rebuild and a fork of the ``Unknown`` turn
  make no adapter call and pin ``Unknown`` again. A malformed answer (#208) takes the policy
  too. Changing a provider's policy is an interpretive configuration change (#64).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import projections
from epistrel.core.propositions.vocabulary import STATE_VALUE
from epistrel.core.readset import StructuredReadSet
from epistrel.core.states import verify as sv
from epistrel.core.verify import rules
from epistrel.orchestrator.providers import pinned, registry, unavailability
from epistrel.orchestrator.providers.manifest import ProviderSpec
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import provider_support as pv
from tests import state_support as ss
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fork_support import fork
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

KNOWS = "You have {} gold."
GREETING = "Welcome back to the forge."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _forge(
    client: Any, engine: AsyncEngine, provider: str, *, surface: bool = False
) -> tuple[str, dict[str, str], str]:
    """Cal (the player) and Bobby the smith at the forge under per-character partitions
    (testimony ``defaulted``, perception ``co_location``), Cal's gold owned by ``provider``
    and a story-owned ``silver`` State (7 each); under ``surface`` an undecided claim stands
    (``verification.safe_disposition``). Answers the story, the cast and the gold State's id."""
    story, ids = await structured_story(client, "Cal", "Bobby")
    await ts.enable_testimony(engine, story, "defaulted", level="co_location")
    await ss.set_level(engine, story, "on")
    if surface:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE story_profile_overrides SET settings = settings || "
                    '\'{"verification": {"safe_disposition": "surface"}}\'::jsonb, '
                    "revision = revision + 1 WHERE story_id = :s"
                ),
                {"s": story},
            )
    forge = await ps.place_entity(engine, story, "forge")
    for character in ids.values():
        await ps.put(engine, story, character, forge)
    gold = await pv.external_gold(engine, story, provider)
    await ss.define_all(
        engine,
        story,
        ss.gold(name="silver", aliases=(), unit=None, initial_value=7),
    )
    assert gold.id is not None
    return story, ids, gold.id


async def _turn(
    client: Any, stub: Any, story: str, cal: str, *beats: dict[str, Any], narration: str
) -> Any:
    """A turn: Cal leans on the counter, the narrator narrates ``narration`` and Bobby answers
    ``beats``. Answers the response."""
    stub.reset()
    ms.renders_for(stub, "Bobby", es.render(*(beats or (beat("say", GREETING),))))
    es.narrator(stub, es.render(beat("narration", narration)))
    return await respond(client, story, body(cal, None, do="leans on the counter"))


async def _policy(engine: AsyncEngine, name: str, policy: str, **fields: Any) -> None:
    """Replace provider ``name``'s unavailability policy (a ``PUT``)."""
    _, row = await registry.entry(engine, name)
    spec = ProviderSpec.model_validate(
        {
            "trust_tier": row.trust_tier,
            "transport": row.transport,
            "manifest": row.manifest,
            "unavailability": policy,
            **fields,
        }
    )
    await registry.replace(engine, name, spec, principal=pv.PRINCIPAL)


def _settled(report: dict[str, Any]) -> set[tuple[str, str | None]]:
    """The ``(kind, reason)`` of the turn's verdicts on owner values: no speech dimension, no
    cited fact (an owner's value is cited by its ``owner_ref``, FR-VERIFY-7)."""
    found = {
        (v["kind"], v.get("reason"))
        for v in report["verify"]["verdicts"]
        if "speech" not in v and not v.get("evidence_ids")
    }
    assert found
    return found


async def _speech(engine: AsyncEngine, story: str) -> dict[str, Any]:
    report = await turn_report(engine, story)
    found = [v for v in report["verify"]["verdicts"] if "speech" in v]
    assert len(found) == 1, found
    return {
        **found[0]["speech"],
        "kind": found[0]["kind"],
        "reason": found[0].get("reason"),
        "repairs": report.get("repairs", []),
    }


async def _gold_transitions(engine: AsyncEngine, story: str, gold: str) -> list[Any]:
    """Every belief transition of the story about the gold State: its holder and kind."""
    return await rows(
        engine,
        "SELECT b.holder_character_id, t.kind, p.literal FROM belief_transitions t "
        "JOIN beliefs b ON b.id = t.belief_id JOIN propositions p ON p.id = b.proposition_id "
        "WHERE b.story_id = :s AND p.predicate_id = :v AND p.qualifiers ->> 'state' = :g "
        "ORDER BY t.id",
        s=story,
        v=STATE_VALUE,
        g=gold,
    )


async def _counts(engine: AsyncEngine, story: str) -> tuple[int, int, int, int]:
    """The story's events, committed operations, ``external_values`` rows and next turn."""
    found = await rows(
        engine,
        "SELECT (SELECT count(*) FROM events WHERE story_id = :s) AS events, "
        "(SELECT count(*) FROM operations WHERE story_id = :s AND status = 'committed') AS ops, "
        "(SELECT count(*) FROM external_values WHERE story_id = :s) AS reads, "
        "(SELECT next_turn_index FROM stories WHERE id = :s) AS head",
        s=story,
    )
    row = found[0]
    return (int(row.events), int(row.ops), int(row.reads), int(row.head))


def _later(minutes: int) -> Callable[[], datetime]:
    return lambda: datetime.now(UTC) + timedelta(minutes=minutes)


@pytest.mark.part("M6", "FR-EXT-7,11")
async def test_s_ext_unavailable(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """See the module docstring."""
    name = pv.unique_name("ledger")
    await pv.register(engine, pv.in_process_spec(name, {"gold": "number"}))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = pv.ScriptedProvider(name).attach(epistrel)

        # -- fail_turn: a clean 503, nothing committed. ---------------------------------------
        await _policy(engine, name, "fail_turn")
        story, ids, gold = await _forge(client, engine, name)
        cal = ids["Cal"]
        fake.script("gold", pv.Fail("unreachable"))
        before = await _counts(engine, story)
        answer = await _turn(client, stub, story, cal, narration="The forge glows.")
        assert answer.status_code == 503, answer.text
        error = answer.json()["error"]
        assert (error["code"], error["details"]) == (
            "provider_unavailable",
            {"provider": name, "value": "gold"},
        )
        assert await _counts(engine, story) == before
        assert await pv.external_reads(engine, story) == []

        # -- last_known within the window (60 minutes by default). ---------------------------
        await _policy(engine, name, "last_known")
        changes = await rows(
            engine,
            "SELECT setting, class, old, new FROM config_changes WHERE setting = :k "
            "ORDER BY config_revision DESC LIMIT 1",
            k=f"external_provider:{name}.unavailability",
        )
        assert [tuple(r) for r in changes] == [
            (f"external_provider:{name}.unavailability", "interpretive", "fail_turn", "last_known")
        ]
        story, ids, gold = await _forge(client, engine, name)
        cal = ids["Cal"]
        fake.script("gold", Decimal(300))
        live = await _turn(client, stub, story, cal, narration="Cal has 300 gold in all.")
        assert live.status_code == 200, live.text
        assert unavailability.REPORT_KEY not in await turn_report(engine, story)
        (origin,) = await pv.external_reads(engine, story)
        assert origin.payload["source"] == "live"

        # The adapter now times out: a hung in-process callable is cut at the read timeout.
        monkeypatch.setattr(pinned, "READ_TIMEOUT_SECONDS", 0.2)
        fake.script("gold", pv.Timeout(seconds=30))
        reads = len(fake.reads)
        started = time.monotonic()
        played = await _turn(client, stub, story, cal, narration="Cal has 300 gold in all.")
        assert played.status_code == 200, played.text
        assert time.monotonic() - started < 20  # cut at the timeout, never the 30 s hang
        assert len(fake.reads) == reads + 1  # one attempt, no retry
        report = await turn_report(engine, story)
        assert report[unavailability.REPORT_KEY] == [
            {
                "provider": name,
                "value": "gold",
                "policy": "last_known",
                "age_minutes": 0,
                "reason": "timeout",
            }
        ]
        assert _settled(report) == {("supported", None)}
        assert "300 gold" in played.json()["message"]["text"]
        stood_in = (await pv.external_reads(engine, story))[-1]
        assert stood_in.narrative_turn == played.json()["turn"]
        assert {k: stood_in.payload.get(k) for k in ("source", "value", "origin_event_id")} == {
            "source": "last_known",
            "value": 300,
            "origin_event_id": origin.id,
        }
        rows_now = await pv.external_value_rows(engine, story)
        assert [(r["value"], str(r["source"])) for r in rows_now][-1] == (300, "last_known")

        # -- last_known past the window: Unknown. --------------------------------------------
        wall_clock = unavailability.now
        monkeypatch.setattr(unavailability, "now", _later(61))
        fake.script("gold", pv.Fail("unreachable"))
        played = await _turn(client, stub, story, cal, narration="Cal has 300 gold in all.")
        assert played.status_code == 200, played.text
        report = await turn_report(engine, story)
        assert report[unavailability.REPORT_KEY] == [
            {
                "provider": name,
                "value": "gold",
                "policy": "unknown",
                "age_minutes": None,
                "reason": "unreachable",
            }
        ]
        assert _settled(report) == {("uncertain", "provider_unknown")}
        assert "300 gold" not in played.json()["message"]["text"]  # the safe disposition
        unknown = (await pv.external_reads(engine, story))[-1]
        assert {k: unknown.payload.get(k) for k in ("source", "value")} == {
            "source": "unknown",
            "value": None,
        }
        monkeypatch.setattr(unavailability, "now", wall_clock)

        # -- unknown: narration may not state it, nobody newly learns it; Bobby's belief and
        # lies keep their semantics; a story-owned State is unaffected. ---------------------
        await _policy(engine, name, "unknown")
        story, ids, gold = await _forge(client, engine, name)
        cal, bobby = ids["Cal"], ids["Bobby"]
        counted = await ts.observed(
            engine, story, bobby, sv.proposition(gold, cal, sv.number_literal(500))
        )
        fake.script("gold", pv.Fail("secret_missing"))
        reads = len(fake.reads)

        # Bobby's sincere 500: belief-consistent, the objective relation unresolved, standing.
        played = await _turn(
            client,
            stub,
            story,
            cal,
            beat("say", f"{KNOWS.format(500)} {GREETING}", intent="sincere"),
            narration="Cal has 7 silver in all.",
        )
        assert played.status_code == 200, played.text
        found = await _speech(engine, story)
        assert (found["classification"], found["belief_relation"]) == ("sincere", "consistent")
        assert (found["kind"], found["reason"]) == ("uncertain", "provider_unknown")
        assert found["repairs"] == []
        assert KNOWS.format(500) in played.json()["message"]["text"]
        (said,) = await rows(
            engine,
            "SELECT classification, belief_relation, objective_relation FROM utterances "
            "WHERE story_id = :s AND speaker_character_id = :b",
            s=story,
            b=bobby,
        )
        assert tuple(said) == ("sincere", "consistent", "unresolved")
        report = await turn_report(engine, story)
        assert [r["policy"] for r in report[unavailability.REPORT_KEY]] == ["unknown"]
        # The story-owned State is unaffected: its claim is settled as ever.
        assert _settled(report) == {("supported", None)}

        # His deliberate lie is verified against his beliefs: a lie, its divergence recorded.
        played = await _turn(
            client,
            stub,
            story,
            cal,
            beat("say", KNOWS.format(900), intent="deceptive"),
            narration="The forge glows.",
        )
        assert played.status_code == 200, played.text
        found = await _speech(engine, story)
        assert (found["classification"], found["belief_relation"]) == ("lie", "inconsistent")
        assert found["contrary_belief_ids"] == [counted.belief.id]
        assert found["repairs"] == []
        (divergence,) = await rows(
            engine, "SELECT speaker_character_id FROM divergences WHERE story_id = :s", s=story
        )
        assert str(divergence.speaker_character_id) == bobby

        # The narrator may not state the balance: undecided, repaired out of the message.
        played = await _turn(client, stub, story, cal, narration="Cal has 500 gold in all.")
        assert played.status_code == 200, played.text
        report = await turn_report(engine, story)
        assert _settled(report) == {("uncertain", "provider_unknown")}
        assert "500 gold" not in played.json()["message"]["text"]
        assert report["repairs"]
        unknown_turn = played.json()["turn"]
        # Nobody newly learned it: Bobby's count is his only state_value belief.
        assert [
            (str(t.holder_character_id), t.kind)
            for t in await _gold_transitions(engine, story, gold)
        ] == [(bobby, "observation")]
        assert len(fake.reads) == reads + 3  # one attempt per turn

        # -- Replay: a rebuild and a fork of the Unknown turns call no adapter and pin Unknown.
        recorded = await pv.reconstruction(engine, story)
        assert [(r[0], r[5], r[6]) for r in recorded if r[0] == unknown_turn] == [
            (unknown_turn, None, "unknown")
        ]
        reads = len(fake.reads)
        await projections.rebuild(engine, story)
        assert await pv.reconstruction(engine, story) == recorded
        forked = await fork(engine, story)
        assert await pv.reconstruction(engine, forked.story_id) == recorded
        assert len(fake.reads) == reads
        async with engine.connect() as conn:
            later = await rules.verify(
                conn, StructuredReadSet(), story, sv.proposition(gold, cal, sv.number_literal(500))
            )
        assert later is not None
        assert (later.kind, later.reason) == (
            rules.VerdictKind.UNCERTAIN,
            rules.VerdictReason.PROVIDER_UNKNOWN,
        )

        # -- Complement: the adapter is back; the next turn reads live, with no policy entry.
        fake.script("gold", Decimal(300))
        played = await _turn(client, stub, story, cal, narration="Cal has 300 gold in all.")
        assert played.status_code == 200, played.text
        report = await turn_report(engine, story)
        assert unavailability.REPORT_KEY not in report
        assert _settled(report) == {("supported", None)}
        assert (await pv.external_reads(engine, story))[-1].payload["source"] == "live"

        # -- Under surface the narration stands, but no one observes the Unknown value. A
        # malformed answer (#208's provider_malformed) takes the same policy. ---------------
        story, ids, gold = await _forge(client, engine, name, surface=True)
        cal = ids["Cal"]
        fake.script("gold", {"balance": 500})
        played = await _turn(client, stub, story, cal, narration="Cal has 500 gold in all.")
        assert played.status_code == 200, played.text
        report = await turn_report(engine, story)
        assert [(r["policy"], r["reason"]) for r in report[unavailability.REPORT_KEY]] == [
            ("unknown", "provider_malformed")
        ]
        assert _settled(report) == {("uncertain", "provider_unknown")}
        assert "500 gold" in played.json()["message"]["text"]
        assert await _gold_transitions(engine, story, gold) == []
