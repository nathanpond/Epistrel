"""CONFIDENCE-PROJECTION: a belief's dual confidence never contradicts itself (docs/04 §6;
FR-BELIEF-4; #160).

Over random levels and scalars (300 examples): whenever a scalar is present the level written is
its projection by the fixed bands (``[0, 0.2)`` unknown, ``[0.2, 0.5)`` suspected, ``[0.5, 0.9)``
believed, ``[0.9, 1]`` known); a level given with a scalar it does not project onto is refused
(422 ``confidence_inconsistent``), and one it does is accepted; a level alone is valid and keeps
no scalar; neither takes the kind's default. Complement: a scalar outside ``[0, 1]`` or with more
than three decimals is refused before projection (422 ``validation_error``). The decay step keeps
the projection too: a forgotten scalar's level is the projection of the halved scalar.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.enums import BeliefHoldState as Hold
from epistrel.core.epistemic.errors import ConfidenceInconsistent
from epistrel.core.epistemic.transitions import (
    Confidence,
    Kind,
    decayed,
    project,
    resolve_confidence,
)
from epistrel.core.epistemic.transitions import HeldState as Held
from epistrel.core.errors import InvalidRequest

Level = BeliefConfidenceLevel


def band(scalar: float) -> Level:
    """FR-BELIEF-4's bands, stated on their own."""
    if scalar < 0.2:
        return Level.UNKNOWN
    if scalar < 0.5:
        return Level.SUSPECTED
    if scalar < 0.9:
        return Level.BELIEVED
    return Level.KNOWN


scalars = st.integers(0, 1000).map(lambda n: n / 1000)


@pytest.mark.part("M5", "FR-BELIEF-4")
@settings(max_examples=300, deadline=None)
@given(
    kind=st.sampled_from(list(Kind)),
    level=st.one_of(st.none(), st.sampled_from(list(Level))),
    scalar=st.one_of(st.none(), scalars),
)
def test_confidence_projection(kind: Kind, level: Level | None, scalar: float | None) -> None:
    if scalar is not None and level is not None and band(scalar) is not level:
        with pytest.raises(ConfidenceInconsistent) as refused:
            resolve_confidence(kind, Confidence(level, scalar))
        assert (refused.value.code, refused.value.http_status) == ("confidence_inconsistent", 422)
        assert refused.value.details["projected"] == band(scalar).value
        return
    written, kept = resolve_confidence(kind, Confidence(level, scalar))
    if scalar is not None:
        assert (written, kept) == (band(scalar), scalar)
        assert project(scalar) is band(scalar)
        # The decay step keeps the projection.
        faded = decayed(Held(written, kept, 1.0, Hold.HELD))
        assert faded.scalar is not None
        assert faded.level is band(faded.scalar)
    elif level is not None:
        assert (written, kept) == (level, None)
    else:
        default = Level.KNOWN if kind in (Kind.OBSERVATION, Kind.AUTHOR) else Level.BELIEVED
        assert (written, kept) == (default, None)


@pytest.mark.part("M5", "FR-BELIEF-4")
@pytest.mark.parametrize(
    ("scalar", "reason"), [(-0.1, "out_of_range"), (1.5, "out_of_range"), (0.1234, "precision")]
)
def test_confidence_projection__malformed_scalar(scalar: float, reason: str) -> None:
    with pytest.raises(InvalidRequest) as refused:
        resolve_confidence(Kind.TESTIMONY, Confidence(scalar=scalar))
    assert (refused.value.code, refused.value.details["reason"]) == ("validation_error", reason)
