"""S-NOVELTY-PERMISSION, M4 part: unknown is not permission (FR-VERIFY-11,12; docs/04 §7; #144).

Bobby's render introduces new material in a structured story (the Bobby/Jenni fixture,
``tests.novelty_support``). Every claim it makes is ``novel`` by the rules, and each passes
``core.verify.permission`` before it is canonized or repaired:

- ``test_s_novelty_permission__m4``: (a) a minor detail of Bobby's own backstory (he attended a
  chess camp) is permitted and canonized at the turn's commit with provenance ``render``,
  anchored to his own canon; a director pass introducing a new arrival (a stranger on the porch)
  is canonized with provenance ``director`` while ``allow_new_arrivals`` is on, and the same
  kind of arrival is denied (``policy``), repaired and never written once the author turns the
  policy off (the complement); (f) Bobby claiming an enrollment at Michigan, on the
  (subject, predicate) the author locked (OSU), is denied ``locked_canon``, goes to repair
  tagged ``unpermitted``, and zero ``facts`` rows exist for it after the turn. The turn report
  lists the denied claims under ``verify.denied`` with their reasons, and their verdict
  entries carry the permission code.
- ``test_s_novelty_permission__matrix``: FR-VERIFY-11's M4 matrix over structured data only
  (each asserter and each reason code; the later milestones' exception stubs and the
  precedence order are unit-tested in ``tests/test_permission.py``).
- ``test_s_novelty_permission__model_free``: the complement: ``core/verify/permission.py``
  calls no model purpose and depends on nothing in the Orchestrator.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions.model import Interval, Layer, Predicate, PredicateOwner
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
from epistrel.core.verify.rules import VerdictKind
from epistrel.orchestrator.models.registry import DELIVERED, PORTS, RESERVED
from epistrel.orchestrator.verify.verifier import UNPERMITTED, ClaimRef
from tests import novelty_support as ns
from tests import verify_support as vs

PERMISSION_MODULE = Path(pm.__file__)


@pytest.mark.integration
@pytest.mark.part("M4", "FR-VERIFY-11,12")
async def test_s_novelty_permission__m4(engine: AsyncEngine) -> None:
    """See the module docstring."""
    cast = await ns.bobby_and_jenni(engine)
    story = cast.story_id
    backstory = ns.claim(cast.bobby, ns.ATTENDED, cast.camp)
    arrival = ns.claim(cast.stranger, ns.LOCATED_AT, cast.porch)
    michigan = ns.claim(cast.bobby, ns.ENROLLED_AT, cast.michigan)
    turn = await ns.read_turn(
        engine,
        story,
        [
            (
                backstory,
                cast.render(cast.bobby),
                ClaimRef("t:1", beat=1, span=(0, 30), channel="do"),
            ),
            (arrival, cast.director, ClaimRef("t:2", beat=2, span=(0, 25), channel="narration")),
            (
                michigan,
                cast.render(cast.bobby),
                ClaimRef("t:3", beat=3, span=(4, 40), channel="do"),
            ),
        ],
    )
    verifier = turn.verifier
    (_, own), (_, arrived), (_, locked) = verifier.results
    assert [r.kind for r in (own, arrived, locked)] == [VerdictKind.NOVEL] * 3

    # (a) Bobby's own minor backstory and the director's arrival are permitted.
    assert own.permission == Permitted(Basis.OWN)
    assert own.anchored_to == cast.seed_fact
    assert arrived.permission == Permitted(Basis.ARRIVAL)
    assert not own.needs_repair
    assert not arrived.needs_repair

    # (f) A claim on author-locked canon is denied and goes to repair, tagged.
    assert locked.permission == Denied(Reason.LOCKED_CANON)
    assert (locked.needs_repair, locked.repair_tag) == (True, UNPERMITTED)
    report = verifier.report()
    assert report["denied"] == [
        {
            "claim_id": "t:3",
            "asserter": f"character:{cast.bobby}",
            "reason": "locked_canon",
            "beat": 3,
            "span": [4, 40],
        }
    ]
    codes = {v["claim_id"]: v["permission"] for v in report["verdicts"]}
    assert codes == {
        "t:1": "permitted:own",
        "t:2": "permitted:arrival",
        "t:3": "denied:locked_canon",
    }
    (rendered,) = turn.denied
    assert rendered["claim"].startswith("Bobby enrolled_at Michigan")
    assert rendered["repaired"] is True

    # The commit canonizes the permitted claims, with their origin, and nothing else.
    op, written = await ns.commit_turn(engine, story, verifier)
    assert [(f.subject_id, f.predicate_id) for f in written] == [
        (cast.bobby, ns.ATTENDED),
        (cast.stranger, ns.LOCATED_AT),
    ]
    (camp,) = await ns.facts_of(engine, story, backstory)
    assert (camp.provenance, camp.authority, camp.operation_id) == (
        "render",
        "engine",
        op.operation_id,
    )
    assert camp.anchored_to == cast.seed_fact
    assert camp.visibility == {"scope": "objective"}
    assert camp.beat == 1
    (porch,) = await ns.facts_of(engine, story, arrival)
    assert porch.provenance == "director"
    assert await ns.facts_of(engine, story, michigan) == []  # the complement of (f)

    # The complement of the arrival: with the policy off, a new arrival is denied, repaired
    # and never written.
    await ns.set_arrivals(engine, story, allowed=False)
    visitor = ns.claim(cast.visitor, ns.LOCATED_AT, cast.kitchen)
    later = (
        await ns.read_turn(
            engine, story, [(visitor, cast.director, ClaimRef("t:4", beat=1, channel="narration"))]
        )
    ).verifier
    ((_, refused),) = later.results
    assert refused.kind is VerdictKind.NOVEL
    assert refused.permission == Denied(Reason.POLICY)
    assert refused.repair_tag == UNPERMITTED
    assert [d["reason"] for d in later.report()["denied"]] == ["policy"]
    assert later.canon_requests() == []
    await ns.commit_turn(engine, story, later)
    assert await ns.facts_of(engine, story, visitor) == []

    # The stranger who arrived earlier is placed now: moving him is no arrival.
    moved = ns.claim(cast.stranger, ns.LOCATED_AT, cast.porch, Interval.of(600, 700))
    again = (
        await ns.read_turn(
            engine, story, [(moved, cast.director, ClaimRef("t:5", beat=1, channel="narration"))]
        )
    ).verifier
    ((_, world),) = again.results
    assert world.permission == Permitted(Basis.WORLD)


# -- the matrix (pure) ----------------------------------------------------------------------

BOBBY, JENNI, CAL, STRANGER, KITCHEN, CAMP = (
    vs.BOBBY,
    vs.ANN,
    vs.CAL,
    vs.STRANGER,
    vs.KITCHEN,
    vs.TEAM,
)
ENTITIES = {
    BOBBY: EntityInfo(BOBBY, "person", BOBBY),
    JENNI: EntityInfo(JENNI, "person", JENNI),
    CAL: EntityInfo(CAL, "person", CAL, is_player=True),
    STRANGER: EntityInfo(STRANGER, "person"),
    KITCHEN: EntityInfo(KITCHEN, "place"),
    CAMP: EntityInfo(CAMP, "other"),
}
EXTERNAL = Predicate(
    id="01JP00000000000000000000EX",
    story_id="01JS00000000000000000000EX",
    name="gold",
    layer=Layer.AUTHOR,
    owner=PredicateOwner.EXTERNAL,
)


def decide(
    subject: str,
    predicate: str,
    obj: str | None,
    asserter: Asserter,
    **facts: object,
) -> pm.Permission:
    found = ns.claim(
        subject, CORE_BY_NAME[predicate].id if predicate != "gold" else EXTERNAL.id, obj
    )
    chosen = EXTERNAL if predicate == "gold" else CORE_BY_NAME[predicate]
    given = PermissionFacts(entities=ENTITIES, predicate=chosen, **facts)  # type: ignore[arg-type]
    return pm.permission(found, asserter, pm.owner_of(found, given), given)


@pytest.mark.part("M4", "FR-VERIFY-11,12")
def test_s_novelty_permission__matrix() -> None:
    """FR-VERIFY-11's M4 matrix (see the module docstring)."""
    bobby = Asserter("character", BOBBY)
    director = Asserter("director")
    player = Asserter("player", CAL)
    author = Asserter("author")

    # A character's render: its own actions and minor backstory, scene texture.
    assert decide(BOBBY, "attended_program", CAMP, bobby) == Permitted(Basis.OWN)
    assert decide(KITCHEN, "located_in", CAMP, bobby) == Permitted(Basis.TEXTURE)
    # ... never another modeled character's (any argument), nor the player character's.
    assert decide(JENNI, "attended_program", CAMP, bobby) == Denied(Reason.OTHER_CHARACTER_SECRET)
    assert decide(BOBBY, "knows", JENNI, bobby) == Denied(Reason.OTHER_CHARACTER_SECRET)
    assert decide(CAL, "located_at", KITCHEN, bobby) == Denied(Reason.PLAYER_CHARACTER)
    # ... nor its definition's locked fields, nor anything the author locked (first in order).
    assert decide(BOBBY, "age", None, bobby) == Denied(Reason.LOCKED_CANON)
    assert decide(JENNI, "attended_program", CAMP, bobby, locked=True) == Denied(
        Reason.LOCKED_CANON
    )
    # ... and a new person arrives as texture only under the arrival policy.
    assert decide(STRANGER, "located_at", KITCHEN, bobby, placed=False) == Permitted(Basis.TEXTURE)
    assert decide(
        STRANGER, "located_at", KITCHEN, bobby, placed=False, allow_new_arrivals=False
    ) == Denied(Reason.POLICY)
    assert decide(BOBBY, "located_at", KITCHEN, bobby, placed=False, allow_new_arrivals=False) == (
        Permitted(Basis.OWN)
    )
    assert decide(BOBBY, "attended_program", CAMP, bobby, protected=True) == Denied(
        Reason.PROTECTED_CONTENT
    )

    # A director pass: arrivals per the policy and world facts, nothing about characters.
    assert decide(STRANGER, "present_at", KITCHEN, director, placed=False) == Permitted(
        Basis.ARRIVAL
    )
    assert decide(
        STRANGER, "present_at", KITCHEN, director, placed=False, allow_new_arrivals=False
    ) == Denied(Reason.POLICY)
    assert decide(JENNI, "located_at", KITCHEN, director, placed=False) == Permitted(Basis.ARRIVAL)
    assert decide(KITCHEN, "located_in", CAMP, director) == Permitted(Basis.WORLD)
    assert decide(JENNI, "attended_program", CAMP, director) == Denied(
        Reason.OTHER_CHARACTER_SECRET
    )
    assert decide(CAL, "attended_program", CAMP, director) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(KITCHEN, "located_in", CAMP, director, locked=True) == Denied(Reason.LOCKED_CANON)

    # Player input: the player's own character; about another, it stands and never canonizes.
    assert decide(CAL, "attended_program", CAMP, player) == Permitted(Basis.OWN)
    about = decide(BOBBY, "attended_program", CAMP, player)
    assert about == Denied(Reason.PLAYER_AUTHORITY)
    assert isinstance(about, Denied)
    assert not about.repaired
    # Input for a character the platform plays is that character's render (FR-AUTH-3).
    assert decide(JENNI, "located_at", KITCHEN, Asserter("player", BOBBY)) == Denied(
        Reason.OTHER_CHARACTER_SECRET
    )

    # The author: anything but an externally owned value, which no one may introduce.
    assert decide(JENNI, "age", None, author, locked=True) == Permitted(Basis.AUTHOR)
    assert decide(BOBBY, "gold", None, author) == Denied(Reason.EXTERNAL_VALUE)
    assert decide(BOBBY, "gold", None, bobby) == Denied(Reason.EXTERNAL_VALUE)

    assert Asserter("director").provenance.value == "director"
    assert bobby.provenance.value == "render"
    assert player.provenance.value == "player"
    assert author.provenance.value == "author"
    with pytest.raises(ValueError, match="names its character"):
        Asserter("character")


def _module_names(tree: ast.Module) -> list[str]:
    """Every module a parsed source names in its import statements."""
    found = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    return found + [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]


@pytest.mark.part("M4", "FR-VERIFY-11")
def test_s_novelty_permission__model_free() -> None:
    """The complement: the permission module names no model purpose, calls no model entry
    point, and names no Orchestrator module (the layering contract also forbids it)."""
    tree = ast.parse(PERMISSION_MODULE.read_text(encoding="utf-8"))
    modules = _module_names(tree)
    assert modules
    assert not [m for m in modules if m.startswith("epistrel.orchestrator")], modules
    strings = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    purposes = {*DELIVERED, *RESERVED, *PORTS}
    assert "entailment" in purposes
    assert not purposes & strings, purposes & strings
    for entry in ("structured_call", "ModelAccessLayer", "chat", "complete", "embed"):
        assert entry not in names, entry
