"""U-22 (M4): the deterministic claim detectors (FR-VERIFY-13; docs/04 §3; #148). One file per
test ID and milestone; U-22's M5 part (an alias of a fact hidden from the speaker, another
character's private ``think`` phrase) joins it.

Every test here runs with the stub adapter instrumented to fail on any call: the detectors are
deterministic data tables and need no model. Detection is checked apart from verification: each
detector hit becomes a claim, and every claim then goes to #142's verifier (over records passed
in), so nothing a detector found is left unchecked."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

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
    UNCOVERED_MENTION,
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
from epistrel.core.verify.protected import PrivateRecord, build_index, term
from epistrel.core.verify.rules import VerdictKind, VerdictReason
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.registry import ExtractionResult
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


TOM = "01JD0000000000000000000007"
ORCHARD = "01JD0000000000000000000008"
LOCATED_AT = CORE_BY_NAME["located_at"].id


def _orchard_env() -> DetectionEnv:
    index = EntityIndex(
        [
            EntityEntry(TOM, "person", "Tom Harrow", gender="masculine"),
            EntityEntry(ORCHARD, "place", "The orchard", aliases=(("orchard", "detected"),)),
        ]
    )
    context = ResolutionContext(index=index, present=frozenset({TOM}))
    return env(context_for=lambda _speaker: context, scene_entity=ORCHARD, placement={})


def _movement(text: str) -> list[tuple[str, str | None, Polarity]]:
    detection = detect_seed(text, _orchard_env())
    return [
        (c.proposition.subject_id, c.proposition.object_id, c.proposition.polarity)
        for c in detection.claims
        if c.category == "movement"
        and c.proposition is not None
        and c.proposition.predicate_id == LOCATED_AT
    ]


@pytest.mark.part("M4", "FR-VERIFY-13")
@pytest.mark.parametrize(
    "text",
    [
        "Tom Harrow left the orchard.",
        "Tom Harrow fled the orchard.",
        "Tom Harrow walked out of the orchard.",
        "Tom Harrow ran away from the orchard.",
    ],
)
async def test_u_22__departure_from_a_place(text: str) -> None:
    """A departure that names its place (#440) is a negated ``located_at`` of that place, never
    an affirmed one: "Tom Harrow left the orchard" says he is no longer there."""
    assert _movement(text) == [(TOM, ORCHARD, Polarity.NEGATED)]


@pytest.mark.part("M4", "FR-VERIFY-13")
@pytest.mark.parametrize(
    "text",
    [
        "Tom Harrow arrived at the orchard.",
        "Tom Harrow walked into the orchard.",
        "Tom Harrow fled to the orchard.",
        "Tom Harrow went out to the orchard.",
    ],
)
async def test_u_22__arrival_at_a_place(text: str) -> None:
    """The complement of #440: an arrival, or a departure toward a named destination, is an
    affirmed ``located_at`` of the place reached."""
    assert _movement(text) == [(TOM, ORCHARD, Polarity.AFFIRMED)]


@pytest.mark.part("M4", "FR-VERIFY-13")
async def test_u_22__negated_departure_from_a_place() -> None:
    """A negated departure from a place ("Tom Harrow did not leave the orchard") asserts no
    movement claim, like a negated placeless departure."""
    assert _movement("Tom Harrow did not leave the orchard.") == []


LINDA_AT_TAVERN = Proposition(
    subject_id=LINDA,
    predicate_id=vs.LOCATED_AT,
    object_id=TAVERN,
    valid_time=Interval.of(30, None),
)


def _declares_linda_at_tavern(index: int, channel: str, text: str) -> DetectBeat:
    """A Linda beat that declares something other than what its prose claims (#478)."""
    return DetectBeat(
        index, channel, text, LINDA, (DeclaredClaim("decl-at", proposition=LINDA_AT_TAVERN),)
    )


class _RecordingModels:
    """A Model Access Layer stand-in that records each ``structured_call`` and answers no
    claims: it shows whether gated extraction chose to read a beat (the stub adapter stays
    instrumented to fail)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def structured_call(self, purpose: str, _schema: Any, inputs: Any, **_: Any) -> Any:
        self.calls.append((purpose, inputs))
        return ExtractionResult()


@pytest.mark.part("M4", "FR-VERIFY-13")
async def test_u_22__uncovered_mention_gates_extraction() -> None:
    """#478 regression (NFR-QUAL-10's ``identity-declared`` family): a ``say`` beat that declares
    ``Linda located_at tavern`` and says "Marcus and I were married once." names Marcus, whom no
    declared proposition covers. In ``say`` that mention becomes no claim, so it flags the beat
    (``uncovered_mention``) and ``gated`` extraction reads it: one call, carrying that beat."""
    beat = _declares_linda_at_tavern(1, "say", "Marcus and I were married once.")
    detection = detect_render([beat], (3, 0), env())
    assert detection.beats[0].flags == (UNCOVERED_MENTION,)

    models = _RecordingModels()
    config = DetectionConfig(structured=True, level="entailment", extraction_mode="gated")
    detector = ClaimDetector(config, env(), models=cast(Any, models), budget=TurnBudget())
    outcome = await detector.render([beat], (3, 0), operation_id="op-identity-declared")
    assert outcome.called
    assert outcome.extraction == {1: "ran"}
    assert [purpose for purpose, _ in models.calls] == ["extraction_backstop"]
    (_, inputs) = models.calls[0]
    assert [b.text for b in inputs.beats] == ["Marcus and I were married once."]


@pytest.mark.part("M4", "FR-VERIFY-13")
async def test_u_22__uncovered_mention_flag() -> None:
    """The ``uncovered_mention`` flag (#478): an uncovered mention in ``say`` or ``think`` flags
    the beat; a mention a declared proposition covers, the speaker's own name or the scene's
    place does not; and in a scene channel (``do``, narration) the uncovered mention is a
    ``present_at`` claim that is checked, so it flags nothing either."""
    flagged = detect_render(
        [
            _declares_linda_at_tavern(1, "say", "Marcus is my cousin."),
            _declares_linda_at_tavern(2, "think", "Marcus never forgave me."),
        ],
        (3, 0),
        env(),
    )
    assert [b.flags for b in flagged.beats] == [(UNCOVERED_MENTION,), (UNCOVERED_MENTION,)]

    marcus_at_tavern = Proposition(
        subject_id=MARCUS,
        predicate_id=vs.LOCATED_AT,
        object_id=TAVERN,
        valid_time=Interval.of(30, None),
    )
    covered = DetectBeat(
        1,
        "say",
        "Marcus is here at the tavern.",
        LINDA,
        (DeclaredClaim("decl-marcus", proposition=marcus_at_tavern),),
    )
    own = _declares_linda_at_tavern(2, "say", "Linda stays at the tavern.")
    scene = _declares_linda_at_tavern(3, "do", "She smiles at Marcus.")
    unflagged = detect_render([covered, own, scene], (3, 0), env())
    assert [b.flags for b in unflagged.beats] == [(), (), ()]
    assert [c.category for c in unflagged.claims if c.beat == 3 and c.layer == "detector"] == [
        "entity_mention"
    ]


# -- U-22 M5 (#172): hidden aliases and private thoughts -----------------------------------------

DANA = "01JD0000000000000000000007"
SECRET = "01JD0000000000000000000008"
SKIMMING = "Marcus has been skimming from the till all week, I just know it."
AFFAIR = Proposition(
    subject_id=MARCUS,
    predicate_id=vs.KNOCKED,
    object_id=DANA,
    valid_time=Interval.of(None, None),
)
"""A record hidden from Linda: only Marcus's own belief names Dana (S-EXTRACT-ADVERSARIAL (b))."""


@pytest.mark.part("M5", "FR-VERIFY-13")
async def test_u_22__m5() -> None:
    """Protected-content matching fires on the alias of a fact hidden from the speaker ("Dee",
    Dana's alias, which only Marcus's hidden record names) and on another character's private
    ``think`` phrase: Linda's thought that Marcus is skimming, repeated by Marcus (two 4-word
    shingles), and narrated as her inner state ("Linda secretly suspects …", the inner-state
    lexicon). The alias hit claims the hidden record and its permission is denied; each thought
    hit is a ``think_leak`` that is always repaired, whatever the safe disposition. Complement:
    Linda's own words are never matched against her own thought, a sentence sharing one
    shingle with it is no hit, and a narration with no inner-state cue is no leak. No model is
    called."""
    hidden = build_index([], [term("Dee", "belief", SECRET, AFFAIR)], max_terms=10)
    thought = PrivateRecord.of(LINDA, "memory:m1", "think", f"Linda thinks: {SKIMMING}")
    beats = [
        DetectBeat(1, "say", "Dee was with me last night.", LINDA),
        DetectBeat(2, "say", "Someone has been skimming from the till all week.", MARCUS),
        DetectBeat(
            3, "narration", "Linda secretly suspects Marcus is skimming from the till.", NARRATOR
        ),
        DetectBeat(4, "say", "Marcus has been skimming from the till all week.", LINDA),
        DetectBeat(5, "say", "Business at the till has been slow.", MARCUS),
        DetectBeat(6, "narration", "Linda counts the till again.", NARRATOR),
    ]
    found = detect_render(
        beats,
        (3, 0),
        env(protected_for=lambda speaker: hidden if speaker == LINDA else None, private=(thought,)),
    )
    by_beat: dict[int | None, list[DetectedClaim]] = {}
    for claim in found.claims:
        if claim.category in ("protected_content", "inner_state"):
            by_beat.setdefault(claim.beat, []).append(claim)
    assert sorted(k for k in by_beat if k is not None) == [1, 2, 3]
    (alias,) = by_beat[1]
    assert (alias.protected_ref, alias.proposition) == (SECRET, AFFAIR)
    assert not alias.think_leak
    (repeated,) = by_beat[2]
    assert (repeated.category, repeated.protected_ref) == ("protected_content", "think:memory:m1")
    assert repeated.think_leak
    assert beats[1].text[repeated.span[1] : repeated.span[2]] == (
        "has been skimming from the till all week"
    )
    (narrated,) = by_beat[3]
    assert (narrated.category, narrated.protected_ref) == ("inner_state", "think:memory:m1")

    surfacing = Verifier(
        RecordsSource([], vs.REGISTRY, entities=ENTITIES),
        VerifyConfig(level="extraction", safe_disposition="surface"),
        models=vs.stub_models(),
        budget=TurnBudget(),
    )
    checked = await verify_detected(
        surfacing, _outcome(found.with_claims([alias, repeated, narrated])), (3, 0)
    )
    results = [c.result for c in checked]
    assert all(r is not None and r.needs_repair for r in results)
    assert [r.repair_tag for r in results if r is not None] == [
        "unpermitted",
        "think_leak",
        "think_leak",
    ]
    assert not any(r.canonizes for r in results if r is not None and r.think_leak)
