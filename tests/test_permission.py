"""Unit checks of ``core.verify.permission`` outside the M4 parts (#144): #180's co-author
exception (which replaced M4's stub), #176's reactive exception, #519's hand-over exception,
#979's toward-another-character exception, and the reason precedence. S-NOVELTY-PERMISSION's
M4 matrix is in ``tests/scenario/test_s_novelty_permission.py``."""

from __future__ import annotations

import pytest

from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    Interval,
    Layer,
    Polarity,
    Predicate,
    Proposition,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.verify import permission as pm
from epistrel.core.verify.permission import (
    Asserter,
    Basis,
    Denied,
    EntityInfo,
    PermissionFacts,
    Permitted,
    Reason,
)
from tests import novelty_support as ns
from tests.scenario.test_s_novelty_permission import (
    BOBBY,
    CAL,
    CAMP,
    ENTITIES,
    JENNI,
    KITCHEN,
    decide,
)


def test_co_author_exception() -> None:
    """#180 (FR-RESP-11) replaces M4's stub: ``co_author`` player input narrating another
    character's observable action (where he is, what he holds, that he spoke) is permitted with
    basis ``co_author`` and canonized with provenance ``player``; his backstory is still
    ``player_authority`` (the player text stands, never canon, never repaired), and so is
    anything about another human's character. Plain player input keeps M4's denial."""
    co_author = Asserter("co_author", CAL)
    assert decide(BOBBY, "located_at", KITCHEN, co_author) == Permitted(Basis.CO_AUTHOR)
    assert decide(BOBBY, "spoke_to", JENNI, co_author) == Permitted(Basis.CO_AUTHOR)
    backstory = decide(BOBBY, "attended_program", CAMP, co_author)
    assert backstory == Denied(Reason.PLAYER_AUTHORITY)
    assert isinstance(backstory, Denied)
    assert not backstory.repaired
    assert decide(BOBBY, "located_at", KITCHEN, Asserter("player", CAL)) == Denied(
        Reason.PLAYER_AUTHORITY
    )
    assert decide(BOBBY, "located_at", KITCHEN, co_author, protected=True) == Denied(
        Reason.PROTECTED_CONTENT
    )
    assert decide(BOBBY, "located_at", KITCHEN, co_author, locked=True) == Denied(
        Reason.LOCKED_CANON
    )
    assert co_author.provenance.value == "player"
    assert Asserter("reactive", JENNI, reactor=BOBBY).provenance.value == "render"


def test_reactive_exception() -> None:
    """#176 (FR-MULTI-9): Jenni's render narrating Bobby's reaction. That he spoke (to her) is
    his observable reaction (``reactive``); a placement change, his backstory, or anything
    about the player through him is not (repaired as the narrator's render would be). A
    reactive asserter names its reactor."""
    narrated = Asserter("reactive", JENNI, reactor=BOBBY)
    assert decide(BOBBY, "spoke", None, narrated) == Permitted(Basis.REACTIVE)
    assert decide(BOBBY, "spoke_to", JENNI, narrated) == Permitted(Basis.REACTIVE)
    assert decide(BOBBY, "located_at", KITCHEN, narrated) == Denied(Reason.OTHER_CHARACTER_SECRET)
    assert decide(BOBBY, "attended_program", CAMP, narrated) == Denied(
        Reason.OTHER_CHARACTER_SECRET
    )
    assert decide(BOBBY, "spoke_to", CAL, narrated) == Denied(Reason.PLAYER_CHARACTER)
    with pytest.raises(ValueError, match="reactor"):
        Asserter("reactive", JENNI)


def test_reason_precedence() -> None:
    """One code per denial, in the documented order (``state_change`` never fires in M4)."""
    assert tuple(Reason) == pm.PRECEDENCE
    assert pm.PRECEDENCE[:3] == (
        Reason.EXTERNAL_VALUE,
        Reason.LOCKED_CANON,
        Reason.STATE_CHANGE,
    )
    assert pm.PRECEDENCE[-1] is Reason.PROTECTED_CONTENT


KEY = "01JD00000000000000000000K1"
"""An item no modeled character is (absent from ``ENTITIES``: world texture)."""


