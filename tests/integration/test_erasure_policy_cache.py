"""#469 (NFR-PRIV-2, FR-ADMIN-6): an erasure that redacts a policy is never followed by the
effective-policy cache (``orchestrator.policies.store``) serving its unredacted text.

The cache is per process, so the fix keys it on a revision the erasure moves in the database
(``policy_erasure_revision``: the story's in story scope, the deployment's in catalog scope)
rather than on an in-process signal. Each test warms the cache with the story's settings read
first, keeps a copy of every cached entry, erases, and reads again twice: in this process, and
with the cache put back to the copy, as a process that never saw the erasure holds it.
"""

from __future__ import annotations

import json
import random
import string
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.orchestrator.policies import store as policy_store
from tests import erasure_support as e
from tests.policy_support import unique, words
from tests.respond_support import new_story, respond_client
from tests.story_support import WRITE

pytestmark = pytest.mark.integration

CTX = RequestContext(principal="p-policy-cache")


@pytest.fixture
def erasure_settings(pg_url: str) -> Settings:
    return e.erasure_settings(pg_url)


def _name() -> str:
    """A capitalised word no other subject of the shared database uses."""
    return "Q" + "".join(random.choices(string.ascii_lowercase, k=9))  # noqa: S311 - a test name


def _names(body: dict[str, Any]) -> dict[str, str | None]:
    return {p["id"]: p["name"] for p in body["effective_policies"]}


async def _settings(client: httpx.AsyncClient, story: str) -> dict[str, Any]:
    shown = await client.get(f"/stories/{story}/settings", headers=WRITE)
    assert shown.status_code == 200, shown.text
    found: dict[str, Any] = shown.json()
    return found


async def _refusal(client: httpx.AsyncClient, story: str, word: str) -> dict[str, Any]:
    """The input gate's 422 for custom instructions using ``word``: the one violation."""
    response = await client.put(
        f"/stories/{story}/custom-instructions",
        json={"text": f"Mention {word} often."},
        headers=WRITE,
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "guardrail_violation"
    (violation,) = error["details"]["violations"]
    found: dict[str, Any] = violation
    return found


async def _reads_redacted(
    client: httpx.AsyncClient,
    lib: Any,
    story: str,
    policy_id: str,
    word: str,
    subject: str,
    stale: dict[Any, Any],
) -> None:
    """In this process, then with the cache as a process that never saw the erasure holds it
    (``stale``): the settings, the library's effective set and the input gate's 422 all show
    the redacted policy and never ``subject``."""
    for label in ("this process", "another process"):
        if label == "another process":
            policy_store._EFFECTIVE.clear()
            policy_store._EFFECTIVE.update(stale)
        shown = await _settings(client, story)
        assert _names(shown)[policy_id] == "Keep [erased] offstage", label
        found = {p.id: p for p in await lib.policies_effective(CTX, story)}
        assert found[policy_id].name == "Keep [erased] offstage", label
        refused = await _refusal(client, story, word)
        assert (refused["policy_id"], refused["name"]) == (policy_id, "Keep [erased] offstage")
        for text in (json.dumps(shown), json.dumps(refused), repr(found)):
            assert subject not in text, label


async def test_a_story_scope_erasure_is_not_served_from_the_policy_cache(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """A story policy naming a story character, read once (cached); the character erased in
    story scope; the next reads show the policy redacted, in this process and in another."""
    subject, word = _name(), unique("zq")
    async with respond_client(erasure_settings, engine) as (client, lib):
        story, ids = await new_story(client, "Pell", subject, "Tamsin")
        patched = await client.patch(
            f"/stories/{story}/settings",
            json={
                "policies": [
                    {
                        "name": f"Keep {subject} offstage",
                        "rule": words(word),
                        "applies_to": ["text_input", "text_output"],
                        "message": f"{subject} is not here.",
                    }
                ]
            },
            headers=WRITE,
        )
        assert patched.status_code == 200, patched.text
        (policy_id,) = (
            p["id"] for p in patched.json()["effective_policies"] if p["scope"] == "story"
        )
        # Complement: before the erasure the policy is served, by name, from the cache.
        assert _names(await _settings(client, story))[policy_id] == f"Keep {subject} offstage"
        refused = await _refusal(client, story, word)
        assert refused["name"] == f"Keep {subject} offstage"
        stale = dict(policy_store._EFFECTIVE)
        assert any(story in key for key in stale)

        done = await e.erase(client, "character", ids[subject], story)
        assert done["records"].get("policies") == 1

        await _reads_redacted(client, lib, story, policy_id, word, subject, stale)


async def test_a_catalog_scope_erasure_is_not_served_from_the_policy_cache(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """A global policy naming a catalog character, read through a story's settings once
    (cached); the catalog character erased in catalog scope; the next reads show the policy
    redacted, in this process and in another. The policy is retired afterwards (the database
    is shared)."""
    subject, alias, word = _name(), _name(), unique("zq")
    async with respond_client(erasure_settings, engine) as (client, lib):
        catalog_id = await e.linda(client, subject, alias)
        made = await client.post(
            "/admin/policies",
            json={
                "scope": "global",
                "name": f"Keep {subject} offstage",
                "rule": words(word),
                "message": f"{subject} is not cursed.",
            },
            headers=e.WRITE,
        )
        assert made.status_code == 201, made.text
        policy_id = made.json()["id"]
        try:
            story, _ = await new_story(client, "Pell", "Tamsin")
            assert _names(await _settings(client, story))[policy_id] == f"Keep {subject} offstage"
            stale = dict(policy_store._EFFECTIVE)
            assert any(story in key for key in stale)

            done = await e.erase(client, "catalog_character", catalog_id)
            assert done["records"].get("policies") == 1

            await _reads_redacted(client, lib, story, policy_id, word, subject, stale)
        finally:
            retired = await client.delete(f"/admin/policies/{policy_id}", headers=e.WRITE)
            assert retired.status_code == 200, retired.text
