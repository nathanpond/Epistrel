"""S-CONFIG-RACE: configuration changes during generation (docs/04 §7; FR-CONC-14,15,16,
FR-MULTI-11, FR-EPI-14, FR-EXT-16; #214), the M6 part.

Every turn is a structured turn played end to end through ``respond`` with the stub adapter; a
change lands while the turn is held (its render latched, ``stub.hold``) or, for a staged turn
(#212), between two of its stages. What a change does depends on what the turn has committed
(``operations.turn_stage``; ``orchestrator.structured.rebase``), and no committed stage is ever
regenerated or rolled back (``stage_log``: each stage once, ``done``).

``test_s_config_race__m6``, **before intent** (``turn_stage IS NULL``): the turn rebases in the
#47/#64 sense (``run_with_revalidation``):

- (a) the author approves a proposal making the auto predicate ``rides_with`` functional, so
  Ada's claim now conflicts with canon: re-checked, failed, regenerated under the new semantics;
- (b) the author revokes the source that may spend Cal's gold: the State effect fails its
  re-check, the turn regenerates;
- (c) the author switches ``player_authority`` from ``co_author`` to ``own_character`` while the
  player's input narrates Jenni: the regenerated turn treats it as a request of her;
- (d) the operator makes the payment provider read-only before intent: the turn rebases and its
  effect comes back as a consumer-applied state effect (#209's ``requested_effect``), no outbox
  row, no staged turn;
- (e) a story policy tightens: the output is re-checked and regenerated without the word;
- (f) the operator changes the generation temperature, the retry budget and
  ``interaction_mode``: prospective, so the turn commits unchanged on its first attempt, under
  the configuration it captured.

``test_s_config_race__m6_staged``, a purchase on a synchronous provider (Cal's gold) with its
sword grant conditional on the payment (#216):

- (h) **after intent, before the claim**: the operator makes the provider read-only, or the
  author revokes the purchase's source: the committed attempt stands, ``claim()`` cancels the
  row (``reconciliation_required``), and the outcome step narrates it as not carried out;
- (i) **after the claim**: the author tightens the sword's schema; the payment is not cancelled
  and the outcome transaction validates the grant under the new schema (granted while it fits;
  complement: refused by the new bound, held pending);
- (j1) **permission revoked after the claim**: the payment outcome is preserved, the grant is not
  committed, the payment is listed ``settlement_conflict``, and the outcome step narrates the
  payment done and the sword held pending;
- (j2) **after the outcome transaction**: the grant is retained; only the outcome step is
  checked, under the configuration current then.

Complement: a prospective change after intent (the sword's aliases) changes nothing.

``test_s_config_race__m6_settled_later``, the same refusal where the settlement runs outside
the request (#213, #217):

- (k) **a late settlement** (#217, an ``asynchronous`` provider): the author revokes the
  sword's source after the dispatcher's claim and before the confirmation settles; the slot's
  settlement re-authorizes the grant (``rebase.revalidate_settlement``) and is refused
  (``settlement_conflict``, nothing granted). Complement: an unrevoked purchase settles.
- (l) **a resumed staged turn** (#213): the outcome transaction fails, the author revokes the
  sword's source, and the resume records the outcome under the current configuration (the
  payment kept, the grant held pending, ``settlement_conflict``); its deterministic completion
  narrates the grant as held pending, whether the resume that completes it recorded the
  outcome itself or found it recorded by an earlier one (``contingency.conflicted``).

``test_s_config_race__m6_cascade``, (g) the same-turn causal cascade: in a sequential turn Bobby
whispers the secret to Cal in beat 2 with Jenni beside him (``co_location``: she hears it) and
she answers it in beat 3. The author switches perception to ``full`` while the turn generates,
so beat 2 is envelope-only for Jenni: the whole turn is regenerated (every render re-runs; owner,
2026-10-28), her staged belief from beat 2 is gone and no committed beat answers the secret.
Then: every committed operation records the configuration revisions it was validated under, an
idempotent replay answers the recorded configuration, a consolidation job whose visibility rules
change while it runs drops its output and recomputes, and an unclassified setting is
interpretive.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import states
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, StateChangeSource
from epistrel.core.epistemic.scene import PERCEPTION_INPUT
from epistrel.core.events import Operation
from epistrel.core.memory.consolidation import Window
from epistrel.core.propositions import registry as predicates
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Cardinality, Interval, Proposition
from epistrel.core.settings_schema import ConfigClass, StorySettings, setting_class, untagged
from epistrel.core.states import StateDefinition, contingency
from epistrel.core.visibility import Visibility
from epistrel.orchestrator.jobs import queue, settlement
from epistrel.orchestrator.jobs.handlers import consolidation
from epistrel.orchestrator.jobs.handlers import outbox as dispatcher
from epistrel.orchestrator.providers import registry as provider_registry
from epistrel.orchestrator.providers.manifest import ProviderSpec
from epistrel.orchestrator.structured import rebase
from epistrel.orchestrator.structured import staged as staged_turns
from tests import consolidation_support as cs
from tests import effect_support as es
from tests import late_settlement_support as ls
from tests import multi_support as ms
from tests import perception_support as pcs
from tests import provider_support as ps
from tests import staged_support as sts
from tests import state_support as ss
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.event_support import execute_in_tx, rows
from tests.policy_support import AUTHOR, story_item, words
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, override
from tests.structured_turn_support import beat, claim, render, structured_story, turn_report
from tests.summary_support import worker as summary_worker

pytestmark = pytest.mark.integration

OPERATOR = "p-operator"
STAGES = ["intent", "claim", "outcome", "completion", "response"]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


# -- shared --------------------------------------------------------------------------------------


async def _held(
    client: Any,
    stub: Any,
    story: str,
    request: dict[str, Any],
    during: Callable[[], Awaitable[Any]],
) -> Any:
    """``respond`` with ``during`` (the change) landing after the turn generated and checked its
    attempt, just before its commit (the FR-CONC-4 decision): what a change made while the turn
    was in flight finds."""
    del stub
    original = Operation.commit
    fired: list[bool] = []

    async def commit(self: Operation, decide: Any = None) -> None:
        if decide is not None and self.story_id == story and not fired:
            fired.append(True)
            await during()
        await original(self, decide)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Operation, "commit", commit)
        answer = await respond(client, story, request)
    assert fired, "the change landed mid-turn"
    return answer


async def _turn(engine: AsyncEngine, story: str) -> Any:
    """The story's latest committed turn's operation row."""
    (found,) = await rows(
        engine,
        "SELECT operation_id, attempts, config_revisions, turn_stage::text AS turn_stage, "
        "stage_log, timeline_revision FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY narrative_turn DESC LIMIT 1",
        s=story,
    )
    return found


def _outcomes(row: Any) -> list[tuple[str, Any]]:
    return [(a["outcome"], a.get("reason") or a.get("recheck")) for a in row.attempts]


def _texts(played: dict[str, Any]) -> list[str]:
    return [b.get("text") or "" for b in played["beats"]]


async def _read_only(engine: AsyncEngine, name: str) -> None:
    """The operator replaces provider ``name``'s registration with a read-only one (an
    interpretive deployment configuration change, FR-CONC-14)."""
    spec = ps.in_process_spec(values={"gold": "number"})
    await provider_registry.replace(
        engine, name, ProviderSpec.model_validate(spec), principal=OPERATOR
    )


async def _patch_state(engine: AsyncEngine, story: str, name: str, member: dict[str, Any]) -> None:
    """The author updates State ``name``'s declaration (#185's ``update_definition``)."""
    async with engine.begin() as conn:
        await states.update_definition(conn, story, name, member, principal="p-author")


# -- before intent: (a) to (f) ---------------------------------------------------------------------


@pytest.mark.part("M6", "FR-CONC-14..16; FR-EXT-16")
async def test_s_config_race__m6(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Before intent (see the module docstring): (a) to (f)."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub

        # -- (a) a predicate keyed differently: Ada's claim now conflicts, regenerated.
        story, ids = await structured_story(client, "Wren", "Ada", "Cal")
        async with engine.begin() as conn:
            rides = await predicates.auto_register(conn, story, "rides with")
            await predicates.file_proposal(
                conn,
                story,
                rides.id,
                predicates.ProposedChanges(cardinality=Cardinality.FUNCTIONAL),
            )
        await commit(
            engine,
            story,
            Proposition(
                subject_id=ids["Ada"],
                predicate_id=rides.id,
                object_id=ids["Wren"],
                valid_time=Interval.of(None, None),
            ),
            provenance="author",
        )
        first = "I ride with Cal."
        second = "I ride with Wren, as ever."
        stub.reset()
        stub.script(
            "generation",
            [
                render(beat("say", first, propositions=[claim("I", "rides with", "Cal")])),
                render(beat("say", second, propositions=[claim("I", "rides with", "Wren")])),
            ],
        )

        async def approve() -> None:
            await lib.approve_predicate(
                RequestContext(principal="p-author"),
                story,
                rides.id,
                predicates.ApproveRequest(),
            )

        answer = await _held(
            client, stub, story, body(ids["Wren"], "Who do you ride with?"), approve
        )
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("recheck", "failed"), ("commit", None)]
        assert "predicate_registry" in row.attempts[0]["config_changed"]
        assert row.turn_stage is None
        assert second in _texts(answer.json())
        assert first not in _texts(answer.json())

        # -- (b) the source that may spend Cal's gold revoked: the effect fails, regenerated.
        story, ids = await es.shop(client, engine, es.gold(), gold_of_player=1000)
        cal = ids["Cal"]
        purchase = es.render(
            beat(
                "do",
                "Mira takes Cal's coins for the sword.",
                effects=[es.effect("gold", scope_key=cal, delta=-500, reason="a sword")],
            )
        )
        stub.reset()
        stub.script("generation", [purchase, purchase])
        stub.script("regeneration", ['{"text": "Mira shakes her head: not today."}'])
        answer = await _held(
            client,
            stub,
            story,
            body(cal, "Mira, I'll buy the sword."),
            lambda: _patch_state(
                engine, story, "gold", {"authorization": {"sources": ["consumer"]}}
            ),
        )
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("recheck", "failed"), ("commit", None)]
        assert "state_definitions" in row.attempts[0]["config_changed"]
        assert await es.gold_of(engine, story, cal) == 1000  # nothing spent under the new rule
        assert row.turn_stage is None

        # -- (c) player_authority switched while a co_author input narrates Jenni: a request.
        story, ids = await structured_story(client, "Cal", "Jenni")
        patched = await client.patch(
            f"/stories/{story}/settings", json={"player_authority": "co_author"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        stub.reset()
        stub.script("generation", [render(beat("say", "There."))] * 2)

        async def own_character() -> None:
            switched = await client.patch(
                f"/stories/{story}/settings",
                json={"player_authority": "own_character"},
                headers=WRITE,
            )
            assert switched.status_code == 200, switched.text

        hands = "Jenni smiles and hands me the key."
        answer = await _held(
            client,
            stub,
            story,
            body(ids["Cal"], "Thanks.", do=hands, render_character_ids=[ids["Jenni"]]),
            own_character,
        )
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("recheck", "failed"), ("commit", None)]
        assert "player_authority" in row.attempts[0]["config_changed"]
        assert row.config_revisions["player_authority"] == "own_character"
        authority = (await turn_report(engine, story))["player_authority"]
        assert authority["mode"] == "own_character"
        assert [r["text"] for r in authority["requests"]] == [hands]
        assert authority.get("narrated", []) == []

        # -- (d) the payment provider made read-only before intent: a consumer request.
        story, cal, name, _ = await sts.sync_story(client, engine, lib)
        stub.reset()
        ms.renders_for(stub, "Mira", sts.purchase(cal))
        ms.renders_for(stub, "Mira", sts.purchase(cal))
        answer = await _held(
            client, stub, story, sts.buy_body(cal), lambda: _read_only(engine, name)
        )
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("recheck", "failed"), ("commit", None)]
        assert "provider_routes" in row.attempts[0]["config_changed"]
        assert row.turn_stage is None  # not staged: nothing waits on the provider now
        assert await ps.outbox_rows(engine, story) == []
        requested = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'requested_effect'",
            s=story,
        )
        assert [(r.payload["state"], r.payload["delta"]) for r in requested] == [("gold", -50)]
        (effect,) = [e for e in answer.json()["state_effects"] if e["state"] == "gold"]
        assert (effect["owner"], effect["status"]) == ("external", "pending")
        assert "outbox_id" not in effect
        await ss.set_level(engine, story, None)

        # -- (e) a story policy tightens while the turn is held: re-checked, regenerated.
        story, ids = await structured_story(client, "Wren", "Ada")
        stub.reset()
        stub.script(
            "generation",
            [render(beat("say", "An amber lamp hums.")), render(beat("say", "A lamp hums."))],
        )

        async def ban() -> None:
            await lib.policies_set_story(
                AUTHOR, story, [story_item(words("amber"), applies_to=["text_output"])]
            )

        answer = await _held(client, stub, story, body(ids["Wren"], "Look."), ban)
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert (row.attempts[0]["outcome"], row.attempts[0]["recheck"]) == ("recheck", "failed")
        assert "policy_set" in row.attempts[0]["config_changed"]
        assert "amber" not in answer.json()["message"]["text"].lower()

        # -- (f) prospective changes: the turn commits unchanged under what it captured.
        story, ids = await structured_story(client, "Wren", "Ada")
        stub.reset()
        stub.script("generation", [render(beat("say", "The kettle sings."))])

        async def prospective() -> None:
            await override(client, story, {"models": {"generation": {"temperature": 0.3}}})
            moved = await client.patch(
                f"/stories/{story}/settings",
                json={"regenerate_retry_budget": 2, "interaction_mode": "simultaneous"},
                headers=WRITE,
            )
            assert moved.status_code == 200, moved.text

        before = await rows(engine, "SELECT config_revision FROM stories WHERE id = :s", s=story)
        answer = await _held(client, stub, story, body(ids["Wren"], "Tea?"), prospective)
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("commit", None)]
        assert row.config_revisions["story"] == before[0].config_revision  # as captured
        after = await rows(engine, "SELECT config_revision FROM stories WHERE id = :s", s=story)
        assert after[0].config_revision > before[0].config_revision
        assert "The kettle sings." in _texts(answer.json())


# -- after intent: (h), (i), (j1), (j2) -----------------------------------------------------------

WRAPS = "Mira wraps the sword in oilcloth."
WENT = "The purchase went through."
HELD = "The payment went through. The sword is kept back for now."
SHRUGS = "The smith shrugs; nothing changes hands."
REVOKED = {"authorization": {"sources": ["director"]}}
"""The sword's acquisition permission revoked for the characters' renders (#188)."""


def _sword(entity: str) -> StateDefinition:
    """Cal's swords, bought with his (external, synchronous) gold."""
    return StateDefinition.model_validate(
        {
            "name": "sword",
            "type": "number",
            "scope": "character",
            "constraints": {"non_negative": True},
            "initial_value": 0,
            "aliases": ("blade",),
            "entity_id": entity,
            "transfer": {"counterparties": ["gold"]},
            "authorization": {"sources": ["any_character", "director"]},
        }
    )


def _trade(cal: str) -> dict[str, Any]:
    """Cal's sword +1 against 50 of his gold (the grant conditional on the payment, #216)."""
    return {
        "kind": "transfer",
        "state": "sword",
        "scope_key": cal,
        "delta": 1,
        "counterparty": {"state": "gold", "scope_key": cal, "delta": -50},
        "reason": "the sword",
    }


async def _sword_shop(client: Any, engine: AsyncEngine, lib: Any) -> tuple[str, str, str, Any]:
    """``staged_support.sync_story`` (Cal's gold on a synchronous provider) with Cal's heirloom
    sword: the story, Cal, the provider's name and its fake (confirming)."""
    story, cal, name, fake = await sts.sync_story(client, engine, lib)
    async with engine.begin() as conn:
        entity = await store.add_entity(conn, story, "item", "sword")
    await ss.define_all(engine, story, _sword(entity))
    await ss.change(
        engine,
        story,
        ss.set_to(
            "sword", 1, scope_key=cal, reason="an heirloom", source=StateChangeSource.CONSUMER
        ),
    )
    fake.script_writes("gold", "confirmed")
    return story, cal, name, fake


async def _buy(client: Any, stub: Any, story: str, cal: str, outcome: str) -> dict[str, Any]:
    """Mira's purchase of the sword for Cal (its payment staged); the outcome step answers
    ``outcome``."""
    stub.reset()
    sts.outcome_step(stub, beat("narration", outcome))
    ms.renders_for(stub, "Mira", es.render(beat("do", WRAPS, effects=[_trade(cal)])))
    answer = await sts.buy(client, story, cal)
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def _no_stage_redone(engine: AsyncEngine, story: str) -> None:
    """No committed stage was regenerated or rolled back: each stage once, ``done``, the
    revision moved at intent, outcome and completion only, and the intent committed once."""
    op = await sts.staged_op(engine, story)
    entries = [e for e in op.stage_log or [] if e.get("stage") in staged_turns.STAGES]
    assert [e["stage"] for e in entries] == STAGES
    assert {e["status"] for e in entries} == {"done"}
    by = {e["stage"]: e["revision"] for e in entries}
    assert by["intent"] == by["claim"] < by["outcome"] < by["completion"] == by["response"]
    assert (op.status, op.turn_stage) == ("committed", "completed")
    (row,) = await rows(
        engine, "SELECT attempts FROM operations WHERE operation_id = :o", o=op.operation_id
    )
    assert [a["outcome"] for a in row.attempts] == ["commit"]


async def _swords(engine: AsyncEngine, story: str) -> list[Any]:
    return [r for r in await ss.change_rows(engine, story) if r.reason == "the sword"]


async def _marks(engine: AsyncEngine, story: str) -> list[tuple[str, str]]:
    found = await rows(
        engine,
        "SELECT outbox_id, reason FROM reconciliation_marks WHERE story_id = :s ORDER BY id",
        s=story,
    )
    return [(str(r.outbox_id), str(r.reason)) for r in found]


def _outcome_prompt(stub: Any) -> str:
    (prompt,) = [p for p in es.prompts(stub, "NarratorRender") if staged_turns.OUTCOME_STEP in p]
    return prompt


@contextmanager
def _around(
    module: Any, name: str, during: Callable[[], Awaitable[Any]], *, after: bool = False
) -> Iterator[list[bool]]:
    """``module.name`` (a stage's function) runs ``during`` once, before its first call (or
    after it returns, ``after``)."""
    original = getattr(module, name)
    fired: list[bool] = []

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not fired and not after:
            fired.append(True)
            await during()
        found = await original(*args, **kwargs)
        if not fired and after:
            fired.append(True)
            await during()
        return found

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, name, wrapped)
        yield fired


