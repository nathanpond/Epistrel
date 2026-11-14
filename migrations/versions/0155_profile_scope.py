"""profile scope: which relationships a retrieved profile carries (#905)

FR-RET-6, ADR-193: ``deployment_state.profile_defaults`` gains
``memory.episodic.profile_scope`` (``epistrel.core.seeds.profiles.PROFILE_SCOPE_DEFAULTS``)
where it does not name it yet, at the schema's default, ``message``. No ``config_changes`` row is
written, and ``config_revision`` does not move. Unlike 0056's dials, the default is not today's
behaviour: from the next retrieval, a story whose profile does not name the setting reads only
the relationships among the message's people (the owner's decision on #868).

Downgrade removes the setting from the defaults.

Revision ID: 0155
Revises: 0154
Create Date: 2026-11-14

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from epistrel.core.seeds.profiles import PROFILE_SCOPE_DEFAULTS

revision: str = "0155"
down_revision: str | None = "0154"
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
                "d": json.dumps(change(dict(row.profile_defaults), PROFILE_SCOPE_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    _rewrite(_added)


def downgrade() -> None:
    _rewrite(_removed)
