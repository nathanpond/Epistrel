"""Objective canon (FR-BITEMP-1..5, FR-STORE-9; docs/02 §4.4; #133).

- ``facts``: the bitemporal fact store's primitives: ``commit_fact`` and ``supersede`` (the
  writes #144, #145, #146 and #149 call), ``canon`` with ``canon_at`` and
  ``canon_as_recorded`` (the two time questions), the visibility filter and ``hidden_from``.
- ``errors``: their errors.
"""
