"""S-EXTRACT-ADVERSARIAL, M4 part: valid structure, hidden claims (FR-VERIFY-13,14; docs/04 §7;
#148). One file per scenario; the M5 part (protected content in a ``say``, reactive narration)
and the M6 part (tracked States, #190: see ``test_s_extract_adversarial__m6``) join it.

Each render is structurally complete with empty or incomplete declarations. (a) Linda's ``do``
"She pockets the key on her way out" declares no effect: the possession detector finds the claim
that Linda holds the key, and since canon says Marcus holds it (an item has one holder at a
time: ``held_by`` is functional, #439) it is contradicted, so it goes to repair (#151 rewrites
it). (e) A claim introduced only by the polish pass, "…the ring Marcus had bought her in Paris",
is found in the final composed message (FR-VERIFY-14) by the extraction backstop; it names a
relation and a place canon has no record of, so it cannot be decided and takes the safe
disposition (repair, or the deterministic assembly, #152). The story runs once with
``extraction_mode = always`` (extraction reads every beat: everything is detected) and once with
``gated`` (extraction reads only the flagged beats; a shadow sample, forced with a rate of 1.0,
runs the extraction after the commit over the beat gating skipped, and records the claim gating
missed). The report states detection and checking separately: what was detected, what was
checked, what the sample missed. Detection and checking are separate metric series."""

from __future__ import annotations

import json
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import StateChangeSource
from epistrel.core.propositions import store
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.states import StateDefinition
from epistrel.core.tables import operations
from epistrel.core.verify import shadow
from epistrel.core.verify.detect import DetectBeat
from epistrel.core.verify.rules import VerdictKind
from tests import clock_support as ck
from tests import detection_support as ds
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import reactive_support as rs
from tests import state_support as ss
from tests import testimony_support as ts
from tests import verify_support as vs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.secrets_support import play, secret, secret_story
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

