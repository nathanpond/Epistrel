"""E-06: generation conformance (docs/04), the M2 part: Lite output honours the prompt
parameters on a real model. Marked ``live_model``: CI never runs it; the model matrix tool runs
the same check (``capability.check_prompt_params``) and records it per tier, reported, not
gated (#54)."""

from __future__ import annotations

import pytest

from epistrel.orchestrator.models.capability import E06_THRESHOLD, check_prompt_params
from tests.live_support import live_layer


@pytest.mark.live_model
@pytest.mark.part("M2", "FR-RESP-3")
async def test_e_06() -> None:
    """For each ``max_tokens`` length the reply's ``tokens_out`` stays within it, and at
    temperature 0 at least 80 % of 10 samples are identical.

    Complement asserted: the lengths differ, so the cap is what bounds each reply."""
    layer = live_layer()
    try:
        result = await check_prompt_params(layer)
    finally:
        await layer.aclose()
    assert len(set(result.lengths)) > 1
    for cap, tokens_out in result.lengths.items():
        assert tokens_out is not None, f"no answer at max_tokens={cap}"
        assert tokens_out <= cap
    assert result.identical / result.samples >= E06_THRESHOLD
