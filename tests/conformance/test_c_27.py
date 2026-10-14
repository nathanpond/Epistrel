"""C-27: feedback export (docs/04 §5), its M3 part (FR-FB-4, FR-ADMIN-8; #126).

Every C-27 (M3) part lives in this file (traceability counts a test ID per milestone in one
file). The details (filters, replays, the log, deletion, expiry, erasure and the object store)
are in ``tests/integration/test_feedback_reporting.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from epistrel.api import create_app
from epistrel.core.erasure.boundary import DELIVERED_EXPORTS, normalised
from epistrel.core.feedback_export import BOUNDARY_NOTE
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from tests.auth_support import auth_settings
from tests.conftest import REPO_ROOT
from tests.event_support import rows
from tests.feedback_support import (
    OPERATOR,
    OTHER_RATER,
    answered,
    as_principal,
    cases_of,
    error_of,
    export,
    export_content,
    export_rows,
    feedback_rows,
    play,
    played_story,
    rate,
    scratch_feedback,
    set_deployment,
)

CASE_FIELDS = {
    "position",
    "message_id",
    "player_input",
    "message_text",
    "rating",
    "tags",
    "text",
    "config_fingerprint",
    "profile_version",
    "models",
    "quality_status",
}


def _files(root: Path) -> set[str]:
    """The export files under the media root (shared by the tests of one process)."""
    folder = root / "exports"
    return {path.name for path in folder.iterdir()} if folder.is_dir() else set()


async def _keys(engine: Any) -> list[Any]:
    """The deployment-scoped idempotency keys (an export's would be one)."""
    return await rows(engine, "SELECT key FROM idempotency_keys WHERE scope = 'deployment'")


@pytest.mark.integration
@pytest.mark.part("M3", "FR-FB-4; FR-ADMIN-8")
async def test_c_27__export(pg_url: str, dummy_dsn: str) -> None:
    """With ``feedback_export`` off (the default) the export endpoint refuses (403
    ``feedback_export_disabled``) and records nothing. With it on, an export returns labelled
    cases, one per current rating, each with the message text, the player input it answered, its
    story position, the configuration fingerprint, the rating, its tags and free text; the file
    is JSON Lines with sorted keys, served behind the service credential; and the export is
    recorded with what (case count, filters), when and by which principal. The API description
    and the operations guide state that delivered exports are outside the erasure boundary.

    Complements asserted: the refusal leaves no export row, file or idempotency key; the file's
    size and checksum are the record's; an export with no matching rating is an empty file; the
    content route refuses a request without the credential."""
    async with scratch_feedback(pg_url) as (client, lib, db):
        played = await played_story(client)
        story = played.story_id
        second = await play(client, story, played.actor, "Is the lighthouse lit?")
        later = second["message"]["message_id"]
        answered(
            await rate(
                client,
                story,
                played.message_id,
                {"rating": 2, "tags": ["factual_error"], "text": "The ferry was cancelled."},
            ),
            201,
        )
        answered(await rate(client, story, later, {"rating": 5}, OTHER_RATER), 201)

        # -- off by default: refused, nothing recorded
        keys = await _keys(db)
        files = _files(lib.media.root)
        refused = await export(client)
        assert error_of(refused) == (
            403,
            "feedback_export_disabled",
            {"setting": "feedback_export"},
        )
        assert await export_rows(db) == []
        assert await _keys(db) == keys
        assert _files(lib.media.root) == files

        # -- on: the labelled cases, recorded
        assert (await set_deployment(client, feedback_export=True))["feedback_export"] is True
        written = answered(await export(client), 201)
        export_id = written["export_id"]
        assert written == {
            "export_id": export_id,
            "content_url": f"/admin/feedback/exports/{export_id}/content",
            "case_count": 2,
            "config_revision": written["config_revision"],
            "idempotency_key": written["idempotency_key"],
        }
        (row,) = await export_rows(db)
        assert (row.id, row.principal, row.case_count, row.blob_key, row.filters) == (
            export_id,
            OPERATOR,
            2,
            f"exports/{export_id}.jsonl",
            {},
        )
        assert row.downloaded_at is None

        downloaded = await export_content(client, export_id)
        assert downloaded.headers["content-type"] == "application/x-ndjson"
        data = downloaded.content
        assert data.endswith(b"\n")
        assert (len(data), hashlib.sha256(data).hexdigest()) == (row.bytes, row.sha256)
        cases = cases_of(downloaded)
        assert [json.dumps(c, sort_keys=True, separators=(",", ":")) for c in cases] == [
            line.decode() for line in data.splitlines()
        ]
        ratings = {r.message_id: r for r in await feedback_rows(db, story)}
        expected = [
            (played.message_id, 1, {"say": "Is the ferry running?", "do": None}, played.turn),
            (later, 2, {"say": "Is the lighthouse lit?", "do": None}, second),
        ]
        assert len(cases) == 2
        for case, (message_id, turn, said, answer) in zip(cases, expected, strict=True):
            rating = ratings[message_id]
            assert set(case) == CASE_FIELDS
            assert case == {
                "position": {"story_id": story, "turn": turn, "variant": 1},
                "message_id": message_id,
                "player_input": said,
                "message_text": answer["message"]["text"],
                "rating": rating.rating,
                "tags": list(rating.tags),
                "text": rating.text,
                "config_fingerprint": answer["fingerprint"],
                "profile_version": f"{LITE_PRESET_ID}@1",
                "models": rating.models,
                "quality_status": "uncertified",
            }
        assert (cases[0]["rating"], cases[0]["tags"], cases[0]["text"]) == (
            2,
            ["factual_error"],
            "The ferry was cancelled.",
        )

        record = answered(
            await client.get(f"/admin/feedback/exports/{export_id}", headers=as_principal(None)),
            200,
        )
        assert (record["principal"], record["case_count"], record["sha256"]) == (
            OPERATOR,
            2,
            row.sha256,
        )
        assert record["downloaded_at"] is not None
        listed = answered(
            await client.get("/admin/feedback/exports", headers=as_principal(None)), 200
        )
        assert [item["export_id"] for item in listed["items"]] == [export_id]

        unauthenticated = await client.get(f"/admin/feedback/exports/{export_id}/content")
        assert unauthenticated.status_code == 401

        empty = answered(await export(client, {"filters": {"min_rating": 4, "max_rating": 4}}), 201)
        assert empty["case_count"] == 0
        assert (await export_content(client, empty["export_id"])).content == b""
        assert len(await export_rows(db)) == 2

    # -- the documentation: the API description and the operations guide
    document = create_app(auth_settings(dummy_dsn)).openapi()
    described = document["paths"]["/admin/feedback/export"]["post"]["description"]
    assert DELIVERED_EXPORTS in described
    assert "outside the erasure boundary" in described
    content = document["paths"]["/admin/feedback/exports/{export_id}/content"]["get"]
    assert normalised(BOUNDARY_NOTE) in normalised(content["description"])
    guide = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert normalised(BOUNDARY_NOTE) in normalised(guide)
    assert f"- {DELIVERED_EXPORTS}" in guide
