"""The repair ladder's deterministic rungs and its classification (FR-REPAIR-1,4; #151).

- ``rungs``: sentence spans, rung 1's narrowing (drop the sentences that carry the violating
  claims), rung 2's substitution (splice the canon value over the claim's object), the typed
  constraints a regeneration keeps, and ``classify`` (cosmetic / substitution / cascade from
  the rung and the neighborhood's size).

Nothing here calls a model: the ladder itself, which re-detects, re-verifies and regenerates,
is the Orchestrator's (``orchestrator.repair.ladder``).
"""
