"""``MODEL_CALL_REVISION`` moves with every change to what a model call sends (#818, owner's
R1 (a); ADR-200; NFR-QUAL-11, FR-PROF-16).

The guarded set is ``tests.model_call_support``'s: the Python-held prompt strings and
request-shaping constants of every model-calling module, the purposes' schemas and the
structured-mode choice. The snapshot pins each entry's hash **and** the revision, so a change
to one fails until both move: bump ``MODEL_CALL_REVISION`` in
``core/profiles/fingerprint.py``, then ``uv run python -m tests.model_call_support --write``.

No test here carries a part marker: C-18's M11 part (``tests/integration/test_c_18_m11.py``)
runs the same check, and a part lives in one file.

Not covered: behaviour reached only through data (a model-config row, a stored policy, a
story's settings), which the deployment owns or the fingerprint already carries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from epistrel.core.profiles.fingerprint import MODEL_CALL_REVISION, RuntimeFacts, fingerprint
from epistrel.core.seeds.profiles import FULL_PRESET_ID
from epistrel.evals.presets import PRESETS
from tests import model_call_support as support


def test_the_guarded_set_matches_the_snapshot_at_the_current_revision() -> None:
    support.check_snapshot()


def test_the_revision_is_a_fingerprint_input() -> None:
    chosen = PRESETS["full"]
    assert chosen.profile_id == FULL_PRESET_ID
    facts = RuntimeFacts(models={}, templates={})
    bumped = RuntimeFacts(models={}, templates={}, model_call_revision=MODEL_CALL_REVISION + 1)
    settings = chosen.effective()
    base = fingerprint(profile_id=chosen.profile_id, version=1, settings=settings, facts=facts)
    moved = fingerprint(profile_id=chosen.profile_id, version=1, settings=settings, facts=bumped)
    assert base != moved
    assert facts.model_call_revision == MODEL_CALL_REVISION


def test_the_writer_refuses_a_changed_set_without_a_bump(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    support.write_snapshot(path)
    document = support.read_snapshot(path)
    document["entries"]["const:example.NAME"] = "0" * 64
    path.write_text(json.dumps(document), "utf-8")
    with pytest.raises(SystemExit, match="bump it first"):
        support.write_snapshot(path)


def test_a_new_model_caller_joins_the_guarded_set() -> None:
    modules = support.guarded_modules()
    assert "epistrel.orchestrator.structured.render" in modules
    assert "epistrel.orchestrator.models.stub" not in modules
    assert set(support.EXTRA_MODULES) <= set(modules)
