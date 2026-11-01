"""#515 (owner's option (a)): a placement supports presence there. ``located_at(X, P)`` matches an
affirmed ``present_at(X, P)`` claim by the coverage test (``core.verify.rules``'s
``PRESENCE_SUPPORT``), so a ``do`` beat's mention of a character placed at the scene's place is
``supported``, not ``novel``. A character placed elsewhere, or not placed, is unchanged: its
mention is still ``novel`` and its permission decides. Pure: records passed in, no model."""

from __future__ import annotations

from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.entities import EntityEntry, EntityIndex, ResolutionContext
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.verify.detect import DetectBeat, DetectionEnv, detect_render
from epistrel.core.verify.permission import Asserter, EntityInfo
from epistrel.core.verify.rules import Rule, Verdict, VerdictKind, check
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.verify.extraction import DetectionOutcome, verify_detected
from epistrel.orchestrator.verify.verifier import RecordsSource, Verifier, VerifyConfig
from tests import verify_support as vs

PRESENT_AT = CORE_BY_NAME["present_at"].id
NOW = vs.span(10, 20)


def _presence(*placements: Proposition, negated: bool = False) -> Verdict:
    claim = vs.prop(vs.BOBBY, PRESENT_AT, vs.KITCHEN, NOW, negated=negated)
    return check(claim, vs.records(*placements), registry=vs.REGISTRY)


def test_a_placement_supports_presence_there() -> None:
    """Bobby placed in the kitchen over the claim's time: his presence in the kitchen is
    ``supported``, citing the placement. Complements: placed in the garden, not placed, placed
    only before the claim, a negated placement, and a negated presence claim are not."""
    placed = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(0, 100)))
    found = check(vs.prop(vs.BOBBY, PRESENT_AT, vs.KITCHEN, NOW), [placed], registry=vs.REGISTRY)
    assert found.kind is VerdictKind.SUPPORTED
    assert [(e.rule, e.proposition_id) for e in found.evidence] == [
        (Rule.COVERAGE, placed.proposition_id)
    ]
    elsewhere = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.GARDEN, vs.span(0, 100))
    assert _presence(elsewhere).kind is VerdictKind.NOVEL
    assert _presence().kind is VerdictKind.NOVEL
    earlier = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(0, 5))
    assert _presence(earlier).kind is VerdictKind.NOVEL
    not_there = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(0, 100), negated=True)
    assert _presence(not_there).kind is not VerdictKind.SUPPORTED
    assert _presence(placed.proposition, negated=True).kind is not VerdictKind.SUPPORTED


WREN = "01JD0000000000000000000D01"
JENNI = "01JD0000000000000000000D02"
DRIVER = "01JD0000000000000000000D03"
ANN = "01JD0000000000000000000D04"
STREET = "01JD0000000000000000000D05"
PARK = "01JD0000000000000000000D06"

INDEX = EntityIndex(
    [
        EntityEntry(WREN, "person", "Wren", gender="feminine", character_id=WREN),
        EntityEntry(JENNI, "person", "Jenni", gender="feminine", character_id=JENNI),
        EntityEntry(DRIVER, "person", "the driver", gender="masculine", character_id=DRIVER),
        EntityEntry(ANN, "person", "Ann", gender="feminine", character_id=ANN),
        EntityEntry(STREET, "place", "street"),
        EntityEntry(PARK, "place", "park"),
    ]
)
ENTITIES = {
    WREN: EntityInfo(WREN, "person", WREN, is_player=True),
    JENNI: EntityInfo(JENNI, "person", JENNI),
    DRIVER: EntityInfo(DRIVER, "person", DRIVER),
    ANN: EntityInfo(ANN, "person", ANN),
    STREET: EntityInfo(STREET, "place"),
    PARK: EntityInfo(PARK, "place"),
}
TEXTS = {
    1: "Jenni waves to Wren again.",
    2: "Jenni waves to the driver again.",
    3: "Jenni waves to Ann.",
}


async def test_a_co_located_mention_is_supported() -> None:
    """Jenni's render on the street: her waves to Wren (the player) and to the driver (a
    modeled minor), both placed on the street, are mention claims (``present_at`` the street)
    that the placements support, so no permission is asked. Ann, placed in the park, and the
    same wave with no placement for anyone, stay ``novel`` and are denied as before."""
    placements = [
        vs.prop(c, vs.LOCATED_AT, STREET, vs.span(None, None)) for c in (WREN, JENNI, DRIVER)
    ]
    placements.append(vs.prop(ANN, vs.LOCATED_AT, PARK, vs.span(None, None)))
    found = await _mention_verdicts(placements)
    assert found == {
        1: ("supported", None),
        2: ("supported", None),
        3: ("novel", "denied:other_character_secret"),
    }
    assert await _mention_verdicts([]) == {
        1: ("novel", "denied:player_character"),
        2: ("novel", "denied:other_character_secret"),
        3: ("novel", "denied:other_character_secret"),
    }


async def _mention_verdicts(
    placements: list[Proposition],
) -> dict[int | None, tuple[str, str | None]]:
    env = DetectionEnv(
        registry=vs.REGISTRY,
        basis=TimeBasis(),
        context_for=lambda _speaker: ResolutionContext(
            index=INDEX, present=frozenset({WREN, JENNI, DRIVER}), player_id=WREN
        ),
        scene_entity=STREET,
        placement={WREN: STREET, JENNI: STREET, DRIVER: STREET, ANN: PARK},
    )
    beats = [DetectBeat(i, "do", text, JENNI) for i, text in TEXTS.items()]
    detection = detect_render(beats, (3, 0), env)
    verifier = Verifier(
        RecordsSource(vs.records(*placements), vs.REGISTRY, entities=ENTITIES),
        VerifyConfig(level="detectors"),
        models=vs.stub_models(),
        budget=TurnBudget(),
    )
    checked = await verify_detected(
        verifier,
        DetectionOutcome(detection),
        (3, 0),
        player_id=WREN,
        asserter_of=lambda _claim: Asserter("character", JENNI),
    )
    found: dict[int | None, tuple[str, str | None]] = {}
    for c in checked:
        if c.claim.category != "entity_mention" or c.result is None:
            continue
        assert c.claim.proposition is not None
        assert c.claim.proposition.predicate_id == PRESENT_AT
        assert c.claim.proposition.object_id == STREET
        permission = None if c.result.permission is None else c.result.permission.code()
        found[c.claim.beat] = (c.result.kind.value, permission)
    return found
