"""The image safety context's deterministic parts (#257; FR-IMG-12, FR-CAT-12): the canonical-age
reader against the policy gate's ``current_age``, and subject resolution by name, alias, part,
declaration and presence. Unit-level tests of the I-24 parts; no requirement part of their own."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock, world_clock
from epistrel.core.catalog import ages
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.images.safety import (
    DeclaredSubject,
    Figure,
    SafetyContext,
    SafetyRequest,
    build_safety_context,
    image_position,
)
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.seeds.ratings import EXPLICIT_ID
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.core.tables.catalog import character_catalog
from epistrel.orchestrator.models.registry import SUBJECT_LABEL_PATTERN, CheckedImageSubject
from epistrel.orchestrator.policies.age import current_age
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from epistrel.orchestrator.policies.store import read_cast
from tests import clock_support as ck
from tests import perception_support as ps
from tests.image_safety_support import (
    LILY,
    MARCUS,
    WREN,
    next_turn,
    whisper_story,
    with_rooms,
)

pytestmark = pytest.mark.integration


async def _story(engine: AsyncEngine, *cast: tuple[str, int]) -> tuple[str, dict[str, str]]:
    created = await create_story_from(
        engine,
        StorySpec(
            name="Ages",
            content_rating_id=EXPLICIT_ID,
            characters=[
                CharacterSpec(name=name, definition="Someone.", true_age=age, is_player=n == 0)
                for n, (name, age) in enumerate(cast)
            ],
        ),
        principal="p-ages",
    )
    return created.story_id, {c.name: c.id for c in created.characters}


async def _both(engine: AsyncEngine, story: str) -> tuple[dict[str, int], dict[str, int]]:
    """``(true_ages_at(HEAD), current_age over read_cast)``."""
    async with engine.connect() as conn:
        found = await ages.true_ages_at(conn, story, world_clock.HEAD)
        cast = await read_cast(conn, story)
    return found, {m.character_id: current_age(m) for m in cast}


async def test_true_ages_agree_with_current_age(engine: AsyncEngine, clock_delivered: None) -> None:
    """Over a fixture cast, ``true_ages_at`` at the story's head equals the policy gate's
    ``current_age`` with the clock off, after 23 months, for a character modeled then, and after
    two years; at an earlier position it is the age then. A catalog age is as written."""
    story, ids = await _story(engine, ("Ada", 34), ("Bea", 31), ("Cy", 16))
    found, expected = await _both(engine, story)
    assert found == expected == {ids["Ada"]: 34, ids["Bea"]: 31, ids["Cy"]: 16}

    await ck.set_level(engine, story, "prompt_only")
    months = 23 * ck.YEAR // 12
    await ck.advance(engine, story, months, reason="the months go by")
    found, expected = await _both(engine, story)
    assert found == expected
    assert found[ids["Cy"]] == 17
    async with engine.begin() as conn:
        (slot,) = await conn.execute(
            text(
                "SELECT narrative_turn, slot_k FROM operations WHERE story_id = :s "
                "AND kind = 'inter_turn' ORDER BY slot_k DESC LIMIT 1"
            ),
            {"s": story},
        )
        await conn.execute(
            text(
                "UPDATE characters SET modeled_turn = :t, modeled_slot_k = :k "
                "WHERE story_id = :s AND id = :c"
            ),
            {"t": slot.narrative_turn, "k": slot.slot_k, "s": story, "c": ids["Bea"]},
        )
    await ck.advance(engine, story, 2 * ck.YEAR - months, reason="summer again")
    found, expected = await _both(engine, story)
    assert found == expected == {ids["Ada"]: 36, ids["Bea"]: 31, ids["Cy"]: 18}
    async with engine.connect() as conn:
        assert await ages.true_age_at(conn, story, ids["Cy"], Position(0)) == 16
        assert await ages.true_age_at(conn, story, "no-such-character", Position(1)) is None

    await ck.set_level(engine, story, None)
    found, expected = await _both(engine, story)
    assert found == expected
    assert found[ids["Cy"]] == 16
    assert ages.catalog_true_age({"definition": {"true_age": 17}}) == 17
    assert ages.catalog_true_age({"definition": {"true_age": "17"}}) is None


def _request(story: str, prompt: str, *subjects: DeclaredSubject, **texts: str) -> SafetyRequest:
    return SafetyRequest(kind="story", story_id=story, prompt=prompt, subjects=subjects, **texts)


async def _context(
    engine: AsyncEngine, request: SafetyRequest, position: Position | None = None
) -> SafetyContext:
    async with engine.connect() as conn:
        return await build_safety_context(conn, request, position)


async def test_resolution_by_name_part_alias_and_declaration(engine: AsyncEngine) -> None:
    """Names resolve through the shared matcher's fold, a shared first name to both
    characters, a common-word name at a sentence's start (a candidate) too; a declaration by id
    resolves an unnamed character; an unknown declared id is a validation error. Labels follow
    first mention, then creation order, and carry no name.

    Complement asserted: a lowercase common word names nobody, and without a position nobody is
    added as present."""
    story, ids = await _story(engine, ("Ann Lee", 30), ("Ann Moss", 16), ("Rose", 40), ("Kit", 25))
    context = await _context(
        engine,
        _request(story, "ANN waves. Rose sits.", DeclaredSubject(ids["Kit"], ("tall",))),
    )
    by_id = {s.character_id: s for s in context.subjects}
    assert set(by_id) == {ids["Ann Lee"], ids["Ann Moss"], ids["Rose"], ids["Kit"]}
    assert by_id[ids["Ann Moss"]].classification == "minor"
    assert by_id[ids["Ann Lee"]].classification == "adult"
    assert by_id[ids["Rose"]].mentions == ("Rose",)
    assert by_id[ids["Kit"]].source == "declared"
    assert by_id[ids["Kit"]].visual_descriptors == ("tall",)
    assert [s.label for s in context.subjects] == [f"subject_{n}" for n in range(1, 5)]
    assert by_id[ids["Kit"]].label == "subject_4", "unmentioned subjects come last"
    assert context.has_minor
    assert not context.figures

    quiet = await _context(engine, _request(story, "A rose on the table.", caption="no one"))
    assert quiet.subjects == ()
    with pytest.raises(InvalidRequest):
        await _context(engine, _request(story, "x", DeclaredSubject("01NOTACHARACTER0000000000")))

    found = context.with_figures([Figure("a young girl", "indeterminate")])
    assert found.figures[0].label == "figure_1"
    assert found.figures[0].minor
    for s in found.subjects:
        CheckedImageSubject(label=s.label, true_age=s.true_age, classification=s.classification)
    assert SUBJECT_LABEL_PATTERN.startswith("^(character|subject|figure)_")
    assert SafetyContext.from_json(found.as_json()) == found


async def test_presence_at_the_image_position(engine: AsyncEngine) -> None:
    """Presence is routing's at the image's position: with perception ``full`` the barn's
    minors are not present for an image of the cabin, and are once one walks in; under Lite
    (perception ``none``) everyone is. A slot uses the scene at the end of its turn."""
    story, ids = await whisper_story(engine)
    try:
        lite = await _context(engine, _request(story, "The stove glows."), Position(1))
        assert {ids[LILY], ids[WREN]} <= set(lite.character_ids)
        places = await with_rooms(engine, story, ids)
        turn = await next_turn(engine, story)
        at = image_position({"turn": turn, "variant": 1})
        assert (at.beat, at.slot_k) == (POSITION_MAX, 0)
        cabin = await _context(engine, _request(story, "Marcus by the stove."), at)
        assert ids[MARCUS] in cabin.character_ids
        assert not {ids[LILY], ids[WREN]} & set(cabin.character_ids)
        assert not cabin.has_minor
        slot = await _context(
            engine,
            _request(story, "Marcus by the stove."),
            image_position({"position": {"turn": turn - 1, "variant": 1, "slot_k": 1}}),
        )
        assert not {ids[LILY], ids[WREN]} & set(slot.character_ids)
        await ps.put(engine, story, ids[WREN], places["cabin"])
        joined = await _context(engine, _request(story, "Marcus by the stove."), at)
        assert ids[WREN] in joined.character_ids
        assert joined.has_minor
    finally:
        await ps.forget_perception(engine)


async def test_catalog_image_subjects(engine: AsyncEngine) -> None:
    """A catalog character entry's image always has the entry's character as a subject, with
    the draft's true age as written and its rating; a name the prompt uses resolves to it; a
    draft with no age on record is indeterminate (fail closed).

    Complement asserted: an entry that does not exist has no subjects."""
    entry = new_ulid()
    ageless = new_ulid()
    rows = [
        {
            "id": entry_id,
            "owner_principal": "p-catalog",
            "listing_status": "private",
            "status": EntryStatus.DRAFT,
            "current_version": None,
            "draft": draft,
            "draft_revision": 1,
            "created_at": clock.now(),
            "updated_at": clock.now(),
        }
        for entry_id, draft in (
            (
                entry,
                {
                    "public_card": {"name": "Mira Dale"},
                    "definition": {"true_age": 16},
                    "metadata": {"aliases": ["Birdie"], "content_rating_id": EXPLICIT_ID},
                },
            ),
            (ageless, {"public_card": {"name": "Odo"}}),
        )
    ]
    async with engine.begin() as conn:
        await conn.execute(insert(character_catalog), rows)
    try:
        request = SafetyRequest(
            kind="catalog", prompt="Birdie by the sea.", entry_kind="character", entry_id=entry
        )
        context = await _context(engine, request)
        (found,) = context.subjects
        assert (found.character_id, found.true_age, found.classification) == (
            entry,
            16,
            "minor",
        )
        assert (found.source, found.mentions, context.rating_id) == (
            "depicted",
            ("Birdie",),
            EXPLICIT_ID,
        )
        odo = await _context(
            engine,
            SafetyRequest(kind="catalog", prompt="x", entry_kind="character", entry_id=ageless),
        )
        assert [s.classification for s in odo.subjects] == ["indeterminate"]
        gone = await _context(
            engine,
            SafetyRequest(kind="catalog", prompt="x", entry_kind="character", entry_id=new_ulid()),
        )
        assert gone.subjects == ()
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                delete(character_catalog).where(character_catalog.c.id.in_([entry, ageless]))
            )


def _migration() -> Any:
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    spec = importlib.util.spec_from_file_location("m0133", path / "0133_minors_image_targets.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_minors_targets_migration_is_an_idempotent_union(engine: AsyncEngine) -> None:
    """Migration 0133's update keeps an administrator's own targets and adds the image ones
    after them, changes nothing the second time, and skips a removed policy (rolled back)."""
    add = _migration().ADD_TARGETS
    select_targets = text("SELECT applies_to FROM policies WHERE id = :p")
    async with engine.connect() as conn, conn.begin() as tx:
        await conn.execute(
            text("UPDATE policies SET applies_to = ARRAY['text_output'] WHERE id = :p"),
            {"p": MINORS_POLICY_ID},
        )
        assert (await conn.execute(add)).rowcount == 1
        expected = ["text_output", "image_prompt", "image_output"]
        assert await conn.scalar(select_targets, {"p": MINORS_POLICY_ID}) == expected
        assert (await conn.execute(add)).rowcount == 0
        assert await conn.scalar(select_targets, {"p": MINORS_POLICY_ID}) == expected
        await conn.execute(text("DELETE FROM policies WHERE id = :p"), {"p": MINORS_POLICY_ID})
        assert (await conn.execute(add)).rowcount == 0
        await tx.rollback()
