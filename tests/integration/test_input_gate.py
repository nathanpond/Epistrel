"""The input gate (#56, FR-GUARD-9,10): every violated policy named in order with its
locators, the budget refusal before any call, character policies bound to their actor, the
``on_uncertain`` path, no audit row on a model fault, #44's precheck order, story creation,
``GET /stories/{id}/settings``, and ``audit_log``'s append-only trigger."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.orchestrator.models import metrics as model_metrics
from epistrel.orchestrator.policies.input_gate import (
    GateText,
    calls_needed,
    input_policies,
)
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from epistrel.orchestrator.policies.store import guard_context
from tests.auth_support import auth_settings
from tests.gate_support import audit_count, audit_rows, gated
from tests.gate_support import input_policies as add_input_policies
from tests.minors_support import explicit_story, guarded_client, recognise
from tests.policy_support import library, unique
from tests.story_support import READ, WRITE, count, create, story_body
from tests.structured_support import story_state

VIOLATION = '{"violation": true, "confidence": 0.97}'
UNSURE = '{"violation": true, "confidence": 0.1}'


def _assisted(prompt: str, on_uncertain: str = "violation") -> dict[str, Any]:
    return {"type": "model_assisted", "prompt": prompt, "on_uncertain": on_uncertain}


def _words(*entries: str) -> dict[str, Any]:
    return {"type": "wordlist", "words": list(entries)}


def _guardrail_calls(stub: Any) -> int:
    return sum(1 for call in stub.calls if call.purpose == "guardrail_check")


@pytest.fixture
def settings_stub(pg_url: str) -> Any:
    return auth_settings(pg_url, model_adapter="stub")


async def test_every_violated_policy_named_in_order(
    settings_stub: Any, engine: AsyncEngine
) -> None:
    """Three ingest texts against two deterministic and one model-assisted input policy: every
    violated policy is named once, deterministic first then by id, each with the texts it hit;
    the model-assisted policy is checked once per text (no short-circuit), and the response
    reports the calls. Complement asserted: a policy no text breaks is not named."""
    lib = library(settings_stub, engine)
    marker = f"Is it dreary {new_ulid()}?"
    async with guarded_client(settings_stub, engine) as (client, epistrel):
        story = (await create(client, story_body("Ada")))["story_id"]
        made = await add_input_policies(
            lib,
            story,
            {"rule": _words("alpha")},
            {"rule": _words("beta")},
            {"rule": _words("never-said")},
            {"rule": _assisted(marker)},
        )
        alpha, beta, _, model = made
        stub = epistrel.models.stub
        stub.when((marker, "gloom"), VIOLATION, purpose="guardrail_check")
        stub.calls.clear()
        texts = ["alpha and beta", "only gloom here", "beta and gloom"]
        before = await story_state(engine, story)
        refused = await gated(client, story, "ingest", texts)
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["message"] == "Input violates 3 policies."
        found = error["details"]["violations"]
        deterministic = sorted([alpha.id, beta.id])
        assert [v["policy_id"] for v in found] == [*deterministic, model.id]
        where = {v["policy_id"]: v.get("locators", [v.get("locator")]) for v in found}
        assert where == {
            alpha.id: ["events[0]"],
            beta.id: ["events[0]", "events[2]"],
            model.id: ["events[1]", "events[2]"],
        }
        context = await guard_context(engine, story)
        gate_texts = [GateText(f"events[{i}]", t) for i, t in enumerate(texts)]
        expected = calls_needed(input_policies(context), gate_texts)
        assert _guardrail_calls(stub) == expected
        assert error["details"]["model_calls"] == expected
        assert isinstance(error["details"]["elapsed_ms"], int)
        assert await story_state(engine, story) == before
        [row] = await audit_rows(engine, story_id=story)
        assert sorted(row.policy_ids) == sorted([alpha.id, beta.id, model.id])
        assert row.details == {"entry_point": "ingest", "text_length": sum(map(len, texts))}


async def test_budget_below_safety_minimum(settings_stub: Any, engine: AsyncEngine) -> None:
    """A budget that cannot cover the model-assisted input checks (plus respond's generation
    call) is refused 422 ``budget_below_safety_minimum`` with ``minimum_required`` before any
    model call; nothing is written, not even an audit row. Complement asserted: a budget that
    covers them lets the same input through."""
    lib = library(settings_stub, engine)
    async with guarded_client(settings_stub, engine) as (client, epistrel):
        story = (await create(client, story_body("Ada")))["story_id"]
        await add_input_policies(lib, story, {"rule": _assisted(unique("Mean?"))})
        context = await guard_context(engine, story)
        needed = calls_needed(input_policies(context), [GateText("player_input", "x")]) + 1
        stub = epistrel.models.stub
        stub.calls.clear()
        before = (await story_state(engine, story), await audit_count(engine))
        refused = await gated(client, story, "respond", ["hello"], max_calls=needed - 1)
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "budget_below_safety_minimum"
        assert error["details"]["minimum_required"] == needed
        assert _guardrail_calls(stub) == 0
        assert (await story_state(engine, story), await audit_count(engine)) == before
        passed = await gated(client, story, "respond", ["hello"], max_calls=needed)
        assert passed.status_code == 200, passed.text


async def test_character_policy_applies_only_to_its_actor(
    settings_stub: Any, engine: AsyncEngine
) -> None:
    """A character-scoped input policy gates input only when that character acts.
    Complement asserted: the same text with no actor, or another actor, passes."""
    lib = library(settings_stub, engine)
    async with guarded_client(settings_stub, engine) as (client, _):
        created = await create(client, story_body("Ada", "Brann"))
        story = created["story_id"]
        ada, brann = (c["id"] for c in created["characters"])
        [policy] = await add_input_policies(
            lib,
            story,
            {"scope": "character", "scope_id": brann, "rule": _words("swordplay")},
        )
        for actor in (None, ada):
            fields = {} if actor is None else {"actor": actor}
            passed = await gated(client, story, "respond", ["some swordplay"], **fields)
            assert passed.status_code == 200, passed.text
        refused = await gated(client, story, "respond", ["some swordplay"], actor=brann)
        assert refused.status_code == 422, refused.text
        violations = refused.json()["error"]["details"]["violations"]
        assert [v["policy_id"] for v in violations] == [policy.id]


async def test_uncertain_follows_on_uncertain(settings_stub: Any, engine: AsyncEngine) -> None:
    """A low-confidence (``Uncertain``) verdict takes the policy's ``on_uncertain``:
    ``violation`` rejects; complement asserted: ``pass`` lets the same text through."""
    lib = library(settings_stub, engine)
    async with guarded_client(settings_stub, engine) as (client, epistrel):
        strict, lenient = unique("Strict?"), unique("Lenient?")
        epistrel.models.stub.when((strict, "murky"), UNSURE, purpose="guardrail_check")
        epistrel.models.stub.when((lenient, "murky"), UNSURE, purpose="guardrail_check")
        first = (await create(client, story_body("Ada")))["story_id"]
        await add_input_policies(lib, first, {"rule": _assisted(strict)})
        second = (await create(client, story_body("Ada")))["story_id"]
        await add_input_policies(lib, second, {"rule": _assisted(lenient, "pass")})
        refused = await gated(client, first, "pinned_memories", ["a murky memory"])
        assert refused.status_code == 422, refused.text
        passed = await gated(client, second, "pinned_memories", ["a murky memory"])
        assert passed.status_code == 200, passed.text


async def test_model_unavailable_writes_no_audit_row(
    settings_stub: Any, engine: AsyncEngine
) -> None:
    """A provider fault during a model-assisted input check is 503 with nothing written — no
    operation, event or audit row: nothing was decided. Complement asserted: the retry passes."""
    lib = library(settings_stub, engine)
    async with guarded_client(settings_stub, engine) as (client, epistrel):
        story = (await create(client, story_body("Ada")))["story_id"]
        await add_input_policies(lib, story, {"rule": _assisted(unique("Rude?"))})
        context = await guard_context(engine, story)
        assisted = calls_needed(input_policies(context), [GateText("items[0]", "x")])
        epistrel.models.stub.fault("guardrail_check", "http_500", n=assisted)
        before = (await story_state(engine, story), await audit_count(engine))
        failed = await gated(client, story, "custom_instructions", ["be kind"])
        assert failed.status_code == 503, failed.text
        assert (await story_state(engine, story), await audit_count(engine)) == before
        assert (await gated(client, story, "custom_instructions", ["be kind"])).status_code == 200


async def test_prechecks_run_before_the_gate(settings_stub: Any, engine: AsyncEngine) -> None:
    """#44's order: a stale expected revision (409) and an archived story (409) are refused
    before the gate runs, so violating text there writes no audit row. Complement asserted:
    on the live story at its revision, the same text is the gate's 422."""
    lib = library(settings_stub, engine)
    async with guarded_client(settings_stub, engine) as (client, _):
        story = (await create(client, story_body("Ada")))["story_id"]
        await add_input_policies(lib, story, {"rule": _words("verboten")})
        audits = await audit_count(engine)
        stale = await gated(
            client,
            story,
            "respond",
            ["verboten"],
            headers={"X-Epistrel-Expected-Revision": "99"},
        )
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "revision_conflict")
        refused = await gated(client, story, "respond", ["verboten"])
        assert refused.status_code == 422, refused.text
        assert await audit_count(engine) == audits + 1
        archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
        assert archived.status_code == 200, archived.text
        refused = await gated(client, story, "respond", ["verboten"])
        assert (refused.status_code, refused.json()["error"]["code"]) == (409, "story_archived")
        assert await audit_count(engine) == audits + 1


