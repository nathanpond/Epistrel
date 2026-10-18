"""Memory records (FR-MEM-1,2,4,5,7, FR-STORE-9,13; docs/02 §4.2; #135).

- ``records``: the ``Memory`` model, the episodic writer ``write_episodic`` (called inside a
  turn's commit transaction), ``working_memory`` (derived from events, never persisted),
  ``decayed_importance`` (pure), the importance writer the scoring job commits through, and the
  view-through-N read ``memories_through``.
"""