async def _done(engine: AsyncEngine, story: str) -> None:
    await ps.drop_outbox_jobs(engine, story)
    await ss.set_level(engine, story, None)


@pytest.mark.part("M6", "FR-CONC-16; FR-EXT-16")
async def test_s_config_race__m6_staged(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """After intent (see the module docstring): (h), (i), (j1), (j2) and the complement."""
    assert rebase.stage_of(None) == rebase.PRE_INTENT
    assert rebase.stage_of("intent_committed") == rebase.CLAIM
    assert rebase.stage_of("awaiting_outcome") == rebase.SETTLEMENT
    assert rebase.stage_of("outcome_recorded") == rebase.OUTCOME_STEP
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub

        # -- (h) after intent, before the claim: read-only, or the source revoked.
        for change in ("read_only", "revoked"):
            story, cal, name, fake = await _sword_shop(client, engine, lib)

            async def before_claim(
                story: str = story, name: str = name, change: str = change
            ) -> None:
                if change == "read_only":
                    await _read_only(engine, name)
                else:
                    await _patch_state(engine, story, "sword", REVOKED)

            with _around(dispatcher, "claim", before_claim) as fired:
                played = await _buy(client, stub, story, cal, SHRUGS)
            assert fired
            await _no_stage_redone(engine, story)
            assert WRAPS in _texts(played)  # the committed attempt stands
            (row,) = await ps.outbox_rows(engine, story)
            assert row.status == "cancelled"
            assert fake.writes == []  # never sent
            assert await _marks(engine, story) == [(str(row.id), "reconciliation_required")]
            assert "not carried out" in _outcome_prompt(stub)
            (intent,) = await _swords(engine, story)
            assert intent.status == "dropped"
            assert await ss.value(engine, story, "sword", cal) == 1
            await _done(engine, story)

        # -- (i) after the claim, the schema changes: the grant validates under the new one.
        for cap, granted in ((3, True), (1, False)):
            story, cal, _, fake = await _sword_shop(client, engine, lib)

            async def tighten(story: str = story, cap: int = cap) -> None:
                await _patch_state(
                    engine, story, "sword", {"constraints": {"non_negative": True, "max": cap}}
                )

            with _around(dispatcher, "send", tighten) as fired:
                await _buy(client, stub, story, cal, WENT if granted else HELD)
            assert fired
            await _no_stage_redone(engine, story)
            (row,) = await ps.outbox_rows(engine, story)
            assert row.status == "confirmed"  # the claim stood: the payment was not cancelled
            if granted:
                intent, settled = await _swords(engine, story)
                assert (intent.status, settled.status, settled.source) == (
                    "converted",
                    "committed",
                    "settlement",
                )
                assert await ss.value(engine, story, "sword", cal) == 2
                assert await _marks(engine, story) == []
            else:
                (intent,) = await _swords(engine, story)
                assert intent.status == "pending"  # held pending, never granted
                assert await ss.value(engine, story, "sword", cal) == 1
                assert await _marks(engine, story) == [(str(row.id), "settlement_conflict")]
            await _done(engine, story)

        # -- (j1) permission revoked after the claim: the payment kept, the grant held, listed.
        story, cal, _, fake = await _sword_shop(client, engine, lib)
        with _around(
            dispatcher, "send", lambda: _patch_state(engine, story, "sword", REVOKED)
        ) as fired:
            played = await _buy(client, stub, story, cal, HELD)
        assert fired
        await _no_stage_redone(engine, story)
        (row,) = await ps.outbox_rows(engine, story)
        assert row.status == "confirmed"  # the payment outcome is preserved
        (intent,) = await _swords(engine, story)
        assert intent.status == "pending"  # not committed under the new configuration
        assert await ss.value(engine, story, "sword", cal) == 1
        assert await _marks(engine, story) == [(str(row.id), "settlement_conflict")]
        prompt = _outcome_prompt(stub)
        assert "confirmed; narrate it as done" in prompt
        assert "the sword for Cal: held pending" in prompt
        assert HELD in _texts(played)
        staged = (await turn_report(engine, story))["staged"]
        assert [s["result"] for s in staged["settled"]] == ["conflict"]
        await _done(engine, story)

        # -- (j2) permission revoked after the outcome transaction: the grant is retained.
        story, cal, _, fake = await _sword_shop(client, engine, lib)
        with _around(
            staged_turns,
            "record_outcomes",
            lambda: _patch_state(engine, story, "sword", REVOKED),
            after=True,
        ) as fired:
            played = await _buy(client, stub, story, cal, WENT)
        assert fired
        await _no_stage_redone(engine, story)
        intent, settled = await _swords(engine, story)
        assert (intent.status, settled.status) == ("converted", "committed")  # durable
        assert await ss.value(engine, story, "sword", cal) == 2
        assert await _marks(engine, story) == []
        assert WENT in _texts(played)  # only the outcome step ran under the new rules
        await _done(engine, story)

        # -- Complement: a prospective change after intent changes nothing at all.
        story, cal, _, fake = await _sword_shop(client, engine, lib)
        with _around(
            dispatcher,
            "send",
            lambda: _patch_state(engine, story, "sword", {"aliases": ["blade", "brand"]}),
        ) as fired:
            played = await _buy(client, stub, story, cal, WENT)
        assert fired
        await _no_stage_redone(engine, story)
        intent, settled = await _swords(engine, story)
        assert (intent.status, settled.status) == ("converted", "committed")
        assert await ss.value(engine, story, "sword", cal) == 2
        assert await _marks(engine, story) == []
        assert "held pending" not in _outcome_prompt(stub)
        assert WENT in _texts(played)
        await _done(engine, story)


# -- (k) and (l): the settlement outside the request ------------------------------------------

HELD_PENDING = "The sword is held pending resolution."


class Boom(Exception):
    """Induced failure."""


async def _lapse(engine: AsyncEngine, story: str) -> None:
    """The story's turn slot lease lapses (the process running the turn is gone)."""
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET in_flight_lease_until = clock_timestamp() "
            "- interval '1 second' WHERE id = :s",
            {"s": story},
        ),
    )


