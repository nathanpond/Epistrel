"""the verification escalation dials in the profile defaults (#142)

FR-VERIFY-3,8: the Orchestrator escalates the deterministic check's ``uncertain`` residual to the
entailment model and validates the answer. Two engine-profile dials (interpretive) join
``deployment_state.profile_defaults``, which must name every delivered setting (#113), at the
schema's defaults (``epistrel.core.seeds.profiles.ESCALATION_DEFAULTS``) where it does not name
them yet: ``verification.entailment_min_confidence`` (0.6) and ``verification.safe_disposition``
(``repair``, the owner's decision on #142). A value already there is kept.

``verification.entailment_gate``'s schema default moves from 0.5 to 1.0 (every ``uncertain``
verdict escalates; the replan of 2026-10-16). A stored 0.5, the default the unreleased ``0047``
seeded, becomes 1.0; any other value is a deployment's choice and is kept. The downgrade leaves
the gate where it is.

No ``config_changes`` row is written and ``config_revision`` does not move: the added dials are
the schema's own defaults, and the gate is prospective (a verify reads it when it runs).

Downgrade removes the two dials from the defaults.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-17

"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from epistrel.core.seeds.profiles import ESCALATION_DEFAULTS, PREVIOUS_ENTAILMENT_GATE

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTAILMENT_GATE = 1.0


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


def _upgraded(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    out = _added(stored, seed)
    verification = out.get("verification")
    if isinstance(verification, Mapping):
        gate = verification.get("entailment_gate")
        if isinstance(gate, int | float) and gate == PREVIOUS_ENTAILMENT_GATE:
            out["verification"] = {**verification, "entailment_gate": ENTAILMENT_GATE}
    return out


def _rewrite(change: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]) -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id, profile_defaults FROM deployment_state")).all():
        bind.execute(
            sa.text(
                "UPDATE deployment_state SET profile_defaults = CAST(:d AS jsonb) WHERE id = :i"
            ),
            {
                "d": json.dumps(change(dict(row.profile_defaults), ESCALATION_DEFAULTS)),
                "i": row.id,
            },
        )


def upgrade() -> None:
    _rewrite(_upgraded)


def downgrade() -> None:
    _rewrite(_removed)
