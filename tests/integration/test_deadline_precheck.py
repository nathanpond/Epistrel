"""#602 (NFR-PERF-6,7; ADR-206): the structured deadline pre-check and per-model latency
estimates.

- ``purpose_latency_estimates`` (a deployment setting): per model, per purpose, the seconds one
  call takes. A lookup falls through model + purpose → model + ``*`` → ``*`` + purpose → ``*`` +
  ``*`` → 2 s. A write merges (``null`` deletes), refuses a purpose that is not a model purpose
  and a value outside (0, 600].
- ``respond``: a request whose explicit ``budget.deadline_s`` (less the time spent) is below its
  single-primary safety minimum's calls at the estimates is 422
  ``deadline_below_safety_minimum``, with the estimate, the deadline and the timed slots, before
  any model call and with no idempotency record; the refusal counts in
  ``epistrel_budget_rejected_total{reason="deadline_below_safety_minimum"}``. Complements: a
  workable deadline plays (the same key, so the refusal left nothing behind), and a profile whose
  own ``budgets.deadline_ms`` is below the estimate is never refused (the owner's R5 (a)).
- ``POST /engine-profiles/{id}/validate`` warns ``below_safety_minimum`` with ``setting:
  budgets.deadline_ms`` and the same figures; a deadline above the estimate warns of nothing.
- The in-turn gates use each kind's own estimate, and a call that just fits may start.
- #1004: a deadline exactly at the estimate passes ``check_deadline`` (the clock frozen), and
  the same over REST gives 200.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import latency
from epistrel.core.db import make_engine
from epistrel.core.errors import DeadlineBelowSafetyMinimum
from epistrel.core.profiles.reservation import (
    Reservation,
    budget_warnings,
    estimated_minimum,
    minimum_slots,
)
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.seeds.profiles import structured_m4_settings
from epistrel.core.settings_schema import LatencyEstimateSettings
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.structured import planner
from epistrel.orchestrator.structured.planner import Timing, TurnPlan, check_deadline
from epistrel.testing.profiles import structured_m4
from tests.auth_support import auth_settings
from tests.character_support import OPERATOR
from tests.event_support import scalar
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, override
from tests.structured_turn_support import beat, render, structured_story

DEADLINE = "deadline_below_safety_minimum"
SLOW = {"*": {"*": 0.1, "generation": 10.0}}
"""Every model's generation costs 10 s, every other call 0.1 s."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture(autouse=True)
async def _seeded_estimates_after(pg_url: str) -> AsyncIterator[None]:
    """The shared database keeps no estimates a test wrote (a later test would plan its turns
    with a 10 s generation): the stored key is removed, which reads as the seeded default, and
    the revision moves so every cache rereads it."""
    yield
    engine = make_engine(auth_settings(pg_url))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE deployment_state SET settings = settings - :key, "
                    "config_revision = config_revision + 1"
                ),
                {"key": latency.SETTING},
            )
    finally:
        await engine.dispose()


def _rejected() -> float:
    return REGISTRY.get_sample_value("epistrel_budget_rejected_total", {"reason": DEADLINE}) or 0.0


async def _estimates(client: httpx.AsyncClient, value: Any) -> httpx.Response:
    return await client.put("/admin/settings", json={latency.SETTING: value}, headers=OPERATOR)


# -- the lookup and the setting's shape ---------------------------------------------------------


def test_the_lookup_falls_through_to_the_wildcards_and_then_two_seconds() -> None:
    table = {
        "m": {"generation": 5.0, "*": 4.0},
        "*": {"guardrail_check": 3.0, "*": 1.5},
    }
    assert latency.estimate(table, "m", "generation") == 5.0
    assert latency.estimate(table, "m", "entailment") == 4.0  # model + *
    assert latency.estimate(table, "other", "guardrail_check") == 3.0  # * + purpose
    assert latency.estimate(table, "other", "entailment") == 1.5  # * + *
    assert latency.estimate(table, None, "generation") == 1.5  # nothing configured: *
    assert latency.estimate({}, "m", "generation") == latency.FALLBACK_S == 2.0
    # The shipped map: Haiku's measured generation, a model with no entry the * fallback.
    assert latency.estimate(None, "claude-haiku-5-5", "generation") == 3.88
    assert latency.estimate(None, "unlisted", "generation") == 2.0


