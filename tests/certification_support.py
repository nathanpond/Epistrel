"""#818's release guard: a model holds Standard, Full and Max certification records at the
presets' **current** fingerprints (ADR-179 as amended by ADR-200).

It is a release gate, not a PR gate. PR CI reports it: the test skips, naming the presets with
no current record, so fingerprint-moving M11 PRs can merge between the baseline (#818) and the
exit certification (#819). ``release.yml`` and ``/n8-verify`` set
``EPISTREL_TEST_RELEASE_GATE=1``, under which it fails instead. (The ``EPISTREL_TEST_`` prefix
is the one ``tests/conftest.py``'s environment scrub keeps.)

The guard asks only that records exist at the current fingerprints, never whether their floors
pass: the floors are the reports' and the records' own business (``test_committed_certifications``).

Under the release gate an owner waiver (ADR-208) in ``docs/evals/release-waivers.toml`` lets the
version in ``pyproject.toml`` release without current records, for the presets it names. The
guard then skips with the waiver's source instead of failing. A waiver never carries over to
another version.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from epistrel.evals.index import Committed, uncertified_presets

RELEASE_GATE_ENV = "EPISTREL_TEST_RELEASE_GATE"

_ROOT = Path(__file__).resolve().parents[1]
WAIVERS_PATH = _ROOT / "docs" / "evals" / "release-waivers.toml"
_WAIVER_KEYS = frozenset({"version", "presets", "decided", "source", "reason"})


@dataclass(frozen=True)
class Waiver:
    """One owner waiver of the release gate (ADR-208): one version, the presets it covers."""

    version: str
    presets: tuple[str, ...]
    decided: str
    source: str
    reason: str


def waivers(path: Path = WAIVERS_PATH) -> list[Waiver]:
    """The committed waivers; each entry must carry exactly the documented keys."""
    if not path.exists():
        return []
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("waiver", [])
    found: list[Waiver] = []
    for entry in entries:
        if set(entry) != _WAIVER_KEYS:
            raise ValueError(f"{path}: a waiver needs exactly {sorted(_WAIVER_KEYS)}: {entry}")
        found.append(
            Waiver(
                version=str(entry["version"]),
                presets=tuple(str(p) for p in entry["presets"]),
                decided=str(entry["decided"]),
                source=str(entry["source"]),
                reason=str(entry["reason"]),
            )
        )
    return found


def project_version() -> str:
    """The version ``pyproject.toml`` declares: the one a release tag must match."""
    document = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(document["project"]["version"])


def waiver_for(
    missing: Sequence[str], version: str, found: Sequence[Waiver] | None = None
) -> Waiver | None:
    """The waiver for exactly ``version`` that covers every preset in ``missing``, if any."""
    for waiver in waivers() if found is None else found:
        if waiver.version == version and set(missing) <= set(waiver.presets):
            return waiver
    return None


def release_gate() -> bool:
    """Whether the guard fails (release, verify) rather than skips (PR CI)."""
    return os.environ.get(RELEASE_GATE_ENV) == "1"


def check_presets_certified_at_current_fingerprints(
    found: list[Committed] | None = None,
    *,
    enforce: bool | None = None,
    version: str | None = None,
    found_waivers: Sequence[Waiver] | None = None,
) -> None:
    """Pass when one model has records at the current fingerprints of every certified preset;
    otherwise, under the release gate, skip when an owner waiver covers this version and those
    presets (ADR-208) and fail when none does; outside the gate, skip naming the presets."""
    missing = uncertified_presets(found)
    if not missing:
        return
    message = (
        "no model has certification records at the current fingerprints of "
        f"{', '.join(missing)} (#818; ADR-179 as amended by ADR-200)"
    )
    if enforce if enforce is not None else release_gate():
        release = project_version() if version is None else version
        waiver = waiver_for(missing, release, found_waivers)
        if waiver is None:
            pytest.fail(message)
        pytest.skip(
            f"WAIVED for {release} by the owner on {waiver.decided} (ADR-208, {waiver.source}); "
            f"this release ships uncertified: {message}"
        )
    pytest.skip(f"release gate ({RELEASE_GATE_ENV}=1 enforces it): {message}")
