"""U-18: configuration classification (FR-CONC-14,15; #64).

Every leaf setting of ``StorySettings`` and ``DeploymentSettings`` carries an interpretive or
prospective tag, inner models walked; a setting without one makes ``test_u_18`` fail (checked
here against a fixture schema with one untagged field, which is the guard biting) and is
treated as interpretive at runtime. ``test_u_18__writes`` shows every configuration write takes
a new configuration revision through the single write path.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import read_settings
from epistrel.core.deployment import SettingsPatch, apply_settings
from epistrel.core.ratings import RatingFields, create_rating
from epistrel.core.settings_schema import (
    PROSPECTIVE,
    ConfigClass,
    DeploymentSettings,
    StorySettings,
    flatten,
    leaves,
    setting_class,
    untagged,
)
from epistrel.core.story_settings import update_story_settings
from tests.event_support import new_story, rows, scalar
from tests.policy_support import AUTHOR, library, story_item, unique, words

SRC = Path(__file__).resolve().parent.parent / "src" / "epistrel"

EXPECTED: dict[type[BaseModel], dict[str, ConfigClass]] = {
    # The planner's class list (#64's Discretion): what a turn in flight depends on is
    # interpretive; budgets, caps, windows, routing and queueing are prospective.
    StorySettings: {
        "default_output_viewpoint": ConfigClass.interpretive,
        "regenerate_retry_budget": ConfigClass.prospective,
        "turn_concurrency": ConfigClass.prospective,
        "turn_queue_max": ConfigClass.prospective,
        "turn_deadline": ConfigClass.prospective,
        "generation_settings.temperature": ConfigClass.prospective,
        "lite_window_messages": ConfigClass.prospective,
        "lite_summary_chunk": ConfigClass.prospective,
        "lite_summary_max_tokens": ConfigClass.prospective,
        "guardrail_retry_budget": ConfigClass.prospective,
    },
    DeploymentSettings: {
        "default_content_rating_id": ConfigClass.interpretive,
        "minor_age_threshold": ConfigClass.interpretive,
        "uncertain_below": ConfigClass.interpretive,
        "structured_retry_budget": ConfigClass.prospective,
        "turn_queue_max": ConfigClass.prospective,
        "idempotency_result_window": ConfigClass.prospective,
        "max_pinned_memories": ConfigClass.prospective,
        "max_custom_instructions_chars": ConfigClass.prospective,
        "lite_generation_defaults.length": ConfigClass.prospective,
        "lite_window_messages": ConfigClass.prospective,
        "lite_summary_chunk": ConfigClass.prospective,
        "lite_summary_max_tokens": ConfigClass.prospective,
        "default_safe_fallback": ConfigClass.prospective,
    },
}


class _Inner(BaseModel):
    tagged: Annotated[int, PROSPECTIVE] = 1
    forgotten: int = 2


class _Fixture(BaseModel):
    """A schema with one untagged leaf at the top and one inside an inner model."""

    fine: Annotated[str, PROSPECTIVE] = "x"
    missing: int = 0
    inner: _Inner | None = None


@pytest.mark.part("M2", "FR-CONC-14,15")
def test_u_18() -> None:
    """Every leaf of both schemas is tagged, with the planned classes. Complements: the same
    check reports a fixture's untagged fields (top-level and inner), each of which reads as
    interpretive at runtime, as does an unknown setting; a tagged prospective leaf stays
    prospective."""
    for schema, classes in EXPECTED.items():
        assert untagged(schema) == [], schema.__name__
        for path, expected in classes.items():
            assert setting_class(schema, path) is expected, (schema.__name__, path)
        assert {p for p, _ in leaves(schema)} >= set(classes)

    assert untagged(_Fixture) == ["missing", "inner.forgotten"]
    assert setting_class(_Fixture, "missing") is ConfigClass.interpretive
    assert setting_class(_Fixture, "inner.forgotten") is ConfigClass.interpretive
    assert setting_class(_Fixture, "inner.tagged") is ConfigClass.prospective
    assert setting_class(_Fixture, "fine") is ConfigClass.prospective
    assert setting_class(StorySettings, "no_such_setting") is ConfigClass.interpretive
    assert setting_class(StorySettings, "generation_settings") is ConfigClass.interpretive

    # One row per leaf: an inner model that is unset contributes each leaf as null.
    assert flatten(_Fixture, {"fine": "y"}) == {
        "fine": "y",
        "missing": None,
        "inner.tagged": None,
        "inner.forgotten": None,
    }
    # The REST patch names exactly the schema's settings.
    assert set(SettingsPatch.model_fields) == set(DeploymentSettings.model_fields)


def test_config_changes_has_one_writer() -> None:
    """Only ``core/config_changes.py`` inserts into ``config_changes`` (FR-CONC-14): every
    admin router and library write reaches it through ``record_config_change``."""
    writers = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "insert"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "config_changes"
            ):
                writers.append(path.relative_to(SRC).as_posix())
    assert writers == ["core/config_changes.py"]


async def _story_revisions(engine: AsyncEngine, story: str) -> tuple[int, int]:
    found = await rows(
        engine,
        "SELECT config_revision, timeline_revision FROM stories WHERE id = :s",
        s=story,
    )
    return int(found[0][0]), int(found[0][1])


@pytest.mark.integration
@pytest.mark.part("M2", "FR-CONC-14")
async def test_u_18__writes(engine: AsyncEngine, settings: Settings) -> None:
    """Every configuration write produces a new configuration revision of its scope, one
    ``config_changes`` row per changed leaf with that leaf's class and the actor, and one
    committed ``control`` operation; story-scoped writes also leave a ``config_changed`` event
    per row. No write moves the timeline revision. Complement: a write that changes nothing
    takes no revision and records nothing."""
    lib = library(settings, engine)
    story = await new_story(engine)
    config, timeline = await _story_revisions(engine, story)

    # A story setting write: one revision, a row per leaf, each with its own class.
    await update_story_settings(
        engine,
        story,
        {"turn_deadline": 30, "generation_settings": {"temperature": 0.2, "top_p": 0.9}},
        principal="p-author",
    )
    assert await _story_revisions(engine, story) == (config + 1, timeline)
    recorded = await rows(
        engine,
        "SELECT setting, \"class\", actor FROM config_changes WHERE scope = 'story' "
        "AND scope_id = :s AND config_revision = :r ORDER BY setting",
        s=story,
        r=config + 1,
    )
    assert [tuple(r) for r in recorded] == [
        ("generation_settings.temperature", "prospective", "p-author"),
        ("generation_settings.top_p", "prospective", "p-author"),
        ("turn_deadline", "prospective", "p-author"),
    ]
    # A story policy write (interpretive) is the next story revision.
    await lib.policies_set_story(AUTHOR, story, [story_item(words(unique("w")))])
    assert await _story_revisions(engine, story) == (config + 2, timeline)
    assert (
        await scalar(
            engine,
            'SELECT "class" FROM config_changes WHERE scope_id = :s AND config_revision = :r',
            s=story,
            r=config + 2,
        )
        == "interpretive"
    )
    controls = await rows(
        engine,
        "SELECT o.operation_id, o.status, count(e.id) AS events FROM operations o "
        "LEFT JOIN events e ON e.operation_id = o.operation_id "
        "WHERE o.story_id = :s AND o.kind = 'control' GROUP BY o.operation_id, o.status",
        s=story,
    )
    assert sorted((r.status, r.events) for r in controls) == [("committed", 1), ("committed", 3)]
    events = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'config_changed' ORDER BY seq",
        s=story,
    )
    assert [e.payload["setting"] for e in events][:3] == [
        "generation_settings.temperature",
        "generation_settings.top_p",
        "turn_deadline",
    ]

    # Deployment writes: a rating and a setting, each one revision with a story-less control.
    start = (await read_settings(engine)).config_revision
    await create_rating(engine, RatingFields(name=unique("U18")), principal="p-operator")
    before = await read_settings(engine)
    assert before.config_revision == start + 1
    try:
        await apply_settings(
            engine,
            {"uncertain_below": 0.4, "max_pinned_memories": 7},
            principal="p-operator",
        )
        assert (await read_settings(engine)).config_revision == start + 2
        changed = await rows(
            engine,
            "SELECT setting, \"class\" FROM config_changes WHERE scope = 'deployment' "
            "AND config_revision = :r ORDER BY setting",
            r=start + 2,
        )
        assert [tuple(r) for r in changed] == [
            ("max_pinned_memories", "prospective"),
            ("uncertain_below", "interpretive"),
        ]
        story_less = await scalar(
            engine,
            "SELECT count(*) FROM operations WHERE story_id IS NULL AND kind = 'control' "
            "AND committed_at >= (SELECT min(at) FROM config_changes "
            "WHERE scope = 'deployment' AND config_revision >= :r)",
            r=start + 1,
        )
        assert story_less >= 2
        # The complement: the same values again take no revision and record nothing.
        await apply_settings(
            engine,
            {"uncertain_below": 0.4, "max_pinned_memories": 7},
            principal="p-operator",
        )
        assert (await read_settings(engine)).config_revision == start + 2
        await update_story_settings(engine, story, {"turn_deadline": 30}, principal="p-author")
        assert await _story_revisions(engine, story) == (config + 2, timeline)
    finally:
        restore = before.as_settings()
        await apply_settings(
            engine,
            {
                "uncertain_below": restore.uncertain_below,
                "max_pinned_memories": restore.max_pinned_memories,
            },
            principal="p-operator",
        )
