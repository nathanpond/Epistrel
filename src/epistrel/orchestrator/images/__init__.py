"""Image generation (FR-IMG; docs/02 §4.13; epic #27).

- ``port``: the ``ImageGenerator`` port every image adapter implements (#254): ``submit``,
  ``collect``, ``capabilities`` and, for adapters that declare ``confirms_completion``,
  ``status`` and ``cancel``; the request and result types; ``Capabilities``.
- ``params``: the ``image_generation`` port's ``params`` (``ImageGenerationParams``), stored in
  its ``model_config`` row and written through the shared ``PUT /admin/model-config/{purpose}``
  body (owner decision 11).
- ``registry``: ``register_image_adapter()`` and its three rejection rules, the adapter built
  from the current model configuration (``get_adapter`` / ``require_image_adapter``, 409
  ``not_configured`` without one), and the ``image_check`` fail-closed helper
  (``ensure_image_check_configured``).

The bundled OpenRouter adapter (#255) lives in ``adapters/`` and registers through
``registry.register_builtin_image_adapters()``. This package's ``__init__`` imports nothing, so
``orchestrator.models.config`` can import ``params`` without a cycle.
"""
