"""#818's release guard: a model holds Standard, Full and Max certification records at the
presets' **current** fingerprints (ADR-179 as amended by ADR-200).

It is a release gate, not a PR gate. PR CI reports it: the test skips, naming the presets with
no current record, so fingerprint-moving M11 PRs can merge between the baseline (#818) and the
exit certification (#819). ``release.yml`` and ``/n8-verify`` set
``EPISTREL_TEST_RELEASE_GATE=1``, under which it fails instead. (The ``EPISTREL_TEST_`` prefix
is the one ``tests/conftest.py``'s environment scrub keeps.)

The guard asks only that records exist at the current fingerprints, never whether their floors
pass: the floors are the reports' and the records' own business (``test_committed_certifications``).
"""

from __future__ import annotations

import os

import pytest

from epistrel.evals.index import Committed, uncertified_presets

RELEASE_GATE_ENV = "EPISTREL_TEST_RELEASE_GATE"


def release_gate() -> bool:
    """Whether the guard fails (release, verify) rather than skips (PR CI)."""
    return os.environ.get(RELEASE_GATE_ENV) == "1"


def check_presets_certified_at_current_fingerprints(
    found: list[Committed] | None = None, *, enforce: bool | None = None
) -> None:
    """Pass when one model has records at the current fingerprints of every certified preset;
    otherwise fail under the release gate, else skip naming the presets that lack one."""
    missing = uncertified_presets(found)
    if not missing:
        return
    message = (
        "no model has certification records at the current fingerprints of "
        f"{', '.join(missing)} (#818; ADR-179 as amended by ADR-200)"
    )
    if enforce if enforce is not None else release_gate():
        pytest.fail(message)
    pytest.skip(f"release gate ({RELEASE_GATE_ENV}=1 enforces it): {message}")
