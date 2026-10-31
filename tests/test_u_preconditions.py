"""``core.states.preconditions`` (#189; FR-STATE-11): the propositions an action depends on and
their verdicts over canon plus the same-turn overlay. Unmarked: S-STATE-PRECONDITIONS is the
gated oracle; this pins the four kinds, the item's resolution (``consideration``, the same
beat's possession change, ``effect_unresolved``, a transfer with no item) and #188's order."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from epistrel.core.epistemic.perception import SceneState
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.registry import core_registry
from epistrel.core.propositions.vocabulary import AGREED_TO, HELD_BY, HOLDS, OWNS
from epistrel.core.resolution.entities import EntityEntry, EntityIndex
from epistrel.core.states import authorize as a
from epistrel.core.states import effects as rules
from epistrel.core.states import preconditions as pc
from epistrel.core.states.model import UndefinedType, Value, validate_definition
from epistrel.core.turns.beats import DeclaredEffect
from epistrel.core.verify.rules import Record

CAL, MIRA, OTTO = new_ulid(), new_ulid(), new_ulid()
SWORD, SHIELD, SHOP, STREET = new_ulid(), new_ulid(), new_ulid(), new_ulid()
INDEX = EntityIndex(
    [
        EntityEntry(CAL, "person", "Cal", character_id=CAL),
        EntityEntry(MIRA, "person", "Mira", character_id=MIRA),
        EntityEntry(OTTO, "person", "Otto", character_id=OTTO),
        EntityEntry(SWORD, "item", "sword"),
        EntityEntry(SHIELD, "item", "shield"),
        EntityEntry(SHOP, "place", "the forge"),
    ]
)
TRANSFER_BEAT, MIRA_BEAT, NARRATOR_BEAT = 2, 3, 4
GOLD = validate_definition(
    {
        "name": "gold",
        "type": "number",
        "scope": "character",
        "constraints": {"non_negative": True},
        "initial_value": 0,
        "transfer": {"conserved": True},
    }
)
DIRECTOR = a.Source("director")
Values = dict[tuple[str, str | None], Value | UndefinedType]


def _fact(subject: str, predicate: str, obj: str, **kw: Any) -> Record:
    return Record(
        Proposition(
            subject_id=subject,
            predicate_id=predicate,
            object_id=obj,
            valid_time=Interval.of(None, None),
            **kw,
        ),
        new_ulid(),
    )


def _claim(
    subject: str, predicate: str, obj: str, *, negated: bool = False, declared: bool = True
) -> pc.Claim:
    return pc.Claim(
        Proposition(
            subject_id=subject,
            predicate_id=predicate,
            object_id=obj,
            polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
            valid_time=Interval.of(90, 110),
        ),
        declared=declared,
    )


def _canon(
    *,
    holds: list[Record] | None = None,
    owns: list[Record] | None = None,
    placed: dict[str, str] | None = None,
    items: tuple[str, ...] = (SWORD, SHIELD),
) -> pc.Canon:
    placements = {CAL: SHOP, MIRA: SHOP} if placed is None else placed
    return pc.Canon(
        core_registry(),
        100,
        INDEX,
        SceneState(
            location=placements.get(CAL),
            placements=placements,
            characters=frozenset({CAL, MIRA, OTTO}),
            places=frozenset({SHOP, STREET}),
        ),
        holds={i: [r for r in holds or () if r.proposition.object_id == i] for i in items},
        owns={i: [r for r in owns or () if r.proposition.object_id == i] for i in items},
        agreed={MIRA: [], OTTO: []},
    )


def _purchase(**kw: Any) -> a.StateEffect:
    """Cal pays Mira 500 gold (declared on the narrator's beat 2)."""
    fields: dict[str, Any] = {
        "state": "gold",
        "scope_key": CAL,
        "kind": a.TRANSFER,
        "delta": Decimal(-500),
        "counterparty": a.Counterparty("gold", MIRA),
        "beat": TRANSFER_BEAT,
    }
    fields.update(kw)
    return a.StateEffect(**fields)


def _context(canon: pc.Canon, *beats: pc.BeatView) -> pc.Context:
    found = {TRANSFER_BEAT: pc.BeatView(TRANSFER_BEAT, "narration", narrator=True)}
    found.update({b.beat: b for b in beats})
    return pc.Context(canon, found)


def _mira(*claims: pc.Claim, kind: str = "say") -> pc.BeatView:
    return pc.BeatView(MIRA_BEAT, kind, MIRA, claims=claims)


def _narrator(*claims: pc.Claim) -> pc.BeatView:
    return pc.BeatView(NARRATOR_BEAT, "narration", narrator=True, claims=claims)


AGREES = _claim(MIRA, AGREED_TO, SWORD)
AVAILABLE = [_fact(MIRA, HOLDS, SWORD)]
OWNED = [_fact(MIRA, OWNS, SWORD)]


def _evaluate(
    effect: a.StateEffect, context: pc.Context, source: a.Source = DIRECTOR
) -> str | None:
    found = pc.evaluate(effect, source, GOLD, context)
    return None if found is None else found.reason


def test_u_preconditions_for() -> None:
    """The four kinds, in order, for a sale whose ``consideration`` names the sword."""
    canon = _canon(holds=AVAILABLE, owns=OWNED)
    context = _context(canon, _mira(AGREES))
    effect = _purchase(consideration="sword")
    found = pc.preconditions_for(effect, context.beats[TRANSFER_BEAT], context)
    assert [r.name for r in found] == [
        pc.CONSENT,
        pc.AVAILABILITY,
        pc.CO_LOCATION,
        pc.CO_LOCATION,
        pc.OWNERSHIP,
    ]
    consent, available, buyer_here, seller_here, owned = (r.proposition for r in found)
    assert (consent.subject_id, consent.predicate_id, consent.object_id) == (
        MIRA,
        AGREED_TO,
        SWORD,
    )
    assert (available.subject_id, available.predicate_id, available.object_id) == (
        MIRA,
        HOLDS,
        SWORD,
    )
    assert {buyer_here.subject_id, seller_here.subject_id} == {CAL, MIRA}
    assert buyer_here.object_id == seller_here.object_id == SHOP
    assert (owned.subject_id, owned.predicate_id, owned.object_id) == (MIRA, OWNS, SWORD)
    # All four hold: no refusal.
    assert _evaluate(effect, context) is None
    # The consideration by id resolves the same way.
    assert _evaluate(_purchase(consideration=SWORD), context) is None


def test_u_preconditions_item_resolution() -> None:
    canon = _canon(holds=AVAILABLE, owns=OWNED)
    # The same beat's possession change toward the payer names the item.
    handed = pc.BeatView(
        TRANSFER_BEAT, "narration", narrator=True, claims=(_claim(SWORD, HELD_BY, CAL),)
    )
    context = _context(canon, handed, _mira(AGREES))
    parties = pc.parties_of(_purchase(), context)
    assert parties is not None
    assert (parties.payer, parties.seller, parties.counterparty, parties.item) == (
        CAL,
        MIRA,
        MIRA,
        SWORD,
    )
    assert _evaluate(_purchase(), context) is None
    # A possession change toward someone else names nothing.
    other = pc.BeatView(
        TRANSFER_BEAT, "narration", narrator=True, claims=(_claim(OTTO, HOLDS, SWORD),)
    )
    assert pc.possession_item(other, CAL) is None
    # A sale with no item anywhere is effect_unresolved (rung 1 asks the render to name it).
    unnamed = _context(canon, _mira(AGREES))
    assert _evaluate(_purchase(justification="sale"), unnamed) == pc.UNRESOLVED
    # A transfer with no item (a tip) skips the item checks: consent and co-location only.
    tip = _purchase(delta=Decimal(-5))
    assert [r.name for r in pc.preconditions_for(tip, None, unnamed)] == [
        pc.CONSENT,
        pc.CO_LOCATION,
        pc.CO_LOCATION,
    ]
    tipped = _context(canon, _mira(_claim(MIRA, AGREED_TO, CAL)))
    assert _evaluate(tip, tipped) is None
    # A consideration that names no entity of the story is uncertain: the safe disposition.
    # (Consent first: her agreement to the sword is no agreement to an item named nowhere.)
    assert _evaluate(_purchase(consideration="a dragon"), unnamed) == pc.reason_of(pc.CONSENT)
    assert _evaluate(_purchase(consideration="a dragon"), tipped) == (pc.reason_of(pc.AVAILABILITY))
    # A change with no item changes no possession: nothing to check.
    spend = a.StateEffect("gold", CAL, delta=Decimal(-5), beat=TRANSFER_BEAT)
    assert pc.parties_of(spend, unnamed) is None
    # A change that pays for an item: the seller is its holder in canon.
    bought = a.StateEffect(
        "gold", CAL, delta=Decimal(-500), consideration="sword", beat=TRANSFER_BEAT
    )
    parties = pc.parties_of(bought, _context(canon))
    assert parties is not None
    assert (parties.payer, parties.seller, parties.counterparty) == (CAL, MIRA, MIRA)
    assert _evaluate(bought, _context(canon)) == pc.reason_of(pc.CONSENT)
    assert _evaluate(bought, _context(canon, _mira(AGREES))) is None


def test_u_preconditions_consent() -> None:
    canon = _canon(holds=AVAILABLE, owns=OWNED)
    sale = _purchase(consideration="sword")
    # No agreement at all: funds don't make a sale.
    assert _evaluate(sale, _context(canon)) == pc.reason_of(pc.CONSENT)
    # The merchant refuses (a negated agreed_to on her own beat).
    refused = _context(canon, _mira(_claim(MIRA, AGREED_TO, SWORD, negated=True)))
    assert _evaluate(sale, refused) == pc.reason_of(pc.CONSENT)
    # Her own do beat agrees; so does a narrator (world) beat.
    assert _evaluate(sale, _context(canon, _mira(AGREES, kind="do"))) is None
    assert _evaluate(sale, _context(canon, _narrator(AGREES))) is None
    # Prose alone never counts: a detected (not declared) agreement, or one on a think.
    detected = _claim(MIRA, AGREED_TO, SWORD, declared=False)
    assert _evaluate(sale, _context(canon, _mira(detected))) == pc.reason_of(pc.CONSENT)
    assert _evaluate(sale, _context(canon, _mira(AGREES, kind="think"))) == pc.reason_of(pc.CONSENT)
    # Someone else's agreement, or Mira's agreement on another beat's speaker, is not hers.
    otto = pc.BeatView(MIRA_BEAT, "say", OTTO, claims=(_claim(OTTO, AGREED_TO, SWORD),))
    assert _evaluate(sale, _context(canon, otto)) == pc.reason_of(pc.CONSENT)
    # A rejected (contradicted) agreement is not kept.
    rejected = pc.Claim(AGREES.proposition, kept=False)
    assert _evaluate(sale, _context(canon, _mira(rejected))) == pc.reason_of(pc.CONSENT)
    # The counterparty's own render declaring the transfer is its own act.
    assert _evaluate(sale, _context(canon), a.Source("character", MIRA)) is None
    # A theft asks no consent.
    assert _evaluate(_purchase(consideration="sword", justification="theft"), _context(canon)) is (
        None
    )
    # Consent read for no party is uncertain: the safe disposition.
    unread = _canon(holds=AVAILABLE, owns=OWNED)
    unread.agreed = {}
    assert _evaluate(sale, _context(unread, _mira(AGREES))) == pc.reason_of(pc.CONSENT)


def test_u_preconditions_availability_location_ownership() -> None:
    sale = _purchase(consideration="sword")
    agreed = _mira(AGREES)
    # The sword is held by someone else: gone.
    gone = _canon(holds=[_fact(OTTO, HOLDS, SWORD)], owns=OWNED)
    assert _evaluate(sale, _context(gone, agreed)) == pc.reason_of(pc.AVAILABILITY)
    # Earlier in the turn the sword was handed to Otto (the same-turn overlay).
    handed = pc.BeatView(1, "narration", narrator=True, claims=(_claim(OTTO, HOLDS, SWORD),))
    unseen = _canon(owns=OWNED)
    assert _evaluate(sale, _context(unseen, agreed, handed)) == pc.reason_of(pc.AVAILABILITY)
    # Nobody's holding fixed in canon: permitted novelty.
    assert _evaluate(sale, _context(unseen, agreed)) is None
    # The item's canon was never read: uncertain, the safe disposition.
    unread = _canon(holds=AVAILABLE, owns=OWNED, items=())
    assert _evaluate(sale, _context(unread, agreed)) == pc.reason_of(pc.AVAILABILITY)
    # Mira is in the street, Cal at the forge: not co-located.
    apart = _canon(holds=AVAILABLE, owns=OWNED, placed={CAL: SHOP, MIRA: STREET})
    assert _evaluate(sale, _context(apart, agreed)) == pc.reason_of(pc.CO_LOCATION)
    # Unplaced: nothing says they are apart.
    loose = _canon(holds=AVAILABLE, owns=OWNED, placed={})
    assert _evaluate(sale, _context(loose, agreed)) is None
    # Mira walked to the forge earlier in the turn.
    walked = pc.BeatView(
        1,
        "narration",
        narrator=True,
        claims=(_claim(MIRA, "01JPRED0000000000000000002", SHOP),),
    )
    assert _evaluate(sale, _context(apart, agreed, walked)) is None
    # Otto owns the sword Mira holds: not hers to sell.
    lent = _canon(holds=AVAILABLE, owns=[_fact(OTTO, OWNS, SWORD)])
    assert _evaluate(sale, _context(lent, agreed)) == pc.reason_of(pc.OWNERSHIP)
    disowned = _canon(holds=AVAILABLE, owns=[_fact(MIRA, OWNS, SWORD, polarity=Polarity.NEGATED)])
    assert _evaluate(sale, _context(disowned, agreed)) == pc.reason_of(pc.OWNERSHIP)


def _planned(effect: dict[str, Any]) -> rules.Planned:
    return rules.Planned(
        TRANSFER_BEAT,
        0,
        None,
        DeclaredEffect(state="gold", reason="a sword", **effect),
        DIRECTOR,
    )


def test_u_preconditions_in_the_dry_run() -> None:
    """#188's order: preconditions after the justification, before the balance and the bounds;
    an unmet one is ``state_unpermitted`` and nothing passes."""
    definitions = {"gold": GOLD}
    values: Values = {("gold", CAL): Decimal(1000), ("gold", MIRA): Decimal(0)}
    canon = _canon(holds=AVAILABLE, owns=OWNED)
    sale: dict[str, Any] = {
        "kind": "transfer",
        "scope_key": CAL,
        "delta": -500,
        "counterparty": ("gold", MIRA, None),
        "consideration": "sword",
    }

    def run(
        effect: dict[str, Any], context: pc.Context, money: Values = values
    ) -> tuple[list[rules.Passed], list[rules.Finding]]:
        return rules.dry_run(definitions, money, [_planned(effect)], preconditions=context)

    passed, found = run(sale, _context(canon))
    assert not passed
    assert [(f.tag, f.constraint) for f in found] == [
        (rules.STATE_UNPERMITTED, pc.reason_of(pc.CONSENT))
    ]
    # Before the balance: an unbalanced transfer with no consent is refused on consent.
    unbalanced = {**sale, "counterparty": ("gold", MIRA, 400)}
    _, found = run(unbalanced, _context(canon))
    assert [f.constraint for f in found] == [pc.reason_of(pc.CONSENT)]
    _, found = run(unbalanced, _context(canon, _mira(AGREES)))
    assert [f.constraint for f in found] == [a.UNBALANCED]
    # Consent and availability: the transfer passes, both sides folded.
    passed, found = run(sale, _context(canon, _mira(AGREES)))
    assert not found
    assert [(p.key, p.new) for p in passed] == [
        (("gold", CAL), Decimal(500)),
        (("gold", MIRA), Decimal(500)),
    ]
    # Complement: consent but not enough gold fails on the bounds, distinguishably.
    poor: Values = {("gold", CAL): Decimal(100), ("gold", MIRA): Decimal(0)}
    _, found = run(sale, _context(canon, _mira(AGREES)), poor)
    assert [(f.tag, f.constraint) for f in found] == [(rules.STATE_CONSTRAINT, "non_negative")]
    # A sale whose item is named nowhere: effect_unresolved, asking for the consideration.
    unnamed = {**sale, "consideration": None, "justification": "sale"}
    _, found = run(unnamed, _context(canon, _mira(AGREES)))
    assert [(f.tag, f.constraint) for f in found] == [
        (rules.EFFECT_UNRESOLVED, rules.CONSIDERATION)
    ]
    line = rules.constraint_line(found[0], GOLD, narrator=True)
    assert "consideration" in line
    # Without a context (a consumer's write, a commit-time re-check) nothing is checked.
    passed, found = rules.dry_run(definitions, values, [_planned(sale)])
    assert passed
    assert not found
    # The repair line and the refusal name the rule, never a value.
    refused = rules.Finding(
        rules.STATE_UNPERMITTED, TRANSFER_BEAT, "gold", constraint=pc.reason_of(pc.CONSENT)
    )
    assert "not agreed" in rules.constraint_line(refused, GOLD, narrator=False)
    assert "not agreed" in rules.refusal_text("say", None, GOLD, pc.reason_of(pc.CONSENT))
    gone = rules.refusal_text("do", "Mira", GOLD, pc.reason_of(pc.AVAILABILITY))
    assert "not there" in gone
