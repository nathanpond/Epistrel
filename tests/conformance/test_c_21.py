"""C-21: rating suggestion (FR-RATE-8, FR-MODEL-2) [M3; #121].

``POST /catalog/{characters|scenarios}/{id}/rating-suggestion`` answers an advisory table with
one row for every rating. Each slice runs on a database of its own, so the ratings are exactly
the four seeded ones plus the fixture's custom non-linear fifth (``Workplace``: it inherits
Explicit's guardrails and adds a word list about affairs, so it is neither stricter nor looser
than Teen), and a retired sixth where a slice needs one.

The fixture's policies: Teen lists a model-assisted ``sexual content`` policy and a word list
on ``makeup sex`` (Everyone inherits both, FR-RATE-2); Workplace lists the ``affair`` word list.
The stub model answers its default ``{"concerns": []}`` unless a slice scripts it per rating.

Every C-21 (M3) part lives in this file; the import-linter contract that keeps the suggestion
unread is checked, unmarked, in ``tests/gates/test_rating_suggestion_contract.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy import table as sql_table
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.core.context import RequestContext
from epistrel.core.ratings import RatingFields, create_rating, retire_rating
from epistrel.core.seeds.ratings import EVERYONE_ID, EXPLICIT_ID, MATURE_ID, TEEN_ID
from epistrel.core.tables.ownership import EntityType
from epistrel.engine import Engine
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.orchestrator.ratings import suggest as driver
from tests.catalog_support import scratch_catalog
from tests.character_support import complete as complete_character
from tests.media_support import files, metadata, png
from tests.scenario_support import WRITE, published_character, role, scenario, seed
from tests.validation_support import filled, list_policy

OPERATOR = {**WRITE, "X-Epistrel-Principal": "p-operator"}
PURPOSE = "rating_suggestion"
INSTRUCTION = (
    "Report only conflicts with the listed policies — never taste, audience fit or quality."
)
WORKPLACE = "Workplace"
WORKPLACE_DESCRIPTION = "Office drama for a work audience: no infidelity and no affairs."
SEXUAL_PROMPT = "Flag any sexual content, explicit or implied."
ROLE_SEED = "After the fight in the car park she and Daniel had makeup sex in his car."
REVEAL_TRUTH = "Mara has been having an affair with Daniel for a year."
REVEAL_CONDITIONS = "When the friend finds the receipt from the Harbor Hotel."
AFFAIR_MARKER = "Harbor Hotel"
"""Only the affair scenario's prompts hold it: the stub's scripts match on it and the rating."""
HUSBAND_PERSONALITY = "Tom is patient, proud and quietly jealous."
EVERYONE_CLAUSE = "No sexual content"
SUGGEST = "/catalog/{kind}/{id}/rating-suggestion"


@dataclass
class World:
    client: httpx.AsyncClient
    db: AsyncEngine
    lib: Engine
    stub: StubAdapter
    workplace: str
    sexual: str
    makeup: str
    affair: str


async def _policy(client: httpx.AsyncClient, rating_id: str, name: str, rule: Any) -> str:
    made = await client.post(
        "/admin/policies",
        json={
            "scope": "rating",
            "scope_id": rating_id,
            "name": name,
            "rule": rule,
            "message": f"{name} is not allowed at this rating.",
        },
        headers=OPERATOR,
    )
    assert made.status_code == 201, made.text
    policy_id: str = made.json()["id"]
    return policy_id


@asynccontextmanager
async def world(pg_url: str) -> AsyncIterator[World]:
    """A fresh deployment with the fixture's ratings and policies (see the module docstring)."""
    async with scratch_catalog(pg_url) as (settings, db):
        app = create_app(settings)
        lib: Engine = app.state.epistrel
        lib.bind(db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            workplace = (
                await create_rating(
                    db,
                    RatingFields(
                        name=WORKPLACE,
                        description=WORKPLACE_DESCRIPTION,
                        display_order=50,
                        inherits_from=[EXPLICIT_ID],
                    ),
                    principal="p-operator",
                )
            ).rating.id
            sexual = await _policy(
                client,
                TEEN_ID,
                "Sexual content",
                {"type": "model_assisted", "prompt": SEXUAL_PROMPT},
            )
            makeup = await _policy(
                client, TEEN_ID, "Makeup sex", {"type": "wordlist", "words": ["makeup sex"]}
            )
            affair = await _policy(
                client, workplace, "Affairs", {"type": "wordlist", "words": ["affair"]}
            )
            await list_policy(db, TEEN_ID, sexual)
            await list_policy(db, TEEN_ID, makeup)
            await list_policy(db, workplace, affair)
            stub = lib.models.stub
            stub.reset()
            yield World(client, db, lib, stub, workplace, sexual, makeup, affair)
        await lib.models.aclose()


async def _ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


async def suggest(
    w: World, kind: str, entry_id: str, body: dict[str, Any] | None = None, **params: Any
) -> httpx.Response:
    return await w.client.post(
        SUGGEST.format(kind=kind, id=entry_id),
        json=body,
        params={k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()},
        headers=WRITE,
    )


async def _published_scenario(w: World, body: dict[str, Any]) -> dict[str, Any]:
    created = await _ok(await w.client.post("/catalog/scenarios", json=body, headers=WRITE), 201)
    await _ok(
        await w.client.post(
            f"/catalog/scenarios/{created['id']}/publish",
            json={"draft_revision": created["draft_revision"]},
            headers=WRITE,
        ),
        201,
    )
    return await _ok(
        await w.client.get(
            f"/catalog/scenarios/{created['id']}", params={"projection": "full"}, headers=WRITE
        )
    )


async def affair_scenario(w: World) -> tuple[dict[str, Any], str]:
    """The affair-and-makeup-sex scenario, published (its ``full`` read), and its locked
    husband's catalog id."""
    husband = await published_character(
        w.client,
        "Tom Harrow",
        definition_changes={"personality": HUSBAND_PERSONALITY},
    )
    body = scenario(
        "The Anniversary",
        cast={
            "roles": [
                role(
                    "wife",
                    starting_location="kitchen",
                    seeded_memories=[seed(ROLE_SEED, holder="wife")],
                ),
                {
                    **filled("husband", "locked", husband),
                    "character": {"catalog_id": husband, "version": 1},
                },
                role("player", kind="player", label="The friend"),
            ]
        },
        drivers={
            "planned_reveals": [
                {
                    "truth": seed(REVEAL_TRUTH),
                    "allowed_roles": ["player"],
                    "conditions": REVEAL_CONDITIONS,
                }
            ]
        },
    )
    return await _published_scenario(w, body), husband


def _location(entry: str, version: Any, **where: Any) -> dict[str, Any]:
    return {
        "entry": entry,
        "version": version,
        "role": None,
        "field": None,
        "seed_id": None,
        "media_id": None,
        **where,
    }


def reveal_element(full: dict[str, Any]) -> dict[str, Any]:
    truth = full["drivers"]["planned_reveals"][0]["truth"]
    return {
        "kind": "reveal",
        "location": _location(
            full["id"], 1, field="drivers.planned_reveals[0].truth.text", seed_id=truth["id"]
        ),
    }


def seed_element(full: dict[str, Any]) -> dict[str, Any]:
    memory = full["cast"]["roles"][0]["seeded_memories"][0]
    return {
        "kind": "seed",
        "location": _location(
            full["id"],
            1,
            role="wife",
            field="cast.roles[0].seeded_memories[0].text",
            seed_id=memory["id"],
        ),
    }


def _compact(element: dict[str, Any]) -> dict[str, Any]:
    """An element as the model copies it from its prompt (unset location keys left out)."""
    location = {k: v for k, v in element["location"].items() if v is not None}
    return {"kind": element["kind"], "location": location}


def script(w: World, rating: str, *concerns: dict[str, Any]) -> None:
    """The stub answers the affair scenario's call for ``rating`` with ``concerns``."""
    w.stub.when(
        (f"## Rating: {rating}\n", AFFAIR_MARKER),
        json.dumps({"concerns": list(concerns)}),
        purpose=PURPOSE,
    )


async def table_counts(db: AsyncEngine) -> dict[str, int]:
    """Every table's row count, enumerated from ``information_schema`` (public and catalog)."""
    async with db.connect() as conn:
        names = (
            await conn.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE' AND table_schema IN ('public', 'catalog') "
                    "ORDER BY 1, 2"
                )
            )
        ).all()
        found: dict[str, int] = {}
        for schema, table in names:
            counted = select(func.count()).select_from(sql_table(table, schema=schema))
            found[f"{schema}.{table}"] = int(await conn.scalar(counted) or 0)
    return found


