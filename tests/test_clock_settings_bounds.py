"""The clock's integer settings stop at the ``INTEGER`` column's maximum (#193).

``PATCH /stories/{id}/clock`` with ``max_unprompted_skip_minutes: 2147483648`` was a 500 (the
value overflowed its column; C-01 found it). The request and the stored settings now refuse it
as a validation error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epistrel.api.routes.clock import ClockPatch
from epistrel.core.states.model import PG_INT_MAX, ClockSettings


@pytest.mark.parametrize("field", ["max_unprompted_skip_minutes"])
def test_the_patch_refuses_a_value_past_the_column(field: str) -> None:
    assert getattr(ClockPatch(**{field: PG_INT_MAX}), field) == PG_INT_MAX
    with pytest.raises(ValidationError):
        ClockPatch(**{field: PG_INT_MAX + 1})


@pytest.mark.parametrize(
    "field", ["max_unprompted_skip_minutes", "grace_minutes", "time_statement_tolerance_minutes"]
)
def test_the_settings_refuse_a_value_past_the_column(field: str) -> None:
    assert getattr(ClockSettings(**{field: PG_INT_MAX}), field) == PG_INT_MAX
    with pytest.raises(ValidationError):
        ClockSettings(**{field: PG_INT_MAX + 1})
