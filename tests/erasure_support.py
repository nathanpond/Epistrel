"""Helpers for the erasure tests (#124): the request, a played Lite story naming a catalog
character in every place a name can sit, and a test-side residue scan.

The scan here is the test's own, independent of ``core.erasure.verify``: it enumerates every
story-scoped table with a text column from ``information_schema`` (plus the rows that name the
story by ``scope_id`` or by one of its operations) and looks for whole-word names in each value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import character_support as characters
from tests import genesis_support as g
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings, bearer
from tests.event_support import execute_in_tx
from tests.feedback_support import rate
from tests.model_support import config_key
from tests.respond_support import body, played
from tests.story_support import override
from tests.summary_support import run_due_jobs, segment_rows

PATH = "/admin/erasure"
OPERATOR = "p-operator"
WRITE = {**bearer(), "X-Epistrel-Principal": OPERATOR}
TEXT_TYPES = ("text", "character varying", "jsonb", "json", "ARRAY")
SUMMARY_OVERRIDES = {
    "memory": {"rolling_summary": {"window_messages": 4, "compress_threshold_tokens": 10_000}}
}


def erasure_settings(pg_url: str, **overrides: Any) -> Settings:
    """The stub adapter with a configuration key (the ledger's hashes are keyed with it)."""
    values: dict[str, Any] = {"model_adapter": "stub", "config_key": config_key(), **overrides}
    return auth_settings(pg_url, **values)


def request(
    kind: str,
    subject_id: str,
    story_id: str | None = None,
    *,
    dry_run: bool = False,
    confirm: Iterable[str] = (),
) -> dict[str, Any]:
    scope: dict[str, Any] = {"story": story_id} if story_id is not None else {"catalog": True}
    found: dict[str, Any] = {
        "subject": {"kind": kind, "id": subject_id},
        "scope": scope,
        "dry_run": dry_run,
    }
    if confirm:
        found["confirm_candidates"] = list(confirm)
    return found


async def erase(
    client: httpx.AsyncClient,
    kind: str,
    subject_id: str,
    story_id: str | None = None,
    *,
    dry_run: bool = False,
    key: str | None = None,
    status: int = 200,
    confirm: Iterable[str] = (),
    confirm_all: bool = False,
) -> dict[str, Any]:
    """``POST /admin/erasure``; ``confirm_all`` first asks a dry run for the candidates
    (ADR-154: "Mum's patience..." starts a sentence with a common word) and confirms them all."""
    if confirm_all:
        listed = await erase(client, kind, subject_id, story_id, dry_run=True)
        confirm = [*confirm, *(c["candidate_id"] for c in listed["candidates"])]
    headers = dict(WRITE)
    if key is not None:
        headers["Idempotency-Key"] = key
    response = await client.post(
        PATH,
        json=request(kind, subject_id, story_id, dry_run=dry_run, confirm=confirm),
        headers=headers,
    )
    assert response.status_code == status, response.text
    found: dict[str, Any] = response.json()
    return found


def word(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def naming(value: str | None, terms: Iterable[str]) -> bool:
    return value is not None and any(word(t).search(value) for t in terms)


async def story_text_tables(engine: AsyncEngine) -> dict[str, list[str]]:
    """Every public table with a ``story_id`` column and its text columns (from
    ``information_schema``)."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT c.table_name, c.column_name FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                "WHERE t.table_type = 'BASE TABLE' AND c.table_schema = 'public' "
                "AND c.data_type = ANY(CAST(:types AS text[])) AND EXISTS ("
                "SELECT 1 FROM information_schema.columns s WHERE s.table_schema = 'public' "
                "AND s.table_name = c.table_name AND s.column_name = 'story_id') "
                "ORDER BY c.table_name, c.ordinal_position"
            ),
            {"types": list(TEXT_TYPES)},
        )
        columns: dict[str, list[str]] = {}
        for row in found:
            columns.setdefault(str(row.table_name), []).append(str(row.column_name))
    return columns


SCOPE_ID_SOURCES = {
    "stories": "id = :s",
    "config_changes": "scope = 'story' AND scope_id = :s",
    "idempotency_keys": "(scope = 'story' AND scope_id = :s) OR operation_id IN "
    "(SELECT operation_id FROM operations WHERE story_id = :s)",
}
"""The story's rows in tables that name it otherwise than by ``story_id``."""
EXCLUDED = frozenset({"subject_references"})
"""The index: erasure purges the subject's rows (asserted on its own)."""


async def story_values(engine: AsyncEngine, story_id: str) -> dict[str, list[str]]:
    """Every text value of the story's rows, per table (``story_id`` tables enumerated from
    ``information_schema``, and the ``SCOPE_ID_SOURCES``)."""
    sources = {
        table: (columns, "story_id = :s")
        for table, columns in (await story_text_tables(engine)).items()
        if table not in EXCLUDED
    }
    async with engine.connect() as conn:
        for table, where in SCOPE_ID_SOURCES.items():
            columns = list(
                await conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE "
                        "table_schema = 'public' AND table_name = :t "
                        "AND data_type = ANY(CAST(:types AS text[]))"
                    ),
                    {"t": table, "types": list(TEXT_TYPES)},
                )
            )
            sources[table] = (columns, where)
        values: dict[str, list[str]] = {}
        for table, (columns, where) in sources.items():
            selected = ", ".join(f'CAST("{c}" AS text)' for c in columns)
            found = await conn.execute(
                text(f'SELECT {selected} FROM "{table}" WHERE {where}'),  # noqa: S608
                {"s": story_id},
            )
            values[table] = [str(v) for row in found for v in row if v is not None]
    return values


