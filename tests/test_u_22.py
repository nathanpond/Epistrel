"""U-22 (M4): the deterministic claim detectors (FR-VERIFY-13; docs/04 §3; #148). One file per
test ID and milestone; U-22's M5 part (an alias of a fact hidden from the speaker, another
character's private ``think`` phrase) joins it.

Every test here runs with the stub adapter instrumented to fail on any call: the detectors are
deterministic data tables and need no model. Detection is checked apart from verification: each
detector hit becomes a claim, and every claim then goes to #142's verifier (over records passed
in), so nothing a detector found is left unchecked."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    Interval,
    LiteralKind,
    Polarity,
    Proposition,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.entities import (
    NARRATOR,
    EntityEntry,
    EntityIndex,
    ResolutionContext,
)
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.verify.detect import (
    DeclaredClaim,
    DetectBeat,
    DetectedClaim,
    DetectionEnv,
    detect_render,
    detect_seed,
    parse_number,
    tables,
    unresolved,
)
from epistrel.core.verify.permission import EntityInfo
from epistrel.core.verify.protected import build_index, term
from epistrel.core.verify.rules import VerdictKind, VerdictReason
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.orchestrator.verify.extraction import (
    ClaimDetector,
    DetectionConfig,
    detection_report,
    verify_detected,
)
from epistrel.orchestrator.verify.verifier import RecordsSource, Verifier, VerifyConfig
from tests import verify_support as vs

LINDA = "01JD0000000000000000000001"
MARCUS = "01JD0000000000000000000002"
KEY = "01JD0000000000000000000003"
TAVERN = "01JD0000000000000000000004"
ELIAS = "01JD0000000000000000000005"
THUNK = "01JD0000000000000000000006"

HOLDS = CORE_BY_NAME["holds"].id
PRESENT_AT = CORE_BY_NAME["present_at"].id
TIME_IS = CORE_BY_NAME["time_is"].id

INDEX = EntityIndex(
    [
        EntityEntry(LINDA, "person", "Linda", gender="feminine", character_id=LINDA),
        EntityEntry(MARCUS, "person", "Marcus", gender="masculine", character_id=MARCUS),
        EntityEntry(KEY, "item", "key"),
        EntityEntry(TAVERN, "place", "tavern"),
        EntityEntry(ELIAS, "person", "Elias Crane", gender="masculine"),
        EntityEntry(vs.DOOR, "item", "door"),
    ]
)
CONTEXT = ResolutionContext(index=INDEX, present=frozenset({LINDA, MARCUS}))
KNOCKER = Proposition(
    subject_id=ELIAS,
    predicate_id=vs.KNOCKED,
    object_id=vs.DOOR,
    valid_time=Instant.of(0, Granularity.MINUTE),
)
"""What the undetermined thunk "someone knocked at the door" would be if Elias were the one."""
PROTECTED = build_index([term("Elias Crane", "thunk", THUNK, KNOCKER)], [], max_terms=10)
ENTITIES = {
    LINDA: EntityInfo(LINDA, "person", LINDA),
    MARCUS: EntityInfo(MARCUS, "person", MARCUS),
    KEY: EntityInfo(KEY, "item"),
    TAVERN: EntityInfo(TAVERN, "place"),
    ELIAS: EntityInfo(ELIAS, "person"),
    vs.DOOR: EntityInfo(vs.DOOR, "item"),
}


def env(**changes: Any) -> DetectionEnv:
    base: dict[str, Any] = {
        "registry": vs.REGISTRY,
        "basis": TimeBasis(),
        "context_for": lambda _speaker: CONTEXT,
        "protected_for": lambda speaker: None if speaker == NARRATOR else PROTECTED,
        "scene_entity": TAVERN,
        "placement": {LINDA: TAVERN, MARCUS: TAVERN},
    }
    return DetectionEnv(**{**base, **changes})


@pytest.fixture(autouse=True)
def _no_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """The stub adapter fails on any call while a test runs; none is ever made."""
    calls: list[Any] = []

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("claim detection called a model")

    monkeypatch.setattr(StubAdapter, "complete", refuse)
    yield calls
    assert calls == []


def verifier(level: str = "extraction") -> Verifier:
    source = RecordsSource([], vs.REGISTRY, entities=ENTITIES)
    return Verifier(source, VerifyConfig(level=level), models=vs.stub_models(), budget=TurnBudget())


def only(claims: tuple[DetectedClaim, ...], beat: int) -> DetectedClaim:
    found = [c for c in claims if c.beat == beat]
    assert len(found) == 1, found
    return found[0]


FIXTURES = [
    DetectBeat(1, "do", "She pockets the key on her way out.", LINDA),
    DetectBeat(2, "say", "That's forty silver, then.", MARCUS),
    DetectBeat(3, "narration", "It's nearly midnight.", NARRATOR),
    DetectBeat(4, "narration", "Marcus wipes down the bar.", NARRATOR),
    DetectBeat(5, "say", "Elias Crane was here last night.", MARCUS),
]


@pytest.mark.part("M4", "FR-VERIFY-13")
async def test_u_22() -> None:
    """Each detector fires on a fixture of otherwise valid prose with no declared proposition
    or effect: "She pockets the key on her way out" (possession: Linda holds the key), "that's
    forty silver, then" (quantity: 40 silver, which no predicate states, so it cannot
    normalize and is ``uncertain``), "it's nearly midnight" (time: ``time_is`` of the
    tavern), a mention of Marcus that no declared proposition covers (he takes part in the
    tavern's scene) and "Elias Crane" in Marcus's speech, a possible value of the undetermined
    knock that Marcus has no way to know (protected content, the knock's proposition with Elias
    as the knocker). Every hit is a claim, and every claim is verified: the protected one is
    novel and its permission is denied (``protected_content``, so it is repaired), the
    unnormalizable quantity takes the safe disposition, the rest are novel. No model is called
    (the stub fails on any call)."""
    detection = detect_render(FIXTURES, (3, 0), env())

    possession = only(detection.claims, 1)
    assert (possession.layer, possession.category) == ("detector", "possession")
    assert possession.proposition is not None
    assert (possession.proposition.subject_id, possession.proposition.predicate_id) == (
        LINDA,
        HOLDS,
    )
    assert possession.proposition.object_id == KEY
    assert possession.span == (1, 4, 11)  # "pockets"

    quantity = only(detection.claims, 2)
    assert quantity.category == "quantity"
    assert quantity.sketch is not None
    assert quantity.sketch.literal is not None
    assert quantity.sketch.literal.kind is LiteralKind.QUANTITY
    assert (quantity.sketch.literal.value, quantity.sketch.literal.unit) == (40, "silver")
    assert quantity.proposition is None
    assert quantity.unnormalized is not None

    time = only(detection.claims, 3)
    assert time.category == "time"
    assert time.proposition is not None
    assert time.proposition.predicate_id == TIME_IS
    assert time.proposition.subject_id == TAVERN
    assert time.proposition.literal is not None
    assert time.proposition.literal.value == "nearly midnight"

    mention = only(detection.claims, 4)
    assert mention.category == "entity_mention"
    assert mention.proposition is not None
    assert (mention.proposition.subject_id, mention.proposition.predicate_id) == (
        MARCUS,
        PRESENT_AT,
    )
    assert mention.proposition.object_id == TAVERN

    protected = only(detection.claims, 5)
    assert protected.category == "protected_content"
    assert protected.protected
    assert protected.protected_ref == THUNK
    assert protected.proposition == KNOCKER

    checked = await verify_detected(verifier(), _outcome(detection), (3, 0))
    assert len(checked) == len(detection.claims) == 5
    assert all(c.result is not None for c in checked)
    by_beat = {c.claim.beat: c for c in checked}
    assert by_beat[5].verdict == "denied"
    assert by_beat[5].result is not None
    assert by_beat[5].result.needs_repair
    denied = by_beat[5].result.denied
    assert denied is not None
    assert denied.reason.value == "protected_content"
    assert by_beat[2].verdict == VerdictKind.UNCERTAIN.value
    assert by_beat[2].result is not None
    assert by_beat[2].result.disposition == "repair"
    for beat in (1, 3, 4):
        assert by_beat[beat].verdict == VerdictKind.NOVEL.value

    report = detection_report([_outcome(detection)], checked)
    assert (report["declared"], report["detector_hits"], report["extraction_claims"]) == (0, 5, 0)
    assert (report["detected"], report["checked"], report["verified_new"]) == (5, 5, 5)


def _outcome(detection: Any) -> Any:
    from epistrel.orchestrator.verify.extraction import DetectionOutcome

    return DetectionOutcome(detection)


@pytest.mark.part("M4", "FR-VERIFY-13")
async def test_u_22__all_declared() -> None:
    """Complement: a render whose claims are all declared yields no detector claim of its own
    (the possession hit is the declared claim, merged into it and counted as matched), flags no
    beat, and makes no extraction call under ``gated`` (the stub fails on any call), while the
    same render without its declaration is flagged."""
    holds = Proposition(
        subject_id=LINDA, predicate_id=HOLDS, object_id=KEY, valid_time=Interval.of(30, None)
    )
    beat = DetectBeat(
        1, "do", "She pockets the key.", LINDA, (DeclaredClaim("decl-1", proposition=holds),)
    )
    config = DetectionConfig(structured=True, level="entailment", extraction_mode="gated")
    detector = ClaimDetector(config, env(), models=vs.stub_models(), budget=TurnBudget())
    outcome = await detector.render([beat], (3, 0), operation_id="op-all-declared")
    assert [c.layer for c in outcome.claims] == ["declared"]
    assert outcome.claims[0].merged == ("detector",)
    assert outcome.detection.counts() == {
        "declared": 1,
        "detector_hits": 1,
        "extraction_claims": 0,
        "matched": 1,
    }
    assert outcome.detection.flagged_beats() == ()
    assert outcome.extraction == {1: "skipped"}
    assert not outcome.called

    bare = DetectBeat(1, "do", "She pockets the key.", LINDA)
    flagged = detect_render([bare], (3, 0), env())
    assert flagged.beats[0].flags == ("possession", "incomplete")


@pytest.mark.part("M4", "FR-VERIFY-13")
async def test_u_22__cues() -> None:
    """Negation, transfer, suppression and noise: "She doesn't take the key" is a negated
    possession; "Marcus hands the key to Linda" is a transfer (Linda holds it, Marcus does not);
    a question, a conditional or a modal asserts nothing ("Would you give me the key?"); "her
    pocket" is a noun, not the verb; and a hit whose object resolves to no entity ("She pockets
    the coin", with no coin in the story) is noise, never a claim. A level below extraction
    (``off``) detects the same claims and verifies none: no verify call is made."""
    beats = [
        DetectBeat(1, "do", "She doesn't take the key.", LINDA),
        DetectBeat(2, "narration", "Marcus hands the key to Linda.", NARRATOR),
        DetectBeat(3, "say", "Would you give me the key?", MARCUS),
        DetectBeat(4, "say", "If she pockets the key, we are done.", MARCUS),
        DetectBeat(5, "do", "She checks her pocket.", LINDA),
        DetectBeat(6, "do", "She pockets the coin.", LINDA),
    ]
    detection = detect_render(beats, (3, 0), env())
    negated = only(detection.claims, 1)
    assert negated.proposition is not None
    assert negated.proposition.polarity is Polarity.NEGATED
    transfer = [c for c in detection.claims if c.beat == 2]
    assert {
        (c.proposition.subject_id, c.proposition.polarity) for c in transfer if c.proposition
    } == {
        (LINDA, Polarity.AFFIRMED),
        (MARCUS, Polarity.NEGATED),
    }
    assert [c for c in detection.claims if c.beat in (3, 4, 5, 6)] == []
    assert detection.beats[5].noise == 1
    assert "possession" in detection.beats[5].flags  # extraction may still resolve the coin

    off = await verify_detected(verifier("off"), _outcome(detection), (3, 0))
    assert [c.result for c in off] == [None] * len(detection.claims)


@pytest.mark.part("M4", "FR-VERIFY-13")
async def test_u_22__seed() -> None:
    """A seed's text through the same detectors (#149's publish check): a capitalized name that
    resolves to no entity ("Lin", an alias the character dropped) is a claim that cannot
    normalize (``unresolved_entity``), not noise; the protected index is never read for a seed;
    the number words and the tables are data (0 to 999 and the thousands)."""
    seed = detect_seed("Linda trusts Lin with the key.", env())
    assert [c.unnormalized.reason for c in unresolved(seed) if c.unnormalized] == [
        VerdictReason.UNRESOLVED_ENTITY
    ]
    assert parse_number("two hundred and six") == 206
    assert parse_number("forty") == 40
    assert parse_number("three thousand") == 3_000
    assert len(tables().verbs) == 5
    assert all(len(t.lemmas) >= 25 for t in tables().verbs)
