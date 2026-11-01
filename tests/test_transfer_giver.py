"""#519 (owner's option (c), FR-VERIFY-11): the transfer detector names the giver of a hand-over
on the recipient's ``holds`` claim, and the permission check reads it. In Jenni's render, "Jenni
hands Cal the key." (Cal is the player's character) is her own action and is permitted; "Cal
picks up the key.", "Cal takes the key from Jenni." and "Bobby hands Cal the key." still say
something about Cal that Jenni's render may not, and are denied ``player_character``. Pure:
detection and verification over records passed in, no model."""

from __future__ import annotations

from collections.abc import Sequence

from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.entities import EntityEntry, EntityIndex, ResolutionContext
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.verify.detect import (
    DeclaredClaim,
    DetectBeat,
    DetectedClaim,
    DetectionEnv,
    detect_render,
)
from epistrel.core.verify.permission import Asserter, EntityInfo
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.verify.extraction import DetectionOutcome, verify_detected
from epistrel.orchestrator.verify.verifier import RecordsSource, Verifier, VerifyConfig
from tests import verify_support as vs

CAL = "01JD0000000000000000000C01"
JENNI = "01JD0000000000000000000C02"
BOBBY = "01JD0000000000000000000C03"
KEY = "01JD0000000000000000000C04"
PARLOR = "01JD0000000000000000000C05"
HOLDS = CORE_BY_NAME["holds"].id

INDEX = EntityIndex(
    [
        EntityEntry(CAL, "person", "Cal", gender="masculine", character_id=CAL),
        EntityEntry(JENNI, "person", "Jenni", gender="feminine", character_id=JENNI),
        EntityEntry(BOBBY, "person", "Bobby", gender="masculine", character_id=BOBBY),
        EntityEntry(KEY, "item", "key"),
        EntityEntry(PARLOR, "place", "parlor"),
    ]
)
CONTEXT = ResolutionContext(index=INDEX, present=frozenset({CAL, JENNI, BOBBY}), player_id=CAL)
ENTITIES = {
    CAL: EntityInfo(CAL, "person", CAL, is_player=True),
    JENNI: EntityInfo(JENNI, "person", JENNI),
    BOBBY: EntityInfo(BOBBY, "person", BOBBY),
    KEY: EntityInfo(KEY, "item"),
    PARLOR: EntityInfo(PARLOR, "place"),
}

TEXTS = {
    1: "Jenni hands Cal the key.",
    2: "Cal picks up the key.",
    3: "Cal takes the key from Jenni.",
    4: "Bobby hands Cal the key.",
}


def _env() -> DetectionEnv:
    return DetectionEnv(
        registry=vs.REGISTRY,
        basis=TimeBasis(),
        context_for=lambda _speaker: CONTEXT,
        scene_entity=PARLOR,
        placement={CAL: PARLOR, JENNI: PARLOR, BOBBY: PARLOR},
    )


def _cal_holds(claims: Sequence[DetectedClaim], beat: int) -> DetectedClaim:
    (found,) = [
        c
        for c in claims
        if c.beat == beat
        and c.proposition is not None
        and (c.proposition.subject_id, c.proposition.predicate_id) == (CAL, HOLDS)
        and c.proposition.polarity is Polarity.AFFIRMED
    ]
    return found


async def test_transfer_names_its_giver_and_permission_reads_it() -> None:
    """See the module docstring."""
    beats = [DetectBeat(i, "do", text, JENNI) for i, text in TEXTS.items()]
    detection = detect_render(beats, (3, 0), _env())
    givers = {b: _cal_holds(detection.claims, b).giver for b in TEXTS}
    assert givers == {1: JENNI, 2: None, 3: None, 4: BOBBY}
    # The giver's own negated ``holds`` names no giver.
    assert all(
        c.giver is None
        for c in detection.claims
        if c.proposition is not None and c.proposition.polarity is Polarity.NEGATED
    )

    verifier = Verifier(
        RecordsSource([], vs.REGISTRY, entities=ENTITIES),
        VerifyConfig(level="detectors"),
        models=vs.stub_models(),
        budget=TurnBudget(),
    )
    checked = await verify_detected(
        verifier,
        DetectionOutcome(detection),
        (3, 0),
        player_id=CAL,
        asserter_of=lambda _claim: Asserter("character", JENNI),
    )
    codes = {
        c.claim.beat: c.result.permission.code()
        for c in checked
        if c.result is not None
        and c.result.permission is not None
        and c.claim.proposition is not None
        and c.claim.proposition.subject_id == CAL
    }
    assert codes == {
        1: "permitted:own",
        2: "denied:player_character",
        3: "denied:player_character",
        4: "denied:player_character",
    }


def test_a_declared_hand_over_takes_the_detected_giver() -> None:
    """A render that declares ``holds(Cal, key)`` on its hand-over beat: the detector's claim
    merges into the declared one, which keeps the giver the text names."""
    declared = DeclaredClaim(
        "d1",
        Proposition(
            subject_id=CAL, predicate_id=HOLDS, object_id=KEY, valid_time=Interval.of(0, None)
        ),
    )
    beat = DetectBeat(1, "do", TEXTS[1], JENNI, declared=(declared,))
    found = detect_render([beat], (3, 0), _env())
    (claim,) = [c for c in found.claims if c.layer == "declared"]
    assert claim.merged == ("detector",)
    assert claim.giver == JENNI