async def current_versions(db: AsyncEngine) -> dict[str, str]:
    async with db.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, current_version_id FROM content_ratings WHERE retired_at IS NULL")
            )
        ).all()
    return {str(r.id): str(r.current_version_id) for r in rows}


def rows_by_name(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["rating_name"]: row for row in table["rows"]}


def suggestion_calls(w: World) -> list[Any]:
    return [call for call in w.stub.calls if call.purpose == PURPOSE]


@pytest.mark.integration
@pytest.mark.part("M3", "FR-RATE-8; FR-MODEL-2")
async def test_c_21__affair_table(pg_url: str) -> None:
    """With the four seeded ratings plus the custom non-linear fifth, the affair scenario's table
    has exactly one row per rating, in display order. Everyone and Teen are not appropriate:
    the deterministic ``makeup sex`` hit names its policy and the role's seed, found with no model
    call; the scripted model concerns name the sexual-content policy at the planned reveal and,
    at Everyone, a clause of its description at the seed. Mature and Explicit are appropriate,
    and Workplace is not (its word list at the reveal). The table is advisory, records the
    rating versions it evaluated, calls the model once per rating with the verbatim instruction,
    and writes nothing (complement: a save moves the counts). A concern naming a policy the
    rating does not have is dropped and counted."""
    async with world(pg_url) as w:
        full, husband = await affair_scenario(w)
        reveal, memory = reveal_element(full), seed_element(full)
        script(
            w,
            "Everyone",
            {
                "policy_id": w.sexual,
                "element": _compact(reveal),
                "explanation": "The reveal is about a sexual affair.",
            },
            {
                "clause": EVERYONE_CLAUSE,
                "element": _compact(memory),
                "explanation": "The seed describes sex.",
            },
        )
        script(
            w,
            "Teen",
            {
                "policy_id": w.sexual,
                "element": _compact(reveal),
                "explanation": "The reveal is about a sexual affair.",
            },
            {
                "policy_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2ZZ",
                "element": _compact(reveal),
                "explanation": "A policy this rating does not have.",
            },
        )
        before = await table_counts(w.db)
        rating_before = full["metadata"]["content_rating_id"]

        table = await _ok(await suggest(w, "scenarios", full["id"]))

        assert table["advisory"] is True
        assert table["entry"] == {"type": "scenario", "id": full["id"], "version": 1}
        names = [row["rating_name"] for row in table["rows"]]
        assert names == ["Everyone", "Teen", "Mature", "Explicit", WORKPLACE]
        versions = await current_versions(w.db)
        assert table["evaluated"] == [
            {"rating_id": row["rating_id"], "rating_version": versions[row["rating_id"]]}
            for row in table["rows"]
        ]
        assert [row["rating_id"] for row in table["rows"]] == [
            EVERYONE_ID,
            TEEN_ID,
            MATURE_ID,
            EXPLICIT_ID,
            w.workplace,
        ]
        assert all(row["rating_version"] == versions[row["rating_id"]] for row in table["rows"])

        found = rows_by_name(table)
        makeup = {
            "policy_id": w.makeup,
            "clause": None,
            "element": memory,
            "explanation": 'Makeup sex: Makeup sex is not allowed at this rating. (matched "makeup '
            'sex")',
        }
        sexual = {
            "policy_id": w.sexual,
            "clause": None,
            "element": reveal,
            "explanation": "The reveal is about a sexual affair.",
        }
        clause = {
            "policy_id": None,
            "clause": EVERYONE_CLAUSE,
            "element": memory,
            "explanation": "The seed describes sex.",
        }
        everyone, teen = found["Everyone"], found["Teen"]
        assert (everyone["appropriate"], everyone["error"]) == (False, None)
        assert everyone["concerns"] == [makeup, sexual, clause]
        assert (teen["appropriate"], teen["concerns"], teen["dropped_concerns"]) == (
            False,
            [makeup, sexual],
            1,
        )
        for name in ("Mature", "Explicit"):
            assert (found[name]["appropriate"], found[name]["concerns"]) == (True, [])
        workplace = found[WORKPLACE]
        assert workplace["appropriate"] is False
        assert [(c["policy_id"], c["element"]) for c in workplace["concerns"]] == [
            (w.affair, reveal)
        ]
        assert all(row["truncated"] is False for row in table["rows"])
        assert table["unavailable"] == []

        # One rating_suggestion call per rating, each with the verbatim instruction; only the
        # Teen closure's prompts list the model-assisted policy; the locked character's
        # definition is among the elements.
        calls = suggestion_calls(w)
        assert len(calls) == 5
        assert [c["purpose"] for c in table["model_calls"]] == [PURPOSE] * 5
        systems = [next(m.content for m in call.messages if m.role == "system") for call in calls]
        assert all(INSTRUCTION in system for system in systems)
        with_policy = [s for s in systems if SEXUAL_PROMPT in s]
        assert sorted("## Rating: Everyone" in s for s in with_policy) == [False, True]
        users = [next(m.content for m in call.messages if m.role == "user") for call in calls]
        assert all(HUSBAND_PERSONALITY in user for user in users)
        assert all(f'"entry":"{husband}"' in user for user in users)

        # Read-only: every table's rows unchanged, the entry's rating too.
        assert await table_counts(w.db) == before
        again = await _ok(
            await w.client.get(
                f"/catalog/scenarios/{full['id']}", params={"projection": "full"}, headers=WRITE
            )
        )
        assert again["metadata"]["content_rating_id"] == rating_before
        # Complement: the count comparison sees a write.
        saved = await w.client.patch(
            f"/catalog/scenarios/{full['id']}",
            json={"draft_revision": 2, "drivers": {"plot_hooks": ["A knock."]}},
            headers=WRITE,
        )
        assert saved.status_code == 200, saved.text
        assert await table_counts(w.db) != before


