"""The checks of a catalog validation (FR-SCN-5; #111), one module per check.

``base`` holds what every check shares: the ``Subject`` it validates, the two phases' contexts
and the ``Check`` base class. ``core.catalog.validation`` runs them, in the explicit order of its
``CHECKS`` list:

``document`` → ``required`` → ``cast_limit`` → ``role_characters`` → ``eligibility`` →
``seeds`` → ``media`` → ``providers`` → ``rating_mismatch`` → ``input_policy`` →
``personality`` → ``seed_normalization`` → ``fact_consistency`` → ``state_schema``.

Each check names the entry types it applies to (``kinds``) and the capability it needs
(``capability``; ``None`` for the always-run checks). ``scenario``, ``cast_members`` and
``character_documents`` are helpers the checks share, not checks.
"""
