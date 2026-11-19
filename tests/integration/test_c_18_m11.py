"""C-18 M11 (NFR-QUAL-11; #818): a certified model is one with Standard, Full and Max
certification records **at the presets' current fingerprints** (ADR-179 as amended by ADR-200),
and the fingerprint covers the engine's model-call revision.

The guard is ``tests.certification_support``'s, the same one
``tests/evals/test_certify_presets.py`` runs. It is a release gate: in PR CI it skips and names
the stale presets, and under ``EPISTREL_TEST_RELEASE_GATE=1`` (``release.yml``, ``/n8-verify``)
it fails. It needs no database; the records and the presets' fingerprints are read from the
repository and the code. The part also runs the model-call revision guard (ADR-200).
"""

from __future__ import annotations

import pytest

from epistrel.core.profiles.fingerprint import MODEL_CALL_REVISION
from epistrel.evals.index import current_fingerprint, records
from tests import model_call_support
from tests.certification_support import check_presets_certified_at_current_fingerprints


@pytest.mark.part("M11", "NFR-QUAL-11")
def test_c_18__certified_at_current_fingerprints() -> None:
    check_presets_certified_at_current_fingerprints()


@pytest.mark.part("M11", "NFR-QUAL-11")
def test_c_18__a_record_from_another_model_call_revision_is_stale() -> None:
    """A record made under an older ``MODEL_CALL_REVISION`` no longer matches: the current
    fingerprint differs from the one computed at the record's models with the revision moved."""
    found = [c for c in records() if c.record.preset == "full"]
    assert found, "no committed Full record"
    models = found[0].record.models
    now = current_fingerprint("full", models)
    before = current_fingerprint("full", models, model_call_revision=MODEL_CALL_REVISION - 1)
    assert before != now


@pytest.mark.part("M11", "NFR-QUAL-11")
def test_c_18__the_model_call_revision_moves_with_what_a_call_sends() -> None:
    """ADR-200: the guarded set (``tests.model_call_support``) equals its snapshot, which pins
    the current ``MODEL_CALL_REVISION`` (``tests/test_model_call_revision.py`` has the rest)."""
    model_call_support.check_snapshot()