def test_hand_over_exception() -> None:
    """#519 (owner's option (c), FR-VERIFY-11): Jenni's render handing Cal (the player's
    character) the key is her own action, so ``holds(Cal, key)`` with the detected giver Jenni
    is permitted (``own``). Every other claim about Cal is unchanged and stays
    ``player_character``: no giver ("Cal picks up the key", Cal acting), another character as
    the giver (Bobby), another render than the giver's, a director pass, a modeled character
    as the "item", a placement, and a negated ``holds``. Protected content and locked canon
    still win."""
    jenni = Asserter("character", JENNI)
    assert decide(CAL, "holds", KEY, jenni, giver=JENNI) == Permitted(Basis.OWN)
    assert decide(CAL, "holds", KEY, jenni) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "holds", KEY, jenni, giver=BOBBY) == Denied(Reason.PLAYER_CHARACTER)
    bobby = Asserter("character", BOBBY)
    assert decide(CAL, "holds", KEY, bobby, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    director = Asserter("director")
    assert decide(CAL, "holds", KEY, director, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "holds", BOBBY, jenni, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "located_at", KITCHEN, jenni, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "holds", KEY, jenni, giver=JENNI, protected=True) == Denied(
        Reason.PROTECTED_CONTENT
    )
    assert decide(CAL, "holds", KEY, jenni, giver=JENNI, locked=True) == Denied(Reason.LOCKED_CANON)
    negated = ns.claim(CAL, CORE_BY_NAME["holds"].id, KEY).model_copy(
        update={"polarity": Polarity.NEGATED}
    )
    given = PermissionFacts(entities=ENTITIES, predicate=CORE_BY_NAME["holds"], giver=JENNI)
    assert pm.permission(negated, jenni, pm.owner_of(negated, given), given) == Denied(
        Reason.PLAYER_CHARACTER
    )


FIAT_ASSERTERS = (
    Asserter("character", JENNI),
    Asserter("character", BOBBY),
    Asserter("reactive", JENNI, reactor=BOBBY),
    Asserter("director"),
    Asserter("player", CAL),
    Asserter("co_author", CAL),
    Asserter("author"),
)
FIAT_CLAIMS = (
    (BOBBY, "located_at", KITCHEN),
    (BOBBY, "attended_program", CAMP),
    (CAL, "located_at", KITCHEN),
    (CAL, "holds", KEY),
    (JENNI, "located_at", KITCHEN),
    (KEY, "located_at", KITCHEN),
)
FIAT_FACTS: tuple[dict[str, object], ...] = (
    {},
    {"protected": True},
    {"locked": True},
    {"state_change": True},
    {"allow_new_arrivals": False, "placed": False},
)


def test_a_fiat_known_claim_is_permitted_where_only_a_secret_denied_it() -> None:
    """#649 (FR-RESP-14, ADR-177), the matrix row: over every asserter kind, a spread of claims
    (another character's placement and backstory, the player character, the speaker itself,
    world texture) and of facts (protected content, locked canon, a state change, the arrival
    policy), ``fiat_known`` changes exactly the cells a character's render (direct, or the
    narrator side of a reactive render, not the reactor's own reaction) is denied
    ``other_character_secret`` in: they become
    ``Permitted(author_fiat)``, or the lower ``policy``/``protected_content`` denial when that
    applies. Every other cell is unchanged."""
    changed = 0
    for asserter in FIAT_ASSERTERS:
        for subject, predicate, obj in FIAT_CLAIMS:
            for facts in FIAT_FACTS:
                plain = decide(subject, predicate, obj, asserter, **facts)
                fiat = decide(subject, predicate, obj, asserter, fiat_known=True, **facts)
                secret = plain == Denied(Reason.OTHER_CHARACTER_SECRET)
                # A reactive render's narrator side (a claim not about the reactor) is the
                # narrator's own render; the reactor's own reaction keeps the reactive rule.
                rendered = asserter.kind == "character" or (
                    asserter.kind == "reactive" and subject != asserter.reactor
                )
                if secret and rendered:
                    changed += 1
                    expected: object = Permitted(Basis.AUTHOR_FIAT)
                    if facts.get("protected"):
                        expected = Denied(Reason.PROTECTED_CONTENT)
                    assert fiat in (expected, Denied(Reason.POLICY)), (asserter, predicate, facts)
                else:
                    assert fiat == plain, (asserter, subject, predicate, facts)
    assert changed > 0
    jenni = Asserter("character", JENNI)
    assert decide(BOBBY, "attended_program", CAMP, jenni, fiat_known=True) == Permitted(
        Basis.AUTHOR_FIAT
    )
    assert decide(BOBBY, "attended_program", CAMP, jenni, fiat_known=True, locked=True) == Denied(
        Reason.LOCKED_CANON
    )
    assert decide(CAL, "located_at", KITCHEN, jenni, fiat_known=True) == Denied(
        Reason.PLAYER_CHARACTER
    )


# -- #979: the toward-another-character exception --------------------------------------------

MAREN = "01JD0000000000000000000T01"
WREN = "01JD0000000000000000000T02"
BOBBY_T = "01JD0000000000000000000T03"
MALIK = "01JD0000000000000000000T04"
ETHAN = "01JD0000000000000000000T05"
CARLOS = "01JD0000000000000000000T06"
MUG = "01JD0000000000000000000T07"
TORCH = "01JD0000000000000000000T08"
INN = "01JD0000000000000000000T09"
TOWARD_ENTITIES = {
    MAREN: EntityInfo(MAREN, "person", MAREN),
    WREN: EntityInfo(WREN, "person", WREN, is_player=True),
    BOBBY_T: EntityInfo(BOBBY_T, "person", BOBBY_T),
    MALIK: EntityInfo(MALIK, "person", MALIK),
    ETHAN: EntityInfo(ETHAN, "person", ETHAN),
    CARLOS: EntityInfo(CARLOS, "person", CARLOS),
    MUG: EntityInfo(MUG, "item"),
    TORCH: EntityInfo(TORCH, "item"),
    INN: EntityInfo(INN, "place"),
}
CAMP_A = frozenset({WREN, MAREN, BOBBY_T, MALIK, ETHAN})
"""The turn's present set: everyone but Carlos, who is at the other camp."""


def _free(name: str) -> Predicate:
    """An extraction backstop's free relation: an auto predicate with the weakest semantics."""
    return Predicate(
        id="01JP0000000000000000000F" + format(abs(hash(name)) % 100, "02d"),
        story_id="01JS00000000000000000000EX",
        name=name,
        layer=Layer.AUTO,
    )


def toward(
    subject: str,
    predicate: str,
    obj: str | None,
    asserter: str | Asserter,
    *,
    free: bool = False,
    **facts: object,
) -> pm.Permission:
    """``subject predicate obj`` asserted in ``asserter``'s render (a ``do`` beat, the turn's
    present set ``CAMP_A``, unless ``facts`` says otherwise)."""
    chosen = _free(predicate) if free else CORE_BY_NAME[predicate]
    found = ns.claim(subject, chosen.id, obj)
    who = asserter if isinstance(asserter, Asserter) else Asserter("character", asserter)
    given = PermissionFacts(
        entities=TOWARD_ENTITIES,
        predicate=chosen,
        **{"present": CAMP_A, "channel": "do", **facts},  # type: ignore[arg-type]
    )
    return pm.permission(found, who, pm.owner_of(found, given), given)


def _toward_ok(found: pm.Permission, basis: Basis) -> bool:
    return isinstance(found, Permitted) and found.basis is basis and found.toward


def test_toward_the_player_is_permitted() -> None:
    """#979 (FR-VERIFY-11, ADR-218): Maren (rendering) acting toward Wren (the player's
    character): she slides or gives him a mug (the hand-over, and the evidence's free relation
    ``slides_cup_to``), nods to him, speaks to him, looks at him, stands beside him. Red before
    the fix: every one was ``player_character``."""
    assert _toward_ok(toward(WREN, "holds", MUG, MAREN, giver=MAREN), Basis.OWN)
    assert _toward_ok(toward(MAREN, "spoke_to", WREN, MAREN), Basis.OWN)
    assert _toward_ok(toward(MAREN, "present_at", WREN, MAREN), Basis.ACTION_TEXTURE)
    for relation in (
        "slides_cup_to",
        "gives_nod_to",
        "nods_to",
        "looks_at",
        "turns_toward",
        "serves_drink_to",
        "slid_mug_to",
        "present_with",
        "beside",
    ):
        found = toward(MAREN, relation, WREN, MAREN, free=True)
        assert _toward_ok(found, Basis.ACTION_TEXTURE), relation
    # A say beat qualifies too; with no present set given, the player still counts.
    assert _toward_ok(toward(MAREN, "spoke_to", WREN, MAREN, channel="say"), Basis.OWN)
    assert _toward_ok(toward(MAREN, "spoke_to", WREN, MAREN, present=None), Basis.OWN)


def test_toward_the_player_stays_denied() -> None:
    """#979: what is about Wren himself stays ``player_character``: Wren as subject (tired,
    picking up the key, taking it from Maren), his relationships or backstory with Maren as
    subject (``sibling_of``, ``is_sister_of``, ``loves``, ``remembers``), a third character's
    action in Maren's render, a claim naming a third modeled character, a negated, past,
    ``think`` or reactive-fallback claim, and a director pass or reactive narration."""
    denied = Denied(Reason.PLAYER_CHARACTER)
    assert toward(WREN, "tired", None, MAREN, free=True) == denied
    assert toward(WREN, "holds", MUG, MAREN) == denied  # no giver: Wren picks it up
    took = ns.claim(WREN, CORE_BY_NAME["holds"].id, MUG).model_copy(
        update={"qualifiers": {"from": MAREN}}
    )
    given = PermissionFacts(
        entities=TOWARD_ENTITIES, predicate=CORE_BY_NAME["holds"], present=CAMP_A, channel="do"
    )
    maren = Asserter("character", MAREN)
    assert pm.permission(took, maren, pm.owner_of(took, given), given) == denied
    assert toward(MAREN, "sibling_of", WREN, MAREN) == denied
    for relation in ("is_sister_of", "loves", "remembers", "took_from"):
        assert toward(MAREN, relation, WREN, MAREN, free=True) == denied, relation
    assert toward(BOBBY_T, "slides_cup_to", WREN, MAREN, free=True) == denied
    assert toward(WREN, "holds", MUG, MAREN, giver=BOBBY_T) == denied
    assert toward(WREN, "holds", MUG, BOBBY_T, giver=MAREN) == denied
    three = ns.claim(MAREN, CORE_BY_NAME["spoke_to"].id, WREN).model_copy(
        update={"qualifiers": {"about": BOBBY_T}}
    )
    given = PermissionFacts(
        entities=TOWARD_ENTITIES, predicate=CORE_BY_NAME["spoke_to"], present=CAMP_A
    )
    assert pm.permission(three, maren, pm.owner_of(three, given), given) == denied
    negated = ns.claim(MAREN, CORE_BY_NAME["spoke_to"].id, WREN).model_copy(
        update={"polarity": Polarity.NEGATED}
    )
    assert pm.permission(negated, maren, pm.owner_of(negated, given), given) == denied
    assert toward(MAREN, "spoke_to", WREN, MAREN, current=False) == denied
    assert toward(MAREN, "spoke_to", WREN, MAREN, channel="think") == denied
    fallback = Asserter("character", MAREN, fallback=True)
    assert toward(MAREN, "spoke_to", WREN, fallback) == denied
    assert toward(MAREN, "spoke_to", WREN, Asserter("director")) == denied
    reactive = Asserter("reactive", BOBBY_T, reactor=MAREN)
    assert toward(MAREN, "nods_to", WREN, reactive, free=True) == denied
    # An off-scene render (Maren not in the present set) acts toward no one.
    assert toward(MAREN, "spoke_to", WREN, MAREN, present=frozenset({WREN})) == denied
    # Locked canon and protected content still win.
    assert toward(MAREN, "spoke_to", WREN, MAREN, locked=True) == Denied(Reason.LOCKED_CANON)
    assert toward(MAREN, "spoke_to", WREN, MAREN, protected=True) == Denied(
        Reason.PROTECTED_CONTENT
    )


def test_toward_a_present_character_is_permitted() -> None:
    """#979 (widened, owner 2026-11-23): Malik (rendering) acting toward Ethan, who is at the
    same place: he speaks to him, hands him the torch, leans toward and looks at him. Red
    before the fix: every one was ``other_character_secret``."""
    assert _toward_ok(toward(MALIK, "spoke_to", ETHAN, MALIK), Basis.OWN)
    assert _toward_ok(toward(ETHAN, "holds", TORCH, MALIK, giver=MALIK), Basis.OWN)
    for relation in ("leans_toward", "looks_at", "glances_at", "stands_beside", "next_to"):
        found = toward(MALIK, relation, ETHAN, MALIK, free=True)
        assert _toward_ok(found, Basis.ACTION_TEXTURE), relation


def test_toward_a_present_character_stays_denied() -> None:
    """#979: what is about Ethan himself stays ``other_character_secret`` (he is nervous, he
    picks up the torch, Malik is his brother), and so does the same action toward Carlos, who
    is at the other camp, or with no present set given; a claim also naming a third modeled
    character is denied too."""
    secret = Denied(Reason.OTHER_CHARACTER_SECRET)
    assert toward(ETHAN, "nervous", None, MALIK, free=True) == secret
    assert toward(ETHAN, "holds", TORCH, MALIK) == secret
    assert toward(MALIK, "sibling_of", ETHAN, MALIK) == secret
    assert toward(MALIK, "spoke_to", CARLOS, MALIK) == secret
    assert toward(MALIK, "nods_to", CARLOS, MALIK, free=True) == secret
    assert toward(MALIK, "spoke_to", ETHAN, MALIK, present=None) == secret
    three = ns.claim(MALIK, CORE_BY_NAME["spoke_to"].id, ETHAN).model_copy(
        update={"qualifiers": {"about": CARLOS}}
    )
    given = PermissionFacts(
        entities=TOWARD_ENTITIES, predicate=CORE_BY_NAME["spoke_to"], present=CAMP_A
    )
    malik = Asserter("character", MALIK)
    assert pm.permission(three, malik, pm.owner_of(three, given), given) == secret
    # The present-bench exception is still decided first.
    benched = toward(ETHAN, "holds", TORCH, MALIK, giver=MALIK, bench_present=frozenset({ETHAN}))
    assert benched == Permitted(Basis.BENCH_PRESENT)


def test_action_verb() -> None:
    """#979: a free relation's first token, lemmatized by the suffix table and the irregulars
    map; anything not on ``OBSERVABLE_ACTION_VERBS`` fails."""
    for name, lemma in (
        ("slides_cup_to", "slide"),
        ("gives_nod_to", "give"),
        ("turns_toward", "turn"),
        ("serves_drink_to", "serve"),
        ("glances_at", "glance"),
        ("passes_salt_to", "pass"),
        ("touches_arm_of", "touch"),
        ("nodded_to", "nod"),
        ("stepping_toward", "step"),
        ("smiled_at", "smile"),
        ("gave_cup_to", "give"),
        ("sat_beside", "sit"),
        ("stood_near", "stand"),
        ("slid_mug_to", "slide"),
        ("leansToward", "lean"),
        ("whispers_to", "whisper"),
    ):
        assert pm.action_verb(name) == lemma, name
    for name in ("is_sister_of", "loves", "remembers", "took_from", "trait", "present_with"):
        assert pm.action_verb(name) is None, name


def _at(when: Interval | Instant) -> Proposition:
    return Proposition(
        subject_id=MAREN, predicate_id=CORE_BY_NAME["spoke_to"].id, object_id=WREN, valid_time=when
    )


def test_current() -> None:
    """#979: a claim is present-time when its valid time does not end before the turn starts:
    an open interval, an instant at or after the turn's start; a past interval or instant is
    history."""
    start = 600
    assert pm._current(_at(Interval.of(start, None)), start)
    assert pm._current(_at(Interval.of(None, None)), start)
    assert not pm._current(_at(Interval.of(0, start)), start)
    assert pm._current(_at(Instant.of(start, Granularity.MINUTE)), start)
    assert not pm._current(_at(Instant.of(start - 60, Granularity.MINUTE)), start)
