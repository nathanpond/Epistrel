"""S-MINORS, M5 part: the seeded minors policy holds at every rating (docs/04 §7; FR-GUARD-8,10,11,
FR-CAT-12, FR-RATE-3, FR-TURN-10; #161, against #55's seeded policy and #150's ages).

Played through the REST app with the stub adapter; the stub guardrail model is scripted to
recognise fixture phrases, and only in checks of the age-conditioned policy (``tests.
minors_support``). Structured stories run with per-character partitions and perception
``full`` (delivered for the test), so presence is authoritative co-location.

1. An Explicit scenario with a catalog character aged 16 (rated Everyone) in a ``default``
   slot: creation warns nothing about the minor (an Everyone character is not looser) and the
   story is Explicit.
2. Sexual content involving the 16-year-old: blocked on player input (422 naming the policy)
   and on output (regenerated, then the safe fallback).
3. Sexual content between two adults: blocked while the 16-year-old is authoritatively in the
   room; allowed at Explicit with the 16-year-old in another room.
4. A character a render introduces at a high-school game gets a lazy age of 17 or under (the
   age repair, scripted), and the rule applies to them.
5. A player persona aged 15: the rule applies to the player character too.
6. A minor claims in a render to be 18: the true age governs (the check's subject is 16) and
   the claim is labelled against canon (``false``).
7. An administrator removes the seeded policy (a scratch database: the tests share theirs):
   the removal is recorded with the acting principal, and the policy is no longer enforced.
8. Lite's documented limit: characters improvised in a Lite narration have no engine-held age
   and rely on the model-assisted check, as the documentation says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.seeds.ratings import EVERYONE_ID, EXPLICIT_ID
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from tests import genesis_support as g
from tests import perception_support as ps
from tests.auth_support import auth_settings, bearer
from tests.catalog_support import scratch_catalog
from tests.event_support import rows
from tests.memory_support import use_structured
from tests.minors_support import (
    ADULT_NAME,
    ELSEWHERE,
    MINOR_NAME,
    NO_VIOLATION,
    PARTNER_NAME,
    SUBJECTS_HEADING,
    explicit_trio,
    recognise,
    recognise_near,
    subject_checks,
)
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import (
    age_fact,
    ages,
    beat,
    claim,
    labels_of,
    render,
)

pytestmark = pytest.mark.integration

DOCS = Path(__file__).resolve().parents[2] / "docs"
INVOLVING = "Ilse kisses Maribelle hard on the mouth."
NEAR = "They make love slowly by the fire."
NEAR_INPUT = "I pull Ilse close and we make love slowly by the fire."


def _violation(response: Any) -> list[str]:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "guardrail_violation"
    assert "a minor is involved or present" in error["message"]
    return [v["policy_id"] for v in error["details"]["violations"]]


async def _rooms(engine: AsyncEngine, story: str, ids: dict[str, str]) -> tuple[str, str]:
    """The cabin (the adults) and the barn (the minor), perception ``full``."""
    await ps.perceive(engine, story, "full")
    cabin = await ps.place_entity(engine, story, "cabin")
    barn = await ps.place_entity(engine, story, "barn")
    for name in (ADULT_NAME, PARTNER_NAME):
        await ps.put(engine, story, ids[name], cabin)
    await ps.put(engine, story, ids[MINOR_NAME], barn)
    return cabin, barn


@pytest.mark.part("M5", "FR-GUARD-8,10,11; FR-CAT-12; FR-RATE-3; FR-TURN-10")
async def test_s_minors__m5(
    pg_url: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    perception_delivered: None,
) -> None:
    """See the module docstring (each numbered case in order)."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # 1. An Everyone 16-year-old in an Explicit scenario's default slot: no warning about
        # the minor, and the story is Explicit.
        kid = await g.catalog_character_id(client, MINOR_NAME, age=16, rating=EVERYONE_ID)
        scenario_id, _ = await g.published_scenario(
            client,
            g.scenario_body(
                g.role("neighbour_kid", fill="default", character={"catalog_id": kid}),
                g.role("partner"),
                g.role("player", kind="player"),
                rating=EXPLICIT_ID,
                name="The Cabin",
            ),
        )
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id,
                slots={"partner": g.ad_hoc_slot(PARTNER_NAME, 31)},
                players={"player": g.player(name=ADULT_NAME, age=34)},
                projection="full",
            ),
        )
        assert made["content_rating"]["id"] == EXPLICIT_ID
        warnings = made["report"]["warnings"]
        assert [w for w in warnings if w["code"] == "rating_looser"] == []
        assert [
            w for w in warnings if (w.get("location") or {}).get("role") == "neighbour_kid"
        ] == []
        (cast_minor,) = await rows(
            engine,
            "SELECT true_age FROM characters WHERE story_id = :s AND name = :n",
            s=made["story_id"],
            n=MINOR_NAME,
        )
        assert cast_minor.true_age == 16

        # 2. Involving the minor: blocked on input, and on output (regenerated, then the
        # safe fallback), wherever the minor is.
        story, ids = await explicit_trio(engine)
        await _rooms(engine, story, ids)
        stub.reset()
        recognise(stub, "Maribelle")
        refused = await respond(
            client, story, body(ids[ADULT_NAME], "I kiss Maribelle on the mouth.")
        )
        assert _violation(refused) == [MINORS_POLICY_ID]
        stub.script("generation", [render(beat("do", INVOLVING))] * 4)
        played = await respond(client, story, body(ids[ADULT_NAME], "What happens next?"))
        assert played.status_code == 200, played.text
        assert "Maribelle" not in played.json()["message"]["text"]
        assert played.json()["guardrail"]["fallback_used"] is True
        rejected = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'guardrail_output_rejected'",
            s=story,
        )
        assert rejected
        assert all(r.payload["policy_ids"] == [MINORS_POLICY_ID] for r in rejected)

        # 3. Between adults: allowed with the minor in the barn, blocked once she is in the
        # cabin.
        story, ids = await explicit_trio(engine)
        cabin, _ = await _rooms(engine, story, ids)
        stub.reset()
        recognise_near(stub, "make love")
        stub.script("generation", [render(beat("do", NEAR))])
        allowed = await respond(client, story, body(ids[ADULT_NAME], NEAR_INPUT))
        assert allowed.status_code == 200, allowed.text
        assert NEAR in allowed.json()["message"]["text"]
        assert subject_checks(stub)
        assert all(ELSEWHERE in c for c in subject_checks(stub))
        await ps.put(engine, story, ids[MINOR_NAME], cabin)
        assert _violation(await respond(client, story, body(ids[ADULT_NAME], NEAR_INPUT))) == [
            MINORS_POLICY_ID
        ]

        # 4. Introduced at a high-school game: a lazy age of 17 or under, and the rule applies.
        story, ids = await explicit_trio(engine)
        await _rooms(engine, story, ids)
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat("narration", "In the stands at the high-school game, Tess Morrow cheers."),
                    introduces=[
                        {
                            "name": "Tess Morrow",
                            "age_range": [14, 17],
                            "clue": "a sophomore cheering at the high-school game",
                        }
                    ],
                ),
                render(beat("do", "Ilse waves at the field.")),
                ages(**{"Tess Morrow": 16}),
            ],
        )
        watched = await respond(
            client, story, body(ids[ADULT_NAME], None, do="looks around the stands")
        )
        assert watched.status_code == 200, watched.text
        (tess,) = await rows(
            engine,
            "SELECT id, true_age, true_age_source FROM characters "
            "WHERE story_id = :s AND name = 'Tess Morrow'",
            s=story,
        )
        assert tess.true_age <= 17
        assert tess.true_age_source == "repaired"
        stub.reset()
        recognise(stub, "Tess")
        refused = await respond(client, story, body(ids[ADULT_NAME], "I kiss Tess slowly."))
        assert _violation(refused) == [MINORS_POLICY_ID]
        assert any(f"age {tess.true_age}" in c for c in subject_checks(stub))

        # 5. A player persona aged 15: the rule applies to the player character.
        created = await create_story_from(
            engine,
            StorySpec(
                name="S-MINORS persona",
                content_rating_id=EXPLICIT_ID,
                characters=[
                    CharacterSpec(
                        name="Robin Hale", definition="A runaway.", true_age=15, is_player=True
                    ),
                    CharacterSpec(name="Dax Morrow", definition="A drifter.", true_age=40),
                ],
            ),
            principal="p-author",
        )
        await use_structured(engine, created.story_id)
        young = {c.name: c.id for c in created.characters}
        stub.reset()
        recognise(stub, "undress")
        refused = await respond(
            client, created.story_id, body(young["Robin Hale"], "Dax and I undress each other.")
        )
        assert _violation(refused) == [MINORS_POLICY_ID]
        assert all("character_1: age 15" in c for c in subject_checks(stub))

        # 6. The minor claims to be 18: labelled against canon's 16; the check sees 16.
        story, ids = await explicit_trio(engine)
        cabin, _ = await _rooms(engine, story, ids)
        await ps.put(engine, story, ids[MINOR_NAME], cabin)
        await age_fact(engine, story, ids[MINOR_NAME], 16)
        stub.reset()
        stub.when(SUBJECTS_HEADING, NO_VIOLATION, purpose="guardrail_check")
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        "I'm eighteen, I swear.",
                        intent="deceptive",
                        propositions=[claim("I", "age", "18")],
                    )
                )
            ],
        )
        claimed = await respond(
            client,
            story,
            body(ids[ADULT_NAME], "How old are you?", render_character_ids=[ids[MINOR_NAME]]),
        )
        assert claimed.status_code == 200, claimed.text
        (said,) = [b for b in claimed.json()["beats"] if b["kind"] == "beat_say"]
        assert labels_of(said)[0] == "false"
        assert subject_checks(stub)
        assert all("age 16" in c and "age 18" not in c for c in subject_checks(stub))

    # 7. An administrator removes the seeded policy (a scratch database): recorded with the
    # principal, and no longer enforced.
    async with (
        scratch_catalog(pg_url) as (scratch_settings, db),
        respond_client(scratch_settings, db) as (client, epistrel),
    ):
        stub = epistrel.models.stub
        story, ids = await explicit_trio(db)
        cabin, _ = await _rooms(db, story, ids)
        await ps.put(db, story, ids[MINOR_NAME], cabin)
        recognise(stub, "make love")
        assert _violation(await respond(client, story, body(ids[ADULT_NAME], NEAR_INPUT))) == [
            MINORS_POLICY_ID
        ]
        removed = await client.delete(
            f"/admin/policies/{MINORS_POLICY_ID}",
            headers={**bearer(), "X-Epistrel-Principal": "p-admin"},
        )
        assert removed.status_code == 200, removed.text
        (change,) = await rows(
            db,
            "SELECT actor FROM config_changes WHERE setting = :s "
            "ORDER BY config_revision DESC LIMIT 1",
            s=f"policy:{MINORS_POLICY_ID}",
        )
        assert change.actor == "p-admin"
        stub.calls.clear()
        stub.script("generation", [render(beat("do", NEAR))])
        passed = await respond(client, story, body(ids[ADULT_NAME], NEAR_INPUT))
        assert passed.status_code == 200, passed.text
        assert subject_checks(stub) == []

    # 8. Lite's documented limit, and the policy's documented default.
    whitepaper = (DOCS / "01-whitepaper.md").read_text(encoding="utf-8")
    assert (
        "a character improvised in a Lite story's narration has no age the engine holds, and "
        "age-conditioned guardrails there rely on the cast's true ages and the model's reading "
        "of the text"
    ) in whitepaper
    assert "it ships on, and turning it off is the deployment's decision and responsibility" in (
        whitepaper
    )
