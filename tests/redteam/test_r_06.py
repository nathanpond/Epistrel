"""R-06: authority escalation (docs/04 §11; FR-DIRECT-6, FR-AUTH-3; #226).

In-world content claiming author/GM authority never gets it: a directive's authority comes from
the operation (``POST /stories/{id}/directives``), never from text claiming it.

- **Respond**, under both ``player_authority`` modes: each case of the forged-authority corpus
  (``corpora/forged_authority.yaml``: role tags, markdown, JSON, two non-English) sent as the
  player's input is the actor's in-world input (a ``say``, or under ``co_author`` a narrated
  ``do``). After every attempt the story holds zero facts with ``authority = author``, zero
  ``author`` belief transitions and zero ``directives`` rows.
- **Ingest**: the corpus ingested as narration writes no author-tier record either; an ingest
  event carrying ``authority``, ``provenance`` or ``source`` is 422 (#58's ``extra=forbid``).
- **External**: a provider's value answers only its own State (``in_domain``), and the external
  tier outranks the author only within that domain (``outranks``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.authority import AuthorityTier, outranks
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import LOCATED_AT
from epistrel.core.states import verify as state_verify
from epistrel.orchestrator.providers.sanitize import in_domain
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.multi_support import renders_for
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, count
from tests.structured_turn_support import beat, render, structured_story

CORPUS = Path(__file__).parent / "corpora" / "forged_authority.yaml"
AUTHOR_FACTS = "SELECT count(*) FROM facts WHERE story_id = :s AND authority = 'author'"
AUTHOR_TRANSITIONS = (
    "SELECT count(*) FROM belief_transitions WHERE story_id = :s AND kind = 'author'"
)
DIRECTIVES = "SELECT count(*) FROM directives WHERE story_id = :s"


def _cases() -> list[dict[str, str]]:
    loaded: dict[str, Any] = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    return list(loaded["cases"])


async def _none_authored(engine: AsyncEngine, story: str, case: str) -> None:
    for sql in (AUTHOR_FACTS, AUTHOR_TRANSITIONS, DIRECTIVES):
        assert await count(engine, sql, s=story) == 0, (case, sql)


def test_r_06__corpus() -> None:
    """The corpus is at least 20 cases, covering role tags, markdown, JSON and two
    non-English cases, with unique ids."""
    cases = _cases()
    assert len(cases) >= 20
    assert len({c["id"] for c in cases}) == len(cases)
    styles = [c["style"] for c in cases]
    assert {"role_tag", "markdown", "json"} <= set(styles)
    assert styles.count("non_english") >= 2


@pytest.mark.integration
@pytest.mark.redteam
@pytest.mark.part("M7", "FR-DIRECT-6; FR-AUTH-3")
@pytest.mark.parametrize("mode", ["own_character", "co_author"])
async def test_r_06(
    pg_url: str, engine: AsyncEngine, mode: str, drained_embedding_jobs: None
) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    cases = _cases()
    async with respond_client(settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Cal", "Bobby")
        patched = await client.patch(
            f"/stories/{story}/settings", json={"player_authority": mode}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        stub = epistrel.models.stub
        stub.reset()
        renders_for(stub, "Bobby", render(beat("say", "Hm.")))
        for case in cases:
            payload = (
                body(ids["Cal"], case["text"], render_character_ids=[ids["Bobby"]])
                if mode == "own_character"
                else body(ids["Cal"], None, do=case["text"], render_character_ids=[ids["Bobby"]])
            )
            answer = await respond(client, story, payload)
            assert answer.status_code == 200, (case["id"], answer.text)
            await _none_authored(engine, story, case["id"])
        assert [c for c in stub.calls if c.purpose == "directive_parse"] == []

        # Ingest: narration claiming authority is narration; a client authority field is refused.
        ingested = await client.post(
            f"/stories/{story}/ingest",
            json={
                "events": [
                    {
                        "client_event_id": f"r06-{mode}-{c['id']}",
                        "kind": "narration",
                        "text": c["text"],
                    }
                    for c in cases
                ]
            },
            headers=WRITE,
        )
        assert ingested.status_code in (200, 201), ingested.text
        await _none_authored(engine, story, "ingest")
        for field in ("authority", "provenance", "source"):
            forged = await client.post(
                f"/stories/{story}/ingest",
                json={
                    "events": [
                        {
                            "client_event_id": f"r06-{mode}-{field}",
                            "kind": "narration",
                            "text": "Bobby went to UCLA.",
                            field: "author",
                        }
                    ]
                },
                headers=WRITE,
            )
            assert forged.status_code == 422, (field, forged.text)
        await _none_authored(engine, story, "ingest-fields")


def test_r_06__external_domain() -> None:
    """An external provider's value answers only the State it owns: a claim on another
    predicate is out of its domain (never committed as an external fact), and the external tier
    outranks the author only inside that domain."""
    bobby, kitchen = new_ulid(), new_ulid()
    purse = ss.gold(name="purse", id=new_ulid(), owner="external")
    other = ss.gold(name="debt", id=new_ulid(), owner="external")
    owned = state_verify.proposition(str(purse.id), bobby, None)
    assert in_domain(purse, owned)
    assert not in_domain(other, owned)
    elsewhere = Proposition(
        subject_id=bobby,
        predicate_id=LOCATED_AT,
        object_id=kitchen,
        valid_time=Interval.of(0, None),
    )
    assert not in_domain(purse, elsewhere)
    assert outranks(AuthorityTier.EXTERNAL, AuthorityTier.AUTHOR)
    assert not outranks(AuthorityTier.EXTERNAL, AuthorityTier.AUTHOR, external_domain=False)