@pytest.mark.part("M6", "FR-CONC-16; FR-EXT-16")
async def test_s_config_race__m6_settled_later(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """(k) and (l) (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        registry = lib.in_process_providers

        # -- (k) a late settlement: revoked after the claim, refused; unrevoked, settled.
        for revoked in (True, False):
            story, ids = await structured_story(client, "Cal", "Mira")
            cal = ids["Cal"]
            await ls.bank(engine, lib, story)
            await ls.swords(engine, story)
            payment = ls.payment_of(await ls.buy(client, stub, story, cal))
            await ls.plain(client, stub, story, cal)
            outcome = await ls.answer(engine, lib, payment)  # claimed under the old rules
            if revoked:
                await _patch_state(engine, story, "sword", REVOKED)
            done = await ls.settle(engine, lib, outcome)
            intent, *settled = await _swords(engine, story)
            if revoked:
                assert done.result == settlement.CONFLICTED
                assert (intent.status, settled) == ("pending", [])  # held pending, not granted
                assert await ls.sword_of(engine, story, cal) == 0
                assert await ls.settlement_rows(engine, story) == []
                assert (payment, "settlement_conflict") in await ls.listed(engine, story)
            else:
                assert done.result == settlement.SETTLED
                assert (intent.status, [r.status for r in settled]) == ("converted", ["committed"])
                assert await ls.sword_of(engine, story, cal) == 1
                assert not {r for o, r in await ls.listed(engine, story) if o == payment}
            await ls.drop_jobs(engine, story)
            await ss.set_level(engine, story, None)

        # -- (l) a resumed staged turn: the grant held pending, narrated as held pending.
        for split in (False, True):
            story, cal, _, fake = await _sword_shop(client, engine, lib)
            original = contingency.settle

            async def boom(*_: Any, **__: Any) -> Any:
                raise Boom("the outcome transaction fails")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(contingency, "settle", boom)
                with suppress(Boom):
                    await _buy(client, stub, story, cal, WENT)
            assert contingency.settle is original
            op = await sts.staged_op(engine, story)
            assert (op.status, op.turn_stage) == ("committed", "awaiting_outcome")
            await _patch_state(engine, story, "sword", REVOKED)
            await _lapse(engine, story)
            if split:
                # The resume that records the outcome dies before its completion.
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(staged_turns, "complete_deterministically", boom)
                    with suppress(Boom):
                        await staged_turns.resume(engine, op.operation_id, registry=registry)
                op = await sts.staged_op(engine, story)
                assert op.turn_stage == "outcome_recorded"
                await _lapse(engine, story)
            resumed = await staged_turns.resume(engine, op.operation_id, registry=registry)
            assert resumed.stage == "completed"
            (row,) = await ps.outbox_rows(engine, story)
            assert row.status == "confirmed"  # the payment outcome is preserved
            assert len(fake.writes) == 1  # never re-sent
            (intent,) = await _swords(engine, story)
            assert intent.status == "pending"
            assert await ss.value(engine, story, "sword", cal) == 1
            assert await _marks(engine, story) == [(str(row.id), "settlement_conflict")]
            texts = [
                str(t.text)
                for t in await rows(
                    engine,
                    "SELECT payload->>'text' AS text FROM events WHERE operation_id = :o "
                    "AND type = 'beat_narration' ORDER BY seq",
                    o=op.operation_id,
                )
            ]
            assert texts[-1] == HELD_PENDING
            assert texts[-2].endswith(staged_turns.WENT_THROUGH + ".")
            report = (await turn_report(engine, story))["staged"]
            assert report["outcome_step"]["deterministic"] is True
            assert [s["result"] for s in report["settled"]] == ["conflict"]
            await _done(engine, story)


# -- (g) the same-turn causal cascade, and what every change leaves recorded ---------------------

COLLAPSED = "The river bridge collapsed."
GONE = "The river bridge is gone, so we'll take the ferry."
FERRY = "Jenni turns and heads for the ferry."
SEEN = claim("the river bridge", "condition", "collapsed")


def _salient(beat_number: int) -> str:
    return json.dumps(
        {
            "salient": [
                {
                    "beat": beat_number,
                    "span": COLLAPSED,
                    "proposition_sketch": {
                        "subject": "the river bridge",
                        "relation": "condition",
                        "object": "collapsed",
                    },
                }
            ],
            "texture": [],
        }
    )


async def _riverbank(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], str]:
    """Cal, Bobby and Jenni at the riverbank (per-character partitions and renders, perception
    ``co_location``); canon, Bobby's secret (visible to him alone): the river bridge's
    ``condition`` is ``collapsed``. The story, the character ids and the bridge."""
    story, ids = await structured_story(client, "Cal", "Bobby", "Jenni")
    await ts.enable_testimony(engine, story, "defaulted", level="co_location")
    await ms.per_character(engine, story)
    bank = await pcs.place_entity(engine, story, "riverbank")
    for name in ("Cal", "Bobby", "Jenni"):
        await pcs.put(engine, story, ids[name], bank)
    async with engine.begin() as conn:
        bridge = await store.add_entity(conn, story, "place", "the river bridge")
        collapsed = await store.add_entity(conn, story, "other", "collapsed")
        rules = predicates.ScenarioRules.parse(
            {
                "predicates": [
                    {
                        "name": "condition",
                        "subject_type": "place",
                        "object_type": "other",
                        "cardinality": "multi",
                        "temporal_kind": "permanent",
                        "docs": "A structure's condition.",
                    }
                ]
            }
        )
        found = await predicates.register_author(conn, story, rules)
    condition = found.named("condition")
    assert condition is not None
    await commit(
        engine,
        story,
        Proposition(
            subject_id=bridge,
            predicate_id=condition.id,
            object_id=collapsed,
            valid_time=Interval.of(None, None),
        ),
        kind=OperationKind.CONTROL,
        provenance="author",
        visibility=Visibility(scope="characters", characters=(ids["Bobby"],)),  # his secret
    )
    return story, ids, bridge


async def _jenni_on_bridge(engine: AsyncEngine, story: str, jenni: str, bridge: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT t.kind, t.antecedent_kind FROM belief_transitions t "
        "JOIN beliefs b ON b.id = t.belief_id JOIN propositions p ON p.id = b.proposition_id "
        "WHERE t.story_id = :s AND t.holder_character_id = :h AND p.subject_id = :bridge",
        s=story,
        h=jenni,
        bridge=bridge,
    )


class _Untagged(BaseModel):
    """A settings model with a setting nobody classified (U-18's guard, at M6)."""

    forgotten: int = 0


@pytest.mark.part("M6", "FR-CONC-14..16; FR-MULTI-11; FR-EPI-14")
async def test_s_config_race__m6_cascade(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """(g) and the trailing assertions (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub

        # -- (g) Bobby whispers the secret to Cal; Jenni, beside him, answers it in beat 3.
        story, ids, bridge = await _riverbank(client, engine)
        jenni = ids["Jenni"]
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(
                beat(
                    "say",
                    COLLAPSED,
                    intent="sincere",
                    propositions=[SEEN],
                    whisper_to=[ids["Cal"]],
                )
            ),
        )
        # Jenni answers what she perceived: the words, while she hears them; else she leaves.
        stub.when(
            ("## You are Jenni\n", COLLAPSED),
            ms.render(beat("say", GONE, intent="sincere", propositions=[SEEN])),
            purpose="generation",
        )
        ms.renders_for(stub, "Jenni", ms.render(beat("do", FERRY)))
        stub.script("salience_detection", [_salient(2), _salient(2)])
        request = body(ids["Cal"], "Can we cross?", render_character_ids=[ids["Bobby"], jenni])
        answer = await _held(
            client, stub, story, request, lambda: pcs.perceive(engine, story, "full")
        )
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("recheck", "failed"), ("commit", None)]
        assert "perception_level" in row.attempts[0]["config_changed"]
        assert row.config_revisions["perception_level"] == "full"
        # Every render re-ran (whole-turn regeneration): Bobby's and Jenni's, twice each.
        assert len(ms.render_prompts(stub, "Bobby")) == 2
        first, second = ms.render_prompts(stub, "Jenni")
        assert COLLAPSED in first  # the attempt that heard the words
        assert COLLAPSED not in second  # beat 2 is now envelope-only for her
        texts = _texts(answer.json())
        assert GONE not in texts  # nothing regenerated cites the envelope-only content
        assert FERRY in texts
        assert await _jenni_on_bridge(engine, story, jenni, bridge) == []  # no staged belief
        envelope = [m for m in await pcs.memories_of(engine, story, jenni) if m.beat == 2]
        assert [m.fidelity for m in envelope] == ["envelope"]

        # -- Every committed operation records the configuration revisions it ran under.
        recorded = await rows(
            engine,
            "SELECT kind::text AS kind, config_revisions FROM operations WHERE story_id = :s "
            "AND status = 'committed'",
            s=story,
        )
        assert {r.kind for r in recorded} >= {"turn", "control"}
        for found in recorded:
            assert isinstance(found.config_revisions["story"], int), found.kind
            assert isinstance(found.config_revisions["deployment"], int), found.kind

        # -- Replay uses the recorded configuration: an idempotent retry after a change
        # answers what was committed, under the revision it ran under.
        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("say", "Let's go.", intent="sincere")))
        ms.renders_for(stub, "Jenni", ms.render(beat("do", FERRY)))
        key = {"Idempotency-Key": "s-config-race-replay"}
        once = await respond(
            client, story, body(ids["Cal"], "Well?", render_character_ids=[ids["Bobby"]]), key
        )
        assert once.status_code == 200, once.text
        moved = await client.patch(
            f"/stories/{story}/settings", json={"regenerate_retry_budget": 3}, headers=WRITE
        )
        assert moved.status_code == 200, moved.text
        again = await respond(
            client, story, body(ids["Cal"], "Well?", render_character_ids=[ids["Bobby"]]), key
        )
        assert again.status_code == 200, again.text
        assert again.json()["config_revision"] == once.json()["config_revision"]
        (current,) = await rows(
            engine, "SELECT config_revision FROM stories WHERE id = :s", s=story
        )
        assert current.config_revision > once.json()["config_revision"]
        committed = await _turn(engine, story)
        assert committed.config_revisions["story"] == once.json()["config_revision"]

        # -- A consolidation job whose visibility rules change while it runs drops its output
        # and recomputes under the new ones.
        made = await cs.bulk_turns(engine, story, 12, actor=ids["Bobby"], name="Bobby", does=True)
        window = Window(made[0].number, made[-1].number)
        async with engine.begin() as conn:
            await consolidation.enqueue_window(conn, story, window)
        job = await cs.claim(engine, story)
        assert job is not None
        assert job.payload["to_turn"] == window.to_turn
        consolidation.bind(lib.models)
        stub.reset()
        latch = stub.hold("consolidation")
        run = asyncio.create_task(summary_worker(engine).execute(job))
        await asyncio.wait_for(latch.entered.wait(), 10)
        await pcs.perceive(engine, story, "co_location")  # the visibility rules change
        latch.release()
        assert await run is queue.CommitOutcome.DROPPED
        (dropped,) = await rows(engine, "SELECT * FROM jobs WHERE id = :j", j=job.id)
        assert (dropped.status, dropped.failures) == ("queued", 0)  # back in the queue
        assert await cs.outputs(engine, story) == []
        stub.reset()
        stub.script("consolidation", [cs.answer(facts=[("Bobby keeps busy.", "observation", [0])])])
        assert await cs.run_jobs(engine, lib.models, story) == ["committed"]
        (rerun,) = await rows(engine, "SELECT * FROM jobs WHERE id = :j", j=job.id)
        assert rerun.status == "done"
        assert rerun.input_versions[f"{PERCEPTION_INPUT}:{story}"] == "co_location"
        assert [o.text for o in await cs.outputs(engine, story)] == ["Bobby keeps busy."]

    # -- A setting missing a classification is interpretive.
    assert untagged(_Untagged) == ["forgotten"]
    assert setting_class(_Untagged, "forgotten") is ConfigClass.interpretive
    assert setting_class(StorySettings, "no_such_setting") is ConfigClass.interpretive
    assert setting_class(StorySettings, "interaction_mode") is ConfigClass.prospective


# -- a regenerated attempt runs under the configuration as it is now (#659, #660) ---------------


@pytest.mark.part("M6", "FR-CONC-16")
async def test_s_config_race__regenerated_under_current(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#659: ``states.level`` turned ``on`` while the turn is held, with a policy the held output
    breaks: the re-check fails and the regenerated attempt runs under the new level (its State
    effect applied), not the captured ``off`` (nothing applied). #660: ``player_authority``
    switched to ``own_character`` mid-turn; the regenerated turn renders the character the input
    now asks something of, who was not a primary when the turn began."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub

        story, ids = await es.shop(client, engine, es.gold(), gold_of_player=1000)
        cal = ids["Cal"]
        await ss.set_level(engine, story, None)  # States off when the turn starts
        stub.reset()
        stub.script(
            "generation",
            [
                es.render(beat("say", "An amber lamp hums.")),
                *[
                    es.render(
                        beat(
                            "do",
                            "Mira takes Cal's coins for the sword.",
                            effects=[es.effect("gold", scope_key=cal, delta=-500, reason="sword")],
                        )
                    )
                ]
                * 3,
            ],
        )

        async def states_on() -> None:
            # The re-check fails on the policy; the States level rides along with it.
            await ss.set_level(engine, story, "on")
            await lib.policies_set_story(
                AUTHOR, story, [story_item(words("amber"), applies_to=["text_output"])]
            )

        answer = await _held(client, stub, story, body(cal, "Mira, I'll buy the sword."), states_on)
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("recheck", "failed"), ("commit", None)]
        assert "amber" not in answer.json()["message"]["text"].lower()
        assert await es.gold_of(engine, story, cal) == 500  # the effect ran under ``on``
        await ss.set_level(engine, story, None)

        story, ids = await structured_story(client, "Cal", "Ada", "Jenni")
        patched = await client.patch(
            f"/stories/{story}/settings", json={"player_authority": "co_author"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        stub.reset()
        ms.renders_for(stub, "Ada", render(beat("say", "Ada here.")))
        ms.renders_for(stub, "Jenni", render(beat("say", "Jenni here.")))

        async def own_character() -> None:
            switched = await client.patch(
                f"/stories/{story}/settings",
                json={"player_authority": "own_character"},
                headers=WRITE,
            )
            assert switched.status_code == 200, switched.text

        answer = await _held(
            client,
            stub,
            story,
            body(ids["Cal"], "Thanks.", do="Jenni smiles and hands me the key."),
            own_character,
        )
        assert answer.status_code == 200, answer.text
        row = await _turn(engine, story)
        assert _outcomes(row) == [("recheck", "failed"), ("commit", None)]
        said = " ".join(b["text"] for b in answer.json()["beats"])
        assert "Jenni here." in said, said
        assert "Ada here." not in said, said
