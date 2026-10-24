"""the seeded profile renamed ``Structured`` with M5's modules at version 2; M5's settings in the
profile defaults (#181)

M5 delivers per-character partitions, nested beliefs, lazy resolution, testimony adjudication,
perception routing and per-character renders (``core.profiles.schema.DELIVERED``).

- ``deployment_state.profile_defaults`` must name every delivered setting (#113): each setting M5
  delivers that it does not name yet is added at its off value (``M5_PROFILE_DEFAULTS``). A value
  already there is kept.
- The seeded ``Structured (M4)`` profile (``STRUCTURED_M4_PROFILE_ID``) is updated in place, the
  owner's decision: renamed ``Structured``, with a new version (``STRUCTURED_SETTINGS``, the next
  after its current one: 2 on a deployment that never edited it) that turns every M5 module on.
  The id is unchanged.
- Every story that follows the profile's latest version is pinned to that version first
  (``stories.engine_profile_version_pin``, #109's pin), so no existing story's configuration
  changes: a story moves to version 2 only through ``PUT /stories/{id}/engine-profile`` with
  ``version: latest``, which runs the two-stage impact report.

No ``config_changes`` row is written and ``config_revision`` does not move, as ``0047`` did: no
story's effective configuration changes (the added defaults are the schema's own, and the stories
that followed the profile keep the version they ran on).

Downgrade restores the name and the previous version as the current one, unpins the stories this
pinned, removes the added version (refused while a story runs pinned to it) and the added
settings from the defaults.

Revision ID: 0080
Revises: 0079
Create Date: 2026-10-23

"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from epistrel.core.seeds.profiles import (
    M5_PROFILE_DEFAULTS,
    SEED_PRINCIPAL,
    STRUCTURED_M4_NAME,
    STRUCTURED_NAME,
    STRUCTURED_PROFILE_ID,
    structured_settings,
)

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _added(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    """``stored`` with every leaf of ``seed`` it does not name added."""
    out = dict(stored)
    for key, value in seed.items():
        if isinstance(value, Mapping):
            inner = out.get(key)
            out[key] = _added(inner if isinstance(inner, Mapping) else {}, value)
        elif key not in out:
            out[key] = value
    return out


def _removed(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    """``stored`` without the leaves ``seed`` names (and the groups they leave empty)."""
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


def _rewrite_defaults(change: Any) -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id, profile_defaults FROM deployment_state")).all():
        bind.execute(
            sa.text(
                "UPDATE deployment_state SET profile_defaults = CAST(:d AS jsonb) WHERE id = :i"
            ),
            {"d": json.dumps(change(dict(row.profile_defaults), M5_PROFILE_DEFAULTS)), "i": row.id},
        )


def upgrade() -> None:
    _rewrite_defaults(_added)
    bind = op.get_bind()
    current = bind.execute(
        sa.text("SELECT current_version FROM engine_profiles WHERE id = :p FOR UPDATE"),
        {"p": STRUCTURED_PROFILE_ID},
    ).scalar_one_or_none()
    if current is None:  # the operator purged it: nothing to update
        return
    bind.execute(
        sa.text(
            "UPDATE stories SET engine_profile_version_pin = :v "
            "WHERE engine_profile_id = :p AND engine_profile_version_pin IS NULL"
        ),
        {"p": STRUCTURED_PROFILE_ID, "v": int(current)},
    )
    bind.execute(
        sa.text(
            "INSERT INTO engine_profile_versions "
            "(profile_id, version, settings, created_by, created_at) "
            "VALUES (:p, :v, CAST(:s AS jsonb), :by, transaction_timestamp())"
        ),
        {
            "p": STRUCTURED_PROFILE_ID,
            "v": int(current) + 1,
            "s": json.dumps(structured_settings()),
            "by": SEED_PRINCIPAL,
        },
    )
    bind.execute(
        sa.text(
            "UPDATE engine_profiles SET name = :n, current_version = :v, "
            "updated_at = transaction_timestamp() WHERE id = :p"
        ),
        {"p": STRUCTURED_PROFILE_ID, "n": STRUCTURED_NAME, "v": int(current) + 1},
    )


def downgrade() -> None:
    bind = op.get_bind()
    current = bind.execute(
        sa.text("SELECT current_version FROM engine_profiles WHERE id = :p FOR UPDATE"),
        {"p": STRUCTURED_PROFILE_ID},
    ).scalar_one_or_none()
    if current is not None and int(current) > 1:
        added, previous = int(current), int(current) - 1
        pinned = bind.execute(
            sa.text(
                "SELECT count(*) FROM stories "
                "WHERE engine_profile_id = :p AND engine_profile_version_pin = :v"
            ),
            {"p": STRUCTURED_PROFILE_ID, "v": added},
        ).scalar_one()
        if pinned:
            raise RuntimeError(
                f"{STRUCTURED_NAME} ({STRUCTURED_PROFILE_ID}) version {added} is pinned by "
                f"{pinned} stories: move them to another version before downgrading"
            )
        bind.execute(
            sa.text(
                "UPDATE engine_profiles SET name = :n, current_version = :v, "
                "updated_at = transaction_timestamp() WHERE id = :p"
            ),
            {"p": STRUCTURED_PROFILE_ID, "n": STRUCTURED_M4_NAME, "v": previous},
        )
        bind.execute(
            sa.text(
                "UPDATE stories SET engine_profile_version_pin = NULL "
                "WHERE engine_profile_id = :p AND engine_profile_version_pin = :v"
            ),
            {"p": STRUCTURED_PROFILE_ID, "v": previous},
        )
        bind.execute(
            sa.text("DELETE FROM engine_profile_versions WHERE profile_id = :p AND version = :v"),
            {"p": STRUCTURED_PROFILE_ID, "v": added},
        )
    _rewrite_defaults(_removed)
