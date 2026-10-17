"""U-28 (M4) and C-16 (M4) (docs/04): the dependency rules M4 puts on the delivered path, the
capability map, and the interim rule for a data-producing increase (#129).

C-16 (M4) has exactly this file (traceability allows one file per test ID and milestone): #129's
parts here, and #132's ``test_c_16__guarantee_table`` when it lands.

- ``test_u_28__m4_dependencies``: each dependency rule now on the delivered path, violated by a
  fixture that is refused naming what it lacks, and accepted with it present.
- ``test_c_16__m4_interim``: switching one module on for a profile's stories cannot reach the
  one with history until M7, which keeps its version; a behavioral edit of the gate threshold
  applies at the next turn boundary.
- ``test_c_16__capabilities``: the map as FR-PROF-13 writes it and as docs/capability-map.md
  renders it; the seeded ``Structured (M4)`` profile; the capabilities and guarantees reports
  read from the map.

The C-16 parts change profiles and stories, so they run on a scratch database of their own.
Module names come from the schema's registry and the map (``tests/profile_support.py``): a part
may not name a later milestone's capability.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import (
    CAPABILITY,
    CAPABILITY_MAP,
    capability_names,
    holding,
    level_at_least,
    module_on,
)
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.estimates import CALL_LATENCY_MS
from epistrel.core.profiles.fingerprint import GUARANTEES, guarantees
from epistrel.core.profiles.validate import (
    RequiredModule,
    missing_dependencies,
    parse_settings,
    validate_profile,
)
from epistrel.core.seeds.profiles import (
    LITE_PRESET_ID,
    STRUCTURED_M4_NAME,
    lite_settings,
    profile_defaults,
    structured_m4_settings,
)
from epistrel.testing.profiles import structured_m4
from epistrel.tools.capability_map import MAP_PATH, render
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.event_support import rows, scalar
from tests.profile_support import (
    PROFILES,
    REDERIVATION,
    STRUCTURED_DOCUMENT,
    UNAVAILABLE_WARNINGS,
    configuration_rows,
    delivered_rules,
    map_units,
    merged,
    nest,
    prof_13_units,
    profile_body,
    violating,
)
from tests.profile_support import holding as holding_doc
from tests.respond_support import body as respond_body
from tests.respond_support import respond_client
from tests.story_support import story_body

A = "p-capability-map"
READ = bearer()
CONSOLIDATION = "memory.consolidation.enabled"
GATE = "verification.entailment_gate"
JOBS = "SELECT count(*) FROM jobs"


def headers(principal: str = A) -> dict[str, str]:
    return {**bearer(), "X-Epistrel-Principal": principal}


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json() if response.content else {}
    return found


@dataclass
class Space:
    settings: Settings
    db: AsyncEngine


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
async def scratch(pg_url: str) -> AsyncIterator[Space]:
    """A database of the test's own, migrated to head."""
    with migrated_database(pg_url, f"epistrel_capmap_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            yield Space(settings, db)
        finally:
            await db.dispose()


async def _story(client: httpx.AsyncClient, profile: str | None = None) -> tuple[str, str]:
    """A new two-character story (assigned ``profile``, confirmed, when named): its id and its
    player character's id."""
    created = await ok(
        await client.post("/stories", json=story_body("Wren", "Ada"), headers=headers()), 201
    )
    story = created["story_id"]
    if profile is not None:
        assigned = await ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": profile, "ignore_warnings": True},
                headers=headers(),
            )
        )
        assert assigned["applied"] is True, assigned
    return story, created["characters"][0]["id"]


async def _effective(client: httpx.AsyncClient, story_id: str) -> dict[str, Any]:
    return await ok(await client.get(f"/stories/{story_id}/effective-config", headers=READ))


async def _play(client: httpx.AsyncClient, story_id: str, actor: str) -> dict[str, Any]:
    return await ok(
        await client.post(
            f"/stories/{story_id}/respond", json=respond_body(actor), headers=headers()
        )
    )


async def _profile(client: httpx.AsyncClient, settings: dict[str, Any]) -> str:
    created = await ok(
        await client.post(
            PROFILES, json=profile_body("Structured trial", settings=settings), headers=headers()
        ),
        201,
    )
    return str(created["id"])


# -- U-28 (M4) -----------------------------------------------------------------------------------


