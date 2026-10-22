"""The deterministic resolution front end (FR-PROP-6,9, FR-RET-2; docs/02 §4.9; #139).

Before any search or verdict, text is resolved: mentions to entity ids (``entities``), relative
time to story-time (``time``), occasion words to event types (``events``). The resolvers are
pure and never call a model (the ``resolution-calls-no-model`` import contract);
``context`` loads their inputs once per operation. Every unresolved result is an ``Uncertain``
with a ``Reason`` (``types``), never a guess. #140's normalization, #141's retrieval and #148's
detectors reach entity ids and story-time only through here.
"""
