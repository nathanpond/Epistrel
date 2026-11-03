"""The history-rewrite side-channel (FR-REWRITE-1..12, FR-DIRECT-3; #223 onward).

- ``api``: the request and response shapes of ``/stories/{id}/rewrites``;
- ``parse``: the ``rewrite_parse`` call and its normalization into #221/#236 part drafts, with
  the clarification questions it asks instead of guessing;
- ``cascade``: ``CASCADE_RULES``, how each dependency edge type is classified;
- ``plan``: the planners (``PLANNERS``, by pipeline), the blast radius and the preview;
- ``session``: the session state machine, its answers, cancel, list and expiry;
- ``templates``: the fixed prompts and recommendation reasons.
"""