async def test_story_creation_gates_definitions(settings_stub: Any, engine: AsyncEngine) -> None:
    """``POST /stories`` gates every character definition (global policies, the rating's, and
    the minors policy with the cast's true ages): a violation is 422 naming the locator, no
    story, and an audit row without a story. Complement asserted: clean definitions create."""
    word = f"quux{new_ulid().lower()}"
    async with guarded_client(settings_stub, engine) as (client, epistrel):
        made = (
            await client.post(
                "/admin/policies",
                json={
                    "scope": "global",
                    "name": unique("create"),
                    "rule": _words(word),
                    "message": "Not in a definition.",
                },
                headers={**WRITE, "X-Epistrel-Principal": "p-operator"},
            )
        ).json()
        try:
            body = story_body("Ada", "Brann", name=unique("gated"))
            body["characters"][1]["definition"] = f"Brann says {word} a lot."
            stories_before = await count(engine, "SELECT count(*) FROM stories")
            refused = await client.post("/stories", json=body, headers=WRITE)
            assert refused.status_code == 422, refused.text
            [violation] = refused.json()["error"]["details"]["violations"]
            assert (violation["policy_id"], violation["locator"]) == (
                made["id"],
                "characters[1].definition",
            )
            assert await count(engine, "SELECT count(*) FROM stories") == stories_before
            last = (await audit_rows(engine))[-1]
            assert (last.story_id, last.details["entry_point"]) == (None, "story_create")

            # The minors policy sees the new cast's true ages at creation.
            recognise(epistrel.models.stub, "moonlit tryst")
            minor = story_body("Ada", "Brann")
            minor["characters"][1].update(true_age=15, definition="Brann plans a moonlit tryst.")
            refused = await client.post("/stories", json=minor, headers=WRITE)
            assert refused.status_code == 422, refused.text
            ids = [v["policy_id"] for v in refused.json()["error"]["details"]["violations"]]
            assert ids == [MINORS_POLICY_ID]
            minor["characters"][1]["true_age"] = 25
            assert (await client.post("/stories", json=minor, headers=WRITE)).status_code == 201

            body["characters"][1]["definition"] = "Brann is quiet."
            await create(client, body)
        finally:
            await client.delete(
                f"/admin/policies/{made['id']}",
                headers={**WRITE, "X-Epistrel-Principal": "p-operator"},
            )


