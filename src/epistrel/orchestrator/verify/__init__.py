"""Verification in the Orchestrator (FR-VERIFY-1,3,8; docs/02 §4.9; #142).

- ``verifier``: ``Verifier.verify`` runs #140's deterministic check first, settles the
  ``uncertain`` residual through the gate, and asks the ``entailment`` model only about what
  the gate escalates, validating every answer; an undecided claim takes the profile's safe
  disposition, never a silent ``supported``.
- ``gate``: the escalation gate, a tunable confidence threshold per predicate shape.

The dependency cluster a repair reads (``fact_neighborhood``, FR-VERIFY-6) is the Engine
Core's: ``core.verify.neighborhood``.
"""
