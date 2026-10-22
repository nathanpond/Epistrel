"""I-27: genesis by capability (docs/04 §4), its M3 parts.

Every I-27 (M3) part lives in this file (traceability counts a test ID per milestone in one
file): #119's ``test_i_27__atomic`` (FR-SCN-9), and #122's ``test_i_27__lite``, the
materialisation by field class (the Lite prompt, the deferred and inactive report; its
capability names stay in ``tests/lite_genesis_support.py``). The scenario is
``tests/scenario_support.COMPLETE``, which fills every group; its later milestones' field names
stay in that fixture.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import store
from tests import erasure_support as e
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import media_support as media
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings
from tests.respond_support import body as body_of
from tests.respond_support import (
    everything_sent,
    generation_calls,
    respond,
    respond_client,
    system_of,
)
from tests.story_support import rest

pytestmark = pytest.mark.integration


async def _one(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


@pytest.mark.part("M3", "FR-SCN-9")
async def test_i_27__atomic(settings: Settings, engine: AsyncEngine) -> None:
    """A story created from a scenario that fills every group: the creation commits as one
    genesis operation; the story's snapshot of the scenario holds every field of the published
    version, exactly; turn 1 is the opening narration (an authored narration beat, no
    character, naming the characters of the roles the opening has present); truncating to 0
    leaves the genesis state, the cast and the snapshots, with turn 1 discarded. The profile it
    would run under leaves a recommended capability unmet, so the request without
    ``ignore_warnings`` creates nothing and answers the reports.

    Complements asserted: the held request wrote no row; the confirmed one did."""
    async with rest(settings, engine) as client:
        locked, default = await scenarios.cast_characters(client)
        warm = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        body = scenarios.complete_with(locked, default)
        scenario_id, version = await g.published_scenario(client, body)
        request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(warm)})

        counted = await g.counts(engine)
        held = await g.create_from(client, request, status=200)
        assert held["applied"] is False
        assert held["story_id"] is None
        assert held["impact"]["recommendations_unmet"]
        assert await g.counts(engine) == counted

        made = await g.create_from(client, {**request, "ignore_warnings": True})
        story_id = made["story_id"]
        assert made["applied"] is True
        assert made["timeline_revision"] == 2
        moved = await g.counts(engine)
        assert moved["stories"] == counted["stories"] + 1
        assert moved["characters"] == counted["characters"] + 4
        assert moved["character_snapshots"] == counted["character_snapshots"] + 3

        snapshot = await _one(
            engine, "SELECT scenario_snapshot FROM stories WHERE id = :s", s=story_id
        )
        published = await _one(
            engine,
            "SELECT content FROM catalog.scenario_versions WHERE catalog_id = :c AND version = :v",
            c=scenario_id,
            v=version,
        )
        assert snapshot == published
        assert isinstance(snapshot, dict)
        assert set(scenarios.GROUPS) <= set(snapshot)
        held_text = json.dumps(snapshot)
        for phrase in scenarios.PRIVATE_PHRASES:
            assert phrase in held_text, phrase
        assert (
            await _one(
                engine,
                "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'genesis'",
                s=story_id,
            )
            == 1
        )

        async with engine.connect() as conn:
            opening = (
                await conn.execute(
                    text(
                        "SELECT e.type, e.payload->>'text' AS said, e.character_id, "
                        "e.provenance, o.kind, o.narrative_turn FROM events e "
                        "JOIN operations o ON o.operation_id = e.operation_id "
                        "WHERE e.story_id = :s AND o.narrative_turn = 1"
                    ),
                    {"s": story_id},
                )
            ).all()
            row = (
                await conn.execute(
                    text(
                        "SELECT role, channel, text FROM transcript_messages "
                        "WHERE story_id = :s AND narrative_turn = 1"
                    ),
                    {"s": story_id},
                )
            ).one()
        assert [(r.type, r.said, r.character_id, r.provenance, r.kind) for r in opening] == [
            (
                "message_rendered",
                scenarios.COMPLETE["starting_state"]["opening"]["narration"],
                None,
                "system",
                "turn",
            )
        ]
        assert (row.role, row.channel) == ("narration", "narration")
        present = g.opening_cast(body, made)
        assert len(present) == 2
        assert await g.opening_present(engine, story_id) == present

        def genesis_state() -> str:
            return (
                "SELECT (SELECT json_agg(c ORDER BY c.ordinal)::text FROM characters c "
                "WHERE c.story_id = :s) || (SELECT json_agg(t ORDER BY t.character_id)::text "
                "FROM character_snapshots t WHERE t.story_id = :s) || "
                "(SELECT scenario_snapshot::text FROM stories WHERE id = :s)"
            )

        kept = await _one(engine, genesis_state(), s=story_id)
        cut = await client.post(
            f"/stories/{story_id}/truncate", json={"to_turn": 0}, headers=g.WRITE
        )
        assert cut.status_code == 200, cut.text
        assert await _one(engine, genesis_state(), s=story_id) == kept
        assert (
            await _one(
                engine,
                "SELECT membership FROM operations WHERE story_id = :s AND narrative_turn = 1",
                s=story_id,
            )
            == "discarded"
        )
        assert (
            await _one(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id) == 1
        )


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M3", "FR-SCN-2,5,9,11")
async def test_i_27__lite(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The Sunday Dinner scenario (every group filled: director guidance, hooks and objectives,
    story settings, a recommended profile, a visual style; a phrase planted in its public
    description, a label and its style) creates a Lite story in one genesis. Under ``full``
    the creation report lists every item the story cannot use as deferred, each with its
    location and the capability it needs (each knowledge seed's structured form among them),
    and never a public-card, director, recommendation or style field; the same items are the
    story's ``genesis_deferred`` rows, unmaterialised, with the scenario's original JSON. Each
    story setting for a capability the story lacks is stored in its settings and listed
    inactive; the settings Lite uses are applied. Under ``card`` the report carries the story,
    its cast as cards and only counts by class and capability with the checks that could not
    run, quoting no private field. A turn's captured prompt carries the seeds as scenario text
    and the director guidance as separately labelled author notes, in that order around the
    characters' notes; the genesis operation keeps the full report for an omniscient read.

    Complements asserted: the planted public-card, label and style phrases are in no captured
    prompt; the creation adds rows to exactly the tables genesis writes (no empty or
    placeholder record for any deferred field); a read of the genesis operation naming no
    viewpoint gets no report."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        body = lg.sunday_dinner(locked, default)
        scenario_id, version = await g.published_scenario(client, body)
        request = g.from_scenario(
            scenario_id,
            slots={"mother": g.catalog_slot(mother)},
            ignore_warnings=True,
            projection="full",
        )

        before = await lg.story_table_counts(engine)
        made = await g.create_from(client, request)
        story_id = made["story_id"]
        report = made["report"]
        added = lg.grown(before, await lg.story_table_counts(engine))
        expected = lg.expected_deferred(body)
        # The subject-reference index (#123) records what genesis wrote; its rows are the
        # index's, not a record of the story (tests/integration/test_subject_index.py).
        assert added.pop("subject_references", 0) > 0
        assert added == {
            "stories": 1,
            "characters": 4,
            "character_snapshots": 3,
            "genesis_deferred": len(expected),
            "operations": 2,
            "events": 6,
            "transcript_messages": 1,
            "story_heads": 1,
            "story_texts": 1,
            "policies": 1,
            # #139: every character is an entity in every profile (its id the character's),
            # with its name as an alias; the role overrides add "Mum" and "Tommy".
            "entities": 4,
            "entity_aliases": 6,
        }
        async with engine.connect() as conn:
            entity_rows = (
                await conn.execute(
                    text(
                        "SELECT e.id, e.character_id, a.alias, a.source FROM entities e "
                        "JOIN entity_aliases a ON a.entity_id = e.id WHERE e.story_id = :s"
                    ),
                    {"s": story_id},
                )
            ).all()
        assert {str(r.id) for r in entity_rows} == {c["id"] for c in made["characters"]}
        assert all(str(r.id) == str(r.character_id) for r in entity_rows)
        assert sorted(r.alias for r in entity_rows if r.source != "canonical") == [
            "Mum",
            "Tommy",
        ]

        # The full report: every deferred item with its location and capability.
        assert report["story_id"] == story_id
        assert lg.deferred_pairs(report) == expected
        for item in report["deferred"]:
            assert item["location"]["entry"] == scenario_id
            assert item["location"]["version"] == version
            assert item["location"]["field"] == item["field_ref"]
            assert not item["field_ref"].startswith(lg.NEVER_DEFERRED), item
            assert item["field_class"] in ("knowledge_seed", "story_rule", "starting_state")
        seeds = [d for d in report["deferred"] if ".seeded_memories[" in d["field_ref"]]
        assert len(seeds) == len(body["starting_state"]["seeded_memories"]) + sum(
            len(r.get("seeded_memories", [])) for r in body["cast"]["roles"]
        )
        assert {s["payload"]["value"]["text"] for s in seeds} >= {
            f"Last winter {scenarios.ROLE_SECRET}."
        }
        counts = report["counts"]
        assert counts["conflicts"] == 0
        assert counts["deferred"]["by_capability"] == lg.counted(expected)
        assert sum(counts["deferred"]["by_class"].values()) == len(expected)
        rows = await lg.deferred_rows(engine, story_id)
        assert [{k: r[k] for k in lg.report_items(report)[0]} for r in rows] == lg.report_items(
            report
        )
        assert {r["materialized_at"] for r in rows} == {None}

        # Inactive settings: stored and listed; the used ones applied.
        filled = [c["role_id"] for c in made["characters"]]
        inactive = lg.expected_inactive(body, filled)
        assert lg.inactive_of(report) == inactive
        assert counts["inactive"]["by_class"] == {"configuration": len(inactive)}
        assert counts["inactive"]["by_capability"] == lg.counted(
            [(name, item["capability"]) for name, item in inactive.items()]
        )
        story = (await client.get(f"/stories/{story_id}", headers=g.READ)).json()
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        assert story["settings"]["inactive"] == inactive
        assert story["settings"]["turn_concurrency"] == "queue"
        assert story["settings"]["audit_retention_days"] == 30
        assert story["settings"]["default_output_viewpoint"] == f"character:{by_role['mother']}"
        assert not set(lg.UNUSED_SETTINGS) & set(story["settings"])

        # The card report: counts only, the cast as cards, no private field.
        card = await g.create_from(client, {**request, "projection": "card"})
        shown = card["report"]
        assert (shown["errors"], shown["warnings"], shown["deferred"], shown["inactive"]) == (
            None,
            None,
            None,
            None,
        )
        assert shown["counts"] == {**counts}
        assert shown["unavailable"] == list(scenarios.M3_UNAVAILABLE)
        assert [c["kind"] for c in shown["cast"]] == ["catalog", "catalog", "catalog", "player"]
        assert [c["role_id"] for c in shown["cast"]] == ["mother", "brother", "aunt", "player"]
        assert shown["cast"][1]["source"] == {
            "catalog_id": locked,
            "version": scenarios.LOCKED_VERSIONS,
        }
        quoted = json.dumps(shown)
        for phrase in (*scenarios.PRIVATE_PHRASES, "Sam, back for", "only key to the old barn"):
            assert phrase not in quoted, phrase

        # The prompt: scenario text, the characters' notes, then the director notes.
        stub.reset()
        player = by_role["player"]
        played_turn = await respond(client, story_id, body_of(player, "Is dinner ready?"))
        assert played_turn.status_code == 200, played_turn.text
        (call,) = generation_calls(stub)
        system = system_of(call)
        scenario_block = lg.section(system, lg.SCENARIO_HEADING, "### Author notes:")
        director_block = lg.section(system, lg.DIRECTOR_HEADING, "## Your part")
        assert scenarios.WORLD_SECRET in scenario_block
        assert scenarios.ROLE_SECRET in scenario_block
        assert "The farm has been in the family for four generations." in scenario_block
        assert scenario_block.rstrip().endswith(lg.PREVAIL)
        assert scenarios.GUIDANCE_SECRET in director_block
        assert scenarios.GUIDANCE_SECRET not in scenario_block
        assert "The brother forged the second sale." in director_block
        assert "The brother forged the second sale." not in scenario_block
        assert "A stranger asks for the deed." in director_block
        notes = system.index("### Author notes: June Harrow")
        assert system.index(lg.SCENARIO_HEADING) < notes < system.index(lg.DIRECTOR_HEADING)
        sent = everything_sent(stub)
        for phrase in lg.PUBLIC_PHRASES:
            assert phrase not in sent, phrase
        # Lite stays untouched (#139): the prompt never reads the entity stores. A sentinel
        # alias and descriptor recorded on the mother's entity reach no captured prompt
        # (the role's "also called" aliases are scenario text Lite renders on its own).
        stub.reset()
        async with engine.begin() as conn:
            await store.add_aliases(conn, story_id, by_role["mother"], [ENTITY_SENTINEL])
            await conn.execute(
                text("UPDATE entities SET descriptors = CAST(:d AS jsonb) WHERE id = :e"),
                {"d": json.dumps([DESCRIPTOR_SENTINEL]), "e": by_role["mother"]},
            )
        again = await respond(client, story_id, body_of(player, "Where is everyone?"))
        assert again.status_code == 200, again.text
        sent = everything_sent(stub)
        assert sent
        for sentinel in (ENTITY_SENTINEL, DESCRIPTOR_SENTINEL):
            assert sentinel not in sent, sentinel

        # The genesis operation keeps the full report, for an omniscient read only.
        async with engine.connect() as conn:
            genesis_op = await conn.scalar(
                text(
                    "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'genesis'"
                ),
                {"s": story_id},
            )
        omniscient = await client.get(
            f"/operations/{genesis_op}", params={"viewpoint": "omniscient"}, headers=g.READ
        )
        assert omniscient.status_code == 200, omniscient.text
        assert omniscient.json()["creation_report"] == report
        plain = await client.get(f"/operations/{genesis_op}", headers=g.READ)
        assert plain.json()["creation_report"] is None


ENTITY_SENTINEL = "Zq-entity-alias-heron"
DESCRIPTOR_SENTINEL = "zqdescriptorwren"
CARD_LABEL = "zq-label-kestrel"
CARD_PHRASE = "Zq-card-phrase: famous for her marmalade"
"""Planted on the cast catalog character's public card (#339): a label and a public
description phrase, distinct from every definition field."""
STORY_KNOWLEDGE = ("characters", "events", "transcript_messages", "story_texts")
"""The story's own knowledge records. The genesis snapshot and the creation report keep the
catalog entry as provenance (``character_snapshots``, ``operations``); see #340."""


