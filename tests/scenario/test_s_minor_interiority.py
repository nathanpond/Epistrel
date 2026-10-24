"""S-MINOR-INTERIORITY (docs/04 §7; FR-EPI-11, FR-TURN-3, FR-VERIFY-9, NFR-PRIV-1).

#163's slice (``__minor_privacy``, FR-EPI-11): "minor" limits weight and sparsity, never
privacy. Jenni hands the delivery driver a tip, so he is modeled as a ``minor``. When the next
request names him to render, his private ``think`` ("She seems sad today.") is a first-class
memory of his partition (channel ``think``, visible to him alone) even under the minor's sparse
accrual, while Jenni and the player hold nothing of it and the player-facing response carries
neither the thought nor its words. #172 appends the grounded resurfacing and the
non-disclosure variants.

#172's ``test_s_minor_interiority`` (FR-EPI-11, FR-TURN-3, FR-VERIFY-9, NFR-PRIV-1): the thought
never reaches the player-facing message, narrated, verbatim or paraphrased; a sympathetic
glance is accepted; six months later the driver's grounded callback stands, and an unrecorded
"memory" is repaired as a confabulation (see its docstring).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import modeling_support as ms
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client, system_of
from tests.structured_turn_support import beat, render, turn_report

pytestmark = pytest.mark.integration

TIP = "Jenni hands the delivery driver a tip."
THOUGHT = "She seems sad today."
REPLY = "Thanks, have a good one."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M5", "FR-EPI-11")
async def test_s_minor_interiority__minor_privacy(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring. Complement: no other partition holds the thought."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, _ = await ms.street(client, engine, salience=None)
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("do", TIP))])
        played = await respond(client, story, body(ids["Wren"], "Morning!"))
        assert played.status_code == 200, played.text
        driver = (await ms.characters_of(engine, story))["the delivery driver"]
        assert driver.weight == "minor"

        stub.reset()
        stub.script(
            "generation",
            [render(beat("think", THOUGHT), beat("say", REPLY, intent="sincere"))],
        )
        played = await respond(
            client,
            story,
            body(ids["Wren"], "Here you go.", render_character_ids=[driver.id]),
        )
        assert played.status_code == 200, played.text
        mine = await ps.memories_of(engine, story, driver.id)
        thought = [m for m in mine if m.channel == "think"]
        assert [m.text for m in thought] == [f"the delivery driver thinks: {THOUGHT}"]
        assert thought[0].visibility == {"scope": "characters", "characters": [driver.id]}
        assert (2, f"the delivery driver says: {REPLY}") in await ms.memory_texts(
            engine, story, driver.id
        )
        # Nobody else holds it, and the player-facing response does not carry it.
        for other in (ids["Jenni"], ids["Wren"]):
            assert not [m for m in await ps.memories_of(engine, story, other) if "sad" in m.text]
        answer = played.json()
        assert "sad" not in answer["message"]["text"]
        assert all(b["kind"] != "think" for b in answer["beats"])
        assert not [b for b in answer["beats"] if "sad" in (b.get("text") or "")]


# -- #172: non-disclosure and grounded resurfacing ---------------------------------------------

SAD = "Jenni seems sad today, and those leggings look great."
NARRATED = "He glances at her, secretly thinking she seems sad."
VERBATIM = "Funny, I seem sad today, and those leggings look great."
PARAPHRASED = "The delivery driver thinks I seem sad."
GLANCE = "He gives her a sympathetic glance."
INFERENCE = "You look a little sad today, Jenni."
CALLBACK = "I remember you seemed sad that day."
CONFABULATED = "I remember you crying at the bus stop."


def _private_words(text: str) -> list[str]:
    folded = text.casefold()
    return [w for w in ("sad", "leggings", "crying", "bus stop") if w in folded]


@pytest.mark.part("M5", "FR-EPI-11; FR-TURN-3; FR-VERIFY-9; NFR-PRIV-1")
async def test_s_minor_interiority(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The delivery driver (a minor, #163) records a private ``think``: Jenni seems sad, and he
    likes the leggings. It never reaches the player-facing message (the secondary control,
    #172): narrated as his inner state in his own ``do`` (the inner-state lexicon), repeated
    verbatim by Jenni (the thought side index's shingles) or paraphrased as his thought by her
    (the lexicon again), each is a ``think_leak`` the repair ladder corrects, its constraint
    naming none of it. Six months on (a later turn: story time is M6's), the driver's
    callback "I remember you seemed sad that day" is a grounded recall (``supported``, rule
    ``recall``, backed by the recorded ``think``), steered by his ``## Your private memories``,
    and stands; "I remember you crying at the bus stop" is backed by nothing: a confabulation,
    repaired ("do not claim to remember …") and recorded as an ``escalations`` row.

    Complement: a verified ``do`` expressing the state (a sympathetic glance) is accepted as
    rendered, and the player's inference from it ("You look a little sad today") is the
    player's own word: nothing repairs it."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, _ = await ms.street(client, engine, salience=None)
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("do", TIP))])
        played = await respond(client, story, body(ids["Wren"], "Morning!"))
        assert played.status_code == 200, played.text
        driver = (await ms.characters_of(engine, story))["the delivery driver"]
        assert driver.weight == "minor"

        async def turn(character: str, *beats: dict[str, Any], say: str = "Hi.") -> Any:
            stub.reset()
            stub.script("generation", [render(*beats)])
            found = await respond(
                client, story, body(ids["Wren"], say, render_character_ids=[character])
            )
            assert found.status_code == 200, found.text
            return found.json(), await turn_report(engine, story)

        def tags(report: dict[str, Any]) -> list[str]:
            return [r["tag"] for r in report.get("repairs", [])]

        await turn(driver.id, beat("think", SAD), beat("say", REPLY, intent="sincere"))
        (thought,) = [
            m for m in await ps.memories_of(engine, story, driver.id) if m.channel == "think"
        ]
        assert thought.text == f"the delivery driver thinks: {SAD}"

        # Narrated as his inner state, in the same turn as a new thought.
        answer, report = await turn(
            driver.id,
            beat("think", "She still seems sad."),
            beat("do", NARRATED),
            beat("say", "See you next week.", intent="sincere"),
        )
        assert tags(report) == ["think_leak"]
        assert not _private_words(answer["message"]["text"]), answer["message"]
        assert not [b for b in answer["beats"] if _private_words(b.get("text") or "")]
        constraints = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'render_rejected' "
            "AND narrative_turn = 3",
            s=story,
        )
        texts = [c["text"] for r in constraints for c in r.payload["constraints"]]
        assert texts
        assert not [t for t in texts if _private_words(t)], texts

        # Complement: the sympathetic glance stands, and the player's inference is legitimate.
        answer, report = await turn(
            driver.id, beat("do", GLANCE), beat("say", "Take care.", intent="sincere")
        )
        assert not tags(report)
        assert GLANCE in answer["message"]["text"]
        answer, report = await turn(
            ids["Jenni"], beat("say", "Oh, thanks.", intent="sincere"), say=INFERENCE
        )
        assert not tags(report)

        # Repeated verbatim, or paraphrased as his thought, by Jenni.
        for leak in (VERBATIM, PARAPHRASED):
            answer, report = await turn(ids["Jenni"], beat("say", leak, intent="sincere"))
            assert tags(report) == ["think_leak"], leak
            assert not _private_words(answer["message"]["text"]), answer["message"]
        jenni_prompts = [c for c in stub.calls if c.purpose == "generation"]
        assert jenni_prompts
        assert "leggings" not in system_of(jenni_prompts[0])

        # Six months later: a grounded callback stands, steered by his private memories.
        answer, report = await turn(driver.id, beat("say", CALLBACK, intent="sincere"))
        assert not tags(report)
        assert CALLBACK in answer["message"]["text"]
        (verdict,) = report["verify"]["verdicts"]
        assert (verdict["kind"], verdict["rule"]) == ("supported", "recall")
        assert verdict["recall"]["grounded"] is True
        backing = await rows(
            engine, "SELECT text FROM memories WHERE id = :m", m=verdict["recall"]["memory_id"]
        )
        recorded = [m.text for m in await ps.memories_of(engine, story, driver.id)]
        (backed,) = [r.text for r in backing]
        assert backed in recorded
        assert backed.startswith("the delivery driver thinks:")
        assert "sad" in backed
        (prompt,) = [c for c in stub.calls if c.purpose == "generation"]
        assert f"## Your private memories\n- you thought: {SAD}" in system_of(prompt)

        # An unrecorded "memory" is a confabulation, repaired and audited.
        answer, report = await turn(driver.id, beat("say", CONFABULATED, intent="sincere"))
        assert tags(report) == ["confabulation"]
        assert not _private_words(answer["message"]["text"]), answer["message"]
        rejected = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'render_rejected' "
            "ORDER BY seq DESC LIMIT 1",
            s=story,
        )
        assert [c["text"] for c in rejected[0].payload["constraints"]] == [
            "Do not claim to remember you crying at the bus stop."
        ]
        audited = await rows(
            engine,
            "SELECT gate_reason, final_kind, called FROM escalations WHERE story_id = :s",
            s=story,
        )
        assert [(r.gate_reason, r.final_kind, r.called) for r in audited] == [
            ("confabulation", "novel", False)
        ]
