"""Engine profiles: named, versioned sets of capability modules and dials, each story running
under one (FR-PROF-1..4; docs/02 §4.11; #106).

``schema`` holds the settings model with every FR-PROF-2 module and its tags, what the
deployment has delivered (``DELIVERED``) and the dependency graph (``DEPENDENCIES``);
``validate`` checks a profile's settings against both; ``store`` holds the profiles and their
versions: create, read, list, edit (a new version), copy, validate, soft delete and the purge.
The Lite preset is ``epistrel.core.seeds.profiles``.
"""