async def residue(engine: AsyncEngine, story_id: str, terms: Iterable[str]) -> dict[str, int]:
    """Per table, how many of the story's text values name one of ``terms`` (0s included)."""
    wanted = list(terms)
    return {
        table: sum(1 for value in found if naming(value, wanted))
        for table, found in (await story_values(engine, story_id)).items()
    }


async def summary_chunk(engine: AsyncEngine, story_id: str, turns: int = 2) -> None:
    """Make the story's summary chunk ``turns`` turns long (``lite_summary_chunk``)."""
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = settings || CAST(:p AS jsonb) WHERE id = :s",
            {"s": story_id, "p": json.dumps({"lite_summary_chunk": turns})},
        ),
    )


async def dump_all(engine: AsyncEngine) -> dict[str, set[str]]:
    """Every row of every ``public`` and ``catalog`` table, as text, per table."""
    snapshot: dict[str, set[str]] = {}
    async with engine.connect() as conn:
        tables = await conn.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN ('public', 'catalog') AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version' ORDER BY table_schema, table_name"
            )
        )
        for schema, table in tables.all():
            found = await conn.scalars(text(f'SELECT t::text FROM "{schema}"."{table}" t'))  # noqa: S608
            snapshot[f"{schema}.{table}"] = set(found)
    return snapshot


# -- the played story ----------------------------------------------------------------------------

NAME = "Linda"
ALIAS = "Lin"
OVERRIDE = "Mum"
OTHER = "Bobby"
TERMS = (NAME, ALIAS, OVERRIDE)
REPLIES = [
    f"{NAME} passes the salt to {OTHER}.",
    f"{ALIAS} laughs; {OTHER} grins.",
    "The rain keeps falling.",
    f"{OVERRIDE}'s patience is wearing thin.",
]
NARRATION = f"{ALIAS} and {OTHER} share a joke by the stove."
PIN = f"{OTHER} keeps bees; {ALIAS} cannot abide walnuts."
INSTRUCTIONS = f"Keep {NAME} gentle and {OTHER} loud."
SUMMARY = f"{NAME} and {OTHER} argued about the deed."
REGENERATED = f"{OTHER} and the eldest argued about the deed."
FIRST_KEY = "erasure-turn-1"


@dataclass(frozen=True)
class Played:
    """The played story: its id, the subject's character (``mother``), the other NPC
    (``neighbour``), the player, the catalog character and the turn replies' message ids."""

    story_id: str
    mother: str
    neighbour: str
    player: str
    catalog_id: str
    messages: list[str]


