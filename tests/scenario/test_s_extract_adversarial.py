"""S-EXTRACT-ADVERSARIAL, M4 part: valid structure, hidden claims (FR-VERIFY-13,14; docs/04 §7;
#148). One file per scenario; the M5 part (protected content in a ``say``, reactive narration)
and the M6 part join it.

Each render is structurally complete with empty or incomplete declarations. (a) Linda's ``do``
"She pockets the key on her way out" declares no effect: the possession detector finds the claim
that Linda holds the key, and since canon says Marcus holds it (an item has one holder at a
time: ``held_by`` is functional, #439) it is contradicted, so it goes to repair (#151 rewrites
it). (e) A claim introduced only by the polish pass, "…the ring Marcus had bought her in Paris",
is found in the final composed message (FR-VERIFY-14) by the extraction backstop; it names a
relation and a place canon has no record of, so it cannot be decided and takes the safe
disposition (repair, or the deterministic assembly, #152). The story runs once with
``extraction_mode = always`` (extraction reads every beat: everything is detected) and once with
``gated`` (extraction reads only the flagged beats; a shadow sample, forced with a rate of 1.0,
runs the extraction after the commit over the beat gating skipped, and records the claim gating
missed). The report states detection and checking separately: what was detected, what was
checked, what the sample missed. Detection and checking are separate metric series."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.tables import operations
from epistrel.core.verify import shadow
from epistrel.core.verify.detect import DetectBeat
from epistrel.core.verify.rules import VerdictKind
from tests import detection_support as ds
from tests import verify_support as vs

pytestmark = pytest.mark.integration

POCKETS = "She pockets the key on her way out."
SMILES = "Linda smiles at Marcus."
COMPOSED = "Linda turns over the ring Marcus had bought her in Paris."


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.part("M4", "FR-VERIFY-13,14")
@pytest.mark.parametrize("mode", ["always", "gated"])
async def test_s_extract_adversarial__m4(engine: AsyncEngine, mode: str) -> None:
    """See the module docstring."""
    t = await ds.tavern_story(engine, mode=mode)
    beats = [
        DetectBeat(1, "do", POCKETS, t.linda),
        DetectBeat(2, "narration", SMILES, NARRATOR),
    ]
    mal = vs.stub_models()
    pocketing = {
        "beat": 0,  # the request's markers are 0..n-1 (#459)
        "span": "pockets the key",
        "subject": "Linda",
        "predicate": "holds",
        "object": "key",
    }
    smiling = {
        "beat": 1,
        "span": "smiles at Marcus",
        "subject": "Linda",
        "predicate": "smiled_at",
        "object": "Marcus",
    }
    polish = {
        "beat": 0,
        "span": "the ring Marcus had bought her in Paris",
        "subject": "Marcus",
        "predicate": "bought_in",
        "object": "Paris",
    }
    render_answer = (pocketing, smiling) if mode == "always" else (pocketing,)
    mal.stub.script(
        "extraction_backstop", [ds.claims_answer(*render_answer), ds.claims_answer(polish)]
    )
    detected_before = _sample(
        "epistrel_claims_detected_total", layer="detector", category="possession"
    )
    checked_before = _sample(
        "epistrel_claims_checked_total", verdict="contradicted", category="possession"
    )

    found = await ds.detect_turn(
        engine, t.story_id, mal, beats, message=COMPOSED, shadow_rate=1.0, scene_entity=t.tavern
    )
    render, message = found.outcomes
    calls = [c for c in mal.stub.calls if c.purpose == "extraction_backstop"]
    assert len(calls) == 2  # one per render (all its beats), one per composed message

    # (a) The pocketed key: detected (possession), and the extraction agrees (merged, not a
    # second claim), contradicted by canon (Marcus holds it): repaired.
    (pocket,) = [c for c in found.checked if c.claim.beat == 1]
    assert (pocket.claim.layer, pocket.claim.category) == ("detector", "possession")
    assert pocket.claim.merged == ("extraction",)
    assert pocket.claim.proposition is not None
    assert (pocket.claim.proposition.subject_id, pocket.claim.proposition.object_id) == (
        t.linda,
        t.key,
    )
    assert pocket.result is not None
    assert pocket.result.kind is VerdictKind.CONTRADICTED
    assert pocket.result.needs_repair

    # (e) The polish pass's claim, only in the composed message: detected by extraction there.
    polished = [c for c in found.checked if c.claim.beat is None and c.claim.layer == "extraction"]
    assert len(polished) == 1
    assert polished[0].claim.span[1:] == (
        COMPOSED.index("the ring"),
        COMPOSED.index("the ring") + len(str(polish["span"])),
    )
    assert polished[0].result is not None
    assert polished[0].result.needs_repair
    assert polished[0].result.kind is VerdictKind.UNCERTAIN
    assert message.extraction == {None: "ran"}

    # Every detected claim was checked.
    assert all(c.result is not None for c in found.checked)
    assert found.report["detected"] == found.report["checked"] == len(found.checked)

    if mode == "always":
        assert render.extraction == {1: "ran", 2: "ran"}
        assert not render.shadow
        smiled = [c for c in found.checked if c.claim.beat == 2 and c.claim.layer == "extraction"]
        assert len(smiled) == 1
    else:
        # Gated: beat 1 is flagged (a possession hit, nothing declared), beat 2 is not.
        assert render.extraction == {1: "ran", 2: "skipped"}
        assert render.shadow
        assert found.report["shadow"] == "pending"
        assert [
            c for c in found.checked if c.claim.layer == "extraction" and c.claim.beat == 2
        ] == []

    op = await ds.commit_turn(engine, t.story_id, found, beats, message=COMPOSED)
    assert (
        _sample("epistrel_claims_detected_total", layer="detector", category="possession")
        == detected_before + 1
    )
    assert (
        _sample("epistrel_claims_checked_total", verdict="contradicted", category="possession")
        == checked_before + 1
    )

    mal.stub.script("extraction_backstop", [ds.claims_answer({**smiling, "beat": 2})])
    outcomes = await ds.run_shadow_jobs(engine, mal, t.story_id)
    rows = await ds.shadow_rows(engine, t.story_id)
    async with engine.connect() as conn:
        report = await shadow.report(conn, story_id=t.story_id)
    assert report.detected_total == found.report["detected"]
    assert report.checked_total == found.report["checked"]
    if mode == "always":
        assert outcomes == []
        assert rows == []
        assert (report.samples, report.missed, report.miss_rate) == (0, 0, None)
        return
    (row,) = rows
    assert row.operation_id == op.operation_id
    assert (row.status, row.extraction_claims, row.missed_count) == ("sampled", 1, 1)
    (missed,) = row.missed
    assert (missed["beat"], missed["start"], missed["end"]) == (
        2,
        SMILES.index("smiles"),
        SMILES.index("smiles") + len("smiles at Marcus"),
    )
    assert missed["category"] == "extracted"
    assert (report.samples, report.extraction_claims, report.missed) == (1, 1, 1)
    assert report.miss_rate == 1.0
    assert report.missed_by_category == {"extracted": 1}
    async with engine.connect() as conn:
        stored = await conn.scalar(
            select(operations.c.turn_report).where(operations.c.operation_id == op.operation_id)
        )
    assert stored is not None
    assert (stored["detection"]["shadow"], stored["detection"]["shadow_missed"]) == ("sampled", 1)