async def test_a_catalog_characters_card_never_reaches_the_story(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A catalog character cast in a story plays from her definition only (#339): the label and
    the public-description phrase on her card reach no captured prompt and none of the story's
    knowledge records, while her definition does reach the prompt.

    Complements asserted: the planted label and phrase are absent from everything the model
    was sent and from every row of the story's knowledge tables; the positive control (her
    name and definition in the prompt) shows the capture saw her."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(
            client,
            "June Harrow",
            age=60,
            labels=["warm", CARD_LABEL],
            public_description=f"{CARD_PHRASE}, at every county fair.",
        )
        body = lg.sunday_dinner(locked, default)
        scenario_id, _ = await g.published_scenario(client, body)
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id, slots={"mother": g.catalog_slot(mother)}, ignore_warnings=True
            ),
        )
        story_id = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}

        stub.reset()
        for line in ("Is dinner ready?", "Tell me about the fair."):
            played_turn = await respond(client, story_id, body_of(by_role["player"], line))
            assert played_turn.status_code == 200, played_turn.text
        sent = everything_sent(stub)
        assert "June Harrow" in sent
        assert CARD_LABEL not in sent
        assert CARD_PHRASE not in sent

        async with engine.connect() as conn:
            for table in STORY_KNOWLEDGE:
                found = await conn.scalar(
                    text(
                        f"SELECT count(*) FROM {table} t "  # noqa: S608 (a fixed table name)
                        "WHERE t.story_id = :s AND (t::text LIKE :label OR t::text LIKE :phrase)"
                    ),
                    {"s": story_id, "label": f"%{CARD_LABEL}%", "phrase": f"%{CARD_PHRASE}%"},
                )
                assert found == 0, table