async def test_story_settings_show_policy_kinds(settings_stub: Any, engine: AsyncEngine) -> None:
    """``GET /stories/{id}/settings``: settings with defaults, every effective policy with its
    scope, source, targets, age condition and kind, the minors policy inactive for an adult
    cast and active with a minor, and ``guardrail_calls`` counting model-assisted policies per
    target. ``/admin/policies`` shows ``kind`` too."""
    lib = library(settings_stub, engine)
    async with guarded_client(settings_stub, engine) as (client, _):
        created = await create(client, story_body("Ada", "Brann"))
        story = created["story_id"]
        brann = created["characters"][1]["id"]
        mine = await add_input_policies(
            lib,
            story,
            {"rule": _assisted(unique("Sad?")), "applies_to": ["text_input", "text_output"]},
            {"scope": "character", "scope_id": brann, "rule": _words("x")},
        )
        response = await client.get(f"/stories/{story}/settings", headers=READ)
        assert response.status_code == 200, response.text
        shown = response.json()
        assert shown["settings"]["regenerate_retry_budget"] == 2
        assert shown["settings"]["turn_concurrency"] == "reject"
        effective = {p["id"]: p for p in shown["effective_policies"]}
        assert effective[mine[0].id]["kind"] == "model_assisted"
        assert effective[mine[0].id]["source"] == "story"
        assert effective[mine[1].id]["source"] == f"character:{brann}"
        assert effective[mine[1].id]["kind"] == "deterministic"
        assert MINORS_POLICY_ID not in effective
        assert [p["id"] for p in shown["inactive_policies"]] == [MINORS_POLICY_ID]
        assert shown["inactive_policies"][0]["age_condition"] == "minor_involved_or_present"
        assisted_in = sum(
            1
            for p in shown["effective_policies"]
            if p["kind"] == "model_assisted"
            and "text_input" in p["applies_to"]
            and p["scope"] != "character"
        )
        assert shown["guardrail_calls"]["input"] == assisted_in
        assert shown["guardrail_calls"]["input"] >= 1

        minor = await explicit_story(engine, minor=True)
        shown = (await client.get(f"/stories/{minor}/settings", headers=READ)).json()
        assert MINORS_POLICY_ID in {p["id"] for p in shown["effective_policies"]}
        assert shown["inactive_policies"] == []

        listed = (await client.get("/admin/policies", headers=READ)).json()["items"]
        assert all(p["kind"] in ("deterministic", "model_assisted") for p in listed)


