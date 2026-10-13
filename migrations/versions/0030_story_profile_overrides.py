"""story overrides and the deployment's profile defaults; M2's engine dials move there (#113)

Two places for the layers of a story's effective configuration (FR-PROF-5: the deployment
defaults, then the profile's current version, then the story's overrides):

- ``deployment_state.profile_defaults`` (jsonb, NOT NULL): the deployment's defaults for every
  engine profile setting, a complete settings document seeded from the Lite preset
  (``epistrel.core.seeds.profiles``).
- ``story_profile_overrides(story_id, settings, revision)``: a story's own overrides, a sparse
  settings document, with ``revision`` from 1. ``story_id`` references ``stories``; deleting a
  story empties it (``core.stories.STORY_TABLES``), so the key does not cascade.

M2's engine dials leave the story and deployment settings (FR-PROF-5: engine dials are the
profile's, never story configuration). A story's ``generation_settings`` (temperature, top_p,
length), ``lite_window_messages``, ``lite_summary_max_tokens``, ``max_model_calls_per_turn``,
``turn_deadline`` (seconds) and ``guardrail_retry_budget`` become its overrides
(``models.generation``, ``memory.rolling_summary.window_messages`` and
``.compress_threshold_tokens``, ``budgets.model_calls``, ``budgets.deadline_ms`` in milliseconds,
``repair.polish.max_regenerations``). The deployment's ``lite_generation_defaults``,
``lite_window_messages``, ``lite_summary_max_tokens`` and ``max_model_calls_per_turn`` become
``profile_defaults`` values the same way. M2 stored every setting at its default whenever a
settings document was written, so a stored value equal to M2's default (or ``null``, which
inherited) is indistinguishable from one never set: it is not carried over, and the setting
follows the profile (whose Lite values are M2's defaults). Each key leaves its document either
way. No ``config_changes`` row is written and no revision moves: nothing a story runs under
changes.

Downgrade puts each carried value back under its M2 name and drops the column and the table.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-13

"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.profiles import lite_settings

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
TABLE = "story_profile_overrides"

GENERATION = "models.generation"
STORY_DIALS = {
    "lite_window_messages": "memory.rolling_summary.window_messages",
    "lite_summary_max_tokens": "memory.rolling_summary.compress_threshold_tokens",
    "max_model_calls_per_turn": "budgets.model_calls",
    "guardrail_retry_budget": "repair.polish.max_regenerations",
}
"""M2's story settings that map one to one onto a profile setting (``turn_deadline`` and
``generation_settings`` are converted)."""
STORY_DEFAULTS: dict[str, Any] = {"guardrail_retry_budget": 2, "turn_deadline": 45.0}
"""M2's defaults of the story dials that were not nullable: stored on every story."""
DEPLOYMENT_DIALS = {
    "lite_window_messages": "memory.rolling_summary.window_messages",
    "lite_summary_max_tokens": "memory.rolling_summary.compress_threshold_tokens",
    "max_model_calls_per_turn": "budgets.model_calls",
}
DEPLOYMENT_DEFAULTS: dict[str, Any] = {
    "lite_window_messages": 30,
    "lite_summary_max_tokens": 2_000,
    "max_model_calls_per_turn": None,
}
M2_GENERATION = {"temperature": 0.8, "top_p": 0.95, "length": "medium"}
"""M2's deployment Lite generation defaults (``lite_generation_defaults``)."""
LENGTH = {"temperature": "temperature", "top_p": "top_p", "length": "response_length"}
STORY_KEYS = (*STORY_DIALS, "turn_deadline", "generation_settings")
DEPLOYMENT_KEYS = (*DEPLOYMENT_DIALS, "lite_generation_defaults")


