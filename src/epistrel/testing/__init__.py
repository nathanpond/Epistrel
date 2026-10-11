"""Test-only scaffolding shipped with the package so tests and the Console can share it.

Nothing in ``epistrel.api.create_app`` or ``epistrel.engine`` imports this package: its routes
exist only where a test registers them (``include_protected``), and its event type is
registered only when it is imported.
"""
