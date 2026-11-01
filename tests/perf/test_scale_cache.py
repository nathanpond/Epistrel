"""The scale fixture's cache keeps the cast's order (#521): a fixture restored from its sidecar
(``scale.read_cached``) has the same ``ScaleStory`` as the one built, so its ``player`` is the
player, not the alphabetically first character, and every name map (``queries`` and
``memory_plan`` walk them in order) comes back in the order it was built in."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from epistrel.core.tables import characters
from epistrel.testing.fixtures import knowledge, scale
from tests.perf.scale_support import ScaleRun


def _round_trip(tmp_path: Path, fixture: scale.ScaleFixture) -> scale.ScaleFixture:
    dump_path, sidecar = scale.cached_paths(tmp_path, fixture.spec)
    dump_path.write_bytes(b"")  # read_cached needs the dump to exist; it never opens it
    sidecar.write_text(scale.sidecar_text(fixture))
    restored = scale.read_cached(tmp_path, fixture.spec)
    assert restored is not None
    return restored


def _built(spec: scale.ScaleSpec) -> scale.ScaleFixture:
    """A fixture's description shaped as ``scale._create`` builds it (no database)."""
    cast = scale.person_names(spec.characters)
    people = scale.person_names(scale.SUBJECTS)
    story = scale.ScaleStory(
        "story",
        {n: f"c-{i}" for i, n in enumerate(cast)},
        {n: f"e-{i}" for i, n in enumerate(people)},
        {f"Place {i}": f"p-{i}" for i in (3, 1, 2)},
        {f"Item {i}": f"i-{i}" for i in (2, 3, 1)},
        head=7,
        memories=3,
    )
    return scale.ScaleFixture(spec, story, scale.version_of(spec.dimension))


def test_a_cached_sidecar_keeps_the_cast_order(tmp_path: Path) -> None:
    """The sidecar round-trip gives back the built story, its player and every map's order;
    the cast's first name is not the alphabetically first, so a sorted sidecar would not."""
    built = _built(scale.ScaleSpec.scaled(10, characters=5))
    names = list(built.story.characters)
    assert sorted(names)[0] != names[0]  # the failure #521 saw is possible here
    restored = _round_trip(tmp_path, built)
    assert restored.story == built.story
    assert restored.story.player == built.story.player == "c-0"
    for name in scale.ORDERED_MAPS:
        assert list(getattr(restored.story, name).items()) == list(
            getattr(built.story, name).items()
        )
    assert knowledge.holders(restored.story) == [f"c-{i}" for i in range(1, 5)]


@pytest.mark.perf_smoke
async def test_a_cached_scale_fixture_keeps_its_player(scale_1k: ScaleRun, tmp_path: Path) -> None:
    """On the 1k fixture: ``ScaleStory.player`` is the character the story stores as its player
    (``is_player``), and the fixture read back from its sidecar has the same story and player."""
    story = scale_1k.fixture.story
    async with scale_1k.db.connect() as conn:
        players = (
            await conn.execute(
                select(characters.c.id).where(
                    characters.c.story_id == story.story_id, characters.c.is_player
                )
            )
        ).scalars()
        stored = list(players)
    assert stored == [story.player]
    restored = _round_trip(tmp_path, scale_1k.fixture)
    assert restored.story == story
    assert restored.story.player == story.player
    assert list(restored.story.characters.items()) == list(story.characters.items())