@pytest.mark.integration
@pytest.mark.part("M3", "FR-RATE-8")
async def test_c_21__gentle_scenario(pg_url: str) -> None:
    """A gentle scenario is appropriate at every rating, Explicit included: no row has a
    concern, so nothing is ever "too tame"."""
    async with world(pg_url) as w:
        full = await _published_scenario(w, scenario("Sunday Dinner"))
        table = await _ok(await suggest(w, "scenarios", full["id"]))
        assert [(r["rating_name"], r["appropriate"], r["concerns"]) for r in table["rows"]] == [
            (name, True, []) for name in ("Everyone", "Teen", "Mature", "Explicit", WORKPLACE)
        ]
        assert len(suggestion_calls(w)) == 5


@pytest.mark.integration
@pytest.mark.part("M3", "FR-RATE-8")
async def test_c_21__deterministic_without_model(pg_url: str) -> None:
    """A deterministic hit is reported without a model: with every call failing, each row says
    why (``model_unavailable``; ``invalid_response`` for an unusable answer; ``deadline`` past
    the overall deadline), the table is still 200, and the deterministic concerns stand
    (``appropriate: false``), while rows with none are ``null``, never ``true``."""
    async with world(pg_url) as w:
        full, _ = await affair_scenario(w)
        w.stub.fault(PURPOSE, "http_500", n=5)
        table = await _ok(await suggest(w, "scenarios", full["id"]))
        found = rows_by_name(table)
        assert {name: row["error"] for name, row in found.items()} == dict.fromkeys(
            found, "model_unavailable"
        )
        assert [(name, found[name]["appropriate"]) for name in found] == [
            ("Everyone", False),
            ("Teen", False),
            ("Mature", None),
            ("Explicit", None),
            (WORKPLACE, False),
        ]
        assert [c["policy_id"] for c in found["Teen"]["concerns"]] == [w.makeup]

        w.stub.reset()
        w.stub.script(PURPOSE, ["not json at all"] * 5)
        broken = rows_by_name(await _ok(await suggest(w, "scenarios", full["id"])))
        assert {row["error"] for row in broken.values()} == {"invalid_response"}
        assert broken["Mature"]["appropriate"] is None

        w.stub.reset()
        latch = w.stub.hold(PURPOSE)
        original = driver.DEADLINE_S
        driver.DEADLINE_S = 0.5
        try:
            late = rows_by_name(await _ok(await suggest(w, "scenarios", full["id"])))
        finally:
            driver.DEADLINE_S = original
            latch.release()
        assert {row["error"] for row in late.values()} == {"deadline"}
        assert late["Workplace"]["appropriate"] is False


