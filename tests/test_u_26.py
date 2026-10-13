"""U-26: scenario slots and the starting-cast limit (docs/04 §3), its M3 parts.

Every U-26 (M3) part lives in this file (traceability counts a test ID per milestone in one
file); #111 (validation) and #119 (story creation) add theirs here.

- #108's part, ``test_u_26__roles_and_limit``: the role rules a scenario save enforces, a
  role's eligibility as stored, a scenario with no predefined character, and the starting-cast
  limit's resolution (from the deployment's stored settings) and ceiling (FR-SCN-3,4). The
  details are in ``tests/integration/test_scenario_catalog.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import CastLimits, effective_cast_limit
from epistrel.config import Settings
from epistrel.core.config_changes import read_settings
from epistrel.core.ids import new_ulid
from tests.event_support import scalar
from tests.scenario_support import (
    BASE,
    READ,
    WRITE,
    create,
    error_of,
    publish,
    role,
    scenario,
    set_cast_limits,
)
from tests.story_support import rest

COUNT = "SELECT count(*) FROM catalog.scenario_catalog"


def _cast(*roles: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"roles": list(roles), **fields}


async def _deployment_limits(engine: AsyncEngine) -> CastLimits:
    """The deployment's starting-cast settings as stored, read the way a scenario save reads
    them (``CastLimits.of`` over ``read_settings``)."""
    return CastLimits.of((await read_settings(engine)).as_settings())


@pytest.mark.integration
@pytest.mark.part("M3", "FR-SCN-3,4")
async def test_u_26__roles_and_limit(settings: Settings, engine: AsyncEngine) -> None:
    """Role rules hold when a scenario is saved: a ``locked`` role without a character, an
    ``open`` role with one, and a second player role are each 422 ``validation_error`` at the
    field, and nothing is stored. A role's eligibility (labels, content ratings, and an
    inclusive age range, here with an open end) is stored as sent. A scenario with no
    predefined character, every role ``open``, saves and publishes.

    The starting-cast limit: a scenario without its own value gets the deployment's default,
    16; a scenario's 20 and a creation's override of 24 stand; under a ceiling of 20 both are
    capped at 20, a scenario saved above the ceiling is 422 ``cast_limit_exceeded`` and one at
    it saves. The default is the deployment's own: the stored settings read 16 with no ceiling,
    and once ``/admin/settings`` sets the default to 14 (``GET`` returns it), three stored
    scenarios resolve through the stored settings to 14 (no value of its own), 20 and 12 (their
    own values, above and below the default); a ceiling of 13 caps the first two at 13 and
    leaves the 12.

    Complement asserted: each refused body saves once its rule is kept, so every 422 is the
    rule's doing."""
    held = {"catalog_id": new_ulid(), "version": 1}
    refusals = [
        (_cast(role("mother", fill="locked")), "body.cast.roles[0].character"),
        (_cast(role("mother", character=held)), "body.cast.roles[0].character"),
        (
            _cast(role("eldest", kind="player"), role("youngest", kind="player")),
            "body.cast.roles[1].kind",
        ),
    ]
    kept = [
        _cast(role("mother", fill="locked", character=held)),
        _cast(role("mother", fill="default", character=held)),
        _cast(role("eldest", kind="player"), role("youngest")),
    ]
    async with rest(settings, engine) as client:
        before = await scalar(engine, COUNT)
        for cast, field in refusals:
            refused = await client.post(BASE, json=scenario(cast=cast), headers=WRITE)
            assert (refused.status_code, error_of(refused)["code"]) == (422, "validation_error")
            assert error_of(refused)["details"]["field"] == field
        assert await scalar(engine, COUNT) == before
        for cast in kept:
            assert (await client.post(BASE, json=scenario(cast=cast), headers=WRITE)).is_success

        eligibility = {
            "labels": ["warm", "patient"],
            "content_ratings": [new_ulid()],
            "age_range": [40, None],
        }
        stored = await create(
            client,
            scenario(cast=_cast(role("mother", eligibility=eligibility), role("guest"))),
        )
        assert stored["cast"]["roles"][0]["eligibility"] == eligibility
        assert [(r["fill"], r.get("character")) for r in stored["cast"]["roles"]] == [
            ("open", None),
            ("open", None),
        ]
        done = await publish(client, stored["id"], stored["draft_revision"])
        assert (done.status_code, done.json()["version"]) == (201, 1)

        deployment = CastLimits()
        assert deployment.default == 16
        assert effective_cast_limit(None, None, deployment) == 16
        assert effective_cast_limit(20, None, deployment) == 20
        assert effective_cast_limit(20, 24, deployment) == 24
        capped = CastLimits(default=16, ceiling=20)
        assert effective_cast_limit(24, None, capped) == 20
        assert effective_cast_limit(None, 24, capped) == 20
        assert effective_cast_limit(None, None, capped) == 16
        assert await _deployment_limits(engine) == deployment
        try:
            await set_cast_limits(client, ceiling=20)
            over = await client.post(
                BASE, json=scenario(cast=_cast(max_characters_at_start=21)), headers=WRITE
            )
            assert (over.status_code, error_of(over)["code"]) == (422, "cast_limit_exceeded")
            at = await create(client, scenario(cast=_cast(max_characters_at_start=20)))
            assert at["cast"]["max_characters_at_start"] == 20
            own = await create(client, scenario(cast=_cast(max_characters_at_start=12)))
            assert own["cast"]["max_characters_at_start"] == 12

            await set_cast_limits(client, default=14, ceiling=None)
            shown = (await client.get("/admin/settings", headers=READ)).json()
            assert (
                shown["max_characters_at_start_default"],
                shown["max_characters_at_start_ceiling"],
            ) == (14, None)
            values = [s["cast"]["max_characters_at_start"] for s in (stored, at, own)]
            assert values == [None, 20, 12]
            limits = await _deployment_limits(engine)
            assert limits == CastLimits(default=14, ceiling=None)
            assert [effective_cast_limit(v, None, limits) for v in values] == [14, 20, 12]
            await set_cast_limits(client, ceiling=13)
            limits = await _deployment_limits(engine)
            assert limits == CastLimits(default=14, ceiling=13)
            assert [effective_cast_limit(v, None, limits) for v in values] == [13, 13, 12]
        finally:
            await set_cast_limits(client, default=16, ceiling=None)
