"""Creating a story from a scenario (FR-SCN-6,9; docs/02 §4.10): #119's genesis.

``models`` holds the request (its player half, the persona rules, is #114's) and the creation
report; ``create`` the checks, the refusals and the one-transaction genesis operation. #122's
materialisation under Lite (FR-SCN-7,11): ``materialize`` dispatches every field by its class
(render, defer, inactive, consume), ``settings_map`` names each setting's capability, ``merge``
holds the merge rule, and ``render`` the Lite prompt's scenario text and director notes.
``structured`` (#149) materializes a structured story's deferred items: canon, memories,
entities and the author layer.
"""
