"""The seeded minors policy (#55; FR-GUARD-11, FR-CAT-12): the seed row, redaction of the
violation, no model call without a minor, the live threshold, and the policy as ordinary
editable, audited data."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.policies.model import Target
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import metrics
from epistrel.orchestrator.policies.check import check_text
from epistrel.orchestrator.policies.seeds.policies import (
    MINORS_POLICY,
    MINORS_POLICY_ID,
    SEED_PRINCIPAL,
)
from epistrel.orchestrator.policies.store import get_policy, guard_context
from tests.auth_support import auth_settings, bearer
from tests.minors_support import (
    MINOR_AGE,
    MINOR_NAME,
    explicit_story,
    guarded_client,
    recognise,
    subject_checks,
)
from tests.story_support import WRITE, rest

PHRASE = "a very private night together"
CHECKS = "epistrel_model_calls_total"


def _checks_made() -> float:
    return sum(
        metrics.sample(CHECKS, purpose="guardrail_check", model="stub", outcome=outcome)
        for outcome in metrics.OUTCOMES
    )


async def test_seed_row(engine: AsyncEngine) -> None:
    """Migration 0009 inserted the module's row: global, text input and output, model-assisted,
    conditioned on a minor involved or present, created by ``system``."""
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM policies WHERE id = :id"), {"id": MINORS_POLICY_ID}
                )
            )
            .one()
            ._mapping
        )
    for key in ("scope", "scope_id", "story_id", "kind", "effect", "age_condition"):
        assert row[key] == MINORS_POLICY[key], key
    assert row["applies_to"] == ["text_input", "text_output"]
    assert row["rule"]["type"] == "model_assisted"
    assert row["rule"]["on_uncertain"] == "violation"
    assert "{minor_age_threshold}" in row["rule"]["prompt"]
    assert (row["created_by"], row["retired_at"]) == (SEED_PRINCIPAL, None)


async def test_minor_error_redaction(pg_url: str, engine: AsyncEngine) -> None:
    """The 422 for a minors violation says a minor is involved or present and never who:
    neither the character's name, nor its age, nor its label appears anywhere in the body.
    Complement asserted: the policy's own id, name and message do appear."""
    settings = auth_settings(pg_url, model_adapter="stub")
    story = await explicit_story(engine, minor=True)
    async with guarded_client(settings, engine) as (client, epistrel):
        recognise(epistrel.models.stub, PHRASE)
        refused = await client.post(
            f"/_test/stories/{story}/guarded-reply",
            json={"said": f"{MINOR_NAME} and Jonah spend {PHRASE}", "story_policies": True},
            headers=WRITE,
        )
    assert refused.status_code == 422, refused.text
    body = refused.text
    for leak in (MINOR_NAME, *MINOR_NAME.split(), str(MINOR_AGE), "character_"):
        assert leak not in body, leak
    assert not re.search(r"\bage\b", body)
    error = refused.json()["error"]
    assert error["message"] == "Input violates 1 policy: a minor is involved or present."
    assert error["details"]["violations"] == [
        {
            "policy_id": MINORS_POLICY_ID,
            "name": MINORS_POLICY["name"],
            "message": MINORS_POLICY["message"],
        }
    ]


async def test_no_call_without_minor(pg_url: str, engine: AsyncEngine) -> None:
    """With an adult-only cast the minors policy costs nothing: checking a text against the
    seeded policy makes no ``guardrail_check`` call and moves no call counter, and a guarded
    reply sends no check with subjects. Complement asserted: with a 16-year-old in the cast
    the same check makes exactly one call, carrying the subjects."""
    settings = auth_settings(pg_url, model_adapter="stub")
    adults = await explicit_story(engine, minor=False)
    minors = await explicit_story(engine, minor=True)
    seeded = await get_policy(engine, MINORS_POLICY_ID)
    async with guarded_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        adult = await guard_context(engine, adults)
        counted = _checks_made()
        outcome = await check_text(
            epistrel.models,
            PHRASE,
            [seeded],
            Target.TEXT_INPUT,
            budget=TurnBudget(),
            cast=adult.cast,
            threshold=adult.threshold,
        )
        assert not outcome.violated
        assert [c for c in stub.calls if c.purpose == "guardrail_check"] == []
        assert _checks_made() == counted
        assert MINORS_POLICY_ID in {p.id for p in adult.inactive}
        assert MINORS_POLICY_ID not in {p.id for p in adult.active}

        reply = await client.post(
            f"/_test/stories/{adults}/guarded-reply",
            json={"said": PHRASE, "story_policies": True},
            headers=WRITE,
        )
        assert reply.status_code == 200, reply.text
        assert subject_checks(stub) == []

        minor = await guard_context(engine, minors)
        stub.calls.clear()
        await check_text(
            epistrel.models,
            PHRASE,
            [seeded],
            Target.TEXT_INPUT,
            budget=TurnBudget(),
            cast=minor.cast,
            threshold=minor.threshold,
        )
        assert len(subject_checks(stub)) == 1
        assert _checks_made() == counted + 1