POCKETS = "She pockets the key on her way out."
SMILES = "Linda smiles at Marcus."
COMPOSED = "Linda turns over the ring Marcus had bought her in Paris."


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.part("M4", "FR-VERIFY-13,14")
@pytest.mark.parametrize("mode", ["always", "gated"])
async def test_s_extract_adversarial__m4(engine: AsyncEngine, mode: str) -> None:
    """See the module docstring."""
    t = await ds.tavern_story(engine, mode=mode)
    beats = [
        DetectBeat(1, "do", POCKETS, t.linda),
        DetectBeat(2, "narration", SMILES, NARRATOR),
    ]
    mal = vs.stub_models()
    pocketing = {
        "beat": 0,  # the request's markers are 0..n-1 (#459)
        "span": "pockets the key",
        "subject": "Linda",
        "predicate": "holds",
        "object": "key",
    }
    smiling = {
        "beat": 1,
        "span": "smiles at Marcus",
        "subject": "Linda",
        "predicate": "smiled_at",
        "object": "Marcus",
    }
    polish = {
        "beat": 0,
        "span": "the ring Marcus had bought her in Paris",
        "subject": "Marcus",
        "predicate": "bought_in",
        "object": "Paris",
    }
    render_answer = (pocketing, smiling) if mode == "always" else (pocketing,)
    mal.stub.script(
        "extraction_backstop", [ds.claims_answer(*render_answer), ds.claims_answer(polish)]
    )
    detected_before = _sample(
        "epistrel_claims_detected_total", layer="detector", category="possession"
    )
    checked_before = _sample(
        "epistrel_claims_checked_total", verdict="contradicted", category="possession"
    )

    found = await ds.detect_turn(
        engine, t.story_id, mal, beats, message=COMPOSED, shadow_rate=1.0, scene_entity=t.tavern
    )
    render, message = found.outcomes
    calls = [c for c in mal.stub.calls if c.purpose == "extraction_backstop"]
    assert len(calls) == 2  # one per render (all its beats), one per composed message

    # (a) The pocketed key: detected (possession), and the extraction agrees (merged, not a
    # second claim), contradicted by canon (Marcus holds it): repaired.
    (pocket,) = [c for c in found.checked if c.claim.beat == 1]
    assert (pocket.claim.layer, pocket.claim.category) == ("detector", "possession")
    assert pocket.claim.merged == ("extraction",)
    assert pocket.claim.proposition is not None
    assert (pocket.claim.proposition.subject_id, pocket.claim.proposition.object_id) == (
        t.linda,
        t.key,
    )
    assert pocket.result is not None
    assert pocket.result.kind is VerdictKind.CONTRADICTED
    assert pocket.result.needs_repair

    # (e) The polish pass's claim, only in the composed message: detected by extraction there.
    polished = [c for c in found.checked if c.claim.beat is None and c.claim.layer == "extraction"]
    assert len(polished) == 1
    assert polished[0].claim.span[1:] == (
        COMPOSED.index("the ring"),
        COMPOSED.index("the ring") + len(str(polish["span"])),
    )
    assert polished[0].result is not None
    assert polished[0].result.needs_repair
    assert polished[0].result.kind is VerdictKind.UNCERTAIN
    assert message.extraction == {None: "ran"}

    # Every detected claim was checked.
    assert all(c.result is not None for c in found.checked)
    assert found.report["detected"] == found.report["checked"] == len(found.checked)

    if mode == "always":
        assert render.extraction == {1: "ran", 2: "ran"}
        assert not render.shadow
        smiled = [c for c in found.checked if c.claim.beat == 2 and c.claim.layer == "extraction"]
        assert len(smiled) == 1
    else:
        # Gated: beat 1 is flagged (a possession hit, nothing declared), beat 2 is not.
        assert render.extraction == {1: "ran", 2: "skipped"}
        assert render.shadow
        assert found.report["shadow"] == "pending"
        assert [
            c for c in found.checked if c.claim.layer == "extraction" and c.claim.beat == 2
        ] == []

    op = await ds.commit_turn(engine, t.story_id, found, beats, message=COMPOSED)
    assert (
        _sample("epistrel_claims_detected_total", layer="detector", category="possession")
        == detected_before + 1
    )
    assert (
        _sample("epistrel_claims_checked_total", verdict="contradicted", category="possession")
        == checked_before + 1
    )

    mal.stub.script("extraction_backstop", [ds.claims_answer({**smiling, "beat": 2})])
    outcomes = await ds.run_shadow_jobs(engine, mal, t.story_id)
    rows = await ds.shadow_rows(engine, t.story_id)
    async with engine.connect() as conn:
        report = await shadow.report(conn, story_id=t.story_id)
    assert report.detected_total == found.report["detected"]
    assert report.checked_total == found.report["checked"]
    if mode == "always":
        assert outcomes == []
        assert rows == []
        assert (report.samples, report.missed, report.miss_rate) == (0, 0, None)
        return
    (row,) = rows
    assert row.operation_id == op.operation_id
    assert (row.status, row.extraction_claims, row.missed_count) == ("sampled", 1, 1)
    (missed,) = row.missed
    assert (missed["beat"], missed["start"], missed["end"]) == (
        2,
        SMILES.index("smiles"),
        SMILES.index("smiles") + len("smiles at Marcus"),
    )
    assert missed["category"] == "extracted"
    assert (report.samples, report.extraction_claims, report.missed) == (1, 1, 1)
    assert report.miss_rate == 1.0
    assert report.missed_by_category == {"extracted": 1}
    async with engine.connect() as conn:
        stored = await conn.scalar(
            select(operations.c.turn_report).where(operations.c.operation_id == op.operation_id)
        )
    assert stored is not None
    assert (stored["detection"]["shadow"], stored["detection"]["shadow_missed"]) == ("sampled", 1)


# -- the M5 part (#172): protected content in a ``say`` --------------------------------------------

UNLIKE_MARCUS = "You deserve better, unlike Marcus, who's been seeing Dana behind your back."


