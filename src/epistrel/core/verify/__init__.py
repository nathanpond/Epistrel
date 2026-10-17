"""Deterministic verification in the Engine Core (FR-PROP-8..12, FR-VERIFY-3; docs/02 §4.9).

- ``rules``: ``check`` applies FR-PROP-10's contradiction rules and coverage logic to a
  normalized claim over the holder's pre-fetched records (pure). ``check_with`` makes the
  keyed fetches through #136's ``KeyedLookup``, and ``verify`` gates on the story's effective
  profile and records the verdict for the turn's rebase re-check (#140).
- ``normalize``: a detected or declared claim sketch becomes a ``Proposition``, through #139's
  resolvers and #131's registry, or it is ``Unnormalizable`` with an ``uncertain`` reason (#140).
- ``merge``: the fact merge by seed layer at structured genesis, decided by ``check`` (#140,
  called by #149).

Nothing here calls a model: escalation of the ``uncertain`` residual is the Orchestrator's
(#142).
"""
