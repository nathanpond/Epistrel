"""C-20: input guardrails (FR-GUARD-8,10,11) [M2, M3].

Text a consumer sends into a story is checked against every policy that applies to input
before anything is recorded. The truth-changing paths are #56's fixture caller
(``POST /_test/stories/{id}/gated-input``) for respond, ingest, pinned memories and custom
instructions, and the real ``POST /stories`` for character definitions; #57/#58/#60 add
``__`` slices through the real respond, ingest and pins/instructions routes.

Every C-20 (M3) part lives in this file. #111's, ``test_c_20__catalog_input_policy``: a catalog
scenario's content that breaks an input policy publishes with a warning and writes no audit row.
#119 adds the story-creation half here (creating a story from that scenario is refused).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.gate_support import ENTRY_POINTS, audit_rows, gated, input_policies
from tests.minors_support import MINOR_AGE, MINOR_NAME, explicit_story, guarded_client, recognise
from tests.policy_support import library, unique
from tests.scenario_support import publish as publish_scenario
from tests.scenario_support import scenario as catalog_scenario
from tests.scenario_support import seed
from tests.story_support import WRITE, count, create, rest, story_body
from tests.structured_support import story_state
from tests.validation_support import link_rating_policy, validate

CREATE_ROWS = "SELECT count(*) FROM audit_log WHERE details->>'entry_point' = 'story_create'"
OPERATOR = {**WRITE, "X-Epistrel-Principal": "p-operator"}
POLICIES = "/admin/policies"


async def _global_wordlist(client: Any, word: str) -> dict[str, Any]:
    response = await client.post(
        POLICIES,
        json={
            "scope": "global",
            "name": unique("c20-global"),
            "rule": {"type": "wordlist", "words": [word]},
            "message": "Please keep that word out of the story.",
        },
        headers=OPERATOR,
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def _retire(client: Any, policy_id: str) -> None:
    response = await client.delete(f"{POLICIES}/{policy_id}", headers=OPERATOR)
    assert response.status_code in (200, 204), response.text


async def _rating_with_policy(client: Any, engine: AsyncEngine, word: str) -> tuple[str, str]:
    """A new rating listing one rating policy created via ``POST /admin/policies`` with its
    ``applies_to`` left at the default (text output only)."""
    rating = (
        await create_rating(engine, RatingFields(name=unique("c20")), principal="p-operator")
    ).rating
    response = await client.post(
        POLICIES,
        json={
            "scope": "rating",
            "scope_id": rating.id,
            "name": unique("c20-rating"),
            "rule": {"type": "wordlist", "words": [word]},
            "message": "Not at this rating.",
        },
        headers=OPERATOR,
    )
    assert response.status_code == 201, response.text
    policy = response.json()
    assert policy["applies_to"] == ["text_output"]
    await update_rating(
        engine,
        rating.id,
        RatingReplacement(
            name=rating.name,
            description=rating.description,
            display_order=rating.display_order,
            prompt_guidance=rating.prompt_guidance,
            image_guidance=rating.image_guidance,
            policy_ids=[policy["id"]],
            inherits_from=[],
        ),
        principal="p-operator",
    )
    return rating.id, policy["id"]


@pytest.mark.integration
@pytest.mark.part("M2", "FR-GUARD-8,10,11")
async def test_c_20(pg_url: str, engine: AsyncEngine) -> None:
    """Violating input on every entry point → 422 naming the policy's id, name and message;
    no event, no operation, no revision change; the audit row holds policy ids only. A rating
    policy at its default scope passes mild input and catches the reply; set to input, it
    rejects that input. A minors-policy violation says a minor is involved or present, never
    who. Editing a global policy is recorded with the principal. Complements asserted: clean
    input on every entry point commits; the same rating policy is silent on input until set."""
    settings = auth_settings(pg_url, model_adapter="stub")
    word = f"zarquon{new_ulid().lower()}"
    async with guarded_client(settings, engine) as (client, epistrel):
        made = await _global_wordlist(client, word)
        try:
            story = (await create(client, story_body("Ada", "Brann")))["story_id"]

            # Every entry point: refused with nothing written but the audit row.
            for entry_point in ENTRY_POINTS:
                before = await story_state(engine, story)
                refused = await gated(client, story, entry_point, ["fine", f"say {word} now"])
                assert refused.status_code == 422, refused.text
                error = refused.json()["error"]
                assert error["code"] == "guardrail_violation"
                assert error["message"] == "Input violates 1 policy."
                [violation] = error["details"]["violations"]
                assert (violation["policy_id"], violation["name"], violation["message"]) == (
                    made["id"],
                    made["name"],
                    made["message"],
                )
                assert await story_state(engine, story) == before
                last = (await audit_rows(engine, story_id=story))[-1]
                assert (last.kind, list(last.policy_ids), last.principal) == (
                    "guardrail_input_rejected",
                    [made["id"]],
                    "p-author",
                )
                assert last.details["entry_point"] == entry_point
                # Complement: clean text on the same entry point commits.
                passed = await gated(client, story, entry_point, ["fine", "all good"])
                assert passed.status_code == 200, passed.text
                after = await story_state(engine, story)
                assert len(after.statuses) == len(before.statuses) + 1

            # A rating policy at its default scope (output) lets mild input through and
            # catches the reply; set to input as well, it rejects that input.
            mild = "oh darn it, the kettle again"
            rating_id, rating_policy = await _rating_with_policy(client, engine, "darn")
            body = story_body("Cyd", content_rating_id=rating_id)
            rated = (await create(client, body))["story_id"]
            epistrel.models.stub.when(mild, "Cyd shrugs: darn indeed.", purpose="generation")
            reply = await client.post(
                f"/_test/stories/{rated}/guarded-reply",
                json={"said": mild, "story_policies": True},
                headers=WRITE,
            )
            assert reply.status_code == 200, reply.text
            assert (reply.json()["input_violation"], reply.json()["output_violation"]) == (
                False,
                True,
            )
            current = (await client.get(f"{POLICIES}/{rating_policy}", headers=WRITE)).json()
            replaced = await client.put(
                f"{POLICIES}/{rating_policy}",
                json={
                    "name": current["name"],
                    "rule": current["rule"],
                    "message": current["message"],
                    "applies_to": ["text_output", "text_input"],
                    "age_condition": None,
                },
                headers=OPERATOR,
            )
            assert replaced.status_code == 200, replaced.text
            before = await story_state(engine, rated)
            refused = await client.post(
                f"/_test/stories/{rated}/guarded-reply",
                json={"said": mild, "story_policies": True},
                headers=WRITE,
            )
            assert refused.status_code == 422, refused.text
            ids = [v["policy_id"] for v in refused.json()["error"]["details"]["violations"]]
            assert ids == [rating_policy]
            assert await story_state(engine, rated) == before

            # The minors policy: a minor is involved or present — never who, never an age.
            minor = await explicit_story(engine, minor=True)
            recognise(epistrel.models.stub, "a very private night")
            refused = await gated(client, minor, "respond", ["a very private night together"])
            assert refused.status_code == 422, refused.text
            error = refused.json()["error"]
            assert error["message"] == "Input violates 1 policy: a minor is involved or present."
            assert [v["policy_id"] for v in error["details"]["violations"]] == [MINORS_POLICY_ID]
            for cost in ("model_calls", "elapsed_ms"):  # numeric; may contain "16"
                error["details"].pop(cost)
            shown = json.dumps(error)
            assert MINOR_NAME not in shown
            assert str(MINOR_AGE) not in shown

            # Editing a global policy is recorded with the acting principal.
            edited = await client.put(
                f"{POLICIES}/{made['id']}",
                json={
                    "name": made["name"],
                    "rule": made["rule"],
                    "message": "Please leave that word out.",
                    "applies_to": made["applies_to"],
                    "age_condition": None,
                },
                headers=OPERATOR,
            )
            assert edited.status_code == 200, edited.text
            actors = await rows(
                engine,
                "SELECT actor FROM config_changes WHERE setting = :s ORDER BY config_revision",
                s=f"policy:{made['id']}",
            )
            assert [r[0] for r in actors][-1] == "p-operator"
        finally:
            await _retire(client, made["id"])


@pytest.mark.integration
async def test_audit_log_no_text(
    pg_url: str, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected text never lands in the database or the logs: no ``audit_log`` column, no
    event, operation or story row holds any 12-character piece of a canary phrase, on every
    entry point and at story creation. Complement asserted: the audit rows exist (one per
    rejection) and record the text's length."""
    settings = auth_settings(pg_url, model_adapter="stub")
    canary = f"unsayable canary {new_ulid()} plum-velvet-orchard"
    pieces = {canary[i : i + 12] for i in range(len(canary) - 11)}
    lib = library(settings, engine)
    caplog.set_level(logging.DEBUG)
    async with guarded_client(settings, engine) as (client, _):
        story = (await create(client, story_body("Dov")))["story_id"]
        await input_policies(lib, story, {"rule": {"type": "wordlist", "words": ["unsayable"]}})
        for entry_point in ENTRY_POINTS:
            refused = await gated(client, story, entry_point, [canary])
            assert refused.status_code == 422, refused.text
        create_rows_before = await count(engine, CREATE_ROWS)
        made = await _global_wordlist(client, "unsayable")
        try:
            body = story_body("Eli", "Fen")
            body["characters"][1]["definition"] = canary
            body["name"] = unique("c20-canary")
            refused = await client.post("/stories", json=body, headers=WRITE)
            assert refused.status_code == 422, refused.text
            named = "SELECT count(*) FROM stories WHERE name = :n"
            assert await count(engine, named, n=body["name"]) == 0
            assert await count(engine, CREATE_ROWS) == create_rows_before + 1
        finally:
            await _retire(client, made["id"])

    audited = await audit_rows(engine, story_id=story)
    assert [a.details["text_length"] for a in audited] == [len(canary)] * len(ENTRY_POINTS)
    stored = await rows(
        engine,
        "SELECT t::text FROM audit_log t UNION ALL SELECT t::text FROM events t "
        "UNION ALL SELECT t::text FROM operations t UNION ALL SELECT t::text FROM stories t "
        "UNION ALL SELECT t::text FROM characters t",
    )
    for row in stored:
        assert not any(piece in row[0] for piece in pieces), row[0]
    for record in caplog.records:
        message = record.getMessage()
        assert not any(piece in message for piece in pieces), message


