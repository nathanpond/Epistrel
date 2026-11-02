"""the M7 presets: ``Structured`` converted in place into Full, Standard and Max seeded; #227's two
dials in the profile defaults (FR-PROF-3)

- ``deployment_state.profile_defaults`` must name every delivered setting (#113): the two dials
  #227 adds (``verification.shape_tightening``, ``commitment.salience_bias``) are added at their
  defaults (``M7_PROFILE_DEFAULTS``) where it does not name them yet.
- **Full** (the owner's decision 1 on #227): the seeded ``Structured`` row (``FULL_PRESET_ID``,
  the same id) gets a new version with ``FULL_SETTINGS`` (the next after its current one: 4 on a
  deployment that never edited it), is renamed ``Full`` and becomes a preset. The version is
  recorded the way ``core.profiles.store`` records an edit: ``config_revision`` moves once, one
  deployment-scope ``config_changes`` row ``engine_profile:<id>`` (the old and the new version)
  and one ``engine_profile:<id>.<path>`` row per setting whose value over the defaults changed,
  each of its class, by ``system:migration:0110``. On a fresh deployment (no story and no
  ``config_changes`` row yet) nothing is recorded and ``config_revision`` stays: Full is then
  a seed, like Standard and Max.
- **The pin branch** is computed here, never hard-coded: the delta between the row's latest
  version and Full, over the defaults, classified with #106's tags. When it raises any
  data-producing module (``schema.data_increases``), every following story with history (#109's
  ``with_history``: a committed, active turn) and every history-less one with a
  ``genesis_deferred`` row for a gained capability is pinned to the previous version
  (``stories.engine_profile_version_pin``), with one story-scope ``config_changes`` row
  ``engine_profile_version_pin`` (old ``null``, new the version) per pinned story; those stories
  move through #229's ``upgrade-pinned``. This departs from ``0080``/``0104``, which pinned every
  following story and wrote no ``config_changes`` row. When the delta raises nothing, nothing is
  pinned: a following story picks the behavioral change up at its next turn, with no backfill
  job. Stories that ``0104`` pinned to version 2 stay there.
- **Standard** and **Max** are new system-owned presets at version 1 (``STANDARD_PRESET_ID``,
  ``MAX_PRESET_ID``).

Downgrade is refused while Standard or Max has a version past 1 or a story runs on either.
Otherwise it deletes them, unpins the stories this pinned and deletes their rows, restores the
previous version as Full's current one under the name ``Structured`` (``preset`` false), deletes
the version this wrote and its ``config_changes`` rows, and removes the two dials from the
defaults.

Revision ID: 0110
Revises: 0109
Create Date: 2026-11-02

"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from epistrel.core.ids import new_ulid, ulid_after
from epistrel.core.profiles import schema
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.seeds.profiles import (
    FULL_NAME,
    FULL_PRESET_ID,
    M7_PROFILE_DEFAULTS,
    MAX_NAME,
    MAX_PRESET_ID,
    SEED_PRINCIPAL,
    STANDARD_NAME,
    STANDARD_PRESET_ID,
    STRUCTURED_NAME,
    full_settings,
    max_settings,
    standard_settings,
)
from epistrel.core.settings_schema import ConfigClass, flatten

revision: str = "0110"
down_revision: str | None = "0109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR = f"system:migration:{revision}"
PIN_SETTING = "engine_profile_version_pin"
PROFILE_FAMILY = "engine_profile"


def _added(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(stored)
    for key, value in seed.items():
        if isinstance(value, Mapping):
            inner = out.get(key)
            out[key] = _added(inner if isinstance(inner, Mapping) else {}, value)
        elif key not in out:
            out[key] = value
    return out


def _removed(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(stored)
    for key, value in seed.items():
        if key not in out:
            continue
        if isinstance(value, Mapping) and isinstance(out[key], Mapping):
            inner = _removed(out[key], value)
            if inner:
                out[key] = inner
            else:
                del out[key]
        elif not isinstance(value, Mapping):
            del out[key]
    return out


def _rewrite_defaults(change: Any) -> dict[str, Any]:
    bind = op.get_bind()
    row = bind.execute(sa.text("SELECT id, profile_defaults FROM deployment_state")).one()
    defaults: dict[str, Any] = change(dict(row.profile_defaults), M7_PROFILE_DEFAULTS)
    bind.execute(
        sa.text("UPDATE deployment_state SET profile_defaults = CAST(:d AS jsonb) WHERE id = :i"),
        {"d": json.dumps(defaults), "i": row.id},
    )
    return defaults


class _Changes:
    """The ``config_changes`` rows of this migration, ids in recording order."""

    def __init__(self, revision_number: int) -> None:
        self.revision = revision_number
        self.last: str | None = None

    def record(
        self,
        setting: str,
        old: Any,
        new: Any,
        change_class: ConfigClass,
        *,
        scope: str = "deployment",
        scope_id: str | None = None,
    ) -> None:
        change_id = ulid_after(self.last) if self.last is not None else new_ulid()
        self.last = change_id
        op.get_bind().execute(
            sa.text(
                "INSERT INTO config_changes "
                '(change_id, scope, scope_id, config_revision, setting, old, new, "class", '
                "actor) VALUES (:c, :scope, :scope_id, :r, :s, CAST(:o AS jsonb), "
                "CAST(:n AS jsonb), :k, :a)"
            ),
            {
                "c": change_id,
                "scope": scope,
                "scope_id": scope_id,
                "r": self.revision,
                "s": setting,
                "o": None if old is None else json.dumps(old),
                "n": None if new is None else json.dumps(new),
                "k": ConfigClass(change_class).value,
                "a": ACTOR,
            },
        )


def _setting(path: str | None = None) -> str:
    return f"{PROFILE_FAMILY}:{FULL_PRESET_ID}" + (f".{path}" if path else "")


def _pinned_stories(gained: Sequence[str]) -> list[str]:
    """The stories following Full that keep ``previous``: those with history, and those with a
    deferred genesis row for a gained capability."""
    if not gained:
        return []
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT s.id FROM stories s WHERE s.engine_profile_id = :p "
                "AND s.engine_profile_version_pin IS NULL AND ("
                "EXISTS (SELECT 1 FROM operations o WHERE o.story_id = s.id AND o.kind = 'turn' "
                "AND o.membership = 'active' AND o.status = 'committed') "
                "OR EXISTS (SELECT 1 FROM genesis_deferred g WHERE g.story_id = s.id "
                "AND g.materialized_at IS NULL AND g.capability_needed = ANY(:gained))) "
                "ORDER BY s.id"
            ),
            {"p": FULL_PRESET_ID, "gained": list(gained)},
        )
        .all()
    )
    return [str(row.id) for row in rows]


def _insert_preset(profile_id: str, name: str, settings: Mapping[str, Any]) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO engine_profiles "
            "(id, name, owner_principal, preset, current_version, created_at, updated_at) "
            "VALUES (:p, :n, :o, true, 1, transaction_timestamp(), transaction_timestamp())"
        ),
        {"p": profile_id, "n": name, "o": SEED_PRINCIPAL},
    )
    bind.execute(
        sa.text(
            "INSERT INTO engine_profile_versions "
            "(profile_id, version, settings, created_by, created_at) "
            "VALUES (:p, 1, CAST(:s AS jsonb), :by, transaction_timestamp())"
        ),
        {"p": profile_id, "s": json.dumps(settings), "by": SEED_PRINCIPAL},
    )


def _convert_full(defaults: Mapping[str, Any]) -> None:
    bind = op.get_bind()
    head = bind.execute(
        sa.text(
            "SELECT p.current_version, v.settings FROM engine_profiles p "
            "JOIN engine_profile_versions v ON v.profile_id = p.id "
            "AND v.version = p.current_version WHERE p.id = :p FOR UPDATE OF p"
        ),
        {"p": FULL_PRESET_ID},
    ).one_or_none()
    if head is None:  # the operator purged it: Full is not converted
        return
    previous, new_version = int(head.current_version), int(head.current_version) + 1
    before: ProfileSettings = merged_settings(defaults, dict(head.settings))
    after: ProfileSettings = merged_settings(defaults, full_settings())
    gained = schema.data_increases(before, after)
    old_values = flatten(ProfileSettings, before.model_dump(mode="json"))
    new_values = flatten(ProfileSettings, after.model_dump(mode="json"))
    tags = schema.setting_tags()
    changed = sorted(path for path in new_values if new_values[path] != old_values.get(path))
    classes = {tags[path].change_class for path in changed}
    version_class = (
        ConfigClass.interpretive if ConfigClass.interpretive in classes else ConfigClass.prospective
    )

    pinned = _pinned_stories(gained)
    if pinned:
        bind.execute(
            sa.text(
                "UPDATE stories SET engine_profile_version_pin = :v "
                "WHERE id = ANY(:ids) AND engine_profile_version_pin IS NULL"
            ),
            {"v": previous, "ids": pinned},
        )
    bind.execute(
        sa.text(
            "INSERT INTO engine_profile_versions "
            "(profile_id, version, settings, created_by, created_at) "
            "VALUES (:p, :v, CAST(:s AS jsonb), :by, transaction_timestamp())"
        ),
        {"p": FULL_PRESET_ID, "v": new_version, "s": json.dumps(full_settings()), "by": ACTOR},
    )
    bind.execute(
        sa.text(
            "UPDATE engine_profiles SET current_version = :v, name = :n, preset = true, "
            "updated_at = transaction_timestamp() WHERE id = :p"
        ),
        {"p": FULL_PRESET_ID, "v": new_version, "n": FULL_NAME},
    )
    in_use = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM stories) OR EXISTS (SELECT 1 FROM config_changes)")
    ).scalar_one()
    if not in_use:  # a fresh deployment: Full is its seed, like the presets beside it
        return
    revision_number = int(
        bind.execute(
            sa.text(
                "UPDATE deployment_state SET config_revision = config_revision + 1 "
                "RETURNING config_revision"
            )
        ).scalar_one()
    )
    changes = _Changes(revision_number)
    changes.record(_setting(), previous, new_version, version_class)
    for path in changed:
        changes.record(
            _setting(path), old_values.get(path), new_values[path], tags[path].change_class
        )
    for story_id in pinned:
        changes.record(
            PIN_SETTING,
            None,
            previous,
            ConfigClass.interpretive,
            scope="story",
            scope_id=story_id,
        )


def upgrade() -> None:
    defaults = _rewrite_defaults(_added)
    _convert_full(defaults)
    _insert_preset(STANDARD_PRESET_ID, STANDARD_NAME, standard_settings())
    _insert_preset(MAX_PRESET_ID, MAX_NAME, max_settings())


def downgrade() -> None:
    bind = op.get_bind()
    presets = [STANDARD_PRESET_ID, MAX_PRESET_ID]
    edited = bind.execute(
        sa.text("SELECT count(*) FROM engine_profiles WHERE id = ANY(:p) AND current_version > 1"),
        {"p": presets},
    ).scalar_one()
    used = bind.execute(
        sa.text("SELECT count(*) FROM stories WHERE engine_profile_id = ANY(:p)"),
        {"p": presets},
    ).scalar_one()
    if edited or used:
        raise RuntimeError(
            f"{STANDARD_NAME} or {MAX_NAME} was edited or is used by {used} stories: "
            "move them off before downgrading"
        )
    bind.execute(
        sa.text("DELETE FROM engine_profile_versions WHERE profile_id = ANY(:p)"), {"p": presets}
    )
    bind.execute(sa.text("DELETE FROM engine_profiles WHERE id = ANY(:p)"), {"p": presets})
    written = bind.execute(
        sa.text(
            "SELECT version FROM engine_profile_versions WHERE profile_id = :p AND created_by = :a"
        ),
        {"p": FULL_PRESET_ID, "a": ACTOR},
    ).scalar_one_or_none()
    if written is not None:
        previous = int(written) - 1
        bind.execute(
            sa.text(
                "UPDATE stories SET engine_profile_version_pin = NULL WHERE id IN ("
                "SELECT scope_id FROM config_changes WHERE actor = :a AND setting = :s)"
            ),
            {"a": ACTOR, "s": PIN_SETTING},
        )
        on_written = bind.execute(
            sa.text(
                "SELECT count(*) FROM stories "
                "WHERE engine_profile_id = :p AND engine_profile_version_pin = :v"
            ),
            {"p": FULL_PRESET_ID, "v": int(written)},
        ).scalar_one()
        if on_written:
            raise RuntimeError(
                f"{FULL_NAME} version {written} is pinned by {on_written} stories: move them to "
                "another version before downgrading"
            )
        bind.execute(
            sa.text(
                "UPDATE engine_profiles SET current_version = :v, name = :n, preset = false, "
                "updated_at = transaction_timestamp() WHERE id = :p"
            ),
            {"p": FULL_PRESET_ID, "v": previous, "n": STRUCTURED_NAME},
        )
        bind.execute(
            sa.text("DELETE FROM engine_profile_versions WHERE profile_id = :p AND version = :v"),
            {"p": FULL_PRESET_ID, "v": int(written)},
        )
    bind.execute(sa.text("DELETE FROM config_changes WHERE actor = :a"), {"a": ACTOR})
    _rewrite_defaults(_removed)