async def test_threshold_is_read_live(settings: Settings, engine: AsyncEngine) -> None:
    """``minor_age_threshold`` is read at check time: raised above the adult's age, the same
    cast now has a minor and the policy applies. Complement asserted: restored, it does not."""
    adults = await explicit_story(engine, minor=False)
    assert MINORS_POLICY_ID not in {p.id for p in (await guard_context(engine, adults)).active}
    async with rest(settings, engine) as client:
        start = (await client.get("/admin/settings", headers=bearer())).json()
        raised = await client.put(
            "/admin/settings", json={"minor_age_threshold": 35}, headers=WRITE
        )
        assert raised.status_code == 200, raised.text
        try:
            context = await guard_context(engine, adults)
            assert context.threshold == 35
            assert MINORS_POLICY_ID in {p.id for p in context.active}
        finally:
            back = {"minor_age_threshold": start["minor_age_threshold"]}
            assert (await client.put("/admin/settings", json=back, headers=WRITE)).is_success
    assert MINORS_POLICY_ID not in {p.id for p in (await guard_context(engine, adults)).active}


async def test_seeded_policy_is_ordinary_audited_data(
    settings: Settings, engine: AsyncEngine
) -> None:
    """An administrator edits the seeded policy through ``/admin/policies`` like any other;
    each edit is recorded in ``config_changes`` with the acting principal. The original
    content is put back afterwards (the tests share one database)."""
    url = f"/admin/policies/{MINORS_POLICY_ID}"
    async with rest(settings, engine) as client:
        original = (await client.get(url, headers=bearer())).json()
        content = {k: original[k] for k in ("name", "applies_to", "rule", "age_condition")}
        edited = await client.put(
            url,
            json={**content, "message": "Not here."},
            headers={**bearer(), "X-Epistrel-Principal": "p-admin"},
        )
        assert edited.status_code == 200, edited.text
        restored = await client.put(
            url, json={**content, "message": original["message"]}, headers=WRITE
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["message"] == MINORS_POLICY["message"]
    async with engine.connect() as conn:
        actors = list(
            await conn.scalars(
                text(
                    "SELECT actor FROM config_changes WHERE setting = :s "
                    "ORDER BY config_revision DESC LIMIT 2"
                ),
                {"s": f"policy:{MINORS_POLICY_ID}"},
            )
        )
    assert actors == ["p-author", "p-admin"]


async def test_age_condition_values(settings: Settings, engine: AsyncEngine) -> None:
    """``age_condition`` accepts ``minor_involved_or_present`` on any kind of policy and
    nothing else in M2 (a narrower condition needs presence, M5)."""
    body: dict[str, Any] = {
        "scope": "global",
        "name": "Age-conditioned wordlist (#55 test)",
        "rule": {"type": "wordlist", "words": ["zzqx-minor-test"]},
        "message": "No.",
    }
    async with rest(settings, engine) as client:
        for value in ("minor_present", "minor_involved", "adult_present"):
            refused = await client.post(
                "/admin/policies", json={**body, "age_condition": value}, headers=WRITE
            )
            assert refused.status_code == 422, value
        created = await client.post(
            "/admin/policies",
            json={**body, "age_condition": "minor_involved_or_present"},
            headers=WRITE,
        )
        assert created.status_code == 201, created.text
        assert created.json()["age_condition"] == "minor_involved_or_present"
        retired = await client.delete(f"/admin/policies/{created.json()['id']}", headers=WRITE)
        assert retired.status_code == 200
