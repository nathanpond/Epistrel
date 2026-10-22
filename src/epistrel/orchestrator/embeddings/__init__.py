"""The embedding port (FR-EMBED-1..4; docs/02 §6; #138): the adapter contract and its
``openai`` and stub implementations (``adapter``) and the Model Access Layer's embedding side
(``service``, reached as ``ModelAccessLayer.embeddings``). The pin and the vectors are Core's
(``epistrel.core.embeddings``); the jobs that write vectors are
``orchestrator.jobs.handlers.reembed``.
"""
