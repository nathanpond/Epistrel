"""Engine profiles: named, versioned sets of capability modules and dials, each story running
under one (FR-PROF-1..8,16; docs/02 §4.11; #106, #109).

``schema`` holds the settings model with every FR-PROF-2 module and its tags, what the
deployment has delivered (``DELIVERED``), the dependency graph (``DEPENDENCIES``) and what makes
a change data-producing; ``validate`` checks a profile's settings against both; ``store`` holds
the profiles and their versions: create, read, list, edit (a new version, in two stages), copy,
validate, soft delete and the purge. ``effective`` resolves the configuration a story runs under
(its profile at its pinned or latest version); ``fingerprint`` identifies a configuration for
quality purposes, with its capabilities, quality status and guarantees; ``impact`` builds impact
reports and runs the two-stage flow every profile-affecting change passes through; and
``estimates`` holds the constants of a re-derivation's estimate. The Lite preset is
``epistrel.core.seeds.profiles``.
"""