def test_the_setting_refuses_a_value_outside_zero_to_six_hundred() -> None:
    assert latency.problems({"m": {"generation": 1.0}}) == []
    assert latency.problems({"m": {"generation": 0}})
    assert latency.problems({"m": {"generation": 601}})
    assert latency.problems({"m": {"generation": True}})
    assert latency.problems({"": {"generation": 1.0}})


# -- the time minimum ---------------------------------------------------------------------------


def test_the_minimum_times_each_reserved_call_in_order() -> None:
    found = Reservation(extraction=1, composed=1, guardrail_input=1, guardrail_output=1, outcome=3)
    assert minimum_slots(found) == [
        "generation",
        "extraction_backstop",
        "extraction_backstop",
        "guardrail_check",
        "guardrail_check",
        # O: its render, its extraction and its output check.
        "generation",
        "extraction_backstop",
        "guardrail_check",
    ]
    timed = estimated_minimum(
        found,
        estimates={"*": {"generation": 3.0, "*": 1.0}},
        models={"generation": "m", "extraction_backstop": None},
    )
    assert timed.seconds == pytest.approx(3.0 * 2 + 1.0 * 6)
    payload = timed.payload()
    assert payload["estimated_minimum_s"] == 12.0
    assert payload["slots"][0] == {"purpose": "generation", "model": "m", "seconds": 3.0}


def _structured(deadline_ms: int) -> ProfileSettings:
    document = structured_m4_settings()
    document["budgets"]["deadline_ms"] = deadline_ms
    return ProfileSettings.model_validate(document)


def test_a_structured_profile_below_the_time_minimum_warns() -> None:
    models = {"generation": None, "extraction_backstop": None, "guardrail_check": None}
    (warning,) = budget_warnings(
        _structured(2_000), guardrail_input=0, guardrail_output=0, estimates=SLOW, models=models
    )
    assert warning["code"] == "below_safety_minimum"
    assert warning["setting"] == "budgets.deadline_ms"
    assert warning["deadline_s"] == 2.0
    assert warning["estimated_minimum_s"] >= 10.0
    # Complements: a deadline above the minimum, and no models (no time check), warn of nothing.
    assert (
        budget_warnings(
            _structured(60_000),
            guardrail_input=0,
            guardrail_output=0,
            estimates=SLOW,
            models=models,
        )
        == []
    )
    assert budget_warnings(_structured(2_000), guardrail_input=0, guardrail_output=0) == []


def test_each_kind_starts_by_its_own_estimate_and_a_call_that_just_fits_starts() -> None:
    budget = TurnBudget(deadline_s=10.0, estimates={"salience": 20.0, "entailment": 1.0})
    assert not budget.can_start("salience")
    assert budget.can_start("entailment")
    assert budget.can_start("unlisted")  # the flat estimate (0 here)
    fits = TurnBudget(deadline_s=10.0, latency_estimate_s=4.0)
    fits.remaining_s = lambda: 4.0  # type: ignore[method-assign]  # exactly the estimate left
    assert fits.can_start()
    fits.remaining_s = lambda: 3.99  # type: ignore[method-assign]
    assert not fits.can_start()
    assert TurnBudget(estimates={"salience": 99.0}).can_start("salience")  # no deadline


