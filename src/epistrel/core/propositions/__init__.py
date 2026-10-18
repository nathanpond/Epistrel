"""Propositions, entities and the predicate registry (FR-PROP; docs/02 §4.9; #131).

- ``model``: the viewpoint-free ``Proposition``, valid-time (``Interval``, ``Instant``) and its
  relations under approximate bounds, typed literals, and ``Predicate`` semantics.
- ``vocabulary``: the core predicates, with fixed ids and documented semantics
  (``docs/predicates.md`` is rendered from it).
- ``registry``: the three layers (core, author, auto), auto-registration, strengthening
  proposals, canonical direction and the bounded implication closure.
- ``store``: the writer of entities, aliases, propositions and closed sets.
- ``codec``: the stored rows.
"""