@pytest.mark.integration
@pytest.mark.part("M3", "FR-RATE-8")
async def test_c_21__characters(pg_url: str) -> None:
    """Characters get the same table: a row per rating, a definition field's deterministic hit
    at its field, and the image's caption reviewed as a ``media_caption``. The version is the
    draft when asked, else the latest published one; an unknown version is 404, and a
    ``projection`` is refused (422)."""
    async with world(pg_url) as w:
        image = await _ok(
            await w.client.post(
                "/media",
                files=files(png(), metadata(caption="An affair at the lighthouse.")),
                headers=WRITE,
            ),
            201,
        )
        body = complete_character(
            "Ada Quill",
            definition_changes={"personality": "Ada remembers the makeup sex after every row."},
            images={"primary": image["id"]},
        )
        created = await _ok(
            await w.client.post("/catalog/characters", json=body, headers=WRITE), 201
        )
        character = created["id"]

        drafted = await _ok(await suggest(w, "characters", character))
        assert drafted["entry"] == {"type": "character", "id": character, "version": "draft"}
        assert len(drafted["rows"]) == 5
        await _ok(
            await w.client.post(
                f"/catalog/characters/{character}/publish",
                json={"draft_revision": 1},
                headers=WRITE,
            ),
            201,
        )
        table = await _ok(await suggest(w, "characters", character))
        assert table["entry"]["version"] == 1
        found = rows_by_name(table)
        assert [(c["policy_id"], c["element"]) for c in found["Teen"]["concerns"]] == [
            (
                w.makeup,
                {
                    "kind": "field",
                    "location": _location(character, 1, field="definition.personality"),
                },
            )
        ]
        assert [(c["policy_id"], c["element"]) for c in found[WORKPLACE]["concerns"]] == [
            (
                w.affair,
                {
                    "kind": "media_caption",
                    "location": _location(character, 1, field="caption", media_id=image["id"]),
                },
            )
        ]
        assert (found["Explicit"]["appropriate"], found["Explicit"]["concerns"]) == (True, [])

        explicit = await _ok(await suggest(w, "characters", character, {"version": "draft"}))
        assert explicit["entry"]["version"] == "draft"
        missing = await suggest(w, "characters", character, {"version": 7})
        assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")
        projected = await suggest(w, "characters", character, projection="full")
        assert projected.status_code == 422, projected.text
        assert projected.json()["error"]["details"]["field"] == "query.projection"
        unknown = await suggest(w, "characters", "01J9Z8Q4N5V6W7X8Y9Z0A1B2ZZ")
        assert unknown.status_code == 404