def _set(document: dict[str, Any], path: str, value: Any) -> None:
    current = document
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _get(document: Mapping[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def story_overrides(settings: Mapping[str, Any]) -> dict[str, Any]:
    """The overrides an M2 story's settings carry (see the module docstring)."""
    found: dict[str, Any] = {}
    for name, path in STORY_DIALS.items():
        value = settings.get(name)
        if value is not None and value != STORY_DEFAULTS.get(name):
            _set(found, path, value)
    deadline = settings.get("turn_deadline")
    if deadline is not None and deadline != STORY_DEFAULTS["turn_deadline"]:
        _set(found, "budgets.deadline_ms", max(1, round(float(deadline) * 1000)))
    generation = settings.get("generation_settings")
    if isinstance(generation, Mapping):
        for name, target in LENGTH.items():
            if generation.get(name) is not None:
                _set(found, f"{GENERATION}.{target}", generation[name])
    return found


def deployment_defaults(settings: Mapping[str, Any]) -> dict[str, Any]:
    """The Lite seed with the M2 deployment dials an operator changed laid over it."""
    found = lite_settings()
    for name, path in DEPLOYMENT_DIALS.items():
        if name in settings and settings[name] != DEPLOYMENT_DEFAULTS[name]:
            _set(found, path, settings[name])
    generation = settings.get("lite_generation_defaults")
    if isinstance(generation, Mapping):
        for name, target in LENGTH.items():
            value = generation.get(name, M2_GENERATION[name])
            if value != M2_GENERATION[name] and not (name == "length" and value is None):
                _set(found, f"{GENERATION}.{target}", value)
    return found


def upgrade() -> None:
    op.add_column(
        "deployment_state",
        sa.Column("profile_defaults", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        TABLE,
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name=f"ck_{TABLE}_revision"),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"], name=f"fk_{TABLE}_story_id_stories"),
        sa.PrimaryKeyConstraint("story_id", name=f"pk_{TABLE}"),
    )
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id, settings FROM deployment_state")).all():
        stored = dict(row.settings or {})
        bind.execute(
            sa.text(
                "UPDATE deployment_state SET profile_defaults = CAST(:defaults AS jsonb), "
                "settings = CAST(:settings AS jsonb) WHERE id = :id"
            ),
            {
                "defaults": json.dumps(deployment_defaults(stored)),
                "settings": json.dumps(
                    {k: v for k, v in stored.items() if k not in DEPLOYMENT_KEYS}
                ),
                "id": row.id,
            },
        )
    op.alter_column("deployment_state", "profile_defaults", nullable=False)

    moved = sa.text(
        "SELECT id, settings FROM stories "
        "WHERE jsonb_exists_any(settings, CAST(:keys AS text[])) ORDER BY id"
    ).bindparams(keys=list(STORY_KEYS))
    for row in bind.execute(moved).all():
        stored = dict(row.settings or {})
        overrides = story_overrides(stored)
        if overrides:
            bind.execute(
                sa.text(
                    f"INSERT INTO {TABLE} (story_id, settings, revision) "  # noqa: S608
                    "VALUES (:story, CAST(:settings AS jsonb), 1)"
                ),
                {"story": row.id, "settings": json.dumps(overrides)},
            )
        bind.execute(
            sa.text("UPDATE stories SET settings = CAST(:settings AS jsonb) WHERE id = :id"),
            {
                "settings": json.dumps({k: v for k, v in stored.items() if k not in STORY_KEYS}),
                "id": row.id,
            },
        )


def _m2_story(overrides: Mapping[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, path in STORY_DIALS.items():
        value = _get(overrides, path)
        if value is not None:
            found[name] = value
    deadline = _get(overrides, "budgets.deadline_ms")
    if deadline is not None:
        found["turn_deadline"] = float(deadline) / 1000
    generation = {name: _get(overrides, f"{GENERATION}.{t}") for name, t in LENGTH.items()}
    if any(value is not None for value in generation.values()):
        found["generation_settings"] = generation
    return found


def _m2_deployment(defaults: Mapping[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, path in DEPLOYMENT_DIALS.items():
        found[name] = _get(defaults, path)
    generation = copy.deepcopy(M2_GENERATION)
    for name, target in LENGTH.items():
        value = _get(defaults, f"{GENERATION}.{target}")
        if value is not None or name != "length":
            generation[name] = value
    found["lite_generation_defaults"] = generation
    return found


def downgrade() -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.text(f"SELECT story_id, settings FROM {TABLE}")).all():  # noqa: S608
        bind.execute(
            sa.text(
                "UPDATE stories SET settings = settings || CAST(:moved AS jsonb) WHERE id = :id"
            ),
            {"moved": json.dumps(_m2_story(row.settings or {})), "id": row.story_id},
        )
    for row in bind.execute(sa.text("SELECT id, profile_defaults FROM deployment_state")).all():
        bind.execute(
            sa.text(
                "UPDATE deployment_state SET settings = settings || CAST(:moved AS jsonb) "
                "WHERE id = :id"
            ),
            {"moved": json.dumps(_m2_deployment(row.profile_defaults or {})), "id": row.id},
        )
    op.drop_table(TABLE)
    op.drop_column("deployment_state", "profile_defaults")
