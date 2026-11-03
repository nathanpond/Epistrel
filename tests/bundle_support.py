"""Story bundles in tests (#704): ``build_rich_story``, the I-10 M7 fixture, and ``open_bundle``.

``build_rich_story`` makes a story with what a bundle must carry beyond its turns: a truncated
tail (discarded operations) and an image on a character (a ``media_assets`` row whose blob
``BLOBS`` holds, read by ``read_blob``). ``open_bundle`` reads an archive into its manifest and
decoded lines per member.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import bundles
from epistrel.core.ids import new_ulid
from tests.event_support import play_turn
from tests.story_support import WRITE, create, story_body

IMAGE = b"\x89PNG\r\n\x1a\nnot really a png, but bytes to carry"
IMAGE_SHA = hashlib.sha256(IMAGE).hexdigest()
BLOBS: dict[str, bytes] = {IMAGE_SHA: IMAGE}


def read_blob(sha256: str) -> bytes:
    """The fixture's blob reader (``FileNotFoundError`` for an unknown checksum)."""
    try:
        return BLOBS[sha256]
    except KeyError:
        raise FileNotFoundError(sha256) from None


@dataclass
class RichStory:
    story_id: str
    media_id: str
    rated: str
    discarded: list[str] = field(default_factory=list)


async def build_rich_story(client: Any, engine: AsyncEngine) -> RichStory:
    """The I-10 M7 story (see the module docstring)."""
    story = (await create(client, story_body("Wren", "Ada")))["story_id"]
    ops = [await play_turn(engine, story, f"Line {n}.", principal="p-export") for n in range(4)]
    cut = await client.post(f"/stories/{story}/truncate", json={"to_turn": 2}, headers=WRITE)
    assert cut.status_code == 200, cut.text
    media_id = new_ulid()
    async with engine.begin() as conn:
        version = await conn.scalar(
            text(
                "SELECT r.current_version_id FROM content_ratings r JOIN stories s "
                "ON s.content_rating_id = r.id WHERE s.id = :s"
            ),
            {"s": story},
        )
        await conn.execute(
            text(
                "INSERT INTO media_assets (id, owner_principal, kind, mime_type, size, checksum, "
                "content_rating_version_id, source, state, depicted_subjects, created_at, "
                "created_by) VALUES (:i, 'p-export', 'image', 'image/png', :n, :c, :v, "
                "'uploaded', 'live', '[]'::jsonb, now(), 'p-export')"
            ),
            {"i": media_id, "n": len(IMAGE), "c": IMAGE_SHA, "v": version},
        )
        await conn.execute(
            text(
                "UPDATE characters SET images = CAST(:img AS jsonb) WHERE story_id = :s "
                "AND name = 'Ada'"
            ),
            {"img": json.dumps([{"media_id": media_id, "role": "portrait"}]), "s": story},
        )
    return RichStory(
        story_id=story,
        media_id=media_id,
        rated="",
        discarded=[o.operation_id for o in ops[2:]],
    )


@dataclass
class Opened:
    manifest: dict[str, Any]
    members: dict[str, bytes]

    def lines(self, path: str) -> list[Any]:
        data = self.members[path]
        if path.endswith(".json"):
            return [json.loads(data)]
        return [json.loads(line) for line in data.splitlines() if line]


def open_bundle(data: bytes) -> Opened:
    members = bundles.read_archive(data)
    assert members[0][0] == "manifest.json"
    return Opened(json.loads(members[0][1]), dict(members[1:]))