@pytest.mark.integration
@pytest.mark.part("M3", "FR-RATE-8")
async def test_c_21__retired_and_unavailable(pg_url: str) -> None:
    """A retired rating has no row (``include_retired=true``: it has one, after the live ones'
    display order allows); a default role naming a character that does not exist is skipped and
    listed ``unavailable``; the library answers the table REST does."""
    async with world(pg_url) as w:
        retired = (
            await create_rating(
                w.db, RatingFields(name="Retired", display_order=60), principal="p-operator"
            )
        ).rating.id
        await retire_rating(w.db, retired, principal="p-operator")
        ghost = "01J9Z8Q4N5V6W7X8Y9Z0A1B2YY"
        body = scenario(
            "Draft only",
            cast={
                "roles": [
                    filled("guest", "default", ghost),
                    role("player", kind="player", label="The host"),
                ]
            },
        )
        created = await _ok(
            await w.client.post("/catalog/scenarios", json=body, headers=WRITE), 201
        )

        live = await _ok(await suggest(w, "scenarios", created["id"]))
        assert retired not in [row["rating_id"] for row in live["rows"]]
        assert len(live["rows"]) == 5
        assert live["entry"]["version"] == "draft"
        assert live["unavailable"] == [
            {"role": "guest", "entry": ghost, "version": None, "reason": "character_unavailable"}
        ]
        everything = await _ok(await suggest(w, "scenarios", created["id"], include_retired=True))
        assert [row["rating_id"] for row in everything["rows"]][-1] == retired
        assert [e["rating_id"] for e in everything["evaluated"]][-1] == retired

        library = await w.lib.suggest_ratings(RequestContext(), EntityType.SCENARIO, created["id"])
        assert [r.rating_id for r in library.rows] == [r["rating_id"] for r in live["rows"]]
        assert library.advisory is True