async def test_rejection_metric_counts_kinds(settings_stub: Any, engine: AsyncEngine) -> None:
    """``epistrel_guardrail_violations_total{kind,stage="input"}`` counts violated policies by
    kind (counts only, no text)."""
    lib = library(settings_stub, engine)
    name = "epistrel_guardrail_violations_total"
    async with guarded_client(settings_stub, engine) as (client, _):
        story = (await create(client, story_body("Ada")))["story_id"]
        await add_input_policies(lib, story, {"rule": _words("nope")})
        before = model_metrics.sample(name, kind="deterministic", stage="input")
        assert (await gated(client, story, "ingest", ["nope", "nope"])).status_code == 422
        after = model_metrics.sample(name, kind="deterministic", stage="input")
        assert after == before + 1


async def test_audit_log_is_append_only(engine: AsyncEngine) -> None:
    """``audit_log`` refuses updates and deletes; the two exceptions are nulling ``story_id``
    (story deletion) and a delete under ``epistrel.allow_audit_expiry`` (#71's sweep)."""
    row_id, story_id = new_ulid(), new_ulid()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_log (id, story_id, principal, kind, policy_ids) "
                "VALUES (:i, :s, 'p', 'guardrail_input_rejected', '{x}')"
            ),
            {"i": row_id, "s": story_id},
        )
    for statement in (
        "UPDATE audit_log SET principal = 'q' WHERE id = :i",
        "UPDATE audit_log SET story_id = NULL, principal = 'q' WHERE id = :i",
        "DELETE FROM audit_log WHERE id = :i",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"i": row_id})
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE audit_log SET story_id = NULL WHERE id = :i"), {"i": row_id}
        )
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('epistrel.allow_audit_expiry', 'on', true)"))
        await conn.execute(text("DELETE FROM audit_log WHERE id = :i"), {"i": row_id})
    assert await count(engine, "SELECT count(*) FROM audit_log WHERE id = :i", i=row_id) == 0
