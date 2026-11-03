"""#504 (ADR-176): a story-only character named in text a promotion copied into the catalog is
erasable from the catalog: ``{subject: {kind: character, id, story}, scope: {catalog: true}}``
reads its names from the story's subject index and redacts them from every catalog record; a
story-scope erasure stays story-scoped and lists the drafts promoted from the story."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ids import new_ulid
from tests import memory_support as memory
from tests.auth_support import bearer
from tests.erasure_support import PATH, WRITE, erasure_settings
from tests.event_support import scalar
from tests.respond_support import respond_client
from tests.story_support import create, story_body

pytestmark = pytest.mark.integration

AUTHOR = {**bearer(), "X-Epistrel-Principal": "p-author"}
READ = bearer()
SAID = "Wren says the ferry is late again, and Wren keeps the fare."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return erasure_settings(pg_url)


def story_character(character: str, story: str | None, *, dry_run: bool = False) -> dict[str, Any]:
    subject: dict[str, Any] = {"kind": "character", "id": character}
    if story is not None:
        subject["story"] = story
    return {"subject": subject, "scope": {"catalog": True}, "dry_run": dry_run}


async def seeded_texts(client: Any, catalog_id: str) -> list[str]:
    found = await client.get(
        f"/catalog/characters/{catalog_id}/companion",
        params={"draft": "true", "projection": "full"},
        headers=READ,
    )
    assert found.status_code == 200, found.text
    return [m["text"] for m in found.json()["starting_state"]["seeded_memories"]]


async def test_a_story_character_is_erased_from_the_catalog(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#504's repro: Wren and Dale; Dale holds a memory "Wren says …" and is promoted with it as
    a seed, so the companion draft names Wren. A story-scope dry run of Wren lists Dale's
    promoted draft and companion with the catalog request to send first. The catalog request:
    without ``story`` 422 ``invalid_erasure_scope`` (and ``story`` on a story scope too); an
    unknown story, or a story that does not hold the character, 422
    ``unknown_erasure_candidate``; its dry run counts the redaction and writes nothing; the real
    run redacts "Wren" from the companion draft, verifies a zero residue, writes a catalog-scope
    ledger row and names the story as referencing (the sentence-initial "Wren" is a candidate,
    ADR-154, confirmed by its dry-run id). The story erasure then still lists the
    draft, and leaves the catalog alone."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create(client, story_body("Wren", "Ada"))
        story_id = story["story_id"]
        wren, ada = (c["id"] for c in story["characters"])
        await memory.use_structured(engine, story_id)
        added = await client.post(
            f"/stories/{story_id}/characters",
            json={"name": "Dale Fennick", "definition": "A ferry hand.", "true_age": 40},
            headers=AUTHOR,
        )
        assert added.status_code == 201, added.text
        dale = added.json()["id"]
        op, _shared = await memory.turn(engine, story_id, actor=wren, voiced=[dale])
        sources = list((await memory.event_ids(engine, op.operation_id)).values())
        held = await memory.derived_memory(engine, story_id, SAID, sources, holder=dale)
        promoted = await client.post(
            f"/stories/{story_id}/characters/{dale}/promote",
            json={"seed_memories": [held]},
            headers=AUTHOR,
        )
        assert promoted.status_code == 201, promoted.text
        catalog_id = promoted.json()["catalog_id"]
        assert any("Wren" in t for t in await seeded_texts(client, catalog_id))

        # -- the story erasure's report line (a dry run, so the names are still there)
        listed = await client.post(
            PATH,
            json={
                "subject": {"kind": "character", "id": wren},
                "scope": {"story": story_id},
                "dry_run": True,
            },
            headers=WRITE,
        )
        assert listed.status_code == 200, listed.text
        (draft,) = listed.json()["promoted_drafts"]
        assert (draft["catalog_id"], draft["character_id"]) == (catalog_id, dale)
        assert len(draft["companion_ids"]) == 1
        assert draft["follow_up"] == {
            "subject": {"kind": "character", "id": wren, "story": story_id},
            "scope": {"catalog": True},
        }

        # -- refused, nothing written
        for body, code in (
            (story_character(wren, None), "invalid_erasure_scope"),
            (
                {
                    "subject": {"kind": "character", "id": wren, "story": story_id},
                    "scope": {"story": story_id},
                },
                "invalid_erasure_scope",
            ),
            (story_character(wren, new_ulid()), "unknown_erasure_candidate"),
            (story_character(new_ulid(), story_id), "unknown_erasure_candidate"),
        ):
            refused = await client.post(PATH, json=body, headers=WRITE)
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["code"] == code, refused.text
        other = await create(client, story_body("Kit", "Lee"))
        elsewhere = await client.post(
            PATH, json=story_character(ada, other["story_id"]), headers=WRITE
        )
        assert elsewhere.status_code == 422, elsewhere.text
        assert elsewhere.json()["error"]["code"] == "unknown_erasure_candidate"

        ledger = await scalar(engine, "SELECT count(*) FROM erasure_ledger")
        dry = await client.post(
            PATH, json=story_character(wren, story_id, dry_run=True), headers=WRITE
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["prose_segments"] >= 1
        # "Wren" starting a sentence is a candidate (a common word, ADR-154): confirmed below.
        candidates = [c["candidate_id"] for c in dry.json()["candidates"]]
        assert len(candidates) == 1
        assert dry.json()["subject"] == {"kind": "character", "id": wren, "story": story_id}
        assert any("Wren" in t for t in await seeded_texts(client, catalog_id))
        assert await scalar(engine, "SELECT count(*) FROM erasure_ledger") == ledger

        # -- the catalog erasure
        erased = await client.post(
            PATH,
            json={**story_character(wren, story_id), "confirm_candidates": candidates},
            headers=WRITE,
        )
        assert erased.status_code == 200, erased.text
        result = erased.json()
        assert (result["scope"], result["residue"]) == ("catalog", 0)
        assert result["prose_segments"] >= 1
        assert result["referencing_stories"] == [{"story_id": story_id, "character_ids": [wren]}]
        texts = await seeded_texts(client, catalog_id)
        assert not any("Wren" in t for t in texts), texts
        assert texts == ["[erased] says the ferry is late again, and [erased] keeps the fare."]
        row = await scalar(
            engine,
            "SELECT json_build_object('scope', scope, 'kind', subject_kind, 'id', subject_id,"
            " 'story', story_ref) FROM erasure_ledger WHERE id = :e",
            e=result["erasure_id"],
        )
        assert row == {"scope": "catalog", "kind": "character", "id": wren, "story": None}
        # The story itself is untouched by the catalog erasure (FR-STORE-14).
        assert await scalar(engine, "SELECT count(*) FROM characters WHERE id = :c", c=wren) == 1

        # -- the story erasure stays story-scoped and still lists the draft
        story_erased = await client.post(
            PATH,
            json={"subject": {"kind": "character", "id": wren}, "scope": {"story": story_id}},
            headers=WRITE,
        )
        assert story_erased.status_code == 200, story_erased.text
        assert [d["catalog_id"] for d in story_erased.json()["promoted_drafts"]] == [catalog_id]
        assert await seeded_texts(client, catalog_id) == texts