@pytest.mark.integration
@pytest.mark.part("M3", "FR-RATE-8; FR-MODEL-2")
async def test_c_21__model_and_documentation(pg_url: str) -> None:
    """``rating_suggestion`` is a delivered purpose whose model is its own row, else
    ``guardrail_check``'s, else ``default``'s, per field (``/admin/model-config`` says
    ``inherited``); the route is documented as advisory and author-facing, takes no
    ``Idempotency-Key``, and answers none."""
    async with world(pg_url) as w:
        put = await w.client.put(
            "/admin/model-config/guardrail_check", json={"model": "judge-small"}, headers=OPERATOR
        )
        assert put.status_code == 200, put.text
        shown = await _ok(
            await w.client.get("/admin/model-config/rating_suggestion", headers=WRITE)
        )
        assert (shown["model"], shown["sources"]["model"]) == ("judge-small", "inherited")
        assert shown["sources"]["base_url"] == "default"
        own = await w.client.put(
            "/admin/model-config/rating_suggestion", json={"model": "judge-own"}, headers=OPERATOR
        )
        assert own.status_code == 200, own.text
        shown = await _ok(
            await w.client.get("/admin/model-config/rating_suggestion", headers=WRITE)
        )
        assert (shown["model"], shown["sources"]["model"]) == ("judge-own", "own")
        assert (await w.lib.models.resolve(PURPOSE)).model == "judge-own"
        removed = await w.client.delete("/admin/model-config/rating_suggestion", headers=OPERATOR)
        assert removed.status_code == 200, removed.text
        assert (await w.lib.models.resolve(PURPOSE)).model == "judge-small"

        full = await _published_scenario(w, scenario("Sunday Dinner"))
        answered = await suggest(w, "scenarios", full["id"])
        assert answered.status_code == 200
        assert "Idempotency-Key" not in answered.headers
        assert [call.model for call in suggestion_calls(w)] == ["judge-small"] * 5

        document = await _ok(await w.client.get("/openapi.json"))
        for path, operation_id in (
            ("/catalog/scenarios/{scenario_id}/rating-suggestion", "suggest_ratings_scenario"),
            ("/catalog/characters/{character_id}/rating-suggestion", "suggest_ratings_character"),
        ):
            operation = document["paths"][path]["post"]
            assert operation["operationId"] == operation_id
            assert "**Advisory** (`advisory: true`)" in operation["description"]
            assert (
                "**Author-facing:** its concerns quote private fields" in operation["description"]
            )
            names = {p["name"] for p in operation.get("parameters", [])}
            assert "Idempotency-Key" not in names
            assert "projection" not in names
