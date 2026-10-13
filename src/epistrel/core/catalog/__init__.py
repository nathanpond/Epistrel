"""The catalog: reusable starting points for stories, outside story data (docs/02 §4.10, FR-CAT,
FR-SCN).

``service`` holds the lifecycle every kind of entry shares: drafts with optimistic concurrency,
immutable numbered versions, archive, delete, and the ``card``/``full`` projections (#103, made
generic by #108). ``characters`` holds catalog characters (#103); ``scenario_models`` and
``scenarios`` hold scenarios: the document with every field's class, the roles and the
starting-cast limit (#108). Running stories never read the catalog; story creation copies from
it, and nothing done to an entry changes a story created from it (FR-CAT-9).
"""
