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
- ``test_s_novelty_permission__repaired`` (#151, the carried obligation of #144): the same
  fixture played end to end through ``respond``: with the arrival toggle off, Bobby's render
  bringing a stranger into the kitchen is denied (``policy``) and repaired (rung 1 drops the
  arrival's sentence); his claim of an enrollment at Michigan on the locked key is denied
  (``locked_canon``) and repaired (rung 3, under "do not introduce facts about Bobby"); neither
  is ever written. Complement: with the toggle on the same arrival is permitted and canonized,
  and nothing is repaired.
- ``test_s_novelty_permission__matrix``: FR-VERIFY-11's M4 matrix over structured data only
  (each asserter and each reason code; the later milestones' exception stubs and the
  precedence order are unit-tested in ``tests/test_permission.py``).
- ``test_s_novelty_permission__model_free``: the complement: ``core/verify/permission.py``
  calls no model purpose and depends on nothing in the Orchestrator.
- ``test_s_novelty_permission__reactive`` (M5, #176): the reactive-narration exception.
- ``test_s_novelty_permission__b`` (M5, #172): (b) a secret about Jenni held only in her
  partition, introduced by Bobby, is matched by the protected-content index and repaired, never
  canonized; spoken by Jenni herself, it is permitted.
- ``test_s_novelty_permission__reactive`` (M5, #176): the reactive-narration exception: Jenni's
  render narrating Bobby's flinch is accepted, his thought or decision repaired.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
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
from tests import multi_support as ms
from tests import novelty_support as ns
from tests import reactive_support as rs
from tests import structured_turn_support as st
from tests import verify_support as vs
from tests.auth_support import auth_settings
from tests.canon_support import PRINCIPAL
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.secrets_support import play, secret, secret_story

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


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


AS_OWNER = {"X-Epistrel-Principal": PRINCIPAL}
"""The fixture's stories belong to ``tests.canon_support.PRINCIPAL``."""


@pytest.mark.integration
@pytest.mark.part("M4", "FR-VERIFY-11,12")
async def test_s_novelty_permission__repaired(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring."""
    arrives = "Bobby looks up. A stranger walks into the kitchen."
    arrival = st.claim("stranger", "located_at", "kitchen")
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- the arrival toggle off: denied (policy), repaired at rung 1, never written
        cast = await ns.bobby_and_jenni(engine, allow_new_arrivals=False)
        stub.script("generation", [st.render(st.beat("do", arrives, propositions=[arrival]))])
        response = await respond(
            client, cast.story_id, body(cast.cal, "Anyone there?"), headers=AS_OWNER
        )
        assert response.status_code == 200, response.text
        assert response.json()["message"]["text"] == "*Bobby looks up.*"
        report = await st.turn_report(engine, cast.story_id)
        assert [d["reason"] for d in report["verify"]["denied"]] == ["policy"]
        (entry,) = report["repairs"]
        assert (entry["tag"], entry["rung"], entry["accepted"]) == (UNPERMITTED, 1, True)
        stranger = ns.claim(cast.stranger, ns.LOCATED_AT, cast.kitchen)
        assert await ns.facts_of(engine, cast.story_id, stranger) == []

        # -- the locked enrollment: denied (locked_canon), repaired by the regeneration
        stub.reset()
        stub.script(
            "generation",
            [
                st.render(
                    st.beat(
                        "say",
                        "I'm enrolled at Michigan.",
                        intent="sincere",
                        propositions=[st.claim("I", "enrolled_at", "Michigan")],
                    )
                )
            ],
        )
        stub.script(
            "regeneration", [json.dumps({"text": "I study at OSU, still.", "propositions": []})]
        )
        response = await respond(
            client, cast.story_id, body(cast.cal, "Where do you study?"), headers=AS_OWNER
        )
        assert response.status_code == 200, response.text
        assert response.json()["message"]["text"] == 'Bobby: "I study at OSU, still."'
        report = await st.turn_report(engine, cast.story_id)
        assert [d["reason"] for d in report["verify"]["denied"]] == ["locked_canon"]
        (entry,) = report["repairs"]
        assert (entry["tag"], entry["rung"]) == (UNPERMITTED, 3)
        (call,) = [c for c in stub.calls if c.purpose == "regeneration"]
        prompt = "\n".join(m.content for m in call.messages)
        assert "Do not introduce facts about Bobby" in prompt
        michigan = ns.claim(cast.bobby, ns.ENROLLED_AT, cast.michigan, ns.SEED_SPAN)
        assert await ns.facts_of(engine, cast.story_id, michigan) == []

        # -- complement: the toggle on, the same arrival is canonized and nothing repaired
        cast = await ns.bobby_and_jenni(engine)
        stub.reset()
        stub.script("generation", [st.render(st.beat("do", arrives, propositions=[arrival]))])
        response = await respond(
            client, cast.story_id, body(cast.cal, "Anyone there?"), headers=AS_OWNER
        )
        assert response.status_code == 200, response.text
        assert response.json()["message"]["text"] == f"*{arrives}*"
        assert "repairs" not in await st.turn_report(engine, cast.story_id)
        stranger = ns.claim(cast.stranger, ns.LOCATED_AT, cast.kitchen)
        assert len(await ns.facts_of(engine, cast.story_id, stranger)) == 1


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


# -- the M5 part (#172): (b) a secret about Jenni -------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M5", "FR-VERIFY-11,12")
async def test_s_novelty_permission__b(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None, testimony_delivered: None
) -> None:
    """(b) Bobby's render introduces a secret about Jenni that exists only in Jenni's partition
    (she believes she works for Vince; no canon names Vince): "Jenni works for Vince, you
    know." The protected-content index (other characters' belief-only records, #172) matches
    "Vince" in Bobby's say; the hit claims Jenni's belief, its permission is denied and the
    beat is repaired, and nothing about Vince is canonized. Complement: the same sentence
    spoken by Jenni herself is permitted (her own record, which her index never protects from
    her) and stands."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        story, ids = await secret_story(client, engine, "Wren", "Bobby", "Jenni")
        vince, belief = await secret(
            engine, story, ids["Jenni"], ids["Jenni"], "employed_by", "Vince"
        )
        stub = epistrel.models.stub
        secret_line = "Jenni works for Vince, you know."
        bobby = await play(
            client,
            engine,
            stub,
            story,
            ids["Wren"],
            ids["Bobby"],
            st.beat("say", secret_line, intent="sincere"),
        )
        assert belief in bobby.protected_refs()
        assert bobby.denied
        assert bobby.tags
        assert "Vince" not in bobby.message
        about = (
            "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND (p.object_id = :v OR p.subject_id = :v)"
        )
        assert await rows(engine, about, s=story, v=vince) == []

        jenni = await play(
            client,
            engine,
            stub,
            story,
            ids["Wren"],
            ids["Jenni"],
            st.beat("say", "I work for Vince, you know.", intent="sincere"),
        )
        assert belief not in jenni.protected_refs()
        assert not jenni.denied
        assert not jenni.tags
        assert "Vince" in jenni.message


# -- the M5 part (#176): the reactive-narration exception ------------------------------------------

FLINCH = "Bobby flinches at the noise."


@pytest.mark.integration
@pytest.mark.part("M5", "FR-MULTI-9; FR-VERIFY-11,12")
async def test_s_novelty_permission__reactive(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None, multi_delivered: None
) -> None:
    """The exception (FR-MULTI-9): Jenni's primary render narrates Bobby (reactive) flinching
    at a noise he perceived ("Jenni drops a pan. Bobby flinches at the noise."): accepted as
    reactive narration (Bobby's ``beat_do``, ``narrated_by`` Jenni), not repaired as an action
    by another character. The same render narrating Bobby's ``think`` ("Bobby thinks Jenni is
    lying.") or a decision of his ("Bobby decides to leave.", Bobby's role fixed ``reactive``,
    so it cannot escalate) is repaired: the thought dropped, the decision limited to a
    non-verbal reaction. The matrix: a reactive asserter may establish that its reactor spoke,
    never a placement change or backstory of his (``tests/test_permission.py``)."""
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
        client,
        epistrel,
    ):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine, names=("Cal", "Jenni", "Bobby"))

        async def jenni(text: str) -> dict[str, Any]:
            stub.reset()
            ms.renders_for(stub, "Jenni", rs.render(st.beat("do", text)))
            answer = await respond(
                client, b.story_id, body(b["Cal"], "Hm?", render_character_ids=[b["Jenni"]])
            )
            assert answer.status_code == 200, answer.text
            found: dict[str, Any] = answer.json()
            return found

        turn = await jenni(f"Jenni drops a pan. {FLINCH}")
        (flinch,) = await rs.narrated(engine, b.story_id)
        assert (flinch.character_id, flinch.narrated_by, flinch.text) == (
            b["Bobby"],
            b["Jenni"],
            FLINCH,
        )
        assert FLINCH in turn["message"]["text"]
        report = await st.turn_report(engine, b.story_id)
        assert "repairs" not in report
        assert report["roles"]["reactions"][0]["character_id"] == b["Bobby"]

        await rs.set_role(client, b.story_id, b["Bobby"], "reactive", fixed=True)
        turn = await jenni(
            "Jenni drops a pan. Bobby thinks Jenni is lying. Bobby decides to leave."
        )
        assert "thinks Jenni" not in turn["message"]["text"]
        assert "decides" not in turn["message"]["text"]
        latest = (await rs.narrated(engine, b.story_id))[-1]
        assert latest.text == "Bobby looks up but says nothing."
        assert turn["escalation_deferred"] == [
            {"character_id": b["Bobby"], "reason": "fixed_role", "needed": "decision"}
        ]
        roles = (await st.turn_report(engine, b.story_id))["roles"]
        assert roles["dropped"] == {"inner_state": 1}
        assert roles["limited"] == 1