@pytest.mark.part("M5", "FR-VERIFY-14; NFR-PRIV-1")
async def test_s_extract_adversarial__m5(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """(b) Bobby's ``say`` "…unlike Marcus, who's been seeing Dana behind your back…" is
    structurally complete and declares nothing, but the affair exists only in Marcus's private
    partition (his belief that he is married to Dana; no canon names her). The protected-content
    index (#148, extended by #172 to other characters' belief-only records) protects "Dana"
    from Bobby: the hit claims Marcus's belief, Bobby's permission to introduce it is denied
    (#144: another character's secret, which precedes ``protected_content``; as Bobby's
    first-person word it has no legitimate basis either, #166's hallucination) and the beat is
    repaired; the secret is never canonized and never
    reaches the message. Complement: Marcus saying it himself matches nothing of his own.
    ((f), a reactive narration implying the reactor already knew a secret, is #176's slice.)"""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        story, ids = await secret_story(client, engine, "Jenni", "Bobby", "Marcus")
        dana, belief = await secret(
            engine, story, ids["Marcus"], ids["Marcus"], "married_to", "Dana"
        )
        stub = epistrel.models.stub

        leaked = await play(
            client,
            engine,
            stub,
            story,
            ids["Jenni"],
            ids["Bobby"],
            beat("say", UNLIKE_MARCUS, intent="sincere"),
        )
        assert belief in leaked.protected_refs()
        assert leaked.denied
        assert set(leaked.denied) <= {"protected_content", "other_character_secret"}
        assert leaked.tags
        assert set(leaked.tags) <= {"unpermitted", "hallucination"}
        assert "Dana" not in leaked.message
        facts = await rows(
            engine,
            "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND (p.object_id = :d OR p.subject_id = :d)",
            s=story,
            d=dana,
        )
        assert facts == []

        own = await play(
            client,
            engine,
            stub,
            story,
            ids["Jenni"],
            ids["Marcus"],
            beat("say", "I've been seeing Dana, and I'm sorry.", intent="sincere"),
        )
        assert belief not in own.protected_refs()
        assert not own.denied
        assert "Dana" in own.message


# -- the M5 part's (f) (#176): a reactive narration implying a secret --------------------------

FLINCHES_AT_DANA = "Dale flinches at the mention of Dana."
FLINCHES = "Dale flinches."


@pytest.mark.part("M5", "FR-VERIFY-14")
async def test_s_extract_adversarial__f(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """(f) Bobby's render is structurally complete and narrates Dale's reaction (#176's
    ``reactions`` channel): "Dale flinches at the mention of Dana." Nobody mentioned Dana: the
    affair exists only in Marcus's private partition, so the reaction implies Dale already knew
    it. The same claim detection runs on the narration (FR-VERIFY-14), attributed to Dale (the
    reactor, not the narrator, is the viewpoint checked): the protected-content index protects
    "Dana" from Dale, the reaction is dropped (``render_rejected``), never committed as Dale's
    ``do`` and never in the message, and nothing about Dana is canonized. Complement: Dale's
    plain flinch is accepted as his narrated ``do``."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        story, ids = await secret_story(client, engine, "Jenni", "Bobby", "Marcus", "Dale")
        await ms.per_character(engine, story)
        dana, _ = await secret(engine, story, ids["Marcus"], ids["Marcus"], "married_to", "Dana")
        stub = epistrel.models.stub
        for text, kept in ((FLINCHES_AT_DANA, False), (FLINCHES, True)):
            stub.reset()
            ms.renders_for(
                stub,
                "Bobby",
                rs.render(
                    beat("say", "Evening, everyone.", intent="sincere"),
                    reactions=[rs.reaction(ids["Dale"], text)],
                ),
            )
            answer = await client.post(
                f"/stories/{story}/respond",
                json={
                    "actor_character_id": ids["Jenni"],
                    "player_input": {"say": "Hello."},
                    "render_character_ids": [ids["Bobby"]],
                },
                headers=rs.WRITE,
            )
            assert answer.status_code == 200, answer.text
            message = answer.json()["message"]["text"]
            narrated = [n.text for n in await rs.narrated(engine, story)]
            if kept:
                assert narrated[-1] == FLINCHES
                assert FLINCHES in message
            else:
                assert narrated == []
                assert "Dana" not in message
                (dropped,) = await rs.rejected(engine, story)
                assert dropped.payload["text"] == FLINCHES_AT_DANA
                assert (
                    await rows(
                        engine,
                        "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
                        "WHERE f.story_id = :s AND (p.object_id = :d OR p.subject_id = :d)",
                        s=story,
                        d=dana,
                    )
                    == []
                )


# -- the M6 part (#190): tracked States ---------------------------------------------------------

M6_POCKETS = "She pockets the key on her way out."
SKIP = "She cooks on, and by the time the stew is ready the lamps are lit."
"""(d): an implied time skip in a subordinate clause (#199)."""
STEW = {"minutes": 45, "reason": "A stew takes about three quarters of an hour.", "confidence": 0.7}
OFFER = "Forty silver, final offer."
PRICED = f"Linda wipes the counter. Linda names her price: {OFFER}"


async def _m6_story(client: Any, engine: AsyncEngine, mode: str) -> tuple[str, dict[str, str]]:
    """Cal (the player) and Linda at her stall, under per-character partitions, with
    ``extraction_mode = mode`` and two tracked States: Linda's ``has_key`` (a boolean
    possession State tracking the key) and the story's ``sword_price`` (40 silver, visible)."""
    story, ids = await structured_story(client, "Cal", "Linda")
    await ts.enable_testimony(engine, story, "defaulted", level="co_location")
    await ss.set_level(engine, story, "on")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE story_profile_overrides SET settings = settings || CAST(:o AS jsonb), "
                "revision = revision + 1 WHERE story_id = :s"
            ),
            {"s": story, "o": json.dumps({"verification": {"extraction_mode": mode}})},
        )
        key = await store.add_entity(conn, story, "item", "key")
    stall = await ps.place_entity(engine, story, "stall")
    for character in ids.values():
        await ps.put(engine, story, character, stall)
    await ss.define_all(
        engine,
        story,
        StateDefinition.model_validate(
            {
                "name": "has_key",
                "type": "boolean",
                "scope": "character",
                "initial_value": False,
                "entity_id": key,
            }
        ),
        StateDefinition.model_validate(
            {
                "name": "sword_price",
                "type": "number",
                "scope": "story",
                "initial_value": 40,
                "unit": "silver",
            }
        ),
    )
    return story, ids


def _quantity_verdicts(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The verdicts of the turn's detected quantities (a claim's id ends in its index among
    the beats' detected claims). A lone quantity that is no State's value stays ``uncertain``;
    one that is (#191's ``state_value``) is decided by the State's owner."""
    claims = [c for c in report["detection"]["claims"] if c["span"][0] is not None]
    wanted = {n for n, c in enumerate(claims) if c["category"] == "quantity"}
    return [
        v
        for v in report["verify"]["verdicts"]
        if int(str(v["claim_id"]).rsplit(":", 1)[1]) in wanted
    ]


@pytest.mark.part("M6", "FR-STATE-13; FR-CLOCK-7")
@pytest.mark.parametrize("mode", ["always", "gated"])
async def test_s_extract_adversarial__m6(
    pg_url: str,
    engine: AsyncEngine,
    mode: str,
    states_delivered: None,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """(a') Linda's ``do`` "She pockets the key on her way out" declares no effect, but the key
    is the entity a tracked boolean possession State (``has_key``) follows: the possession is
    that State's change (#190's ``undeclared``, one ``state_undeclared`` repair), and the
    regeneration declares ``has_key = true``, validated and committed.

    (c) "Forty silver, final offer" is a quantity buried in dialogue, against the player-visible
    story State ``sword_price`` (unit silver): detected as the State's value and verified against
    its owner (#191). In narration with the State at 40 it is ``supported``; at 50 it is
    ``contradicted`` and repaired. In Linda's own ``say`` it is judged on the belief path (her
    partition, a speech act): she saw the narration state 40, so against the ledger's 50 her
    offer is a sincere error that stands, never repaired as a mismatch with the ledger.

    (d) (#199) With the world clock on, Linda starts cooking and then "cooks on, and by the time
    the stew is ready the lamps are lit": an implied skip in a subordinate clause, with no time
    advance declared. The ``time`` detector's ``elapsed`` hit makes it an undeclared skip (#190's
    diff), routed to FR-CLOCK-7: one ``time_estimation`` call, whose answer (45 minutes) is
    recorded as an ``estimate`` advance; the clause is not repaired (her cooking is established
    in the scene, an earlier ``do``).

    Under both ``extraction_mode = always`` and ``gated``."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _m6_story(client, engine, mode)
        cal, linda = ids["Cal"], ids["Linda"]

        async def turn(
            linda_beats: list[dict[str, Any]],
            narration: str | None = None,
            regenerated: tuple[dict[str, Any], ...] = (),
        ) -> dict[str, Any]:
            stub.reset()
            ms.renders_for(stub, "Linda", es.render(*linda_beats))
            if narration is not None:
                es.narrator(stub, es.render(beat("narration", narration)))
            stub.script("regeneration", [json.dumps(r) for r in regenerated])
            request = body(cal, None, do="browses the stall") if narration else body(cal, "Hm?")
            answer = await respond(client, story, request)
            assert answer.status_code == 200, answer.text
            return {"played": answer.json(), "report": await turn_report(engine, story)}

        # -- (a') the pocketed key becomes a validated effect.
        found = await turn(
            [beat("do", M6_POCKETS)],
            regenerated=(
                {
                    "text": M6_POCKETS,
                    "effects": [es.effect("has_key", value=True, reason="pocketed the key")],
                },
            ),
        )
        state_repairs = [r for r in found["report"]["repairs"] if r["tag"] == "state_undeclared"]
        assert [(r["rung"], r["accepted"]) for r in state_repairs] == [(3, True)]
        assert [
            (e["state"], e["scope_key"], e["new_value"]) for e in found["played"]["state_effects"]
        ] == [("has_key", linda, True)]
        assert await ss.value(engine, story, "has_key", linda) is True

        # -- (c) in narration, at 40: supported, standing.
        found = await turn([beat("say", "Take your time.")], narration=PRICED)
        (judged,) = _quantity_verdicts(found["report"])
        assert judged["kind"] == "supported"
        assert not found["report"].get("repairs")
        assert OFFER in found["played"]["message"]["text"]

        # -- at 50: contradicted, and repaired.
        await ss.change(
            engine,
            story,
            ss.set_to("sword_price", 50, reason="a new list", source=StateChangeSource.CONSUMER),
        )
        found = await turn([beat("say", "Take your time.")], narration=PRICED)
        (judged,) = _quantity_verdicts(found["report"])
        assert judged["kind"] == "contradicted"
        assert [r["accepted"] for r in found["report"]["repairs"]] == [True]
        assert OFFER not in found["played"]["message"]["text"]
        assert await ss.value(engine, story, "sword_price") == 50

        # -- in Linda's say: the belief path (her partition), not a mismatch with the ledger.
        found = await turn([beat("say", OFFER, intent="sincere")])
        (judged,) = _quantity_verdicts(found["report"])
        # She saw the narration state 40 (her belief), so against the ledger's 50 it is her
        # sincere error: it stands, never repaired as a mismatch.
        assert (judged["kind"], judged["speech"]["classification"]) == (
            "contradicted",
            "sincere_error",
        )
        assert judged["speech"]["belief_relation"] == "consistent"
        assert not found["report"].get("repairs")
        assert OFFER in found["played"]["message"]["text"]

        # -- (d) an implied time skip in a subordinate clause: detected, routed to FR-CLOCK-7.
        await ck.set_level(engine, story, "on")
        stub.reset()
        ms.renders_for(
            stub,
            "Linda",
            es.render(beat("do", "She starts cooking supper behind the stall."), beat("do", SKIP)),
        )
        stub.script("time_estimation", [json.dumps(STEW)])
        answer = await respond(client, story, body(cal, "Hm?"))
        assert answer.status_code == 200, answer.text
        report = await turn_report(engine, story)
        estimated = [
            "\n".join(m.content for m in c.messages)
            for c in stub.calls
            if c.purpose == "time_estimation"
        ]
        assert len(estimated) == 1
        assert SKIP.split(",")[1].strip() in estimated[0]
        assert report["clock"]["skips"]["estimated"] == 1
        assert not report.get("repairs")
        assert SKIP in answer.json()["message"]["text"]
        estimates = await rows(
            engine,
            "SELECT c.delta FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
            "WHERE c.story_id = :s AND d.name = 'world_clock' AND c.source = 'estimate'",
            s=story,
        )
        assert [e.delta for e in estimates] == [45]
