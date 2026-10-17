"""the Structured (M4) profile, and M4's settings in the profile defaults (#129)

M4 delivers the ``structured`` pipeline, episodic retrieval, consolidation, verification,
contradiction checking, commitment and the repair attempts (``core.profiles.schema.DELIVERED``).

- ``Structured (M4)`` (``epistrel.core.seeds.profiles``) is inserted with the fixed id
  ``STRUCTURED_M4_PROFILE_ID`` at version 1, owned by ``system`` and not a preset, with every
  module M4 delivers on. It is an ordinary profile: edited, copied and deleted like any other.
  It does not become the deployment's default.
- ``deployment_state.profile_defaults`` must name every delivered setting (#113): each setting
  M4 delivers that it does not name yet is added at its off value (``M4_PROFILE_DEFAULTS``). A
  value already there is kept, and nothing undelivered is written.

No ``config_changes`` row is written and ``config_revision`` does not move: every story's
effective configuration is what it was (the added defaults are the schema's own).

Downgrade removes the profile (refused while a story runs under it or it is the deployment's
default) and the added settings from the defaults.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-16

"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.seeds.profiles import (
    M4_PROFILE_DEFAULTS,
    SEED_PRINCIPAL,
    STRUCTURED_M4_NAME,
    STRUCTURED_M4_PROFILE_ID,
    structured_m4_settings,
)

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.transaction_timestamp()


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
            {"d": json.dumps(change(dict(row.profile_defaults), M4_PROFILE_DEFAULTS)), "i": row.id},
        )


def upgrade() -> None:
    heads = sa.table(
        "engine_profiles",
        sa.column("id"),
        sa.column("name"),
        sa.column("owner_principal"),
        sa.column("preset"),
        sa.column("current_version"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    versions = sa.table(
        "engine_profile_versions",
        sa.column("profile_id"),
        sa.column("version"),
        sa.column("settings", postgresql.JSONB()),
        sa.column("created_by"),
        sa.column("created_at"),
    )
    op.execute(
        heads.insert().values(
            id=STRUCTURED_M4_PROFILE_ID,
            name=STRUCTURED_M4_NAME,
            owner_principal=SEED_PRINCIPAL,
            preset=False,
            current_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    op.execute(
        versions.insert().values(
            profile_id=STRUCTURED_M4_PROFILE_ID,
            version=1,
            settings=structured_m4_settings(),
            created_by=SEED_PRINCIPAL,
            created_at=NOW,
        )
    )
    _rewrite_defaults(_added)


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM stories WHERE engine_profile_id = :p) + "
            "(SELECT count(*) FROM deployment_state WHERE default_engine_profile_id = :p)"
        ),
        {"p": STRUCTURED_M4_PROFILE_ID},
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"{STRUCTURED_M4_NAME} ({STRUCTURED_M4_PROFILE_ID}) is in use: assign its stories "
            "another profile, and make another the default, before downgrading"
        )
    _rewrite_defaults(_removed)
    bind.execute(
        sa.text("DELETE FROM engine_profiles WHERE id = :p"), {"p": STRUCTURED_M4_PROFILE_ID}
    )