@pytest.mark.integration
@pytest.mark.part("M3", "FR-GUARD-10")
async def test_c_20__catalog_input_policy(settings: Settings, engine: AsyncEngine) -> None:
    """A catalog scenario whose seed breaks a global policy publishes (201) with an
    ``input_policy`` warning: outcome ``kept``, the policy's id as the rule, the seed's field and
    id, and the words it matched quoted as the text. Catalog content enters no story until it
    is created from it, so the publish refuses nothing and writes no audit row. A rating policy
    set to apply to input warns the same way for a scenario at that rating; one left at its
    default (output only) does not.

    Complement asserted: the same scenario with the word taken out validates without the
    warning."""
    word = f"zarquon{new_ulid().lower()}"
    async with rest(settings, engine) as client:
        made = await _global_wordlist(client, word)
        try:
            body = catalog_scenario(
                "Policed",
                starting_state={
                    "seeded_memories": [seed(f"The cellar hides a {word.upper()} under the hay.")],
                    "opening": {"narration": "Night falls on the farm."},
                },
            )
            created = (await client.post("/catalog/scenarios", json=body, headers=WRITE)).json()
            seed_id = created["starting_state"]["seeded_memories"][0]["id"]
            audit_before = await count(engine, "SELECT count(*) FROM audit_log")
            published = await publish_scenario(client, created["id"], created["draft_revision"])
            assert published.status_code == 201, published.text
            (warning,) = published.json()["report"]["warnings"]
            assert (warning["code"], warning["outcome"], warning["rule"]) == (
                "input_policy",
                "kept",
                made["id"],
            )
            assert warning["location"]["field"] == "starting_state.seeded_memories[0].text"
            assert (warning["location"]["seed_id"], warning["text"]) == (seed_id, word.upper())
            assert await count(engine, "SELECT count(*) FROM audit_log") == audit_before

            clean = await client.post(
                "/catalog/scenarios", json=catalog_scenario("Clean"), headers=WRITE
            )
            checked = await validate(client, clean.json()["id"])
            assert checked.json()["report"]["warnings"] == [], checked.text

            for applies_to, warned in (("text_input", True), (None, False)):
                rating = (
                    await create_rating(
                        engine, RatingFields(name=unique("c20-catalog")), principal="p-operator"
                    )
                ).rating
                guard = await link_rating_policy(client, engine, rating.id, applies_to=applies_to)
                policy = (await client.get(f"{POLICIES}/{guard}", headers=OPERATOR)).json()
                (rated_word,) = policy["rule"]["words"]
                rated = catalog_scenario(
                    "Rated",
                    metadata={"labels": [], "content_rating_id": rating.id},
                    world={
                        "description": f"A farm where the {rated_word} grows.",
                        "locations": [{"id": "kitchen", "name": "The kitchen"}],
                    },
                )
                drafted = (
                    await client.post("/catalog/scenarios", json=rated, headers=WRITE)
                ).json()
                found = (await validate(client, drafted["id"])).json()["report"]["warnings"]
                rules = [(w["code"], w["location"]["field"], w["rule"]) for w in found]
                assert rules == ([("input_policy", "world.description", guard)] if warned else [])
        finally:
            await _retire(client, made["id"])
