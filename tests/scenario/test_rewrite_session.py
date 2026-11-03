"""Rewrite sessions (FR-REWRITE-1,2,4; #223): states, clarifications, recommendations, answers,
cancel, expiry, limits and the Lite interim refusal, through REST with the stub adapter.

- "Bobby went to the other school" when the story has two schools the parse cannot tell apart
  asks a ``clarification`` question with a recommended option and its reason, and the session
  waits (``awaiting_answers``, no preview); ``accept_recommended`` binds it, re-plans and the
  session is ``awaiting_approval`` with a preview. A free-text answer naming a word a global
  policy forbids is 422 ``guardrail_violation`` and no answer is stored.
- Every question this module's sessions created has a recommended option and a reason.
- Cancel moves the session to ``cancelled`` with no amendment; any action after it is 409
  ``rewrite_session_closed``; an unknown session is 404; a character viewpoint 403.
- ``rewrite_session_expiry`` moves an idle session to ``expired``; ``applying`` never expires.
- A story's sixth open session is 409 ``rewrite_session_limit``.
- A Lite story's session is 409 ``module_off {module: lite_rewrites}`` with no row and no
  ``rewrite_parse`` call, on REST and through the library (#245 inverts this).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.errors import EpistrelError
from epistrel.core.ids import new_ulid
from epistrel.core.propositions import store
from epistrel.orchestrator.rewrite.api import RewriteRequest
from epistrel.orchestrator.rewrite.session import expire_idle
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, rows, scalar
from tests.policy_support import unique
from tests.respond_support import respond_client
from tests.story_support import WRITE, create, story_body
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration

OMNISCIENT = {"viewpoint": "omniscient"}
OPERATOR = {**WRITE, "X-Epistrel-Principal": "p-operator"}
OTHER_SCHOOL = json.dumps(
    {
        "changes": [
            {
                "kind": "proposition",
                "subject": "Bobby",
                "predicate": "enrolled_at",
                "object": "school",
            }
        ]
    }
)
PLAIN = json.dumps(
    {
        "changes": [
            {"kind": "proposition", "subject": "Bobby", "predicate": "enrolled_at", "object": "Oak"}
        ]
    }
)
STORIES: list[str] = []


async def _story(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], dict[str, str]]:
    story, ids = await structured_story(client, "Cal", "Bobby")
    async with engine.begin() as conn:
        schools = {
            name: await store.add_entity(conn, story, "organization", name)
            for name in ("North School", "South School", "Oak")
        }
    STORIES.append(story)
    return story, ids, schools


async def _open(
    client: Any, story: str, instruction: str = "Bobby went to the other school."
) -> Any:
    return await client.post(
        f"/stories/{story}/rewrites",
        params=OMNISCIENT,
        json={"instruction": instruction},
        headers=WRITE,
    )


async def test_rewrite_session(pg_url: str, engine: AsyncEngine) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    word = f"zarquon{new_ulid().lower()}"
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, _ = await _story(client, engine)
        stub.script("rewrite_parse", [OTHER_SCHOOL])
        opened = await _open(client, story)
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert (session["state"], session["preview"]) == ("awaiting_answers", None)
        (question,) = session["questions"]
        assert question["kind"] == "clarification"
        labels = {o["label"]: o["id"] for o in question["options"]}
        assert set(labels) == {"North School", "South School"}
        assert question["recommended_option_id"] in labels.values()
        assert question["recommendation_reason"]

        # A free-text answer that breaks a global policy: 422, nothing stored.
        policy = await client.post(
            "/admin/policies",
            json={
                "scope": "global",
                "name": unique("rewrite"),
                "rule": {"type": "wordlist", "words": [word]},
                "message": "Please keep that word out of the story.",
            },
            headers=OPERATOR,
        )
        assert policy.status_code == 201, policy.text
        path = f"/stories/{story}/rewrites/{session['id']}"
        refused = await client.post(
            f"{path}/answers",
            params=OMNISCIENT,
            json={"answers": [{"question_id": question["id"], "text": f"the {word} one"}]},
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "guardrail_violation"
        assert (await client.get(path, params=OMNISCIENT, headers=WRITE)).json()["questions"][0][
            "answered_at"
        ] is None
        bad = await client.post(
            f"{path}/answers",
            params=OMNISCIENT,
            json={"answers": [{"question_id": question["id"], "option_id": "o9"}]},
            headers=WRITE,
        )
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "invalid_answer"

        # Accepting the recommendation binds the school and re-plans.
        answered = await client.post(
            f"{path}/answers",
            params=OMNISCIENT,
            json={"answers": [{"question_id": question["id"], "accept_recommended": True}]},
            headers=WRITE,
        )
        assert answered.status_code == 200, answered.text
        done = answered.json()
        assert done["state"] == "awaiting_approval"
        (change,) = done["preview"]["changes"]
        recommended = next(
            o for o in question["options"] if o["id"] == question["recommended_option_id"]
        )
        assert change["object_id"] == recommended["effect"]["bind_entity"]
        (kept,) = done["questions"]
        assert kept["accepted_recommendation"] is True
        assert kept["answered_by"] == WRITE["X-Epistrel-Principal"]

        # Reads: a character viewpoint is 403, an unknown session 404.
        character = await client.get(path, params={"viewpoint": ids["Bobby"]}, headers=WRITE)
        assert character.status_code == 403
        missing = await client.get(
            f"/stories/{story}/rewrites/{new_ulid()}", params=OMNISCIENT, headers=WRITE
        )
        assert (missing.status_code, missing.json()["error"]["code"]) == (
            404,
            "rewrite_session_not_found",
        )

        # Cancel: nothing committed; any action afterwards is 409.
        cancelled = await client.post(f"{path}/cancel", params=OMNISCIENT, headers=WRITE)
        assert cancelled.json()["state"] == "cancelled"
        assert (
            await scalar(engine, "SELECT count(*) FROM amendments WHERE story_id = :s", s=story)
            == 0
        )
        for again in (
            client.post(f"{path}/cancel", params=OMNISCIENT, headers=WRITE),
            client.post(
                f"{path}/answers",
                params=OMNISCIENT,
                json={"answers": [{"question_id": question["id"], "accept_recommended": True}]},
                headers=WRITE,
            ),
        ):
            closed = await again
            assert (closed.status_code, closed.json()["error"]["code"]) == (
                409,
                "rewrite_session_closed",
            )
        assert (await client.get(path, params=OMNISCIENT, headers=WRITE)).status_code == 200

        # Expiry: an idle session expires; one being applied does not.
        stub.when("", PLAIN, purpose="rewrite_parse")
        idle = (await _open(client, story, "Bobby went to Oak.")).json()
        busy = (await _open(client, story, "Bobby went to Oak, really.")).json()
        await execute_in_tx(
            engine,
            (
                "UPDATE rewrite_sessions SET expires_at = now() - interval '1 minute', "
                "state = CASE WHEN id = :b THEN 'applying' ELSE state END "
                "WHERE id IN (:i, :b)",
                {"i": idle["id"], "b": busy["id"]},
            ),
        )
        assert await expire_idle(engine) >= 1
        states = {
            r.id: r.state
            for r in await rows(
                engine,
                "SELECT id, state FROM rewrite_sessions WHERE id IN (:i, :b)",
                i=idle["id"],
                b=busy["id"],
            )
        }
        assert states == {idle["id"]: "expired", busy["id"]: "applying"}

        # The limit: five open sessions (``applying`` counts), the sixth refused.
        listed = await client.get(f"/stories/{story}/rewrites", params=OMNISCIENT, headers=WRITE)
        assert [i["id"] for i in listed.json()["items"]][:2] == [busy["id"], idle["id"]]
        for n in range(4):
            assert (await _open(client, story, f"Bobby went to Oak ({n}).")).status_code == 201
        limited = await _open(client, story, "Bobby went to Oak, once more.")
        assert (limited.status_code, limited.json()["error"]["code"]) == (
            409,
            "rewrite_session_limit",
        )
        assert limited.json()["error"]["details"] == {"limit": 5, "open": 5}

        # Every question created here carries a recommendation (owner decision 4).
        asked = await rows(
            engine,
            "SELECT recommended_option_id, recommendation_reason, options FROM rewrite_questions "
            "WHERE story_id = ANY(:s)",
            s=STORIES,
        )
        assert asked
        for row in asked:
            assert row.recommendation_reason
            assert row.recommended_option_id in {o["id"] for o in row.options}


async def test_rewrite_lite_interim(pg_url: str, engine: AsyncEngine) -> None:
    """Until #245, a Lite story's session is 409 ``module_off {module: lite_rewrites}`` on REST
    and through ``Engine.open_rewrite``. Complement: no session row and no ``rewrite_parse``
    call."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        epistrel.models.stub.calls.clear()
        refused = await _open(client, story, "Ada was always a sailor.")
        # 409, the engine-wide status of ``module_off`` (the plan's 422 is logged as a deviation).
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["details"]["module"]) == ("module_off", "lite_rewrites")
        with pytest.raises(EpistrelError) as found:
            await epistrel.open_rewrite(
                RequestContext(principal="p-author"),
                story,
                RewriteRequest(instruction="Ada was always a sailor."),
                viewpoint="omniscient",
            )
        assert found.value.code == "module_off"
        assert [c for c in epistrel.models.stub.calls if c.purpose == "rewrite_parse"] == []
        assert (
            await scalar(
                engine, "SELECT count(*) FROM rewrite_sessions WHERE story_id = :s", s=story
            )
            == 0
        )