async def _cast_june(
    client: Any,
    label: str = CARD_LABEL,
    phrase: str = CARD_PHRASE,
    images: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """A story from ``lg.sunday_dinner`` with the catalog character "June Harrow" (``label``,
    ``phrase``, ``images`` when given) cast as ``mother``: the story's id, her story character's
    id and her catalog id."""
    locked, default = await scenarios.cast_characters(client)
    extra: dict[str, Any] = {"images": images} if images is not None else {}
    mother = await g.catalog_character_id(
        client,
        "June Harrow",
        age=60,
        labels=["warm", label],
        public_description=f"{phrase}, at every county fair.",
        **extra,
    )
    body = lg.sunday_dinner(locked, default)
    scenario_id, _ = await g.published_scenario(client, body)
    made = await g.create_from(
        client,
        g.from_scenario(
            scenario_id, slots={"mother": g.catalog_slot(mother)}, ignore_warnings=True
        ),
    )
    june = next(c["id"] for c in made["characters"] if c["name"] == "June Harrow")
    return made["story_id"], june, mother


KEPT_CARD_FIELDS = frozenset(
    {
        *("kind", "id", "character_id", "role_id", "source", "version"),
        *("status", "listing_status", "labels", "public_description"),
    }
)
"""The card fields a story-scope erasure keeps as they were (``boundary.KEPT_CARD``: labels and
public description, and the structural fields: kind, ids, source, version, status values)."""
ERASED_CARD_FIELDS = frozenset({"name", "images", "content_rating"})
"""The rest: the name redacted, the images and the content rating emptied (#390)."""


async def _report(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The story's genesis creation report, as stored."""
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'genesis'"),
            {"s": story_id},
        )
    assert isinstance(found, dict)
    report: dict[str, Any] = found["creation_report"]
    return report


def _card(report: dict[str, Any], catalog_id: str) -> dict[str, Any]:
    (card,) = [c for c in report["cast"] if (c.get("source") or {}).get("catalog_id") == catalog_id]
    found: dict[str, Any] = card
    return found


def _holding(report: dict[str, Any], character_id: str) -> list[dict[str, Any]]:
    return [
        d for d in report["deferred"] if character_id in d["payload"].get("characters", {}).values()
    ]


async def test_a_story_erasure_keeps_the_public_card_in_the_creation_report(
    pg_url: str, engine: AsyncEngine
) -> None:
    """docs/04 C-15 as amended for #376 (the owner's option 2): a story-scope erasure of a cast
    catalog character deletes her genesis snapshot, and the creation report keeps her public
    catalog card (id, labels, public description) with her name redacted, because the card is
    public catalog data until a catalog-scope erasure. #390, #393: nothing else of the card stays
    (her primary and gallery images and her content rating are emptied); #400: the card keeps
    exactly its structural fields beside those. #381: the report's deferred items that belong only
    to her (her secret memory, her stats, her relationships) go with her ``genesis_deferred``
    rows, and ``counts.deferred`` is recounted.

    Complements asserted: before the erasure the snapshot exists, the report names her, shows
    both her images and holds her deferred items and her secret; after it none of that does, and
    the card's label and phrase are still there."""
    report_sql = "SELECT o::text FROM operations o WHERE o.story_id = :s AND o::text LIKE :label"
    snapshot_sql = "SELECT count(*) FROM character_snapshots WHERE character_id = :c"
    async with respond_client(e.erasure_settings(pg_url), engine) as (client, _):
        primary = (await media.uploaded(client, headers=scenarios.WRITE))["id"]
        gallery = (await media.uploaded(client, headers=scenarios.WRITE))["id"]
        images = {"primary": primary, "gallery": [gallery]}
        story_id, june, mother = await _cast_june(client, images=images)
        shown = _card(await _report(engine, story_id), mother)
        assert shown["images"]["primary"]["media_id"] == primary
        assert [item["media_id"] for item in shown["images"]["gallery"]] == [gallery]
        kept = {k: v for k, v in shown.items() if k in KEPT_CARD_FIELDS}
        label = f"%{CARD_LABEL}%"
        assert await _one(engine, snapshot_sql, c=june) == 1
        (before,) = await _rows(engine, report_sql, s=story_id, label=label)
        assert "June Harrow" in before
        assert scenarios.ROLE_SECRET in before
        held = _holding(await _report(engine, story_id), june)
        assert held
        dry = await e.erase(client, "character", june, story_id, dry_run=True)
        assert dry["records"].get("operations", 0) >= 1
        await e.erase(client, "character", june, story_id)
    assert await _one(engine, snapshot_sql, c=june) == 0
    (after,) = await _rows(engine, report_sql, s=story_id, label=label)
    # "Harrow" alone is the cast's shared surname, which the boundary keeps.
    assert not e.naming(after, ["June Harrow", "June"]), after
    assert CARD_PHRASE in after
    assert mother in after
    assert scenarios.ROLE_SECRET not in after
    report = await _report(engine, story_id)
    assert _holding(report, june) == []
    card = _card(report, mother)
    assert (card["images"], card["content_rating"]) == ({"primary": None, "gallery": []}, None)
    assert primary not in after
    assert gallery not in after
    assert CARD_LABEL in card["labels"]
    # #400: what boundary.KEPT_CARD declares, and only that: the structural fields unchanged,
    # labels and public description kept, the name redacted, the rest emptied.
    assert set(card) == KEPT_CARD_FIELDS | ERASED_CARD_FIELDS
    assert {k: v for k, v in card.items() if k in KEPT_CARD_FIELDS} == kept
    assert not e.naming(card["name"], ["June Harrow", "June"])
    deferred = report["counts"]["deferred"]
    assert sum(deferred["by_class"].values()) == len(report["deferred"])
    assert sum(deferred["by_capability"].values()) == len(report["deferred"])


@pytest.mark.parametrize("story_first", [True, False], ids=["story_first", "catalog_first"])
async def test_a_catalog_erasure_empties_the_card_in_every_creation_report(
    pg_url: str, engine: AsyncEngine, story_first: bool
) -> None:
    """#382 (the owner's decision): the card is public catalog data until a catalog-scope
    erasure of that catalog character, which empties it in every creation report that casts
    her, in either order with a story-scope erasure of her story character: afterwards no table
    holds the card's label or phrase (this test's own, so another test's catalog entry is not
    counted), and the genesis operation still reads (omniscient), its card keeping only ids,
    version and status. Complement: both are in the report before."""
    tag = "first" if story_first else "second"
    label, phrase = f"{CARD_LABEL}-{tag}", f"{CARD_PHRASE} ({tag})"
    async with respond_client(e.erasure_settings(pg_url), engine) as (client, _):
        story_id, june, mother = await _cast_june(client, label, phrase)
        before = json.dumps(await _report(engine, story_id))
        assert label in before
        assert phrase in before
        if story_first:
            await e.erase(client, "character", june, story_id)
        await e.erase(client, "catalog_character", mother)
        if not story_first:
            await e.erase(client, "character", june, story_id)
        genesis = await _one(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'genesis'",
            s=story_id,
        )
        read = await client.get(
            f"/operations/{genesis}", params={"viewpoint": "omniscient"}, headers=e.WRITE
        )
    assert read.status_code == 200, read.text
    for table, values in (await e.dump_all(engine)).items():
        assert not any(label in v or phrase in v for v in values), table
    card = _card(read.json()["creation_report"], mother)
    assert card["id"] == mother
    assert (card["name"], card["public_description"], card["labels"]) == (None, None, [])
    assert (card["images"], card["content_rating"]) == ({"primary": None, "gallery": []}, None)


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> list[str]:
    async with engine.connect() as conn:
        return [str(r) for r in await conn.scalars(text(sql), params)]