@pytest.mark.part("M4", "FR-PROF-2,4")
async def test_u_28__m4_dependencies(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Each declared dependency whose modules M4 delivers is exercised by a fixture that
    violates it (every other dependency of its module met; under ``structured`` when the rule is
    not the pipeline's, so nothing else is missing): validation refuses it with that missing
    dependency named, and the dry run over REST is 422 ``invalid_profile`` naming it. The same
    fixture with the dependency present validates (complement). The three docs/04 names:
    consolidation without episodic memory; contradiction checking with verification off;
    verification on a ``lite`` profile, which names the ``structured`` pipeline."""
    rules = delivered_rules()
    named = {(rule.module, tuple(r.module for r in rule.requires)) for rule in rules}
    assert {
        ("consolidation", ("episodic",)),
        ("contradiction_checking", ("verification",)),
        ("verification", ("pipeline",)),
    } <= named
    assert len(rules) == 8
    async with respond_client(stub_settings, engine) as (client, _):
        for rule in rules:
            expected = [
                RequiredModule(module=r.module, levels=list(r.levels)) for r in rule.requires
            ]
            needs_pipeline = any(r.module == "pipeline" for r in rule.requires)
            others = [
                holding_doc(r)
                for other in rules
                if other.module == rule.module and other is not rule
                for r in other.requires
            ]
            fixture = merged(
                {} if needs_pipeline else STRUCTURED_DOCUMENT, *others, violating(rule)
            )
            found = missing_dependencies(parse_settings(fixture))
            assert [(f.module, f.requires) for f in found if f.module == rule.module] == [
                (rule.module, expected)
            ], rule.describe()
            if not needs_pipeline:  # under `structured`, nothing else is missing
                assert len(found) == 1, rule.describe()
            assert not validate_profile(fixture).valid
            dry = await client.post(
                f"{PROFILES}/{structured_m4()}/validate",
                json={"settings": fixture},
                headers=headers(),
            )
            assert dry.status_code == 422, dry.text
            error = dry.json()["error"]
            assert error["code"] == "invalid_profile"
            mine = [
                d for d in error["details"]["missing_dependencies"] if d["module"] == rule.module
            ]
            assert [d["requires"] for d in mine] == [[r.model_dump() for r in expected]]
            present = merged(fixture, *(holding_doc(r) for r in rule.requires))
            assert validate_profile(present).valid, rule.describe()
            checked = await ok(
                await client.post(
                    f"{PROFILES}/{structured_m4()}/validate",
                    json={"settings": present},
                    headers=headers(),
                )
            )
            assert checked["valid"] is True


# -- C-16 (M4) -----------------------------------------------------------------------------------


@pytest.mark.part("M4", "FR-PROF-7", interim_until="M7")
async def test_c_16__m4_interim(scratch: Space) -> None:
    """Until M7 nothing re-derives a story's data from its transcript. Two stories follow a
    structured profile with episodic memory and verification at ``entailment``; one has played
    a turn. Switching consolidation on, without ``ignore_warnings``, changes nothing and
    reports, by story id: the story with history ``upgrade_unavailable``, its re-derivation
    needed with an estimate over its transcript, keeping its fingerprint and its capabilities
    (no consolidation); the story without turns gaining consolidation with nothing to re-derive
    (complement). With ``ignore_warnings`` the edit applies to the second story, which runs on
    version 2 with consolidation, while the first keeps version 1, pinned, and its next turn
    runs on it.

    A behavioral edit of the gate threshold (version 3) then applies at once with no warning:
    one configuration change for the version and one, prospective, for
    ``verification.entailment_gate``; no job is queued. At the following story's next turn
    boundary it runs on version 3 with the new threshold; the pinned story is not affected."""
    base = merged(
        STRUCTURED_DOCUMENT,
        nest("memory.episodic.enabled", True),
        nest("verification.level", "entailment"),
        nest(GATE, 0.5),
    )
    grown = merged(base, nest(CONSOLIDATION, True))
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile = await _profile(client, base)
        played, actor = await _story(client, profile)
        fresh, fresh_actor = await _story(client, profile)
        first = await _play(client, played, actor)
        assert first["profile"] == {"id": profile, "version": 1}
        before = {s: (await _effective(client, s))["fingerprint"] for s in (played, fresh)}
        sizes = await rows(
            scratch.db,
            "SELECT count(*), sum(length(text)) FROM transcript_messages "
            "WHERE story_id = :s AND membership = 'active'",
            s=played,
        )
        (messages, characters) = tuple(sizes[0])
        edit = {"name": "Structured trial", "settings": grown}

        unchanged = await configuration_rows(scratch.db)
        held = await ok(await client.put(f"{PROFILES}/{profile}", json=edit, headers=headers()))
        assert held["applied"] is False
        assert await configuration_rows(scratch.db) == unchanged
        report = held["impact"]
        assert report["warnings"] == UNAVAILABLE_WARNINGS
        entries = {e["story_id"]: e for e in report["affected_stories"]}
        assert list(entries) == sorted([played, fresh])
        kept, moved = entries[played], entries[fresh]
        assert (kept["upgrade_unavailable"], kept["capabilities_gained"]) == (
            True,
            ["consolidation"],
        )
        assert kept[REDERIVATION] == {
            "needed": True,
            "estimate": {
                "model_calls": -(-messages // 30),
                "tokens": characters // 4,
                "duration_s": -(-messages // 30) * CALL_LATENCY_MS / 1000,
            },
        }
        assert kept["fingerprint_before"] == kept["fingerprint_after"] == before[played]
        assert "consolidation" not in kept["capabilities_after"]
        assert (moved["upgrade_unavailable"], moved["capabilities_gained"]) == (
            False,
            ["consolidation"],
        )
        assert moved[REDERIVATION] == {"needed": False, "estimate": None}
        assert moved["fingerprint_before"] == before[fresh] != moved["fingerprint_after"]
        assert "consolidation" in moved["capabilities_after"]

        confirmed = await ok(
            await client.put(
                f"{PROFILES}/{profile}", json={**edit, "ignore_warnings": True}, headers=headers()
            )
        )
        assert (confirmed["applied"], confirmed["result"]["version"]) == (True, 2)
        stays, goes = await _effective(client, played), await _effective(client, fresh)
        assert (stays["profile"]["version"], stays["profile"]["pinned"]) == (1, True)
        assert stays["capabilities"] == kept["capabilities_after"]
        assert stays["fingerprint"] == before[played]
        assert (goes["profile"]["version"], goes["profile"]["pinned"]) == (2, False)
        assert goes["capabilities"] == moved["capabilities_after"]
        assert goes["effective"]["memory"]["consolidation"]["enabled"] is True
        again = await _play(client, played, actor)
        assert (again["profile"], again["fingerprint"]) == (
            {"id": profile, "version": 1},
            before[played],
        )

        # -- a behavioral edit of the gate threshold: at once, no warning, next turn boundary
        revision = int(await scalar(scratch.db, "SELECT config_revision FROM deployment_state"))
        jobs = await scalar(scratch.db, JOBS)
        tightened = {"name": "Structured trial", "settings": merged(grown, nest(GATE, 0.8))}
        edited = await ok(
            await client.put(f"{PROFILES}/{profile}", json=tightened, headers=headers())
        )
        assert (edited["applied"], edited["result"]["version"]) == (True, 3)
        assert edited["impact"]["warnings"] == []
        (entry,) = edited["impact"]["affected_stories"]
        assert entry["story_id"] == fresh
        assert (entry["capabilities_gained"], entry["capabilities_lost"]) == ([], [])
        assert entry[REDERIVATION] == {"needed": False, "estimate": None}
        changes = await rows(
            scratch.db,
            'SELECT setting, old, new, "class" FROM config_changes WHERE scope = '
            "'deployment' AND config_revision = :r ORDER BY setting",
            r=revision + 1,
        )
        assert [tuple(c) for c in changes] == [
            (f"engine_profile:{profile}", 2, 3, "prospective"),
            (f"engine_profile:{profile}.{GATE}", 0.5, 0.8, "prospective"),
        ]
        assert await scalar(scratch.db, JOBS) == jobs
        assert (await _effective(client, fresh))["effective"]["verification"][
            "entailment_gate"
        ] == 0.8
        later = await _play(client, fresh, fresh_actor)
        assert later["profile"] == {"id": profile, "version": 3}
        async with scratch.db.connect() as conn:
            resolved = await effective_configuration(conn, fresh)
        assert resolved.settings.verification.entailment_gate == 0.8
        assert (await _effective(client, played))["profile"]["version"] == 1


@pytest.mark.part("M4", "FR-PROF-12,13")
async def test_c_16__capabilities(scratch: Space) -> None:
    """The capability map holds FR-PROF-13's areas exactly as written: the requirement groups
    of every entry are the ones the requirement names (less the pointers it makes to the
    floor), and docs/capability-map.md is what the map renders.

    The ``Structured (M4)`` profile is seeded: owned by ``system``, not a preset, at version 1
    with its shipped settings, and valid; the deployment's defaults name every delivered
    setting. A story assigned it resolves to it: its effective configuration and every respond
    response report the profile and version, the capabilities (each module by name, a ladder
    module by its level), the quality status and the degradation steps.

    Guarantees follow the map: each FR-VERIFY-15 guarantee holds exactly where its map entry
    does. Under Lite none holds, so its table lists every one as not provided, among them
    structural knowledge separation, verification and commitment; the structured profile
    provides verification and commitment only. Previewing Lite for the structured story reports
    those two as ``guarantees_lost``, with the capabilities it loses by module name."""
    assert map_units() == prof_13_units()
    assert render() == MAP_PATH.read_text(encoding="utf-8")

    lite = parse_settings(lite_settings())
    structured = parse_settings(structured_m4_settings())
    assert level_at_least(structured, "pipeline", "structured")
    assert all(module_on(structured, name) for name in ("consolidation", "commitment"))
    for guarantee in GUARANTEES:
        entry = CAPABILITY[guarantee.entry]
        assert (guarantee.name in guarantees(lite)) is entry.holds(lite) is False
        assert (guarantee.name in guarantees(structured)) is entry.holds(structured)
    not_provided = [g.name for g in GUARANTEES if g.name not in guarantees(lite)]
    assert not_provided == [g.name for g in GUARANTEES]
    assert {"structural knowledge separation", "verification", "commitment"} <= set(not_provided)
    assert guarantees(structured) == ("verification", "commitment")
    assert "floor" in holding(lite)
    assert len(CAPABILITY_MAP) == len(set(CAPABILITY))

    (stored,) = await rows(
        scratch.db,
        "SELECT p.name, p.owner_principal, p.preset, p.current_version, v.settings, "
        "d.default_engine_profile_id, d.profile_defaults FROM engine_profiles p "
        "JOIN engine_profile_versions v ON v.profile_id = p.id AND v.version = 1 "
        "CROSS JOIN deployment_state d WHERE p.id = :p",
        p=structured_m4(),
    )
    assert tuple(stored)[:6] == (
        STRUCTURED_M4_NAME,
        "system",
        False,
        1,
        structured_m4_settings(),
        LITE_PRESET_ID,
    )
    assert validate_profile(stored.settings).valid
    assert stored.profile_defaults == profile_defaults()
    leaves = {path for path, _ in schema.walk(stored.profile_defaults)}
    assert set(schema.delivered_tags()) <= leaves

    expected = list(capability_names(structured))
    assert "verification:entailment" in expected
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        story, actor = await _story(client, structured_m4())
        found = await _effective(client, story)
        assert found["profile"] == {
            "id": structured_m4(),
            "version": 1,
            "pinned": False,
            "deleted": False,
        }
        assert found["capabilities"] == expected
        assert found["quality_status"] == {"status": "uncertified"}
        played = await _play(client, story, actor)
        assert played["profile"] == {"id": structured_m4(), "version": 1}
        assert played["capabilities"] == expected
        assert played["quality_status"] == {"status": "uncertified"}
        assert played["fingerprint"] == found["fingerprint"]
        assert isinstance(played["degradation"], list)

        preview = await ok(
            await client.post(
                f"/stories/{story}/impact-preview",
                json={"profile_id": LITE_PRESET_ID},
                headers=headers(),
            )
        )
        assert preview["guarantees_lost"] == ["verification", "commitment"]
        assert "guarantees_lost" in preview["warnings"]
        (entry,) = preview["affected_stories"]
        assert {"structured", "verification", "commitment"} <= set(entry["capabilities_lost"])
        assert not any(":" in name for name in entry["capabilities_lost"])
        assert entry["capabilities_gained"] == ["lite", "rolling_summary"]


def test_capability_map_units_are_parsed() -> None:
    """The FR-PROF-13 reader the C-16 part compares with finds families, single ids, lists and
    ranges, and leaves out the floor sentence."""
    units = prof_13_units()
    assert {"FR-TURN", "FR-MEM-8", "FR-CONC-12", "FR-COMMIT-14", "NFR-PRIV-4"} <= units
    assert "FR-AUTH" not in units  # the floor sentence
    assert re.fullmatch(r"(?:FR|NFR)-[A-Z]+(?:-\d+)?", min(units)) is not None
