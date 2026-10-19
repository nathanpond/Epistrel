"""What a backfill would cost, for an impact report's estimate (FR-PROF-7,9; #109).

M3 has no backfill (FR-PROF-9 arrives in M7), but a report must say what one would take for a
story that cannot gain a data-producing capability without it (``upgrade_unavailable``): its
model calls, tokens and duration. The estimate reads the story's transcript length and these
constants, never ``docs/`` (which the image does not ship):

- the history is read in chunks of ``CHUNK_MESSAGES`` transcript messages, one model call per
  chunk for each module the change adds;
- a chunk's tokens are its characters over ``CHARS_PER_TOKEN``, read once per module;
- the calls run one after another, each taking ``CALL_LATENCY_MS``.

``CALL_LATENCY_MS`` comes from the model matrix (``docs/model-matrix.json``, #54's
measurements): each row's median turn latency over the model calls a turn made, and the largest
of those, so an estimate errs long. At 2026-10-12 that is ``cydonia-24b-v4.3`` on the local
tier with no model-assisted policy: 13,398 ms at one generation call per turn.
``tests/test_profile_facts.py`` derives it from the matrix again, so a re-measured matrix fails
until the constant follows it.
"""

from __future__ import annotations

import math

CHUNK_MESSAGES = 30
"""Transcript messages one backfill call reads (FR-PROF-9's scene-sized chunks)."""
CHARS_PER_TOKEN = 4
"""Characters per token, the usual rough rule for English text."""
CALL_LATENCY_MS = 11_868
"""One model call's latency, in milliseconds (see the module docstring)."""


def backfill_cost(messages: int, characters: int, modules: int) -> tuple[int, int, float]:
    """``(model_calls, tokens, duration_s)`` of re-deriving ``modules`` modules' data from a
    transcript of ``messages`` messages and ``characters`` characters."""
    if messages <= 0 or modules <= 0:
        return 0, 0, 0.0
    calls = math.ceil(messages / CHUNK_MESSAGES) * modules
    tokens = (characters // CHARS_PER_TOKEN) * modules
    return calls, tokens, calls * CALL_LATENCY_MS / 1000


__all__ = ["CALL_LATENCY_MS", "CHARS_PER_TOKEN", "CHUNK_MESSAGES", "backfill_cost"]
