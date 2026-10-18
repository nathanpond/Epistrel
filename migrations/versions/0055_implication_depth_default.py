"""the implication closure's depth in the profile defaults (#140)

FR-PROP-11: the implication closure stops at a configurable depth. ``verification.
implication_max_depth`` (an interpretive engine-profile dial, default 3) is that depth, so
``deployment_state.profile_defaults``, which must name every delivered setting (#113), gains it
at the schema's default (``epistrel.core.seeds.profiles.VERIFY_DEPTH_DEFAULTS``) where it does not
name it yet. A value already there is kept.

No ``config_changes`` row is written and ``config_revision`` does not move: every story's
effective configuration is what it was (the added default is the schema's own).

Downgrade removes the setting from the defaults.

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-17

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from epistrel.core.seeds.profiles import VERIFY_DEPTH_DEFAULTS

revision: str = "0055"
down_revision: str | None = "0054"
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


def _rewrite(change: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]) -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id, profile_defaults FROM deployment_state")).all():
        bind.execute(
            sa.text(
                "UPDATE deployment_state SET profile_defaults = CAST(:d AS jsonb) WHERE id = :i"
            ),
            {
                "d": json.dumps(change(dict(row.profile_defaults), VERIFY_DEPTH_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    _rewrite(_added)


def downgrade() -> None:
    _rewrite(_removed)