async def linda(client: httpx.AsyncClient, name: str = NAME, alias: str = ALIAS) -> str:
    """Catalog character ``name`` (alias ``alias``), published once: her id."""
    document = characters.complete(name, metadata={"aliases": [alias], "labels": ["warm"]})
    created = await client.post("/catalog/characters", json=document, headers=scenarios.WRITE)
    assert created.status_code == 201, created.text
    catalog_id: str = created.json()["id"]
    done = await client.post(
        f"/catalog/characters/{catalog_id}/publish",
        json={"draft_revision": 1},
        headers=scenarios.WRITE,
    )
    assert done.status_code == 201, done.text
    return catalog_id


async def played_story(client: httpx.AsyncClient, lib: Any, engine: AsyncEngine) -> Played:
    """A Lite story from a scenario whose ``mother`` is catalog character Linda ("Lin", and
    "Mum" from the role) and whose ``neighbour`` is Bobby (ad hoc), with Linda named in a seed,
    the player's persona, a pin, the custom instructions, the replies (the first under an
    ``Idempotency-Key``, so its stored result names her), an ingested narration, a rolling
    summary segment and a rating's text; Bobby is named beside her in most of them. The story's
    default viewpoint is Linda's."""
    stub = lib.models.stub
    catalog_id = await linda(client)
    scenario = g.scenario_body(
        g.role("mother", starting_location="kitchen", name_overrides={"aliases": [OVERRIDE]}),
        g.role("neighbour", starting_location="kitchen"),
        g.role("player", kind="player", label="The eldest"),
    )
    scenario["starting_state"]["seeded_memories"] = [
        scenarios.seed(f"{ALIAS} hid the orchard deed under the stairs.")
    ]
    scenario_id, _ = await g.published_scenario(client, scenario)
    made = await g.create_from(
        client,
        g.from_scenario(
            scenario_id,
            slots={
                "mother": g.catalog_slot(catalog_id),
                "neighbour": g.ad_hoc_slot(OTHER, 44, f"{OTHER}, the neighbour, keeps bees."),
            },
            players={
                "player": g.player(description=f"The eldest, {NAME}'s son, home for a weekend.")
            },
            ignore_warnings=True,
        ),
    )
    story = made["story_id"]
    cast = {c["role_id"]: c["id"] for c in made["characters"]}
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = settings || CAST(:p AS jsonb) WHERE id = :s",
            {
                "s": story,
                "p": json.dumps(
                    {
                        "lite_summary_chunk": 2,
                        "default_output_viewpoint": f"character:{cast['mother']}",
                    }
                ),
            },
        ),
    )
    await override(client, story, SUMMARY_OVERRIDES)
    for path, payload in (
        ("pinned-memories", {"items": [{"text": PIN}]}),
        ("custom-instructions", {"text": INSTRUCTIONS}),
    ):
        response = await client.put(f"/stories/{story}/{path}", json=payload, headers=g.WRITE)
        assert response.status_code == 200, response.text
    stub.script("generation", REPLIES)
    turns = []
    for n, key in enumerate((FIRST_KEY, "erasure-turn-2", "erasure-turn-3", "erasure-turn-4")):
        turns.append(
            await played(
                client,
                story,
                body(cast["player"], f"Pass the bread, please ({n})."),
                headers={"Idempotency-Key": key},
            )
        )
    ingested = await client.post(
        f"/stories/{story}/ingest",
        json={"events": [{"client_event_id": "evt-joke", "kind": "narration", "text": NARRATION}]},
        headers=g.WRITE,
    )
    assert ingested.status_code in (200, 201), ingested.text
    stub.script("lite_summary", [SUMMARY] * 6)
    await run_due_jobs(engine, lib.models, story)
    assert await segment_rows(engine, story), "the summary job wrote no segment"
    messages = [t["message"]["message_id"] for t in turns]
    rated = await rate(client, story, messages[0], {"rating": 4, "text": f"{ALIAS} shines."})
    assert rated.status_code == 201, rated.text
    stub.reset()
    return Played(story, cast["mother"], cast["neighbour"], cast["player"], catalog_id, messages)


__all__ = [
    "ALIAS",
    "NAME",
    "OTHER",
    "OVERRIDE",
    "PATH",
    "TERMS",
    "WRITE",
    "Played",
    "dump_all",
    "erase",
    "erasure_settings",
    "linda",
    "naming",
    "played_story",
    "request",
    "residue",
    "story_text_tables",
    "story_values",
    "summary_chunk",
]