def _frozen(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    """``planner``'s clock stands at ``now``: no time is spent since a turn started there."""
    monkeypatch.setattr(planner, "time", SimpleNamespace(monotonic=lambda: now))


def test_a_deadline_at_the_estimate_passes_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1004 (#602's test plan): with no time spent, a deadline exactly at the timed minimum
    passes (``>=``); a hair below it is refused with the figures.

    Complement asserted: the refusal names the deadline and the estimate."""
    plan = TurnPlan(
        primaries=1, beats_planned=1, reservation=Reservation(extraction=1, composed=1), optional=()
    )
    timing = Timing(models={"generation": "m"}, estimates={"*": {"generation": 3.0, "*": 1.0}})
    _frozen(monkeypatch, 100.0)
    # generation 3 s, then two extraction backstops at 1 s each.
    check_deadline(plan, timing, deadline_s=5.0, started=100.0)
    with pytest.raises(DeadlineBelowSafetyMinimum) as refused:
        check_deadline(plan, timing, deadline_s=4.99, started=100.0)
    assert refused.value.details["deadline_s"] == 4.99
    assert refused.value.details["estimated_minimum_s"] == 5.0
    # Time spent counts against the deadline: 0.5 s since the start leaves 4.5 s.
    with pytest.raises(DeadlineBelowSafetyMinimum):
        check_deadline(plan, timing, deadline_s=5.0, started=99.5)


# -- the REST surfaces ---------------------------------------------------------------------------


async def test_a_deadline_below_the_timed_minimum_is_refused_before_any_call(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")
        written = await _estimates(client, SLOW)
        assert written.status_code == 200, written.text
        assert written.json()[latency.SETTING]["*"] == SLOW["*"]
        key = str(uuid.uuid4())
        before, calls = _rejected(), len(stub.calls)

        refused = await respond(
            client,
            story,
            body(ids["Wren"], "Hello.", budget={"deadline_s": 2}),
            headers={"Idempotency-Key": key},
        )
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == DEADLINE
        details = error["details"]
        assert details["deadline_s"] == 2
        assert details["estimated_minimum_s"] >= 10.0
        assert details["slots"][0]["purpose"] == "generation"
        assert details["slots"][0]["seconds"] == 10.0
        assert len(stub.calls) == calls  # no model call
        assert (
            await scalar(engine, "SELECT count(*) FROM idempotency_keys WHERE key = :k", k=key) == 0
        )
        assert _rejected() == before + 1

        # Complement: the same key with a workable deadline plays; the refusal left nothing.
        stub.script("generation", [render(beat("say", "Hello, Wren."))])
        played = await respond(
            client,
            story,
            body(ids["Wren"], "Hello.", budget={"deadline_s": 40}),
            headers={"Idempotency-Key": key},
        )
        assert played.status_code == 200, played.text
        assert _rejected() == before + 1


async def test_a_deadline_at_the_estimate_plays(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1004 (#602's test plan): an explicit ``budget.deadline_s`` exactly at the timed minimum
    gives 200. The pre-check's clock is frozen at the turn's start (``check_deadline`` sees no
    time spent), so the deadline is the estimate to the second. The pre-check runs after the
    idempotency claim (``call.claim``), not before it as #602's plan said; a refusal releases
    the row (``test_a_deadline_below_the_timed_minimum_is_refused_before_any_call``), so the
    order is left as it is.

    Complement asserted: one second less is refused with the same estimate."""
    real = planner.check_deadline

    def frozen(plan: TurnPlan, timing: Timing, *, deadline_s: float, started: float) -> None:
        with monkeypatch.context() as patch:
            _frozen(patch, started)
            real(plan, timing, deadline_s=deadline_s, started=started)

    monkeypatch.setattr(planner, "check_deadline", frozen)
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await structured_story(client, "Wren", "Ada")
        # Every call 1 s: the minimum is a whole number of seconds, its slots' count.
        assert (await _estimates(client, {"*": {"*": 1.0}})).status_code == 200
        refused = await respond(
            client, story, body(ids["Wren"], "Hello.", budget={"deadline_s": 1})
        )
        assert refused.status_code == 422, refused.text
        details = refused.json()["error"]["details"]
        at = details["estimated_minimum_s"]
        assert at == len(details["slots"]) > 1

        below = await respond(
            client, story, body(ids["Wren"], "Hello.", budget={"deadline_s": at - 1})
        )
        assert below.status_code == 422, below.text
        assert below.json()["error"]["details"]["estimated_minimum_s"] == at

        lib.models.stub.script("generation", [render(beat("say", "Hello, Wren."))])
        played = await respond(
            client, story, body(ids["Wren"], "Hello.", budget={"deadline_s": at})
        )
        assert played.status_code == 200, played.text


async def test_a_profile_deadline_below_the_estimate_is_never_refused(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The owner's R5 (a): only an explicit ``budget.deadline_s`` is refused; the story's own
    deadline below the estimate plays (degrading as its budget allows), and the validator
    warns instead."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await structured_story(client, "Wren", "Ada")
        assert (await _estimates(client, SLOW)).status_code == 200
        await override(client, story, {"budgets": {"deadline_ms": 2_000}})
        lib.models.stub.script("generation", [render(beat("say", "Hello, Wren."))])
        played = await respond(client, story, body(ids["Wren"], "Hello."))
        assert played.status_code == 200, played.text

        settings = structured_m4_settings()
        settings["budgets"]["deadline_ms"] = 2_000
        validated = await client.post(
            f"/engine-profiles/{structured_m4()}/validate",
            json={"settings": settings},
            headers=WRITE,
        )
        assert validated.status_code == 200, validated.text
        (warning,) = [
            w for w in validated.json()["warnings"] if w["setting"] == "budgets.deadline_ms"
        ]
        assert warning["code"] == "below_safety_minimum"
        assert warning["deadline_s"] == 2.0
        assert warning["estimated_minimum_s"] >= 10.0
        # Complement: a deadline above the minimum warns of no deadline.
        settings["budgets"]["deadline_ms"] = 60_000
        clean = await client.post(
            f"/engine-profiles/{structured_m4()}/validate",
            json={"settings": settings},
            headers=WRITE,
        )
        assert clean.status_code == 200, clean.text
        assert all(w["setting"] != "budgets.deadline_ms" for w in clean.json()["warnings"])


async def test_the_setting_merges_and_refuses_an_unknown_purpose(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    async with respond_client(stub_settings, engine) as (client, _lib):
        # The shipped default (a test before this one may have written the deployment's).
        shipped = LatencyEstimateSettings().purpose_latency_estimates
        assert shipped == latency.SEEDED
        assert (await _estimates(client, latency.SEEDED)).status_code == 200
        merged = await _estimates(client, {"m": {"generation": 4.0}, "cydonia-24b-v4.3": None})
        assert merged.status_code == 200, merged.text
        found = merged.json()[latency.SETTING]
        assert found["m"] == {"generation": 4.0}
        assert "cydonia-24b-v4.3" not in found  # null deletes
        assert found["claude-haiku-5-5"] == latency.SEEDED["claude-haiku-5-5"]
        unknown = await _estimates(client, {"m": {"no_such_purpose": 1.0}})
        assert unknown.status_code == 422, unknown.text
        assert unknown.json()["error"]["details"]["unknown_purposes"] == ["no_such_purpose"]
        zero = await _estimates(client, {"m": {"generation": 0}})
        assert zero.status_code == 422, zero.text


def test_the_local_seeds_are_the_model_matrix_figures() -> None:
    """``tools/latency_seeds`` recomputes the local tier's seeds from the committed matrix (the
    hosted seeds need #818's local traces: ``--check`` on the owner's machine)."""
    from epistrel.tools import latency_seeds

    assert latency_seeds.local() == latency.SEEDED[latency_seeds.LOCAL]
    assert latency_seeds.quantile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]) == 9.0
