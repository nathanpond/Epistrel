"""Consumer purposes (FR-MODEL-14; #824): model purposes a consumer registers through the
operator API, each a named prompt template and output schema, stored and versioned in the
database rather than declared in the Orchestrator's purpose registry (ADR-185).

- ``validation``: what a purpose may hold (name, sandboxed template, JSON Schema subset),
  checked at save.
- ``output``: an output schema compiled to a pydantic model.
- ``store``: register, replace (a new version), archive, list and read.

Resolution and routing are the Orchestrator's (``orchestrator.models.consumer``).
"""
