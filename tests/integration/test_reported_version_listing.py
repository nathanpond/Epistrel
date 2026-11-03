"""#710 (FR-PROF-16; #234's capture clause): a completion that reports no
``system_fingerprint`` records the ``version`` field of its model's ``/models`` entry, probed
once and then reused for an hour; a reported ``system_fingerprint`` wins; neither leaves the
version unknown (no row)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from epistrel.core.db import make_engine
from epistrel.orchestrator.models import reported_version
from epistrel.orchestrator.models.openai_compatible import versions_of
from tests.auth_support import auth_settings
from tests.conformance.conftest import migrated_database
from tests.event_support import rows
from tests.respond_support import FIRST, PLAYER, body, new_story, played, respond_client
from tests.story_support import WRITE

pytestmark = pytest.mark.integration

VERSIONS = "SELECT model_id, reported_version FROM model_reported_versions"


def test_versions_of_reads_the_version_field() -> None:
    def model(id: str, **extra: object) -> SimpleNamespace:
        return SimpleNamespace(id=id, model_extra=extra)

    listed = [model("a", version="1.2"), model("b"), model("c", version=" "), model("d", version=3)]
    assert versions_of(listed) == {"a": "1.2", "d": "3"}
    assert versions_of([SimpleNamespace(id="e", model_extra=None)]) == {}


async def test_the_listed_version_is_the_fallback(pg_url: str) -> None:
    reported_version.forget()
    # A database of its own: the model configuration and the versions are deployment-wide.
    with migrated_database(pg_url, "epistrel_listed_version") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            async with respond_client(settings, db) as (client, lib):
                stub = lib.models.stub
                put = await client.put(
                    "/admin/model-config/generation", json={"model": "stub-model"}, headers=WRITE
                )
                assert put.status_code == 200, put.text
                story, ids = await new_story(client, PLAYER, FIRST)
                actor = ids[PLAYER]

                # Neither reported nor listed: nothing recorded.
                await played(client, story, body(actor, "Hello?"))
                assert await rows(db, VERSIONS) == []

                # Listed only: the listing's version, probed once for the hour.
                reported_version.forget()
                stub.listed_version = "lm-2026-11"
                await played(client, story, body(actor, "Anyone home?"))
                listings = stub.listings
                await played(client, story, body(actor, "Hello again."))
                assert stub.listings == listings, "the listing is reused within the hour"
                found = {tuple(r) for r in await rows(db, VERSIONS)}
                assert ("stub-model", "lm-2026-11") in found

                # A reported fingerprint wins over the listing.
                stub.reported_version = "fp-abc"
                await played(client, story, body(actor, "Still there?"))
                assert ("stub-model", "fp-abc") in {tuple(r) for r in await rows(db, VERSIONS)}
        finally:
            await db.dispose()
            reported_version.forget()
